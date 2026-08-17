"""Model/key resolution in ai_config — especially resolve_geo_model, which escalates the
location sub-call to a capable model when it reads job descriptions — plus the pricing table."""

from ai_config import (DEFAULT_EFFORT, DEFAULT_MODEL, MODEL_PRICING, effective_effort,
                       estimate_cost, is_reasoning_model, resolve_effort, resolve_geo_effort,
                       resolve_geo_model)


# ── effort resolution (parallels the model resolution) ────────────────────────
def test_is_reasoning_model():
    for m in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-6", "claude-fable-5",
              "claude-sonnet-4-6"):
        assert is_reasoning_model(m), m
    for m in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-5", "", None):
        assert not is_reasoning_model(m), m


def test_resolve_effort_precedence_and_explicit_flag():
    # section wins over [ai] wins over the built-in default; the bool reports "came from config".
    assert resolve_effort({"ai": {"effort": "high"}, "viability": {"effort": "max"}},
                          "viability") == ("max", True)
    assert resolve_effort({"ai": {"effort": "high"}}, "viability") == ("high", True)
    assert resolve_effort({}, "viability") == (DEFAULT_EFFORT, False)


def test_resolve_geo_effort_mirrors_geo_model():
    # explicit location_effort always wins
    cfg = {"viability": {"location_effort": "xhigh", "effort": "high"}, "ai": {"effort": "low"}}
    assert resolve_geo_effort(cfg, True) == ("xhigh", True)
    # else inherits the viability effort when the sub-call reads descriptions, else [ai].effort
    cfg2 = {"viability": {"effort": "high"}, "ai": {"effort": "low"}}
    assert resolve_geo_effort(cfg2, True) == ("high", True)
    assert resolve_geo_effort(cfg2, False) == ("low", True)
    assert resolve_geo_effort({}, True) == (DEFAULT_EFFORT, False)


def test_effective_effort_is_none_on_non_reasoning_model():
    assert effective_effort("claude-sonnet-5", "high") == "high"
    assert effective_effort("claude-haiku-4-5", "high") is None


def test_warn_effort_ignored_only_when_explicit_and_non_reasoning(capsys):
    from ai_config import warn_effort_ignored
    warn_effort_ignored("viability", "claude-haiku-4-5", "high", True)   # explicit + non-reasoning
    assert "ignored" in capsys.readouterr().err
    warn_effort_ignored("viability", "claude-haiku-4-5", "high", False)  # just the default → silent
    warn_effort_ignored("viability", "claude-sonnet-5", "high", True)    # applies → silent
    assert capsys.readouterr().err == ""


# ── Pricing table ─────────────────────────────────────────────────────────────
def test_current_models_are_priced():
    """Every model the app might be configured to use must be in MODEL_PRICING, or
    estimate_cost returns None and the cost line silently disappears. Opus 5 in particular
    was missing — the table jumped from Fable 5 straight to Opus 4.8."""
    for model in ("claude-fable-5", "claude-opus-5", "claude-opus-4-8",
                  "claude-sonnet-5", "claude-haiku-4-5"):
        assert model in MODEL_PRICING, model


def test_opus_5_priced_at_5_and_25_per_million():
    """Opus 5 is $5 / $25 per MTok (same as Opus 4.8)."""
    assert estimate_cost("claude-opus-5", input=1_000_000) == 5.00
    assert estimate_cost("claude-opus-5", output=1_000_000) == 25.00


def test_sonnet_5_priced_at_the_permanent_2_and_10():
    """The launch 'introductory' $2/$10 is now the standard price (the $3/$15 increase was
    cancelled), so the table must not have been bumped."""
    assert estimate_cost("claude-sonnet-5", input=1_000_000) == 2.00
    assert estimate_cost("claude-sonnet-5", output=1_000_000) == 10.00


def test_unpriced_model_returns_none():
    """An unknown model yields None (not 0) so the caller can omit the cost line rather than
    print a misleading $0.0000."""
    assert estimate_cost("some-unknown-model", input=1_000_000) is None


def test_explicit_location_model_always_wins():
    """An explicit [viability].location_model overrides both defaults, either toggle state."""
    cfg = {"ai": {"model": "claude-haiku-4-5"},
           "viability": {"model": "claude-sonnet-5", "location_model": "claude-opus-4-8"}}
    assert resolve_geo_model(cfg, True) == "claude-opus-4-8"
    assert resolve_geo_model(cfg, False) == "claude-opus-4-8"


def test_escalates_to_viability_model_when_reading_description():
    """With the description on and no explicit override, use the (stronger) viability model —
    the cheap ai.model false-POORs remote jobs on noisy descriptions."""
    cfg = {"ai": {"model": "claude-haiku-4-5"}, "viability": {"model": "claude-sonnet-5"}}
    assert resolve_geo_model(cfg, True) == "claude-sonnet-5"


def test_uses_cheap_ai_model_when_not_reading_description():
    """Without the description the sub-call is a trivial match, so the cheap ai.model stands
    even though the viability model is pricier."""
    cfg = {"ai": {"model": "claude-haiku-4-5"}, "viability": {"model": "claude-sonnet-5"}}
    assert resolve_geo_model(cfg, False) == "claude-haiku-4-5"


def test_escalation_falls_back_to_ai_model_then_default():
    """If the description is read but no viability model is configured, escalation resolves to
    ai.model, then the built-in default — never to nothing."""
    assert resolve_geo_model({"ai": {"model": "claude-haiku-4-5"}}, True) == "claude-haiku-4-5"
    assert resolve_geo_model({}, True) == DEFAULT_MODEL


def test_default_when_nothing_configured():
    assert resolve_geo_model({}, False) == DEFAULT_MODEL
