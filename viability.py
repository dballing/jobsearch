#!/usr/bin/env python3
"""Shared viability-scoring helpers used by rescore_viability.py."""

import hashlib
import json
import re

VIABILITY_RATINGS = {"low", "medium", "high"}

# Boilerplate prepended to every system prompt before the candidate description.
# Changing this constant invalidates all existing scores (prompt_hash changes).
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

    Hashes both the fixed system boilerplate and the candidate description so
    that changes to either (config edits OR code-level boilerplate updates)
    correctly invalidate existing scores.
    """
    return hashlib.sha256((_SYSTEM_BOILERPLATE + prompt).encode()).hexdigest()[:32]


def score_job(
    client,
    viability_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
) -> tuple[str, str, object] | tuple[None, None, None]:
    """Score a job posting for viability against the candidate description.

    Returns (rating, reason) where rating is 'low', 'medium', or 'high',
    and reason is a single sentence.  Returns (None, None) on any failure.

    The system prompt is marked for prompt caching so repeated calls within
    the same session (same candidate description) only pay full token cost once.
    """
    title          = (job.get("title")           or "(no title)").strip()
    company_raw    = (job.get("company")         or "(unknown company)").strip()
    company_actual = (job.get("company_actual")  or "").strip()
    company        = f"{company_actual} (posted via {company_raw})" if company_actual and company_actual != company_raw else company_raw
    description    = (job.get("job_description") or "").strip()[:4000]
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
            max_tokens=150,
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
        # Extract JSON even if the model wraps it in backticks or adds whitespace.
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None, None
        data = json.loads(m.group())
        rating = str(data.get("rating", "")).lower().strip()
        reason = str(data.get("reason", "")).strip()
        if rating not in VIABILITY_RATINGS or not reason:
            return None, None, None
        return rating, reason, message.usage
    except Exception:
        return None, None, None
