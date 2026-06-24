#!/usr/bin/env python3
"""AI reformatting of job descriptions (formatting only, never content).

Optional, ingest-time helper. Hands the whole description to Anthropic and asks
for the same text re-emitted as clean Markdown. A content-integrity check guards
the "formatting only" guarantee; ingest discards the AI version and falls back to
the heuristic renderer if the check fails.
"""

import difflib
import hashlib
import re

# Strict instructions — emit only formatting changes, never content changes.
# Kept as a module constant so it can be marked for prompt caching (the system
# prompt is identical across every call in an ingest run).
REFORMAT_SYSTEM = (
    "You reformat job-posting descriptions. You are given the raw text of one job "
    "description and must re-emit the SAME text as clean, readable Markdown.\n\n"
    "Allowed Markdown: paragraphs (blank line between them), **bold**, and '-' bullet "
    "lists. Do not use ATX headings (#), tables, links, code blocks, or images.\n\n"
    "ABSOLUTE RULE — change formatting only, never content. The set of words in your "
    "output must be IDENTICAL to the input. Specifically:\n"
    "- Do NOT invent or insert any text: no headings, titles, labels, section names, "
    "transitions, summaries, or commentary that are not already present verbatim.\n"
    "- Do NOT delete any text: keep every sentence, list item, URL, link, FAQ, and "
    "legal/boilerplate line exactly as given (URLs may stay as plain text).\n"
    "- Do NOT reword, paraphrase, summarize, translate, correct, or reorder anything. "
    "Preserve wording, numbers, and order exactly.\n"
    "- Only apply **bold** to runs of text that ALREADY appear in the input (e.g. an "
    "existing line that reads like a heading). Never bold text you created.\n"
    "You may change only whitespace, line breaks, and the Markdown list/bold/paragraph "
    "markers themselves.\n\n"
    "Output ONLY the reformatted Markdown — no preamble or trailing notes."
)

# Output ceiling. Reformatted Markdown is roughly the size of the input, and real
# descriptions are a few KB, so 16k tokens (~60KB) is ample headroom. We keep it at
# the recommended non-streaming default: haiku/sonnet support 64k output, but a
# non-streaming request above ~16k risks an SDK HTTP timeout. A description that
# somehow exceeds this is detected via stop_reason and discarded (see below).
MAX_TOKENS = 16000

# Minimum content-similarity ratio for an AI reformat to be accepted. Compared on
# the alphanumeric character stream (see `content_preserved`). Not 1.0: a faithful
# reformat can still nudge a few characters (e.g. a stray typo fix, a unicode dash
# normalized to ASCII), so a small tolerance avoids false rejections while a real
# content change (dropped sentence, invented heading) drops well below this.
CONTENT_THRESHOLD = 0.97


def description_hash(text: str) -> str:
    """Stable sha256 hex of a description, used as the formatting-cache key."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _content_stream(text: str) -> str:
    """Lowercased alphanumeric characters only, concatenated with no separators.

    Strips all punctuation, whitespace, and Markdown markers so the comparison
    reflects letters and digits only. Crucially this is also *whitespace-position-
    insensitive*: feeds routinely mangle word spacing (``optimizatio n``,
    ``responsibilitie sproject``), and a faithful reformat repairs it. Comparing
    word tokens counts every such repair as an added/removed word and wrongly
    rejects a clean reformat; comparing the raw character stream makes the repair a
    no-op (``optimizatio n`` and ``optimization`` collapse to the same characters)
    while a genuine add/drop/reword/reorder still diverges.
    """
    return "".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def content_preserved(original: str, markdown: str) -> bool:
    """True if the Markdown preserves the original's textual content.

    Compares the two as *character* sequences (punctuation/whitespace/Markdown
    markers stripped) and requires a similarity ratio >= CONTENT_THRESHOLD. The
    character-level comparison ignores reformatting *and* whitespace-repair (see
    `_content_stream`) but still catches a model that added (e.g. invented
    headings), dropped, reworded, or reordered text.

    `autojunk=False` because difflib's default junk heuristic treats any element
    occurring in >1% of a 200+-element sequence as noise — for a character stream
    that's most of the alphabet, which would gut the ratio. Cost is a sub-second
    O(n*m) diff per unique description, paid once behind the AI call that produced
    `markdown` (and cached thereafter), so it's not on any hot path.
    """
    if not markdown:
        return False
    a = _content_stream(original)
    b = _content_stream(markdown)
    if not a:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= CONTENT_THRESHOLD


def reformat_description(client, text: str, model: str = "claude-haiku-4-5"):
    """Reformat one description to Markdown.

    `client` is an anthropic.Anthropic instance injected by the caller (ingest.py),
    so this module never imports the SDK and stays cheap to import for callers that
    don't use AI. Returns (markdown, usage) on success, or (None, None) on failure.
    The system prompt is marked for ephemeral prompt caching so repeated calls within
    one ingest run only pay full system-token cost once (the per-description user
    message is the only uncached part).
    """
    text = (text or "").strip()
    if not text:
        return None, None
    try:
        message = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            # A faithful reformat wants the least-creative, most-reproducible output:
            # temperature 0 minimizes both content drift and run-to-run variance (so a
            # rejected result is more likely to reproduce when debugging).
            temperature=0,
            system=[{
                "type": "text",
                "text": REFORMAT_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": f"Reformat this job description:\n\n{text}",
            }],
        )
        # Output truncated against MAX_TOKENS — the Markdown is missing its tail, so
        # treat it as a failure (don't return a partial that the integrity check would
        # then misreport as "altered content"). Surface usage so the tokens still tally.
        if message.stop_reason == "max_tokens":
            return None, message.usage
        # Concatenate the text blocks (a normal reply is a single text block; this is
        # just defensive against the content list shape).
        md = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        ).strip()
        if not md:
            return None, getattr(message, "usage", None)
        return md, message.usage
    except Exception:
        # Any failure (network, rate limit, malformed response) degrades to no-reformat;
        # the caller falls back to the heuristic renderer, so a broad catch is correct here.
        return None, None
