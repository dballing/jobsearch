#!/usr/bin/env python3
"""Shared viability-scoring helpers used by rescore_viability.py."""

import hashlib
import json
import re

VIABILITY_RATINGS = {"low", "medium", "high"}


def prompt_hash(prompt: str) -> str:
    """Return a stable 32-char hex digest of the viability prompt.

    Used to detect when existing scores were computed with a different
    (now stale) candidate description.
    """
    return hashlib.sha256(prompt.encode()).hexdigest()[:32]


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
    title       = (job.get("title")           or "(no title)").strip()
    company     = (job.get("company")         or "(unknown company)").strip()
    description = (job.get("job_description") or "").strip()[:4000]

    system_text = (
        "You evaluate job postings for a specific candidate. "
        "Respond ONLY with a JSON object on a single line — no markdown, no explanation:\n"
        '{"rating": "low|medium|high", "reason": "one sentence"}\n\n'
        f"Candidate description:\n{viability_prompt}"
    )

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
                    f"Company: {company}\n\n"
                    f"Description:\n{description}"
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
