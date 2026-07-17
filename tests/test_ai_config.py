"""Model/key resolution in ai_config — especially resolve_geo_model, which escalates the
location sub-call to a capable model when it reads job descriptions."""

from ai_config import DEFAULT_MODEL, resolve_geo_model


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
