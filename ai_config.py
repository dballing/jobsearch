#!/usr/bin/env python3
"""Shared AI configuration + cost accounting for the AI-backed features.

Both viability scoring (rescore_viability.py) and description reformatting
(ingest.py) read their engine settings from a shared ``[ai]`` config stanza and
report token usage / cost the same way. This module is the single source of truth
for both so the two features stay consistent.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

# Fallback model when neither the feature section nor [ai] specifies one. Haiku is the
# cheapest current model — a sane default for high-volume, low-complexity AI calls
# (description reformatting, viability scoring) where cost matters more than peak quality.
DEFAULT_MODEL = "claude-haiku-4-5"

# Fallback thinking effort for a reasoning model with no explicit setting. 'medium' balances
# instruction-following against cost for these high-volume calls. Only ever applied when the
# resolved model is a reasoning model (see is_reasoning_model); a non-reasoning model ignores it.
DEFAULT_EFFORT = "medium"

# Claude 4.6+/5 models that run with adaptive thinking, so a configured `effort` takes effect
# (reasoning stays in a hidden thinking block; effort controls its depth/cost). Haiku 4.5 and
# older don't support adaptive thinking, so effort is meaningless there and is thrown away.
# Matched by prefix so dated snapshots (e.g. a future 'claude-sonnet-5-YYYYMMDD') still count.
_REASONING_MODELS = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
)


def is_reasoning_model(model: str) -> bool:
    """True when `model` runs with adaptive thinking, so a configured `effort` actually applies
    (and, for reformat, `temperature` must be dropped). False for Haiku/older, where effort is
    ignored. Pure, so it's unit-testable and shared by every AI call site."""
    m = (model or "").lower()
    return any(m.startswith(p) for p in _REASONING_MODELS)

# ── Model pricing: shipped data file + optional local override ────────────────
# Pricing lives in JSON rather than in this module so a new model can be priced without a code
# change or a release — the whole point of the override file. JSON (not TOML) because .gitignore
# carries a blanket `*.toml`, the rule that keeps the secret-bearing config.toml and every
# searches/*.toml out of the repo; the shipped table has to be *tracked*, and punching a negation
# in that glob to allow one filename would weaken the protection for all of them.
_SHIPPED_PRICING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_pricing.json")

# The local override is resolved next to the shipped file, or from JOBSEARCH_MODEL_PRICING —
# the same env-indirection the suite already uses for JOBSEARCH_CONFIG/JOBSEARCH_DB, and what
# keeps tests hermetic: without it, a real override sitting in the repo root would silently
# change what every pricing test sees.
LOCAL_PRICING_ENV = "JOBSEARCH_MODEL_PRICING"
_DEFAULT_LOCAL_PRICING = os.path.join(os.path.dirname(_SHIPPED_PRICING), "model_pricing.local.json")

# Defaults for the derived cache rates when a model names neither (see model_pricing.json).
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT  = 0.10


class PricingError(Exception):
    """The pricing data is unreadable or malformed.

    Fatal rather than fail-soft, in both files. Falling back to "no pricing" would send every
    call to a $0 ledger row — the exact silent-mispricing failure the unpriced-model gate below
    exists to prevent — and falling back to the shipped table when a *local* file is broken
    would quietly bill at rates the user has explicitly overridden.
    """


def local_pricing_path() -> str:
    """Path to the optional local override file (env wins; else beside the shipped one)."""
    return os.environ.get(LOCAL_PRICING_ENV) or _DEFAULT_LOCAL_PRICING


def _rates(entry: "dict", model: str, source: str) -> "dict[str, float]":
    """One pricing period's JSON entry → the four per-token rates.

    Only input/output are stored; the cache rates are derived by multiplier, because that
    mirrors how the pricing page presents them. The 0.1x cache-read default is standard but not
    universal (Fable/Mythos 5.1 bill 0.025x, per a footnote), so a period may override either
    multiplier — deriving blindly priced those two 4x high on every cached call.
    """
    try:
        input_per_m  = float(entry["input"])
        output_per_m = float(entry["output"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PricingError(
            f"{source}: model {model!r} needs numeric 'input' and 'output' rates "
            f"(USD per million tokens); got {entry!r}"
        ) from exc
    write_mult = float(entry.get("cache_write_mult", _CACHE_WRITE_MULT))
    read_mult  = float(entry.get("cache_read_mult",  _CACHE_READ_MULT))
    return {
        "input":       input_per_m / 1_000_000,
        "output":      output_per_m / 1_000_000,
        "cache_write": input_per_m * write_mult / 1_000_000,
        "cache_read":  input_per_m * read_mult  / 1_000_000,
    }


# Pricing is temporal: a model's rate is a sequence of half-open [start, until) periods, not a
# single number. Modelling it as "the current price" was lossy — it made a historical ledger row
# unrepriceable, because nothing recorded what the rate was when the row was written. Half-open
# so two adjacent periods can share a boundary date without overlapping.
_EPOCH    = "0000-01-01T00:00:00"
_FOREVER  = "9999-12-31T23:59:59"


def _parse_when(value, model: str, field: str, source: str) -> str:
    """Normalize a date/timestamp to a sortable 'YYYY-MM-DDTHH:MM:SS' string.

    Compared as strings rather than datetimes because the ledger's ``ts`` is SQLite's UTC
    'YYYY-MM-DD HH:MM:SS' — already lexicographically ordered — so normalizing both to the same
    shape avoids a parse on every one of thousands of rows during a reprice.
    """
    if not isinstance(value, str):
        raise PricingError(f"{source}: model {model!r} has a non-string {field}: {value!r}")
    v = value.strip().replace(" ", "T").rstrip("Z")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v + "T00:00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v):
        return v
    raise PricingError(
        f"{source}: model {model!r} has an unparseable {field} {value!r} — "
        f"use 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS' (UTC)."
    )


def _periods(raw, model: str, source: str) -> "list[dict]":
    """One model's JSON value → its validated, start-sorted list of pricing periods.

    A bare object is shorthand for a single epoch→forever period, so the overwhelmingly common
    "this model has one price" case doesn't pay for the temporal machinery.

    Overlaps and interior gaps are both fatal. An overlap makes the rate at an instant
    ambiguous; a gap makes it unknown — and an unknown past rate is the same failure as an
    unknown present one, which we already refuse to run on. Silently picking a neighbouring
    period would invent a number, which is the one thing worse than refusing.
    """
    entries = raw if isinstance(raw, list) else [raw]
    if not entries:
        raise PricingError(f"{source}: model {model!r} has an empty pricing list.")
    periods = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PricingError(f"{source}: model {model!r} has a non-object period: {entry!r}")
        start = _parse_when(entry["effective_start"], model, "effective_start", source) \
            if entry.get("effective_start") else _EPOCH
        until = _parse_when(entry["effective_until"], model, "effective_until", source) \
            if entry.get("effective_until") else _FOREVER
        if until <= start:
            raise PricingError(
                f"{source}: model {model!r} has a period ending at or before it starts "
                f"({start} → {until}).")
        periods.append({"start": start, "until": until, "rates": _rates(entry, model, source),
                        "raw": entry})
    periods.sort(key=lambda p: p["start"])
    for prev, nxt in zip(periods, periods[1:]):
        if nxt["start"] < prev["until"]:
            raise PricingError(
                f"{source}: model {model!r} has overlapping pricing periods "
                f"({prev['start']}→{prev['until']} and {nxt['start']}→{nxt['until']}). "
                f"Periods are half-open [start, until), so an ending date may equal the next "
                f"start.")
        if nxt["start"] > prev["until"]:
            raise PricingError(
                f"{source}: model {model!r} has a gap in its pricing history "
                f"({prev['until']} → {nxt['start']}) — nothing prices a call made in that "
                f"window. Add a period covering it (consult the published pricing table for "
                f"the historical rate) or extend an adjacent one.")
    return periods


def _rates_at(periods: "list[dict]", when: str) -> "dict[str, float] | None":
    """The rates in effect at `when`, or None when no period covers it."""
    for p in periods:
        if p["start"] <= when < p["until"]:
            return p["rates"]
    return None


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_when(when: "str | None") -> str:
    """A caller-supplied instant (ledger ts or ISO string) normalized for comparison; None ⇒ now."""
    if when is None:
        return _now_stamp()
    return when.strip().replace(" ", "T").rstrip("Z")


def _read_pricing_file(path: str, *, required: bool) -> "dict[str, dict]":
    """Parse one pricing file into {model: raw entry}. Missing + optional ⇒ {}."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        if required:
            raise PricingError(f"{path}: shipped pricing table is missing.") from None
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PricingError(f"{path}: could not read pricing data — {exc}") from exc
    models = data.get("models")
    if not isinstance(models, dict):
        raise PricingError(f"{path}: expected a top-level 'models' object mapping model id → rates.")
    return models


# Cache keyed by each file's (path, mtime, size) so an edit is picked up without a restart —
# app.py hot-reloads config, and pricing that needed a bounce would be a trap. Batch entry
# points are short-lived processes, so for them this just means "read once".
_pricing_cache: "tuple | None" = None


def _pricing_stamp() -> tuple:
    def stat(path):
        try:
            st = os.stat(path)
            return (path, st.st_mtime_ns, st.st_size)
        except OSError:
            return (path, None, None)
    return (stat(_SHIPPED_PRICING), stat(local_pricing_path()))


def load_pricing() -> "tuple[dict[str, list], dict[str, dict]]":
    """Return (merged timelines, override metadata), reloading when either file changes.

    Merge is per *model*, not per period or per rate: a local entry replaces the shipped model's
    ENTIRE timeline. Splicing the two would produce a history no one authored — our periods
    interleaved with theirs — and the resulting hybrid would be silently wrong with nothing to
    catch it. The consequence is that overriding a model means owning its whole price history;
    see docs/configuration.md, and `override_coverage_warnings()` below, which detects the case
    where an override silently drops history the shipped table used to cover.
    """
    global _pricing_cache
    stamp = _pricing_stamp()
    if _pricing_cache is not None and _pricing_cache[0] == stamp:
        return _pricing_cache[1], _pricing_cache[2]

    shipped_raw = _read_pricing_file(_SHIPPED_PRICING, required=True)
    local_raw   = _read_pricing_file(local_pricing_path(), required=False)

    base: dict[str, list] = {}
    for model, entry in shipped_raw.items():
        base[model.lower()] = _periods(entry, model, _SHIPPED_PRICING)
    merged = dict(base)
    overrides: dict[str, dict] = {}
    for model, entry in local_raw.items():
        key = model.lower()
        merged[key] = _periods(entry, model, local_pricing_path())
        overrides[key] = {"periods": merged[key], "_shipped": key in base}

    _pricing_cache = (stamp, merged, overrides, base)
    return merged, overrides


def model_pricing(at: "str | None" = None) -> "dict[str, dict[str, float]]":
    """The merged table as of `at` (default now): {model: {input, output, cache_write, cache_read}}.

    Models with no period covering `at` are omitted — "unpriced at that instant" and "unknown
    model" are the same thing to every caller. Call this rather than binding a dict at import
    time; it reloads on a file edit.
    """
    when = _normalize_when(at)
    out = {}
    for model, periods in load_pricing()[0].items():
        rates = _rates_at(periods, when)
        if rates is not None:
            out[model] = rates
    return out


def pricing_overrides() -> "dict[str, dict]":
    """{model: {periods, _shipped}} for every model the local file overrides or adds.

    ``_shipped`` is True when it *replaces* a shipped timeline (the risky case worth disclosing
    loudly), False when it merely adds a model we don't ship yet.
    """
    return load_pricing()[1]


def shipped_pricing(at: "str | None" = None) -> "dict[str, dict[str, float]]":
    """The shipped table alone as of `at`, ignoring any local override.

    The drift check validates THIS against Anthropic's published page — that comparison has to
    be about our data, not the user's, or a deliberate local override would fail the suite.
    """
    load_pricing()
    when = _normalize_when(at)
    out = {}
    for model, periods in _pricing_cache[3].items():
        rates = _rates_at(periods, when)
        if rates is not None:
            out[model] = rates
    return out


def override_coverage_warnings() -> "list[str]":
    """Warn where an override's timeline covers less history than the shipped one it replaced.

    Not an error — declaring only "from today" is legitimate for a model whose history you
    genuinely don't know. But it silently makes older ledger rows unrepriceable, and the reason
    (a whole-timeline replacement) isn't obvious from looking at a two-line override file. This
    turns a documentation-dependent trap into something the tooling actually says out loud.
    """
    out = []
    shipped_all = load_pricing()
    shipped = _pricing_cache[3]
    for model, meta in sorted(pricing_overrides().items()):
        if not meta["_shipped"]:
            continue
        was = shipped[model][0]["start"]
        now = meta["periods"][0]["start"]
        if now > was:
            out.append(
                f"{model}: your override starts at {now[:10]}, but the shipped table priced it "
                f"from {'the epoch' if was == _EPOCH else was[:10]}. An override replaces the "
                f"whole timeline, so ledger rows before {now[:10]} can no longer be repriced — "
                f"add an earlier period if you need that history.")
    return out


def describe_pricing_overrides() -> "list[str]":
    """One disclosure line per locally-priced model, for batch startup output (empty if none).

    Always produced, even for a ``suppress_drift_warning`` entry: that switch turns off the
    comparison nag, not the fact that the ledger is being priced off something other than the
    shipped table. Silencing that too is how a forgotten override quietly distorts months of
    cost figures.
    """
    lines = []
    when = _now_stamp()
    for model, meta in sorted(pricing_overrides().items()):
        what = "replaces the shipped price" if meta["_shipped"] else "not in the shipped table"
        current = _current_period(meta["periods"], when)
        if current is None:
            # An override whose timeline has run out (or hasn't started). Still disclosed — the
            # startup gate will refuse it if it's configured, but a *past* period of it may
            # already have priced live ledger rows, so staying silent would hide that.
            lines.append(f"{model}: {len(meta['periods'])} pricing period(s) from "
                         f"{os.path.basename(local_pricing_path())}, none covering now ({what})")
            continue
        rates, raw = current["rates"], current["raw"]
        span = "" if len(meta["periods"]) == 1 else f", period from {current['start'][:10]}"
        line = (f"{model}: ${rates['input'] * 1_000_000:g}/${rates['output'] * 1_000_000:g} "
                f"per MTok from {os.path.basename(local_pricing_path())} ({what}{span})")
        if raw.get("note"):
            line += f" — {raw['note']}"
        if raw.get("suppress_drift_warning"):
            line += " [drift warnings silenced]"
        lines.append(line)
    return lines


def _current_period(periods: "list[dict]", when: str) -> "dict | None":
    """The period covering `when`, or None."""
    for p in periods:
        if p["start"] <= when < p["until"]:
            return p
    return None


def override_is_silenced(model: str) -> bool:
    """True when a local entry opts out of the drift warnings via ``suppress_drift_warning``.

    Deliberately narrow: it silences the *nag* (redundant / disagrees-with-published), never the
    "prices are overridden locally" disclosure. A negotiated rate is a legitimate reason to stop
    comparing against the public page; it is never a reason to hide that the ledger is being
    priced off something other than the shipped table.
    """
    meta = pricing_overrides().get((model or "").lower())
    if not meta:
        return False
    # Read off the currently-effective period: the drift check compares against today's published
    # price, so today's period is the one whose opt-out is relevant.
    current = _current_period(meta["periods"], _now_stamp())
    return bool(current and current["raw"].get("suppress_drift_warning"))


# A dated snapshot ID is a base model plus an 8-digit date (claude-sonnet-5-20260101). That
# suffix shape is the ONLY thing distinguishing a pin of the same model from a *point release*
# (claude-sonnet-5-5), which is a genuinely different model that may bill differently — Opus 5.5
# undercuts Opus 5 by 20%. Plain prefix matching conflates the two and would silently price a
# point release at its predecessor's rates, which is exactly the error this check exists to stop.
_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


def base_model_id(model: str) -> str:
    """`model` lowercased with a dated-snapshot suffix stripped (unchanged if it has none)."""
    return _SNAPSHOT_SUFFIX.sub("", (model or "").lower())


def pricing_for(model: str, at: "str | None" = None) -> "dict[str, float] | None":
    """The rates for `model` in effect at `at` (default now), or None when nothing prices it then.

    A dated snapshot inherits its base model's timeline — it IS that model, pinned. Everything
    else must be priced explicitly, so a new point release surfaces as unpriced rather than
    quietly inheriting the older sibling's numbers.
    """
    table = model_pricing(at)
    m = (model or "").lower()
    return table.get(m) or table.get(base_model_id(m))


def resolve_ai_settings(config: dict, section: str) -> tuple[str | None, str]:
    """Return (api_key, model) for an AI feature section.

    Precedence (so a feature can override the shared defaults):
        [<section>] -> [ai] -> built-in default / ANTHROPIC_API_KEY env.

    This is backward compatible with the older layout where api_key/model lived
    directly under [viability]: that section-level value still wins as an override.
    """
    sect = config.get(section, {}) or {}
    ai   = config.get("ai", {}) or {}
    model   = sect.get("model")   or ai.get("model")   or DEFAULT_MODEL
    api_key = sect.get("api_key") or ai.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    return api_key, model


def resolve_effort(config: dict, section: str) -> tuple[str, bool]:
    """Return (effort, explicitly_set) for an AI feature section — the effort sibling of
    resolve_ai_settings, with the same precedence:

        [<section>].effort -> [ai].effort -> DEFAULT_EFFORT.

    ``explicitly_set`` is True when the value came from config (either level) rather than the
    built-in default, so a caller can warn that an explicit effort is being ignored on a
    non-reasoning model. The effort is validated at the call site, not here (Anthropic rejects a
    bad value)."""
    sect = config.get(section, {}) or {}
    ai   = config.get("ai", {}) or {}
    if "effort" in sect:
        return sect["effort"], True
    if "effort" in ai:
        return ai["effort"], True
    return DEFAULT_EFFORT, False


def resolve_geo_effort(config: dict, geo_uses_description: bool) -> tuple[str, bool]:
    """Return (effort, explicitly_set) for the location sub-call — the effort sibling of
    resolve_geo_model, mirroring its precedence: an explicit ``[viability].location_effort``
    wins; otherwise it inherits the *viability* effort when the sub-call reads the description
    (it escalates to the viability model there), else the ``[ai]`` effort."""
    viability = config.get("viability", {}) or {}
    if "location_effort" in viability:
        return viability["location_effort"], True
    if geo_uses_description:
        return resolve_effort(config, "viability")
    return resolve_effort(config, "ai")


def effective_effort(model: str, effort: str) -> str | None:
    """The effort a call will actually use: the resolved effort on a reasoning model, else None
    (a non-reasoning model ignores it). Folding THIS into a scoring hash means a config effort
    change re-scores only when it truly changes behavior — not on a Haiku config."""
    return effort if is_reasoning_model(model) else None


def warn_effort_ignored(label: str, model: str, effort: str, explicit: bool) -> None:
    """Print a one-line stderr notice when an EXPLICITLY configured effort is being thrown away
    because the resolved model isn't a reasoning model. Silent when the effort is just the
    default, or when it actually applies. Called once at a batch driver's startup."""
    if explicit and not is_reasoning_model(model):
        print(f"NOTE: {label} effort={effort!r} is ignored — {model} is not a reasoning model "
              "(effort only applies to Claude 4.6+/5 models with adaptive thinking).",
              file=sys.stderr)


def resolve_geo_model(config: dict, geo_uses_description: bool) -> str:
    """Return the model for the focused location sub-call (viability.assess_location_fit).

    An explicit ``[viability].location_model`` always wins. Otherwise the default depends on
    whether that sub-call reads the job description (the ``location_use_description`` toggle):

    - With the description, the call must tell an EXPLICIT eligibility restriction ("remote only
      for residents of X") from incidental office/regional/pay-zone prose — real reading
      comprehension. The cheap ``[ai].model`` (haiku) gets this wrong, false-POORing fully-remote
      roles whose descriptions merely name other regions, which then gets clamped to low. So we
      escalate to the *viability scoring* model (the stronger model the user already trusts for
      the main judgment, typically sonnet).
    - Without the description, the call is the trivial location-vs-preferences match it was
      designed as, so the cheap ``[ai].model`` default is right.

    Kept here (not inlined at the call sites) so the batch rescore and the on-demand single-job
    rescore resolve it identically, and so it's unit-testable without an API call.
    """
    location_model = (config.get("viability", {}) or {}).get("location_model")
    if location_model:
        return location_model
    if geo_uses_description:
        return resolve_ai_settings(config, "viability")[1]
    return (config.get("ai", {}) or {}).get("model") or DEFAULT_MODEL


class UnpricedModelError(Exception):
    """A configured model has no pricing entry, so its calls would ledger at $0.

    Fatal rather than a warning because the spend ledger is the only record of what a lens
    costs, rows are priced at call time, and an unpriced model's $0 rows are indistinguishable
    from free ones afterwards — by the time anyone notices, the real cost is unrecoverable.
    """


def configured_models(config: dict) -> "dict[str, list[str]]":
    """Every model one search's config could send a billed call to → the config keys selecting it.

    The location sub-call appears under both of its resolutions because which one applies is a
    per-job property (it escalates to the viability model when the call reads the description),
    so both are reachable from a single config and both must be priced.
    """
    surfaces = [
        (resolve_ai_settings(config, "viability")[1],   "[viability].model"),
        (resolve_ai_settings(config, "descriptions")[1], "[descriptions].model"),
        (resolve_geo_model(config, True),  "[viability].location_model (reading the description)"),
        (resolve_geo_model(config, False), "[viability].location_model"),
    ]
    out: dict[str, list[str]] = {}
    for model, where in surfaces:
        out.setdefault(model, [])
        if where not in out[model]:
            out[model].append(where)
    return out


def unpriced_models(config: dict) -> "dict[str, list[str]]":
    """The subset of configured_models() with no pricing entry. Empty dict ⇒ all priced."""
    return {m: where for m, where in configured_models(config).items() if pricing_for(m) is None}


def require_priced_models(items: "list[tuple[str, dict]]") -> None:
    """Raise UnpricedModelError if any (search_id, config) pair selects an unpriced model.

    Takes the pairs rather than an AppConfig so this stays a leaf module testable with plain
    dicts. Entry points pass ``[(s.id, s.config) for s in app_cfg.searches]``.
    """
    problems: list[str] = []
    for search_id, config in items:
        for model, where in sorted(unpriced_models(config).items()):
            problems.append(f"  {model!r} — selected by {', '.join(where)} in search {search_id!r}")
    if not problems:
        return
    raise UnpricedModelError(
        "no pricing is configured for these models, so their calls would be recorded as $0 "
        "in the spend ledger:\n" + "\n".join(problems)
        + "\n\nFix by either:\n"
          "  - switching the config to a model listed in model_pricing.json, or\n"
          f"  - adding it to {local_pricing_path()} with its published rates, e.g.\n"
          '      {"models": {"<model-id>": {"input": 2.00, "output": 10.00}}}\n'
          "    (rates are USD per million tokens; check the pricing page's footnotes for a\n"
          "     non-standard cache-read multiplier, e.g. \"cache_read_mult\": 0.025).\n"
          "A dated snapshot (e.g. claude-sonnet-5-20260101) inherits its base model's rates "
          "automatically; a point release (e.g. claude-sonnet-5-5) is a distinct model and "
          "needs its own entry."
    )


def describe_model_change(current: str, candidate: str) -> "list[str]":
    """Advisory lines comparing a configured model against a newer one, for the currency notice.

    Two facts a bare "a newer model exists" note leaves out, both decision-relevant:

    * **Price, in either direction.** Newer is often cheaper (Sonnet 4.6 → 5 drops a third) but
      not always, and "newer and dearer" is just as worth knowing before switching. Silent when
      either side is unpriced — guessing here would be the one thing worse than saying nothing.
    * **A change of thinking regime.** Moving from a non-reasoning model to an adaptive-thinking
      one adds billed reasoning tokens that simply didn't exist before, so a lower per-token rate
      can still mean a higher bill per job. This is the case where "cheaper" is most likely to be
      wrong, and it's invisible in a price table — hence the explicit warning and the pointer at
      the comparison harness.

    Deliberately phrased as information, not a recommendation: the model is never folded into the
    scoring hash (see viability.prompt_hash), so switching leaves every existing score in place
    and silently mixes two judges' ratings in one table. That's a real cost this function cannot
    weigh for the user.
    """
    out = []
    now, new = pricing_for(current), pricing_for(candidate)
    if now and new:
        pct = lambda a, b: (b - a) / a * 100 if a else 0.0
        d_in, d_out = pct(now["input"], new["input"]), pct(now["output"], new["output"])
        rates = (f"${new['input'] * 1_000_000:g}/${new['output'] * 1_000_000:g} vs "
                 f"${now['input'] * 1_000_000:g}/${now['output'] * 1_000_000:g} per MTok")
        if d_in < 0 and d_out < 0:
            out.append(f"It is also cheaper per token: {rates} "
                       f"({d_in:+.0f}% input, {d_out:+.0f}% output).")
        elif d_in > 0 or d_out > 0:
            out.append(f"Note it costs MORE per token: {rates} "
                       f"({d_in:+.0f}% input, {d_out:+.0f}% output).")
        else:
            out.append(f"Per-token rates are unchanged ({rates}).")
    if is_reasoning_model(candidate) and not is_reasoning_model(current):
        out.append(
            "It runs adaptive thinking and your current model does not, so it bills reasoning "
            "tokens your current model never produced — cheaper per token is NOT necessarily "
            "cheaper per job.")
    if out:
        out.append(f"Before switching, check the rating drift: "
                   f"./compare_scoring.sh --model {base_model_id(candidate)}")
    return out


def estimate_cost(model: str, *, input: int = 0, output: int = 0,
                  cache_write: int = 0, cache_read: int = 0,
                  at: "str | None" = None) -> float | None:
    """Estimated USD cost for a token tally, or None if the model is unpriced.

    The keyword-only param names (input/output/cache_write/cache_read) deliberately
    mirror the Anthropic ``usage`` fields so callers can pass tallies through directly.
    Returns None (rather than 0) for an unknown model so the caller can distinguish
    "no pricing data" from "genuinely free" and omit the cost line entirely.
    """
    pricing = pricing_for(model, at)
    if not pricing:
        return None
    return (
        input       * pricing["input"]
      + output      * pricing["output"]
      + cache_write * pricing["cache_write"]
      + cache_read  * pricing["cache_read"]
    )


def format_token_summary(model: str, *, input: int = 0, output: int = 0,
                         cache_write: int = 0, cache_read: int = 0) -> str:
    """Human-readable "N tokens total (...), estimated cost: $X" line.

    Returns "" when no tokens were spent. Callers prepend their own label.
    """
    total = input + output + cache_write + cache_read
    if not total:
        return ""
    detail = f"{input:,} input, {output:,} output"
    if cache_write or cache_read:
        detail += f", {cache_write:,} cache write, {cache_read:,} cache read"
    parts = [f"{total:,} tokens total ({detail})"]
    cost = estimate_cost(model, input=input, output=output,
                         cache_write=cache_write, cache_read=cache_read)
    if cost is not None:
        parts.append(f"estimated cost: ${cost:.4f}")
    return ", ".join(parts)
