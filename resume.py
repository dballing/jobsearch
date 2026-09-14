#!/usr/bin/env python3
"""Load a candidate resume file into plain text for the viability scorer.

The ``[viability].resume_file`` config key points at a resume; its extracted text is folded
into the candidate prompt (see ``viability.compose_candidate_prompt``) so the scorer judges a
posting against the candidate's ACTUAL experience, not just their self-description — which lets
it assess how likely a positive response is (are they competitive for this role?), not only
whether the role is a good fit.

Supported formats: plain text / Markdown (read directly as UTF-8) and PDF / DOCX (text
extracted via ``pypdf`` / ``python-docx``, imported lazily so those libraries are only needed
when such a resume is actually configured).

Every failure raises :class:`ResumeError` with an actionable message; ``config.py`` turns that
into a :class:`config.ConfigError` so a *misconfigured* resume is loud rather than silently
ignored. That's deliberate and distinct from the "AI is fail-soft" rule elsewhere: if you point
the config at a resume, you meant to use it — silently dropping it would fold the resume into
neither the prompt nor its hash, so scores would look resume-informed (and "current") when they
never saw the resume at all. Better to refuse to load until it's fixed.
"""
import re
from pathlib import Path

# Bound the extracted text so a pathological file can't balloon the (cached) system prompt.
# A real resume is a few KB; this is generous headroom, not a target — it only ever trims an
# accidentally-huge file, which is why there's no "(+N more)" marker as elsewhere.
_MAX_RESUME_CHARS = 20000

# Suffixes read straight as UTF-8 text with no extraction step. An extension-less file is
# treated as text too — a bare "resume" written in Markdown is common enough to accept.
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text", ""}


# ── Letter-spacing repair ──────────────────────────────────────────────────────────────────
# A common PDF artifact: a header styled with wide letter *tracking* extracts with a space between
# every glyph — "P R O F E S S I O N A L  S U M M A R Y". Left alone it wastes tokens and can
# confuse the scorer. It's recoverable because such a line marks WORD boundaries with a wider gap
# (2+ spaces) than the single spaces between letters, so we split on the wide gaps and drop the
# inter-letter spaces within each piece. Detection is deliberately conservative — only a line that
# is overwhelmingly single characters is touched — so ordinary prose (even a line with a stray
# "a"/"I") passes through byte-for-byte.
_LETTERSPACE_MIN_TOKENS = 4     # fewer single letters than this → not confidently tracking vs. prose
_LETTERSPACE_MIN_RATIO = 0.75   # ≥ this share of whitespace-split tokens must be single characters


def _looks_letterspaced(tokens: "list[str]") -> bool:
    """True when a line's whitespace-split ``tokens`` look like letter-spaced tracking (mostly
    single characters) rather than prose. Requires several tokens, a high single-char share, AND a
    few real letters — so a short normal phrase, or a line of lone symbols/digits, isn't matched."""
    if len(tokens) < _LETTERSPACE_MIN_TOKENS:
        return False
    singles = [t for t in tokens if len(t) == 1]
    alpha_singles = sum(1 for t in singles if t.isalpha())
    return alpha_singles >= 3 and len(singles) / len(tokens) >= _LETTERSPACE_MIN_RATIO


def _collapse_letterspaced(text: str) -> str:
    """Repair letter-spaced header lines in extracted resume text (see the note above).

    Operates line by line and rewrites ONLY lines confidently detected as letter-spaced, leaving
    ordinary prose untouched. In a matched line, runs of 2+ spaces are treated as word boundaries
    and the single spaces between glyphs are removed: "D E R E K  J .  B A L L I N G" → "DEREK J.
    BALLING". A header that used single spaces even between words is unrecoverable (its words would
    merge), but real-world tracking uses the wider inter-word gap this keys off. Pure, so it's
    unit-testable without a file."""
    out = []
    for line in text.split("\n"):
        if _looks_letterspaced(line.split()):
            words = re.split(r" {2,}", line.strip())
            out.append(" ".join(re.sub(r"\s+", "", w) for w in words))
        else:
            out.append(line)
    return "\n".join(out)


class ResumeError(Exception):
    """A configured resume file that can't be turned into text: missing, empty, an unsupported
    format, or an extractor library that isn't installed. Carries an actionable message;
    ``config.py`` re-raises it as ``ConfigError`` so the whole load fails loudly."""


def _extract_pdf(path: Path) -> str:
    """Concatenate the text layer of every page of a PDF. Raises ResumeError with an install
    hint when ``pypdf`` isn't available (kept a lazy import so PDF support is only *required*
    when a PDF resume is actually configured)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ResumeError(
            f"{path}: reading a PDF resume needs the 'pypdf' package — "
            "pip install pypdf (or: pip install -r requirements.txt)."
        ) from exc
    try:
        reader = PdfReader(str(path))
        # extract_text() yields "" for a page with no text layer (e.g. a scanned image); join
        # across pages and let the empty-check in extract_resume_text catch an image-only PDF.
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # corrupt / encrypted / unreadable PDF
        raise ResumeError(f"{path}: could not read PDF resume ({exc}).") from exc


def _extract_docx(path: Path) -> str:
    """Join the text of every paragraph in a .docx. Raises ResumeError with an install hint
    when ``python-docx`` isn't available (lazy import, same rationale as _extract_pdf)."""
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise ResumeError(
            f"{path}: reading a DOCX resume needs the 'python-docx' package — "
            "pip install python-docx (or: pip install -r requirements.txt)."
        ) from exc
    try:
        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:  # corrupt / not actually a .docx
        raise ResumeError(f"{path}: could not read DOCX resume ({exc}).") from exc


def extract_resume_text(path) -> str:
    """Return the resume at ``path`` as plain text, stripped and capped at ``_MAX_RESUME_CHARS``.

    Dispatches on the file extension: ``.txt``/``.md``/``.markdown``/``.text`` (and an
    extension-less file) are read as UTF-8; ``.pdf`` and ``.docx`` are extracted via pypdf /
    python-docx (imported only when needed). Raises :class:`ResumeError` on a missing file, an
    empty/whitespace-only extraction (a resume that yields no text — an image-only PDF, an empty
    file — is a misconfiguration, not an empty resume), a missing extractor library, or an
    unsupported format. Touches only the given file, so it's unit-testable with a temp file."""
    path = Path(path)
    if not path.exists():
        raise ResumeError(f"resume_file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _extract_pdf(path)
    elif suffix == ".docx":
        text = _extract_docx(path)
    elif suffix == ".doc":
        # The legacy binary .doc format needs a heavier extractor than we want to depend on;
        # ask for a modern format rather than fail obscurely deep in a parser.
        raise ResumeError(
            f"{path}: legacy .doc isn't supported — save the resume as .docx, .pdf, or .md.")
    elif suffix in _TEXT_SUFFIXES:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # A binary file given a text-ish (or no) extension — point at the real fix rather
            # than surface a raw decode error.
            raise ResumeError(
                f"{path}: not valid UTF-8 text — if this is a PDF/Word file, give it a "
                ".pdf/.docx extension so its text is extracted correctly.") from exc
    else:
        raise ResumeError(
            f"{path}: unsupported resume format '{suffix}' — use .md/.txt, .pdf, or .docx.")
    # Repair letter-spaced headers ("P R O F E S S I O N A L") before the empty-check and cap, so
    # both see the cleaned text. Conservative and no-op on prose (see _collapse_letterspaced).
    text = _collapse_letterspaced(text).strip()
    if not text:
        raise ResumeError(
            f"{path}: resume extracted to no text (an image-only PDF, or an empty file?).")
    return text[:_MAX_RESUME_CHARS]


def main(argv=None) -> int:
    """`preview_resume.sh <file>` — print the resume EXACTLY as the scorer will see it.

    PDF/DOCX extraction routinely mangles multi-column layouts, tables, and headers/footers, and
    the scorer judges the candidate against precisely this extracted text — so this lets you eyeball
    it before trusting a scoring run. The extracted body goes to stdout (pipeable / redirectable);
    a short human summary — resolved path, format, character count, and a truncation notice if the
    text hit the ``_MAX_RESUME_CHARS`` cap — goes to stderr so it doesn't pollute a redirect. A
    :class:`ResumeError` (missing/empty/unsupported/lib-absent) prints to stderr and exits non-zero,
    mirroring exactly what ``config.load_config`` would reject."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Show how the resume parser sees a file — the exact text folded into the "
                    "viability prompt. Useful for spotting PDF/DOCX extraction garble before a run.")
    parser.add_argument("file", help="Path to a resume file (.md/.txt, .pdf, or .docx).")
    args = parser.parse_args(argv)

    try:
        text = extract_resume_text(args.file)
    except ResumeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    path = Path(args.file)
    fmt = path.suffix.lower().lstrip(".") or "text"
    print(f"--- Parsed resume: {path}  (format: {fmt}, {len(text)} chars) ---", file=sys.stderr)
    # len == cap means the raw extraction was longer and got trimmed — the scorer sees only this
    # much, so say so rather than let a silently-truncated resume look complete.
    if len(text) == _MAX_RESUME_CHARS:
        print(f"NOTE: truncated to the {_MAX_RESUME_CHARS}-char cap — the scorer sees only the "
              "text shown above this line's worth.", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
