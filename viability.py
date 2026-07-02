#!/usr/bin/env python3
"""Shared viability-scoring helpers used by rescore_viability.py.

Rates a job posting 'low'/'medium'/'high' for one specific candidate by asking
Anthropic to compare the posting against a candidate-profile prompt. A fixed
boilerplate plus the candidate profile form the (cached) system prompt; each job is
a single user message. ``score_job()`` returns (rating, reason, usage);
``prompt_hash()`` lets the caller detect when a stored score predates the current
prompt and therefore needs re-running.
"""

import hashlib
import json
import re

# The valid rating values; anything else from the model is treated as a failure.
VIABILITY_RATINGS = {"low", "medium", "high"}

# Boilerplate prepended to every system prompt before the candidate description.
# The single-line-JSON instruction keeps the reply tiny and trivially parseable; the
# compensation note teaches the model the cents=hourly / round=annual heuristic so it
# normalizes pay before judging fit. Changing this constant changes prompt_hash, which
# correctly invalidates every existing score (they were produced under the old prompt).
_SYSTEM_BOILERPLATE = (
    "You evaluate job postings for a specific candidate. "
    "Respond ONLY with a JSON object on a single line — no markdown, no explanation:\n"
    '{"rating": "low|medium|high", "reason": "one sentence"}\n\n'
    "Compensation note: dollar amounts with cents (e.g. $51.45, $62.99) are hourly "
    "wages, not annual salaries. Convert hourly rates to annual (multiply by ~2,080) "
    "before comparing against the candidate's expectations. Round numbers "
    "(e.g. $120,000 or $120k) are annual.\n\n"
)


def prompt_hash(prompt: str) -> str:
    """Return a stable 32-char hex digest of the full scoring prompt.

    Hashes both the fixed system boilerplate and the candidate description so that
    changes to either (config edits OR code-level boilerplate updates) correctly
    invalidate existing scores. Truncated to 32 hex chars purely to keep the stored
    value compact — collision resistance is irrelevant here (it's a change-detector,
    not a security hash).
    """
    return hashlib.sha256((_SYSTEM_BOILERPLATE + prompt).encode()).hexdigest()[:32]


def score_job(
    client,
    viability_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
) -> tuple[str, str, object] | tuple[None, None, None]:
    """Score a job posting for viability against the candidate description.

    Returns (rating, reason, usage): rating is 'low'/'medium'/'high', reason is a
    one-sentence justification, and usage is the Anthropic token-usage object (for
    cost tallying). Returns (None, None, None) on any failure — unparseable response,
    invalid rating, or API error — so the caller can skip the job and move on.

    The system prompt (boilerplate + candidate profile) is identical for every job in
    a run, so it's marked for ephemeral prompt caching — only the per-job user message
    is uncached, making a full rescore cheap after the first call.
    """
    title          = (job.get("title")           or "(no title)").strip()
    company_raw    = (job.get("company")         or "(unknown company)").strip()
    company_actual = (job.get("company_actual")  or "").strip()
    # If a company-name override exists, show both — the model needs the real employer
    # to judge fit, but seeing the posting agent (recruiter/aggregator) adds context.
    company        = f"{company_actual} (posted via {company_raw})" if company_actual and company_actual != company_raw else company_raw
    # Cap the description for scoring: the model only needs the gist to rate fit, and
    # this bounds token cost per job. (Reformatting, by contrast, sends the full text.)
    description    = (job.get("job_description") or "").strip()[:4000]
    # Manual salary override (salary_*_actual) wins over the feed value. The override
    # is an all-or-nothing pair: if either bound is set, use the override pair (a blank
    # bound stays open-ended) rather than mixing an overridden bound with a feed bound.
    if job.get("salary_min_actual") is not None or job.get("salary_max_actual") is not None:
        sal_min, sal_max = job.get("salary_min_actual"), job.get("salary_max_actual")
    else:
        sal_min, sal_max = job.get("salary_min"), job.get("salary_max")
    if sal_min and sal_max:
        salary_line = f"Salary: ${sal_min:,} – ${sal_max:,}"
    elif sal_min:
        salary_line = f"Salary: ${sal_min:,}+"
    elif sal_max:
        salary_line = f"Salary: up to ${sal_max:,}"
    else:
        salary_line = None  # absent — say nothing; candidate prompt handles the neutral case

    system_text = _SYSTEM_BOILERPLATE + f"Candidate description:\n{viability_prompt}"

    try:
        message = client.messages.create(
            model=model,
            max_tokens=256,  # only a one-line JSON verdict is expected (headroom for a longer reason)
            # Disable the model's internal chain-of-thought: this is a trivial one-line
            # classification that needs no reasoning, and the human-readable justification
            # is the `reason` field of the JSON answer (normal output), NOT thinking.
            # Models with adaptive thinking on by default (e.g. Sonnet 5, whose effort
            # defaults to 'high') otherwise spend the whole small max_tokens budget
            # thinking and return an empty/truncated verdict — every such job then "fails".
            # (Always-on-thinking models like Fable 5 reject this, but they're not sensible
            # choices for a cheap high-volume scorer, and the default is Haiku.)
            thinking={"type": "disabled"},
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Job title: {title}\n"
                    f"Company: {company}\n"
                    + (f"{salary_line}\n" if salary_line else "")
                    + f"\nDescription:\n{description}"
                ),
            }],
        )
        raw = message.content[0].text.strip()
        # The model is told to emit bare JSON, but tolerate it wrapping the object in
        # backticks or stray prose — grab the first {...} block.
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None, None, None  # must be a 3-tuple: caller unpacks (rating, reason, usage)
        data = json.loads(m.group())
        rating = str(data.get("rating", "")).lower().strip()
        reason = str(data.get("reason", "")).strip()
        # Reject anything that isn't a recognized rating with a non-empty reason.
        if rating not in VIABILITY_RATINGS or not reason:
            return None, None, None
        return rating, reason, message.usage
    except Exception:
        # Any failure (API error, malformed JSON, missing content) → skip this job.
        return None, None, None
