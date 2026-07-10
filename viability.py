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


# Version of the per-job message schema (which fields we send and how — see
# build_score_message). It's folded into prompt_hash so that changing *what the model
# sees for each job* invalidates prior scores just like editing the prompt does. Bump it
# whenever build_score_message changes materially. History: 2 = added the Location line;
# 3 = send *all* of a job's locations, not just the first; 4 = added the Work arrangement
# (remote/hybrid/on-site) line; 5 = gloss the arrangement enum into plain English.
_SCORING_INPUT_VERSION = "5"

# Cap on locations included in the scoring message — bounds token cost for the rare job
# posted across dozens of sites, while staying generous enough to almost never truncate
# (the candidate only needs their target region to be among those listed).
_MAX_SCORE_LOCATIONS = 40


def prompt_hash(prompt: str) -> str:
    """Return a stable 32-char hex digest of everything that determines a job's score.

    Covers the fixed system boilerplate, the candidate description, AND the per-job
    message schema version — so a config edit, a boilerplate change, OR a change to which
    fields we send (e.g. adding Location) all correctly mark existing scores stale. The
    version is hashed but never sent to the model. Truncated to 32 hex chars purely to
    keep the stored value compact — collision resistance is irrelevant (it's a
    change-detector, not a security hash).
    """
    material = f"{_SCORING_INPUT_VERSION}\x00{_SYSTEM_BOILERPLATE}{prompt}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _job_locations(job: dict) -> list[str]:
    """All locations for a job. The feed lists every site under raw.locations_derived, but
    the `location` column keeps only the first — which made a multi-site job (e.g. one
    open in SC *and* CT) look single-site to the scorer, so it judged only that one region.
    Falls back to the `location` column when there's no raw list (e.g. manual jobs)."""
    try:
        locs = json.loads(job.get("raw") or "{}").get("locations_derived")
    except (json.JSONDecodeError, TypeError):
        locs = None
    if isinstance(locs, list):
        cleaned = [str(loc).strip() for loc in locs if str(loc).strip()]
        if cleaned:
            return cleaned
    one = (job.get("location") or "").strip()
    return [one] if one else []


def _work_arrangement(job: dict) -> str | None:
    """Work-arrangement summary (remote / hybrid / on-site) from the feed's AI-extracted
    `ai_work_arrangement` plus office-days. Remote status often drives fit — a remote-OK
    role is viable regardless of the office city — so send it explicitly rather than
    leaving the model to infer it from the prose. None when the feed didn't classify it
    (e.g. manual jobs).

    The feed emits terse enums ("Remote OK", "Remote Solely") whose meaning — and whose
    office-days semantics — aren't self-evident, so they're glossed into plain English:
    for "Remote OK" the office-days apply only if you live near the office; for "Hybrid"
    they're required. An unrecognized value is passed through so nothing is lost.
    """
    try:
        raw = json.loads(job.get("raw") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    arrangement = str(raw.get("ai_work_arrangement") or "").strip()
    if not arrangement or arrangement.lower() in ("none", "null"):
        return None
    try:
        days = int(raw.get("ai_work_arrangement_office_days"))
    except (TypeError, ValueError):
        days = 0
    days_wk = f"{days} day{'' if days == 1 else 's'}/week"

    key = arrangement.lower()
    if key == "remote solely":
        return "Fully remote"
    if key == "remote ok":
        return (f"Remote-friendly (fully remote unless you live near the office, then ~{days_wk} on-site)"
                if days > 0 else "Remote-friendly (fully remote; no office requirement)")
    if key == "hybrid":
        return f"Hybrid — {days_wk} in office" if days > 0 else "Hybrid"
    if key == "on-site":
        return "On-site"
    # Unrecognized enum from the feed — pass through so the signal isn't dropped.
    return f"{arrangement} ({days_wk} in office)" if days > 0 else arrangement


def _location_line(job: dict) -> str:
    """The 'Location: …' line for the scoring message (all locations, capped), or ''."""
    locs = _job_locations(job)
    if not locs:
        return ""
    shown, extra = locs[:_MAX_SCORE_LOCATIONS], len(locs) - _MAX_SCORE_LOCATIONS
    return "Location: " + "; ".join(shown) + (f" (+{extra} more)" if extra > 0 else "") + "\n"


def build_score_message(job: dict) -> str:
    """Build the per-job user message the scorer sends (title/company/location/salary +
    description). Pure and self-contained so it can be unit-tested without an API call.

    Location matters to viability (commute/remote fit) and is a first-class field, so it's
    sent explicitly — and *all* of a job's locations are sent (see _job_locations), so a
    multi-site posting isn't judged on just its first city. Work arrangement (remote /
    hybrid / on-site) is sent too, since remote status can make a role viable regardless of
    the office city. Fields that are genuinely absent are omitted rather than sent blank,
    so the candidate prompt handles the neutral case.
    """
    title       = (job.get("title")    or "(no title)").strip()
    company_raw = (job.get("company")  or "(unknown company)").strip()
    company_actual = (job.get("company_actual") or "").strip()
    # If a company-name override exists, show both — the model needs the real employer to
    # judge fit, but seeing the posting agent (recruiter/aggregator) adds context.
    company = (f"{company_actual} (posted via {company_raw})"
               if company_actual and company_actual != company_raw else company_raw)
    # Cap the description for scoring: the model only needs the gist to rate fit, and this
    # bounds token cost per job. (Reformatting, by contrast, sends the full text.)
    description = (job.get("job_description") or "").strip()[:4000]
    # Manual salary override (salary_*_actual) wins over the feed value. The override is an
    # all-or-nothing pair: if either bound is set, use the override pair (a blank bound
    # stays open-ended) rather than mixing an overridden bound with a feed bound.
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
        salary_line = None

    return (
        f"Job title: {title}\n"
        f"Company: {company}\n"
        + _location_line(job)
        + (f"Work arrangement: {wa}\n" if (wa := _work_arrangement(job)) else "")
        + (f"{salary_line}\n" if salary_line else "")
        + f"\nDescription:\n{description}"
    )


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
                "content": build_score_message(job),
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
