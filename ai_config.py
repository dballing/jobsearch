#!/usr/bin/env python3
"""Shared AI configuration + cost accounting for the AI-backed features.

Both viability scoring (rescore_viability.py) and description reformatting
(ingest.py) read their engine settings from a shared ``[ai]`` config stanza and
report token usage / cost the same way. This module is the single source of truth
for both so the two features stay consistent.
"""

import os

DEFAULT_MODEL = "claude-haiku-4-5"

# Approximate pricing per token (USD). Update if Anthropic changes rates.
# Source: https://docs.anthropic.com/en/docs/about-claude/models/overview
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input":       1.00 / 1_000_000,
        "output":      5.00 / 1_000_000,
        "cache_write": 1.25 / 1_000_000,
        "cache_read":  0.10 / 1_000_000,
    },
    "claude-sonnet-4-5": {
        "input":       3.00 / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
        "cache_read":  0.30 / 1_000_000,
    },
    "claude-sonnet-4-6": {
        "input":       3.00 / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
        "cache_read":  0.30 / 1_000_000,
    },
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


def estimate_cost(model: str, *, input: int = 0, output: int = 0,
                  cache_write: int = 0, cache_read: int = 0) -> float | None:
    """Estimated USD cost for a token tally, or None if the model is unpriced."""
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
