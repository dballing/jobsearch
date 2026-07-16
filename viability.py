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
# whenever build_score_message OR the location sub-call's inputs/_GEO_SYSTEM change materially
# (they feed the Geographic fit line, so they shape the score too). History: 2 = added the
# Location line; 3 = send *all* of a job's locations, not just the first; 4 = added the Work
# arrangement (remote/hybrid/on-site) line; 5 = gloss the arrangement enum into plain English;
# 6 = geography replaced by a pre-assessed 'Geographic fit' verdict from a focused location
# sub-call (assess_location_fit), with location_prompt folded into prompt_hash; 7 = feed the
# description to that sub-call so it catches eligibility conditions (state-restricted remote,
# relocation) the structured fields miss; 8 = made that a configurable toggle
# (location_use_description) and reworded the eligibility clause — re-bumped because 7 and 8
# were developed together and a cron rescore may have stamped scores mid-change.
_SCORING_INPUT_VERSION = "8"

# Cap on locations included in the scoring message — bounds token cost for the rare job
# posted across dozens of sites, while staying generous enough to almost never truncate
# (the candidate only needs their target region to be among those listed).
_MAX_SCORE_LOCATIONS = 40

# Cap on the description fed to the location sub-call. Eligibility conditions ("remote only
# for residents of …") sit in the prose, so the call needs the description — but bounded, to
# keep the (now per-description) sub-call cheap.
_MAX_GEO_DESC_CHARS = 4000


def prompt_hash(prompt: str, location_prompt: str = "", geo_uses_description: bool = True) -> str:
    """Return a stable 32-char hex digest of everything that determines a job's score.

    Covers the fixed system boilerplate, the candidate description, the separate
    geographic-preferences prompt (location_prompt — fed to the focused location sub-call,
    see assess_location_fit), the location_use_description toggle (geo_uses_description —
    whether that sub-call reads the description, which changes its verdict), AND the per-job
    message schema version — so a config edit to any of them, a boilerplate change, OR a
    change to which fields we send all correctly mark existing scores stale. The version is
    hashed but never sent to the model. Truncated to 32 hex chars purely to keep the stored
    value compact — collision resistance is irrelevant (it's a change-detector, not a
    security hash).
    """
    material = (f"{_SCORING_INPUT_VERSION}\x00{_SYSTEM_BOILERPLATE}{prompt}"
               f"\x00{location_prompt}\x00{geo_uses_description}")
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

    A manual override (work_arrangement_actual — set when the feed is wrong, e.g. a
    recruiter says a role is hybrid though the posting reads on-site) wins and is sent
    verbatim. Otherwise the feed emits terse enums ("Remote OK", "Remote Solely") whose
    meaning — and whose office-days semantics — aren't self-evident, so they're glossed
    into plain English: for "Remote OK" the office-days apply only if you live near the
    office; for "Hybrid" they're required. An unrecognized value is passed through so
    nothing is lost.
    """
    override = str(job.get("work_arrangement_actual") or "").strip()
    if override:
        return override
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


def build_score_message(job: dict, geo_note: str | None = None) -> str:
    """Build the per-job user message the scorer sends (title/company/location/salary +
    description). Pure and self-contained so it can be unit-tested without an API call.

    Geography: when geo_note is supplied (a pre-assessed 'Geographic fit' verdict from the
    focused location sub-call, see assess_location_fit + geo_note), it REPLACES the raw
    Location line — the main scorer gets an authoritative conclusion instead of a multi-city
    list it has to match against the candidate's preferences itself (which it did
    unreliably: burying a preferred city in a long list, or dinging a fully-remote role for
    the other cities it lists). When geo_note is None (no location_prompt configured, or the
    sub-call failed), we fall back to sending the raw location list as before. Either way
    Work arrangement (remote / hybrid / on-site) is still sent, since remote status is a
    work-style fact the model may reference beyond geography. Fields that are genuinely
    absent are omitted rather than sent blank, so the candidate prompt handles the neutral
    case.
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

    # The pre-assessed geographic verdict, when present, stands in for the raw location
    # list — see the docstring. Phrased as authoritative so the main scorer treats it as a
    # fact rather than re-deriving geography from cities it can't reliably match.
    geo_line = (f"Geographic fit (already assessed against the candidate's location "
                f"preferences): {geo_note}\n") if geo_note else _location_line(job)

    return (
        f"Job title: {title}\n"
        f"Company: {company}\n"
        + geo_line
        + (f"Work arrangement: {wa}\n" if (wa := _work_arrangement(job)) else "")
        + (f"{salary_line}\n" if salary_line else "")
        + f"\nDescription:\n{description}"
    )


# System boilerplate for the focused location sub-call. Kept separate from the main
# scorer's so this call does exactly one thing — match a job's location(s) against the
# candidate's geographic preferences — which the model does reliably in isolation, unlike
# doing it inline while also judging scope/comp/industry. It describes only the *method*
# (best-match among the options) and asserts NOTHING about which locations or work
# arrangements are good — every value judgment, including how remote/hybrid/on-site is
# weighed, comes solely from the candidate's location_prompt, appended (and cached) below.
_GEO_SYSTEM = (
    "You assess how well a job's location fits a candidate's geographic preferences. "
    "Respond ONLY with a JSON object on a single line — no markdown, no explanation:\n"
    '{"fit": "preferred|good|acceptable|poor", "match": "the single best-matching location, or empty"}\n\n'
    "The fit tiers, best to worst, are: preferred, good, acceptable, poor. Use whichever tier "
    "the candidate's stated preferences assign to the matching location — the preferences "
    "define what earns each tier.\n"
    "A posting may list several locations and a work arrangement (remote / hybrid / on-site). "
    "Judge by the BEST available option against the candidate's stated preferences — never "
    "downgrade a job because one option is a poor fit if another option fits well. Weigh the "
    "work arrangement only as the candidate's preferences direct; do not assume any "
    "arrangement is inherently good or bad.\n\n"
)

# Appended to _GEO_SYSTEM only when the job description is included in the call (the
# location_use_description toggle). Omitted when it isn't, so the model is never told to
# consult a description it wasn't given.
_GEO_DESC_CLAUSE = (
    "The job description is provided below; it may make a work mode CONDITIONAL — e.g. remote "
    "work is offered only to residents of certain states/regions, or the role requires "
    "relocation. If the candidate does not meet a mode's condition (they don't reside where "
    "remote is permitted, they can't relocate, etc. — judge by their stated residence/"
    "constraints), that mode is NOT available to them: exclude it and rate by the options that "
    "remain. So a role that's on-site in an acceptable location but whose remote option excludes "
    "the candidate is still judged on the on-site option, not called remote.\n\n"
)

# Valid geographic-fit tiers, ordered best→worst; anything else from the model is a failure.
# The tier *names* are generic ordinal labels — which locations earn which tier is defined
# entirely by the candidate's location_prompt, never here.
GEO_FITS = {"preferred", "good", "acceptable", "poor"}


def geo_note(fit: str | None, match: str | None) -> str | None:
    """Compose the pre-assessed 'Geographic fit' phrase the main scorer sees, or None.

    None when there's no usable verdict (fit not in GEO_FITS) so build_score_message falls
    back to the raw location list. The tier leads in caps so the main model can't overlook
    it; the best-matching location is named so the score's 'reason' text can cite it.
    """
    if fit not in GEO_FITS:
        return None
    if fit == "poor":
        return "POOR — none of the listed locations matches the candidate's preferences"
    m = (match or "").strip()
    return fit.upper() + (f" (best match: {m})" if m else "")


def assess_location_fit(
    client,
    location_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
    include_description: bool = True,
) -> tuple[str, str, object] | tuple[None, None, None]:
    """Classify one job's geographic fit as its own cheap, single-purpose AI call.

    Isolating geography (see the build_score_message docstring for why the inline approach
    failed) means this call — not the main scorer — does all location reasoning, including
    the multi-dimensional kind (e.g. on-site in NC is acceptable even if the role's remote
    option is restricted to states the candidate doesn't live in). It weighs the location
    list, work arrangement, AND the description against the candidate's location_prompt and
    returns a tier. The description is included because eligibility *conditions* — "remote
    candidates must reside in <states>", relocation requirements — live in the prose, not the
    structured fields; without it the call would call a state-restricted "remote" role remote.
    All value judgments live in location_prompt; this function contributes only the plumbing.
    Returns (fit, match, usage) with fit in GEO_FITS, or (None, None, None) on any failure so
    the caller falls back to sending the raw location list.

    The candidate's location_prompt is identical for every job in a run, so it goes in the
    (ephemeral-cached) system block; the per-job location text + description are uncached.
    """
    locs = _job_locations(job)
    wa = _work_arrangement(job)
    if not locs and not wa:
        return None, None, None  # nothing geographic to assess — let the caller fall back

    shown, extra = locs[:_MAX_SCORE_LOCATIONS], len(locs) - _MAX_SCORE_LOCATIONS
    loc_text = ("; ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")
                if shown else "(none listed)")
    # The description carries eligibility conditions the structured fields miss (state-
    # restricted remote, relocation requirements). Capped to bound token cost per call, and
    # skippable via the location_use_description toggle (a cost/coverage trade-off: without
    # it the sub-call dedups hard by location set; with it, per description). When skipped,
    # the description-handling clause is dropped too so the prompt stays coherent.
    description = (job.get("job_description") or "").strip()[:_MAX_GEO_DESC_CHARS] if include_description else ""
    user_text = (
        f"Job locations: {loc_text}\n"
        + (f"Work arrangement: {wa}\n" if wa else "")
        + (f"\nJob description:\n{description}\n" if description else "")
    )
    system_text = (_GEO_SYSTEM + (_GEO_DESC_CLAUSE if description else "")
                   + f"Candidate location preferences:\n{location_prompt}")

    try:
        message = client.messages.create(
            model=model,
            max_tokens=128,  # a one-line JSON verdict — even smaller than the scorer's
            thinking={"type": "disabled"},  # trivial classification; same rationale as score_job
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_text}],
        )
        raw = message.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None, None, None
        data = json.loads(m.group())
        fit = str(data.get("fit", "")).lower().strip()
        match = str(data.get("match", "")).strip()
        if fit not in GEO_FITS:
            return None, None, None
        return fit, match, message.usage
    except Exception:
        return None, None, None


def score_job(
    client,
    viability_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
    geo_note: str | None = None,
) -> tuple[str, str, object] | tuple[None, None, None]:
    """Score a job posting for viability against the candidate description.

    Returns (rating, reason, usage): rating is 'low'/'medium'/'high', reason is a
    one-sentence justification, and usage is the Anthropic token-usage object (for
    cost tallying). Returns (None, None, None) on any failure — unparseable response,
    invalid rating, or API error — so the caller can skip the job and move on.

    geo_note, when provided (from geo_note(*assess_location_fit(...))), is a pre-assessed
    geographic-fit verdict that replaces the raw location list in the message so the model
    doesn't re-derive geography itself. None → the raw location list is sent as before.

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
                "content": build_score_message(job, geo_note=geo_note),
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
