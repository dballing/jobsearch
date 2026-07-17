#!/usr/bin/env python3
"""Shared AI configuration + cost accounting for the AI-backed features.

Both viability scoring (rescore_viability.py) and description reformatting
(ingest.py) read their engine settings from a shared ``[ai]`` config stanza and
report token usage / cost the same way. This module is the single source of truth
for both so the two features stay consistent.
"""

import os

# Fallback model when neither the feature section nor [ai] specifies one. Haiku is the
# cheapest current model — a sane default for high-volume, low-complexity AI calls
# (description reformatting, viability scoring) where cost matters more than peak quality.
DEFAULT_MODEL = "claude-haiku-4-5"

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
    "claude-opus-4-8":   _pricing(5.00, 25.00),
    # Sonnet 5 introductory pricing ($2/$10) runs through 2026-08-31; it then
    # reverts to the standard $3.00 / $15.00 — bump these two numbers after that date.
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
