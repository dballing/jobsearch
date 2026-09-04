#!/usr/bin/env python3
"""Shared viability-scoring helpers used by rescore_viability.py.

Rates a job posting 'low'/'medium'/'high' for one specific candidate by asking
Anthropic to compare the posting against a candidate-profile prompt. A fixed
boilerplate plus the candidate profile form the (cached) system prompt; each job is
a single user message. ``score_job()`` returns (rating, reason, factors, usage) — where
``factors`` is the model's self-reported per-dimension breakdown (see parse_factors);
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

# The fixed factor-breakdown dimensions the scorer must always self-report (see
# _SYSTEM_BOILERPLATE and parse_factors). Kept as a constant — not inlined — so the prompt text,
# the UI render order, and the tests all agree on the canonical set and its order. The model may
# append extra model-chosen axes beyond these; those are surfaced as-is so we can normalize and
# enumerate them later as patterns emerge.
#
# These six deliberately split what a single "work_fit" axis used to conflate — CAN I do it
# (role_requirements_fit), do I WANT it (role_interest_fit), and is the LEVEL right (seniority_fit)
# — which were three independent judgments hidden in one number, so a consideration like
# "farmed-out consulting" had no clean home and leaked into the rating invisibly.
FACTOR_DIMENSIONS = (
    "role_requirements_fit", "role_interest_fit", "seniority_fit",
    "company_fit", "compensation", "location",
)

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
    '{"rating": "low|medium|high", "reason": "one sentence", '
    '"factors": [{"dimension": "work_fit", "score": 0, "note": "short phrase"}]}\n'
    'The "reason" is a single plain sentence a person can read at a glance — the one biggest '
    "factor behind the rating. State the conclusion, not your reasoning process: no step-by-step "
    "analysis, no self-questions, no parenthetical asides, no scratchpad.\n\n"
    # The factors array is a self-report that makes the rating auditable: for each consideration it
    # states how much it ACTUALLY moved this rating, so a reader can tell a factor merely mentioned
    # (score 0) from one silently held against the job (negative). The six fixed dimensions split
    # what a single "work_fit" used to conflate — capability, desire, and level — so a consideration
    # like "farmed-out consulting" has an explicit home (role_interest_fit) instead of leaking into
    # the rating with no factor. The "rating must be fully accounted for by the factors" rule is the
    # crux: it forbids invisible up/down-scoring, which is the entire point of the breakdown.
    'The "factors" array reports how much each consideration ACTUALLY moved THIS rating, so the '
    "verdict is auditable and nothing is scored invisibly. ALWAYS include all six of the following "
    "dimensions, by these exact names — they are the required MINIMUM, never a maximum; add extra "
    "axes on top of them whenever a real consideration doesn't fit one of the six (see the rule "
    "below the list):\n"
    "- role_requirements_fit: CAN the candidate do this job as described? Skills, experience, "
    "technical scope, required domain expertise, judged against the candidate profile. Capability "
    "only — not whether they'd want it, not whether the level is right.\n"
    "- role_interest_fit: does the candidate WANT this kind of role? Judge from the DESCRIBED "
    "NATURE of the work, not the employer's name. A role at a consulting/professional-services firm "
    "that is INTERNAL (running the firm's own projects) is neutral; one where the candidate would "
    "be FARMED OUT to client engagements (staff-augmentation / client-delivery) is negative when "
    "they've said they avoid that. Likewise industry/domain/values distaste (e.g. defense "
    "contractors), heavy customer-facing work, or a mission mismatch. Judge farm-out from the "
    "role's genuine described scope, NOT from generic recruiter/aggregator 'our client' boilerplate "
    "(see the employment-relationship note). Desire, not capability.\n"
    "- seniority_fit: is the role's LEVEL (title/scope seniority) right for how the candidate "
    "describes themselves? Weight this GENTLY: a role about one level below or above target is a "
    "MILD signal at most (-1 to +1) — candidates often take a small step back to re-enter or a "
    "small step forward to grow, so a minor level mismatch must NOT sink an otherwise-strong role. "
    "Reserve -2/+2 for a LARGE mismatch (far junior or far senior). Defer to any explicit seniority "
    "tolerance stated in the candidate profile.\n"
    "- company_fit: is the EMPLOYER itself a good match/bet? Size, stage, financial viability, "
    "reputation, growth — plus the candidate's hard company preferences: a named-company exclusion "
    "matching THIS employer is strongly negative (apply it precisely — see the company-preference "
    "note).\n"
    "- compensation: pay versus the candidate's stated target (apply the compensation note above "
    "to normalize hourly/annual first).\n"
    "- location: geographic and work-arrangement fit — reflect the pre-assessed 'Geographic fit' "
    "verdict when one is provided above.\n"
    "Each entry's \"score\" is a signed number from -2 to +2 for that dimension's effect ON THE "
    "RATING: +2 strongly raised it, +1 mildly raised it, 0 had NO effect, -1 mildly lowered it, "
    "-2 strongly lowered it. A score of 0 is required and correct whenever a dimension is neutral "
    "or simply absent — e.g. when no compensation is listed and that neither helps nor hurts, "
    "compensation MUST be 0, never negative; likewise never lower a score for anything the notes "
    "above tell you to treat as neutral. The \"note\" is a terse phrase of a few words, not a "
    "sentence and not a scratchpad.\n"
    "The rating MUST be fully accounted for by the factors: every consideration that raised or "
    "lowered it has to appear as a factor. If something moved the rating that none of the six "
    "dimensions genuinely captures, you MUST add an extra entry for it — coining a short, "
    "descriptive snake_case name for that specific consideration — rather than stretch a fixed "
    "dimension to cover it or let it affect the rating invisibly. These extra axes are OPEN-ENDED: "
    "name whatever the posting actually raises and do NOT restrict yourself to a fixed menu "
    "(things like travel_burden or security_clearance are only illustrations of the KIND of factor "
    "— invent whatever fits, e.g. relocation_required, visa_sponsorship, on_call, night_shift), "
    "adding one only when it genuinely applies and moved the rating. Never return a rating the "
    "scores don't explain (e.g. an overall 'low' whose factors are all 0 or positive). The "
    "factors, the reason, and the rating must all agree.\n"
    # Veto rule: the rating is a holistic judgment, NOT a sum of the factors. Some negatives are
    # disqualifying — a stated absolute dealbreaker, or an inability to do the core job — and must
    # sink the job to 'low' no matter how strong everything else is. Without this the model dilutes
    # a dealbreaker into the average and returns 'medium' (observed: a term-limited role the
    # candidate rejects, and the inconsistency where "can't do the job" vetoed but "won't take it"
    # did not). Soft preferences are deliberately NOT vetoes, so this doesn't over-suppress.
    "The rating is a holistic judgment, not an average of the scores. Some factors are VETOES: when "
    "the candidate's profile states an ABSOLUTE dealbreaker (wording like 'won't', 'will not', "
    "'not interested in', 'refuse', 'avoid entirely') and this posting has that trait, OR the "
    "candidate cannot do the core job as described, that factor is disqualifying — rate the job "
    "'low' regardless of how positive everything else is (a strongly positive factor total does "
    "NOT rescue a vetoed job), and name the veto in the reason. Distinguish a veto from a mere soft "
    "preference ('prefer to avoid', 'ideally', 'less interested in'): a soft preference is a strong "
    "negative (down to -2) but NOT an automatic veto.\n\n"
    "Compensation note: dollar amounts with cents (e.g. $51.45, $62.99) are hourly "
    "wages, not annual salaries. Convert hourly rates to annual (multiply by ~2,080) "
    "before comparing against the candidate's expectations. Round numbers "
    "(e.g. $120,000 or $120k) are annual.\n\n"
    "Employment relationship note: decide whether a role is a third-party "
    "contract/staffing/outsourced arrangement ONLY from the Company line's "
    "'(posted via <agent>)' note or from an EXPLICIT statement of contract terms in the "
    "description (e.g. 'contract role', 'W2 contract', 'corp-to-corp', a fixed engagement "
    "length). Everything else is neutral and must NOT lower the rating or become the reason. "
    "In particular, treat as neutral: generic 'clients'/'our client' references, vertical/"
    "industry mentions, and templated wording; the recruiter-style framing common to aggregated "
    "postings ('For our client, we are seeking …', 'Our client is an equal-opportunity employer', "
    "an unnamed employer described only by industry) — this means the posting was distributed "
    "through an intermediary, NOT that the role is a contract/staffing engagement; and a Company "
    "line that is itself a job board or aggregator (e.g. Ladders, LinkedIn, Indeed) with the "
    "underlying employer unnamed — a distribution artifact, not a hiring structure. In all these "
    "cases judge the ROLE on its stated scope, seniority, compensation, and industry, exactly as "
    "you would a direct posting; do not dock it or headline it for how it was distributed or for "
    "the employer not being named.\n\n"
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
# 15 = strengthened the employment-relationship note so the "our client"/aggregator signal is
# fully neutral. v11 dropped the "(posted via <board>)" note and told the model generic "client"
# body text was neutral, but a job whose *Company line is itself a bare job board* (e.g. Company:
# Ladders, no real employer named) with a recruiter-style description ("For our client, we are
# seeking …") still got docked as staffing — the old note's "treat as a direct hire at the named
# employer" didn't compute when the named employer was a distribution channel. The note now
# explicitly treats recruiter framing and a bare-job-board employer as distribution artifacts, not
# a hiring structure, and says to judge the role on its stated merits regardless. Verified live:
# the Ladders "Product Program Manager" went medium→high with a role-focused reason.
# 16 = the scorer now also self-reports a factor breakdown: alongside rating+reason it returns a
# "factors" array giving each fixed dimension a signed -2..+2 contribution + terse note, where 0
# means "no effect on the rating", plus any extra model-chosen axes. This makes the verdict
# auditable — a factor merely mentioned (0) is now distinguishable from one silently held against
# the job (negative), the exact ambiguity the old one-sentence reason couldn't resolve. The six
# fixed dimensions (role_requirements_fit / role_interest_fit / seniority_fit / company_fit /
# compensation / location) deliberately split what a single "work_fit" conflated — CAN I do it /
# do I WANT it / is the LEVEL right — after an early four-axis version (work_fit/compensation/
# location/company) let a real consideration (farmed-out consulting at KPMG) leak into the rating
# with no factor; the paired invariant "the rating must be fully accounted for by the factors"
# forbids such invisible scoring. It's a boilerplate change (already hashed), but the version bump
# follows the discipline and re-runs every score once under the new contract. Rating stability vs.
# the prior prompt was validated with compare_scoring.py before this bump.
#
# NOT a version bump: build_score_message now prefers a manual description override
# (description_actual) over the feed's job_description, via effective_description. Like the other
# per-job overrides it already reads (title_actual/company_actual/salary_*_actual/
# work_arrangement_actual/geo_fit_actual), this changes what the model sees ONLY for a job that
# has the override set — and setting the override always flags that job needs_rescored, so it
# re-scores on its own. A global bump would needlessly re-score the whole DB for jobs whose
# effective description is unchanged, so — consistent with those existing overrides — it holds.
#
# v17: build_score_message now renders the salary in its real currency (currency_symbol on the
# feed's salary_currency / the salary_currency_actual override) instead of hardcoding "$". This
# changes what the model sees for every non-USD (or currency-overridden) posting — a €/£ band was
# previously mislabeled as dollars — so it's a global bump: every score re-runs once under the
# corrected contract.
_SCORING_INPUT_VERSION = "17"

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


def effective_description(job: dict) -> str:
    """The job description to score/display/reformat: a manual paste-in override
    (description_actual) wins over the feed's job_description.

    The override exists to repair a description the feed delivered wrong or partial — most
    often a career-site teaser (see ingest.feed_description_truncated) — so once set it is
    the authoritative text everywhere the description is consumed. A blank/whitespace-only
    override is treated as absent so it never blanks out a real feed description. Pure, so
    it's unit-testable without the DB."""
    override = (job.get("description_actual") or "").strip()
    return override if override else (job.get("job_description") or "")


def has_description_override(job: dict) -> bool:
    """True when a non-blank manual description override (description_actual) is set."""
    return bool((job.get("description_actual") or "").strip())


def description_is_truncated(job: dict) -> bool:
    """Whether the EFFECTIVE description is a partial career-site teaser: the feed's
    description_truncated flag is set AND no manual full-text override supersedes it.

    A pasted-in override is the full posting, so it clears the practical truncation regardless
    of the (feed-derived) flag — which is why the auto-skip exemption and the UI badge both key
    off this, not the raw column. Pure, so it's unit-testable without the DB."""
    return bool(job.get("description_truncated")) and not has_description_override(job)


# Currency code → the glyph shown immediately before a salary figure. Only the currencies a
# feed realistically reports get a dedicated symbol; anything else falls through to a "CODE "
# prefix (e.g. "CHF 120,000") so an unusual currency is shown honestly rather than silently
# mislabeled as dollars. CAD/AUD keep a disambiguating letter so they don't read as USD.
CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "AUD": "A$",
    "NZD": "NZ$", "CHF": "CHF ", "JPY": "¥", "INR": "₹",
    "SEK": "kr ", "NOK": "kr ", "DKK": "kr ", "PLN": "zł ", "SGD": "S$",
}


def currency_symbol(code: object) -> str:
    """The prefix to place before a salary amount for a currency code.

    A known code maps to its glyph ("EUR" → "€"); an unrecognized-but-present code falls back
    to itself as a spaced prefix ("SEK 120,000") so nothing is silently rendered as dollars; a
    missing/blank code defaults to "$" — historically every salary was shown in dollars, and a
    feed that omits the currency is overwhelmingly a US posting, so this preserves prior output.
    """
    c = str(code or "").strip().upper()
    if not c:
        return "$"
    return CURRENCY_SYMBOLS.get(c, f"{c} ")


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
    # bounds token cost per job. (Reformatting, by contrast, sends the full text.) A manual
    # paste-in override wins over the feed text (effective_description) — the whole point of the
    # override is to score against the real, complete posting.
    description = effective_description(job).strip()[:4000]
    # Manual salary override (salary_*_actual) wins over the feed value. The override is an
    # all-or-nothing pair: if either bound is set, use the override pair (a blank bound
    # stays open-ended) rather than mixing an overridden bound with a feed bound.
    if job.get("salary_min_actual") is not None or job.get("salary_max_actual") is not None:
        sal_min, sal_max = job.get("salary_min_actual"), job.get("salary_max_actual")
    else:
        sal_min, sal_max = job.get("salary_min"), job.get("salary_max")
    # Show the salary in its real currency (a manual currency override wins over the feed): a
    # €200k or £200k band must not be presented to the scorer as "$200,000", or it comp-judges a
    # foreign-currency role as if it were dollars. cur is a bare prefix ("$", "€", "CHF ").
    cur = currency_symbol(job.get("salary_currency_actual") or job.get("salary_currency"))
    if sal_min and sal_max:
        salary_line = f"Salary: {cur}{sal_min:,} – {cur}{sal_max:,}"
    elif sal_min:
        salary_line = f"Salary: {cur}{sal_min:,}+"
    elif sal_max:
        salary_line = f"Salary: up to {cur}{sal_max:,}"
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
    description = effective_description(job).strip()[:_MAX_GEO_DESC_CHARS] if include_description else ""
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


def parse_factors(value: object) -> "list[dict] | None":
    """Normalize the model's self-reported factor breakdown into a clean list, or None.

    The breakdown is supplementary transparency (see _SYSTEM_BOILERPLATE): each entry states how
    much one dimension moved the rating, so a reader can tell a factor merely mentioned (score 0)
    from one silently held against the job (negative). We accept it defensively — a malformed
    breakdown must never sink an otherwise-valid rating — keeping only well-formed entries:
    a non-empty ``dimension`` and a numeric ``score`` (bool rejected — Python's bool-is-int would
    otherwise turn true into 1.0; numeric strings like "+2" are coerced); ``note`` is optional and
    defaults to "". Order (and any extra model-chosen dimensions beyond FACTOR_DIMENSIONS) is
    preserved so we can surface and later enumerate them. Scores are stored VERBATIM, never clamped
    to [-2, 2] — an off-scale value is exactly the kind of prompt disobedience this feature exists
    to make visible. Returns None for a non-list, or when nothing valid survives, so the caller
    stores SQL NULL rather than an empty array. Pure, so it's unit-testable without an API call."""
    if not isinstance(value, list):
        return None
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension", "")).strip().lower()
        if not dimension:
            continue
        raw_score = item.get("score")
        if isinstance(raw_score, bool):
            continue  # bool is an int subclass in Python — don't read True/False as 1.0/0.0
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue  # None, non-numeric string, etc. — drop this entry
        note = str(item.get("note", "")).strip()
        out.append({"dimension": dimension, "score": score, "note": note})
    return out or None


def score_job(
    client,
    viability_prompt: str,
    job: dict,
    model: str = "claude-haiku-4-5",
    geo_note: str | None = None,
    effort: str = DEFAULT_EFFORT,
) -> "tuple[str, str, list[dict] | None, object] | tuple[None, None, None, None]":
    """Score a job posting for viability against the candidate description.

    Returns (rating, reason, factors, usage): rating is 'low'/'medium'/'high', reason is a
    one-sentence justification, factors is the parsed self-reported factor breakdown (a list of
    {dimension, score, note} dicts, or None when the model omitted/mangled it), and usage is the
    Anthropic token-usage object (for cost tallying). Returns (None, None, None, None) on any
    failure — unparseable response, invalid rating, or API error — so the caller can skip the job
    and move on. Note that factors are supplementary: a valid rating+reason with a missing/bad
    breakdown still succeeds (factors=None), since the breakdown must never sink the core score.

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
            return None, None, None, None  # 4-tuple: caller unpacks (rating, reason, factors, usage)
        data = json.loads(m.group())
        rating = str(data.get("rating", "")).lower().strip()
        reason = str(data.get("reason", "")).strip()
        # Reject anything that isn't a recognized rating with a non-empty reason.
        if rating not in VIABILITY_RATINGS or not reason:
            return None, None, None, None
        # Factors are supplementary transparency — a bad/absent breakdown yields None but never
        # invalidates an otherwise-good rating (see parse_factors).
        factors = parse_factors(data.get("factors"))
        return rating, reason, factors, message.usage
    except Exception:
        # Any failure (API error, malformed JSON, missing content) → skip this job.
        return None, None, None, None
