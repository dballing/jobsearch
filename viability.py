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

from ai_config import (DEFAULT_EFFORT, effective_effort, is_reasoning_model,
                       resolve_ai_settings, resolve_effort, resolve_geo_effort, resolve_geo_model)

# The valid rating values; anything else from the model is treated as a failure.
VIABILITY_RATINGS = {"low", "medium", "high"}

# Boilerplate prepended to every system prompt before the candidate description.
# The single-line-JSON instruction keeps the reply tiny and trivially parseable; the
# one-plain-sentence rule keeps the `reason` a legible verdict rather than a leaked scratchpad
# (a reasoning model run with thinking disabled otherwise dumps its analysis there — see
# _scorer_thinking, which turns thinking back on for such models); the compensation note teaches
# the model the cents=hourly / round=annual heuristic so it normalizes pay before judging fit;
# the company-preference note pins a named-company exclusion to the exact employer, so it doesn't
# bleed onto similar/adjacent companies or get confused with the candidate's own former employer
# (a Google-only "avoid" — Google also being the candidate's background — was both dragging down
# Microsoft as "Google-adjacent big tech" and, on one run, mislabeling Microsoft as excluded);
# the employment-relationship note stops it inferring a
# contract/staffing arrangement from generic "client" boilerplate (aggregated postings are full
# of it), deferring instead to the curated "(posted via …)" note (see is_job_board /
# build_score_message) or explicit contract terms. Changing this constant changes prompt_hash,
# which correctly invalidates every existing score (they were produced under the old prompt).
_SYSTEM_BOILERPLATE = (
    "You evaluate job postings for a specific candidate. "
    "Respond ONLY with a JSON object on a single line — no markdown, no explanation:\n"
    '{"rating": "low|medium|high", "reason": "one sentence"}\n'
    'The "reason" is a single plain sentence a person can read at a glance — the one biggest '
    "factor behind the rating. State the conclusion, not your reasoning process: no step-by-step "
    "analysis, no self-questions, no parenthetical asides, no scratchpad.\n\n"
    "Compensation note: dollar amounts with cents (e.g. $51.45, $62.99) are hourly "
    "wages, not annual salaries. Convert hourly rates to annual (multiply by ~2,080) "
    "before comparing against the candidate's expectations. Round numbers "
    "(e.g. $120,000 or $120k) are annual.\n\n"
    "Employment relationship note: decide whether a role is a third-party "
    "contract/staffing/outsourced arrangement ONLY from the Company line's "
    "'(posted via <agent>)' note or from an EXPLICIT statement of contract terms in the "
    "description (e.g. 'contract role', 'W2 contract', 'corp-to-corp', a fixed engagement "
    "length). Generic listing or marketing boilerplate — references to serving 'clients' or "
    "'our client', vertical/industry mentions, or templated wording — is common on aggregated "
    "postings and is NOT by itself evidence of such an arrangement; treat it as neutral. Absent "
    "a 'posted via' note or explicit contract terms, treat the posting as a direct hire at the "
    "named employer.\n\n"
    "Company-preference note: apply the candidate's company-specific preferences literally and "
    "precisely. A company exclusion lowers a rating ONLY when the posting's EMPLOYER is exactly "
    "that named company. It never applies to a DIFFERENT company for being a competitor, similar "
    "in size, in the same industry, or 'adjacent' to the excluded one; and never confuse the "
    "excluded company with a company the candidate merely worked at in the past (the candidate's "
    "own former employer is not an exclusion). When the employer is any other company, the "
    "exclusion is irrelevant and must not lower the rating or appear in the reason.\n\n"
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
# were developed together and a cron rescore may have stamped scores mid-change; 9 = a POOR
# geographic-fit verdict now deterministically clamps the overall rating to 'low'
# (clamp_viability_for_geo) instead of trusting the main scorer, which discounted it, AND the
# description clause (_GEO_DESC_CLAUSE) was hardened so the description only downgrades a work mode
# on an EXPLICIT eligibility condition — not from ambient office/regional/pay-zone language, which
# had produced false-POOR verdicts on fully-remote jobs (e.g. a remote CA role read POOR). Also
# shipped: the geo sub-call's model auto-escalates to the viability model when it reads the
# description (resolve_geo_model) — not a prompt_hash input (the model never is), but part of the
# same scoring-behavior change, so the bump ensures existing scores re-run under it too. 10 =
# re-bumped because 9 and 10 were developed together: 9 was set at the start of the clamp work, then
# the description clause + model escalation changed the scoring behavior *after* 9 was already on
# disk, where a cron rescore (or the --debug single-job rescore) could have stamped scores hash-9
# with that intermediate behavior — the re-bump invalidates any such score so all re-run under the
# final geo path. 11 = two paired changes to how the third-party/contract signal is conveyed:
# (a) build_score_message drops the "(posted via <source>)" company note when the source is a
# known job board/aggregator (is_job_board) — pure distribution, not a recruiter/contract signal,
# which was making the scorer infer staffing arrangements for aggregator reposts (e.g. a Netflix
# role relisted via Ladders scored low); unknown sources (a possible recruiter) keep the note;
# and (b) the _SYSTEM_BOILERPLATE now tells the model to judge a contract/staffing arrangement
# ONLY from that (now-curated) "posted via" note or explicit contract terms, treating generic
# "our client"/vertical boilerplate in the description as neutral. 12 = re-bumped because 11 was
# briefly on disk (the app auto-reloads under --debug) while the two paired changes above were
# still being written, and a cron rescore fired in that window — so some scores got stamped
# hash-11 under intermediate behavior and would look current forever; 12 invalidates them so all
# re-run under the final (a)+(b) behavior. 13 = stop the scorer's "reason" from becoming a leaked
# scratchpad: a reasoning-capable model (e.g. the configured Sonnet 5) run with thinking DISABLED
# reasons out loud into the only prose slot, the JSON "reason". Fix is paired: _scorer_thinking
# now runs adaptive thinking (effort=medium, bigger max_tokens) on such models so the reasoning stays
# in a hidden thinking block, and the boilerplate now demands a single plain-sentence reason. Also
# folded in: a company-preference note telling the model not to generalize a named-company
# exclusion to similar/adjacent companies (a Google-only "avoid" was penalizing Microsoft as
# "Google-adjacent big tech"). The thinking change isn't a prompt_hash input (like model choice),
# but the boilerplate changes are, and the bump makes existing scores re-run under all of it.
# 14 = scorer/geo thinking effort became configurable ([viability].effort / .location_effort,
# defaulting to [ai].effort → "medium"), and the geo sub-call now runs adaptive thinking on a
# reasoning model too (was always thinking-disabled). The *effective* effort (None on a
# non-reasoning model) is now folded into prompt_hash, so changing it re-scores only when it
# actually changes behavior; the bump re-runs everything once under the new machinery.
_SCORING_INPUT_VERSION = "14"

# Cap on locations included in the scoring message — bounds token cost for the rare job
# posted across dozens of sites, while staying generous enough to almost never truncate
# (the candidate only needs their target region to be among those listed).
_MAX_SCORE_LOCATIONS = 40

# Cap on the description fed to the location sub-call. Eligibility conditions ("remote only
# for residents of …") sit in the prose, so the call needs the description — but bounded, to
# keep the (now per-description) sub-call cheap.
_MAX_GEO_DESC_CHARS = 4000


def prompt_hash(prompt: str, location_prompt: str = "", geo_uses_description: bool = True,
                effort: str | None = None, geo_effort: str | None = None) -> str:
    """Return a stable 32-char hex digest of everything that determines a job's score.

    Covers the fixed system boilerplate, the candidate description, the separate
    geographic-preferences prompt (location_prompt — fed to the focused location sub-call,
    see assess_location_fit), the location_use_description toggle (geo_uses_description —
    whether that sub-call reads the description, which changes its verdict), the *effective*
    scorer and geo thinking effort (``effort`` / ``geo_effort`` — pass the value actually used,
    i.e. None on a non-reasoning model, so a config change re-scores only when it truly changes
    behavior; unlike the model, which is never hashed), AND the per-job message schema version —
    so a config edit to any of them, a boilerplate change, OR a change to which fields we send all
    correctly mark existing scores stale. The version is hashed but never sent to the model.
    Truncated to 32 hex chars purely to keep the stored value compact — collision resistance is
    irrelevant (it's a change-detector, not a security hash).
    """
    material = (f"{_SCORING_INPUT_VERSION}\x00{_SYSTEM_BOILERPLATE}{prompt}"
               f"\x00{location_prompt}\x00{geo_uses_description}\x00{effort}\x00{geo_effort}")
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def scoring_hash_for_config(config: dict) -> str | None:
    """The current scoring hash for a loaded config — the single source every caller uses so the
    batch rescore, the on-demand single-job rescore, and the web-UI staleness check derive an
    IDENTICAL hash (a mismatch would make freshly-scored jobs read as permanently stale).

    Resolves the same prompt / location-prompt / description-toggle the scorer sees, plus the
    *effective* scorer and geo effort — effort folded in via effective_effort so it only affects
    the hash when it actually applies (a reasoning model), never on a Haiku config, and the model
    itself is never hashed. Returns None when no viability prompt is configured (nothing to score
    or compare)."""
    vcfg = config.get("viability", {}) or {}
    viability_prompt = vcfg.get("prompt", "").strip()
    if not viability_prompt:
        return None
    location_prompt = vcfg.get("location_prompt", "").strip()
    gud = bool(vcfg.get("location_use_description", True))
    v_model  = resolve_ai_settings(config, "viability")[1]
    v_effort = resolve_effort(config, "viability")[0]
    geo_model  = resolve_geo_model(config, gud)
    geo_effort = resolve_geo_effort(config, gud)[0]
    return prompt_hash(viability_prompt, location_prompt, gud,
                       effort=effective_effort(v_model, v_effort),
                       geo_effort=effective_effort(geo_model, geo_effort))


# Statuses on which a reject-listed company forces the job to low/autoskipped. This is the
# pre-decision set (new/reviewing/deferred) plus autoskipped itself (so a --autoskipped
# re-evaluation re-denies a still-listed company for free rather than paying to AI-score it and
# possibly promoting it back). A job you've already acted on — applied, interviewing, etc. — is
# deliberately NOT here: your human decision outranks a later reject-list edit, so those get a
# normal AI score for reference and keep their status. Shared by the batch rescorer and the
# web-UI single-job rescore so both honor the same guardrail.
REJECT_DENYABLE_STATUSES = ("new", "reviewing", "deferred", "autoskipped")


def build_reject_set(config: dict) -> "frozenset[str]":
    """The set of reject-listed employer names (lowercased, trimmed) from
    ``[viability].reject_companies`` — companies the candidate will never work for, whose jobs
    are forced to low/autoskipped WITHOUT an AI call (a hard human decision, not a judgment call).

    Matching is exact and case-insensitive against the *whole* company field — the same semantics
    as [company_aliases] and, crucially, run AFTER it: the stored ``company`` is already the
    alias-normalized canonical name at ingest time, so listing the canonical "Foo" here catches
    every "Foo, LLC"/"Foo Co." variant the alias map folds into it. Empty/absent → empty set (the
    feature is off, indistinguishable from a config with no [viability] section). Deliberately NOT
    part of scoring_hash_for_config: folding it into the hash would mark the whole DB stale and
    force a full-DB AI re-score just to add one name — instead, listed jobs are denied cheaply as
    they flow through selection, and un-listing one re-scores it via `--autoskipped`."""
    vcfg = config.get("viability", {}) or {}
    raw = vcfg.get("reject_companies") or []
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(name).strip().lower() for name in raw if str(name).strip())


def is_rejected_company(company: object, reject_set: "frozenset[str]") -> bool:
    """True when ``company`` is on the reject-list. Case-insensitive, whitespace-trimmed, whole
    field — never substring (so "Foo" never matches "Foobar Inc"). Empty set / blank company →
    False."""
    if not reject_set or not company:
        return False
    return str(company).strip().lower() in reject_set


def reject_reason(company: object) -> str:
    """The stored viability_reason for a job denied by the reject-list — names the employer so the
    UI verdict is self-explanatory (vs. a bare 'low' with no rationale)."""
    name = (str(company).strip() if company else "") or "this employer"
    return f"Autoskipped: {name} is on your company reject-list."


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


# Known job boards / aggregators — pure *distribution* channels that repost a real employer's
# listing under their own name (so an ingested job can arrive scraped as "<board>" and inherit
# the true employer as a company_actual override). These are NOT recruiters/staffing firms: when
# a job's "posted via <source>" is one of these, the source says nothing about the employment
# arrangement, and surfacing it to the scorer made it wrongly infer a contract/staffing role (a
# Netflix posting relisted "via Ladders" scored low as if it were a staffing gig). Deliberately
# excludes staffing firms (Jobot, Robert Half, …), whose "via" genuinely IS a recruiter signal
# worth keeping. Stored normalized (see _normalize_org); match is on the normalized scraped name.
_KNOWN_JOB_BOARDS = frozenset({
    "ladders", "linkedin", "indeed", "ziprecruiter", "glassdoor", "monster", "dice", "builtin",
    "jobgether", "remotehunter", "weworkremotely", "wellfound", "angellist", "simplyhired",
})


def _normalize_org(name: str) -> str:
    """Collapse an org name to a bare comparison key: lowercase, drop a leading 'the', strip a
    trailing TLD-ish suffix (.com/.io/…), and remove all non-alphanumerics — so 'The Ladders',
    'Ladders.com', and 'ladders' all become 'ladders' (and 'We Work Remotely' -> 'weworkremotely')."""
    s = (name or "").strip().lower()
    if s.startswith("the "):
        s = s[4:]
    s = re.sub(r"\.[a-z]{2,4}$", "", s)          # trailing .com / .io / .co / .org …
    return re.sub(r"[^a-z0-9]", "", s)


def is_job_board(name: str) -> bool:
    """True when `name` is a recognized job board / aggregator (pure distribution) rather than a
    recruiter/staffing firm — used to decide whether a 'posted via <name>' note is meaningful
    context for the scorer or misleading noise. Pure, so it's unit-testable without the DB."""
    return _normalize_org(name) in _KNOWN_JOB_BOARDS


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
    # A manual title override (title_actual) wins over the scraped title — the user has
    # corrected a mangled/generic feed title, so score against the corrected one.
    title       = (job.get("title_actual") or job.get("title") or "(no title)").strip()
    company_raw = (job.get("company")  or "(unknown company)").strip()
    company_actual = (job.get("company_actual") or "").strip()
    # With a company-name override, always show the real employer. Keep the scraped "(posted via
    # <source>)" note ONLY when the source could be a recruiter/staffing firm — that context helps
    # the model judge a genuine contract arrangement. For a known job board/aggregator (Ladders,
    # LinkedIn, Indeed, …) the "via" is pure distribution and says nothing about the employment
    # structure; surfacing it made the model wrongly infer a contract/staffing role, so drop it
    # and show the employer alone. See is_job_board.
    if company_actual and company_actual != company_raw:
        company = (company_actual if is_job_board(company_raw)
                   else f"{company_actual} (posted via {company_raw})")
    else:
        company = company_raw
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
    "The job description is provided below. Use it for ONE purpose only: to detect an EXPLICIT "
    "eligibility condition that changes which work modes are actually open to this candidate — "
    "e.g. the text states remote work is limited to residents of specific states/regions, or the "
    "role requires relocation. If such a stated condition excludes the candidate (judge by their "
    "stated residence/constraints), that mode is NOT available to them: exclude it and rate by the "
    "options that remain — so an on-site-in-an-acceptable-location role whose remote option excludes "
    "the candidate is judged on the on-site option, not called remote.\n"
    "Otherwise IGNORE the description for location purposes. Do NOT infer a restriction from the "
    "office or headquarters location, from regional/'global'/multi-region language, from pay zones, "
    "or from anything short of an explicit eligibility condition. Absent such a condition, rate "
    "EXACTLY as you would from the location list and work arrangement alone; in particular a "
    "fully-remote role with no office requirement keeps its remote-based fit no matter where an "
    "office happens to be.\n\n"
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


# The manual work-arrangement override the user sets when a role is genuinely remote BUT
# only for residents of states/regions the candidate can't be in — the one geographic dead
# end neither the feed nor the location sub-call reliably catches. The feed reports a plain
# "Remote OK" (glossed to "fully remote; no office requirement"), and the state restriction
# lives only in an *implicit* location list rather than an explicit eligibility sentence, so
# the sub-call's description clause never fires and it rates the remote option a preferred fit.
# (Observed: NVIDIA's "Senior Manager, Customer Program Management" — remote only in CA/TX/WA,
# scored high for an Alexandria-based candidate.) Selecting this in the work-arrangement
# dropdown flags the job as a deterministic POOR geographic fit; see is_manual_geo_poor.
GEO_UNSUPPORTED_ARRANGEMENT = "Remote (unsupported location)"


def is_manual_geo_poor(job: dict) -> bool:
    """True when the job's manual work-arrangement override is the 'remote in an unsupported
    location' flag (GEO_UNSUPPORTED_ARRANGEMENT). The caller treats this as a POOR geographic
    verdict without an AI location call. Pure, so it's unit-testable without the DB."""
    return str(job.get("work_arrangement_actual") or "").strip() == GEO_UNSUPPORTED_ARRANGEMENT


# The manual "I'd take this job at this location" override — the positive counterpart to the
# GEO_UNSUPPORTED_ARRANGEMENT flag. When a posting's geography would otherwise sink it (the AI
# location sub-call rates it POOR, or it's a manually-tracked job the candidate is only
# entering *because* they'd work it), the user flags the location fit ACCEPTABLE. That verdict
# is fed to the scorer verbatim in place of the billed sub-call and — being ACCEPTABLE, not
# POOR — never trips the low-clamp. Only "acceptable" is offered (the candidate is asserting
# "workable", not ranking the location); the column holds the general geo tier so it stays
# symmetric with GEO_FITS should that ever grow.
MANUAL_GEO_FIT_CHOICES = ("acceptable",)


def manual_geo_fit(job: dict) -> str | None:
    """The manual geographic-fit override (geo_fit_actual) for a job, or None.

    When set (only 'acceptable' today), the caller uses it as the geographic verdict and
    SKIPS the billed location sub-call — the candidate has asserted the location is workable,
    so there's nothing to assess. Returns a value only when it's a recognized GEO_FITS tier,
    so a stray column value can't inject a bogus verdict. Pure, so it's unit-testable without
    the DB."""
    v = str(job.get("geo_fit_actual") or "").strip().lower()
    return v if v in GEO_FITS else None


def manual_geo_verdict(job: dict) -> "tuple[str | None, str | None, bool]":
    """Resolve any *manual* geographic verdict for a job, before the (billed) AI location call.

    Returns (fit, gnote, manual_poor):
      - An ACCEPTABLE override (manual_geo_fit) WINS over everything: (that tier, its note,
        False) — the candidate has asserted the location is workable.
      - Else the 'remote in an unsupported location' flag (is_manual_geo_poor): a deterministic
        POOR verdict → ('poor', its note, True).
      - Else (None, None, False): no manual verdict, so the caller should run assess_location_fit.
    A `fit` of None is the sole signal to fall through to the AI call. manual_poor is echoed so
    the caller passes it to clamp_viability_for_geo for the honest 'manual flag' reason suffix —
    and, since the override branch returns manual_poor=False, an ACCEPTABLE override also
    suppresses the low-clamp a coexisting POOR flag would otherwise apply. Pure, so the whole
    precedence is unit-testable without an API call."""
    override = manual_geo_fit(job)
    if override:
        return override, geo_note(override, ""), False
    if is_manual_geo_poor(job):
        return "poor", geo_note("poor", ""), True
    return None, None, False


# Appended to the score reason when a POOR geographic fit forces the rating down (see
# clamp_viability_for_geo). Leads with the trigger so the override is self-explaining in the UI.
_GEO_POOR_SUFFIX = (
    " [Forced to LOW: geographic fit is POOR — none of this role's locations or work "
    "arrangements is workable for the candidate, which is disqualifying regardless of other merits.]"
)

# The manual counterpart, used when the POOR verdict comes from the GEO_UNSUPPORTED_ARRANGEMENT
# flag rather than the AI location call — so the score is honestly attributed to a manual flag,
# not the model (which, for these roles, would have rated the remote option a good fit).
_GEO_MANUAL_POOR_SUFFIX = (
    " [Forced to LOW: manually flagged as remote-only in a location the candidate can't work "
    "from — the remote option is geographically unavailable, which is disqualifying.]"
)


def clamp_viability_for_geo(
    fit: str | None, rating: str | None, reason: str, manual: bool = False,
) -> tuple[str | None, str]:
    """Force a POOR-geography job down to 'low', preserving the model's own reasoning.

    A POOR fit is the bottom tier — *no* listed location or remote option the candidate can
    actually work — so the role is untenable however well it fits on scope/comp/industry. But
    the main scorer treats the pre-assessed 'Geographic fit: POOR' line as merely one negative
    and routinely still returns 'medium' (observed: a role whose reason literally said the
    location "excludes candidate" scored medium). Rather than trust prompt wording the model
    demonstrably discounts, we clamp deterministically here.

    Only POOR is clamped — 'acceptable'/'good'/'preferred' (and a failed/None geo verdict) pass
    through untouched, so the sub-call's nuance is respected. If the model already rated it low
    the rating and reason are returned verbatim (no redundant suffix); otherwise the rating
    becomes 'low' and a bracketed note explaining the override is appended to the model's reason
    (kept, so the richer 'why' — staffing firm, methodology focus, etc. — isn't lost). A None
    rating (score_job failed) is left as-is for the caller to skip. Pure, so it's unit-testable
    without an API call.

    manual=True selects the reason suffix that attributes the clamp to a manual flag
    (GEO_UNSUPPORTED_ARRANGEMENT) instead of the AI location verdict; the clamp is otherwise
    identical. It's ignored unless a POOR fit actually forces a change.
    """
    if fit != "poor" or rating is None or rating == "low":
        return rating, reason
    suffix = _GEO_MANUAL_POOR_SUFFIX if manual else _GEO_POOR_SUFFIX
    return "low", (reason or "").rstrip() + suffix


def assess_location_fit(
    client,
    location_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
    include_description: bool = True,
    effort: str = DEFAULT_EFFORT,
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

    # Reasoning model → adaptive thinking at `effort` (its reasoning stays hidden; on Sonnet 5
    # this avoids the same scratchpad-in-output problem the main scorer had); Haiku/older →
    # thinking disabled with a tiny budget (a trivial one-line JSON verdict), effort ignored.
    cfg = _thinking_call_config(model, effort, disabled_max_tokens=128, adaptive_max_tokens=2048)
    try:
        message = client.messages.create(
            model=model,
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_text}],
            **cfg,
        )
        # With thinking on, content[0] is a thinking block — pull the text block explicitly.
        raw = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "").strip()
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


def _scorer_thinking(model: str) -> dict:
    """Thinking config for an AI scorer/geo call, by model: adaptive for reasoning-capable models
    (so their reasoning stays in a hidden block and the configured `effort` applies), disabled for
    Haiku/older. A reasoning model run with thinking *disabled* reasons out loud in the visible
    output — and since the only prose slot is the JSON "reason", its whole scratchpad lands there
    (observed on a Sonnet-5 scorer). Pure, so it's unit-testable without an API call."""
    return {"type": "adaptive"} if is_reasoning_model(model) else {"type": "disabled"}


def _thinking_call_config(model: str, effort: str, *, disabled_max_tokens: int,
                          adaptive_max_tokens: int) -> dict:
    """The model-generation-aware bits of a messages.create() call — {max_tokens, thinking} plus
    output_config when adaptive. On a reasoning model: adaptive thinking at `effort`, with the
    larger token budget to leave room for the (hidden) thinking. On Haiku/older: thinking disabled
    and the small budget, and `effort` is simply not sent (thrown away). Shared by score_job and
    assess_location_fit so both handle the reasoning/non-reasoning split identically."""
    thinking = _scorer_thinking(model)
    if thinking["type"] == "adaptive":
        return {"max_tokens": adaptive_max_tokens, "thinking": thinking,
                "output_config": {"effort": effort}}
    return {"max_tokens": disabled_max_tokens, "thinking": thinking}


def score_job(
    client,
    viability_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
    geo_note: str | None = None,
    effort: str = DEFAULT_EFFORT,
) -> tuple[str, str, object] | tuple[None, None, None]:
    """Score a job posting for viability against the candidate description.

    Returns (rating, reason, usage): rating is 'low'/'medium'/'high', reason is a
    one-sentence justification, and usage is the Anthropic token-usage object (for
    cost tallying). Returns (None, None, None) on any failure — unparseable response,
    invalid rating, or API error — so the caller can skip the job and move on.

    geo_note, when provided (from geo_note(*assess_location_fit(...))), is a pre-assessed
    geographic-fit verdict that replaces the raw location list in the message so the model
    doesn't re-derive geography itself. None → the raw location list is sent as before.

    On a reasoning model the run uses adaptive thinking at ``effort`` (its reasoning stays in a
    hidden block, keeping `reason` a clean sentence); a non-reasoning model runs thinking-disabled
    and ignores ``effort`` (see _thinking_call_config).

    The system prompt (boilerplate + candidate profile) is identical for every job in
    a run, so it's marked for ephemeral prompt caching — only the per-job user message
    is uncached, making a full rescore cheap after the first call.
    """
    system_text = _SYSTEM_BOILERPLATE + f"Candidate description:\n{viability_prompt}"
    cfg = _thinking_call_config(model, effort, disabled_max_tokens=256, adaptive_max_tokens=3072)
    try:
        message = client.messages.create(
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": build_score_message(job, geo_note=geo_note),
            }],
            model=model,
            **cfg,
        )
        # With thinking on, content[0] is a thinking block — pull the text block explicitly.
        raw = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "").strip()
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
