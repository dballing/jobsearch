#!/usr/bin/env python3
"""Shared AI configuration + cost accounting for the AI-backed features.

Both viability scoring (rescore_viability.py) and description reformatting
(ingest.py) read their engine settings from a shared ``[ai]`` config stanza and
report token usage / cost the same way. This module is the single source of truth
for both so the two features stay consistent.
"""

import os
import sys

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

# Approximate pricing per token (USD). Update if Anthropic changes rates.
# Source: https://platform.claude.com/docs/en/about-claude/models/overview (2026-07-02).
# Per-model entries list only input/output $/1M; cache_write is 1.25x input (5-min TTL)
# and cache_read is 0.1x input, so they're derived rather than hand-typed.
def _pricing(input_per_m: float, output_per_m: float) -> dict[str, float]:
    return {
        "input":       input_per_m / 1_000_000,
        "output":      output_per_m / 1_000_000,
        "cache_write": input_per_m * 1.25 / 1_000_000,
        "cache_read":  input_per_m * 0.10 / 1_000_000,
    }


MODEL_PRICING: dict[str, dict[str, float]] = {
    # Current models (latest generation).
    "claude-fable-5":    _pricing(10.00, 50.00),
    "claude-mythos-5":   _pricing(10.00, 50.00),  # Project Glasswing only; same specs/price as Fable 5
    "claude-opus-5":     _pricing(5.00, 25.00),
    "claude-opus-4-8":   _pricing(5.00, 25.00),
    # Sonnet 5's launch "introductory" $2/$10 is now the permanent standard price:
    # Anthropic cancelled the increase to $3/$15 that had been scheduled for 2026-09-01.
    "claude-sonnet-5":   _pricing(2.00, 10.00),
    "claude-haiku-4-5":  _pricing(1.00, 5.00),
    # Legacy (still active).
    "claude-opus-4-7":   _pricing(5.00, 25.00),
    "claude-opus-4-6":   _pricing(5.00, 25.00),
    "claude-opus-4-5":   _pricing(5.00, 25.00),
    "claude-sonnet-4-6": _pricing(3.00, 15.00),
    "claude-sonnet-4-5": _pricing(3.00, 15.00),
}


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


def estimate_cost(model: str, *, input: int = 0, output: int = 0,
                  cache_write: int = 0, cache_read: int = 0) -> float | None:
    """Estimated USD cost for a token tally, or None if the model is unpriced.

    The keyword-only param names (input/output/cache_write/cache_read) deliberately
    mirror the Anthropic ``usage`` fields so callers can pass tallies through directly.
    Returns None (rather than 0) for an unknown model so the caller can distinguish
    "no pricing data" from "genuinely free" and omit the cost line entirely.
    """
    pricing = MODEL_PRICING.get(model)
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
