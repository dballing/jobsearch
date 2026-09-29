"""Model/key resolution in ai_config — especially resolve_geo_model, which escalates the
location sub-call to a capable model when it reads job descriptions — plus the pricing table."""

import pytest

from ai_config import (DEFAULT_EFFORT, DEFAULT_MODEL, UnpricedModelError, base_model_id,
                       configured_models, describe_model_change, effective_effort, estimate_cost,
                       is_reasoning_model,
                       model_pricing, pricing_for, require_priced_models, resolve_effort,
                       resolve_geo_effort, resolve_geo_model, unpriced_models)


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
    """Every model the app might be configured to use must be priced, or
    estimate_cost returns None and the cost line silently disappears. Opus 5 in particular
    was missing — the table jumped from Fable 5 straight to Opus 4.8."""
    for model in ("claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-opus-4-8",
                  "claude-sonnet-5", "claude-haiku-4-5"):
        assert model in model_pricing(), model


def test_fable_5_1_cache_hits_use_the_cheaper_multiplier():
    """Fable/Mythos 5.1 bill cache hits at 0.025x base input, not the 0.1x the rest of the lineup
    uses — a footnote on the pricing page rather than a column. Deriving the standard multiplier
    would overcharge every cached call 4x, which for a cache-heavy workload is most of the bill."""
    assert estimate_cost("claude-fable-5-1", cache_read=1_000_000) == 0.25
    assert estimate_cost("claude-mythos-5-1", cache_read=1_000_000) == 0.25
    # Base rates are unchanged from Fable 5, and the standard 0.1x still applies elsewhere.
    assert estimate_cost("claude-fable-5-1", input=1_000_000) == 10.00
    assert estimate_cost("claude-fable-5", cache_read=1_000_000) == 1.00


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


# ── unpriced-model gate ───────────────────────────────────────────────────────
# A configured model with no pricing entry bills at $0 in the spend ledger, and rows are
# priced at call time, so the real cost can't be reconstructed afterwards. The entry points
# refuse to run rather than spend silently; these cover the resolution rules behind that.
def test_base_model_id_strips_only_a_dated_suffix():
    assert base_model_id("claude-sonnet-5-20260101") == "claude-sonnet-5"
    assert base_model_id("claude-sonnet-5") == "claude-sonnet-5"
    assert base_model_id("CLAUDE-SONNET-5") == "claude-sonnet-5"   # normalized
    assert base_model_id("") == ""
    assert base_model_id(None) == ""
    # A point release is NOT a date — it must survive intact or it inherits the wrong rates.
    assert base_model_id("claude-sonnet-5-5") == "claude-sonnet-5-5"
    # Near-misses on the 8-digit shape stay untouched.
    assert base_model_id("claude-sonnet-5-2026010") == "claude-sonnet-5-2026010"
    assert base_model_id("claude-sonnet-5-202601011") == "claude-sonnet-5-202601011"


def test_pricing_for_dated_snapshot_inherits_its_base_model():
    """A pinned snapshot IS the base model, so it prices identically rather than as unknown."""
    assert pricing_for("claude-sonnet-5-20260101") == model_pricing()["claude-sonnet-5"]
    assert estimate_cost("claude-sonnet-5-20260101", input=1_000_000) == \
           estimate_cost("claude-sonnet-5", input=1_000_000)


def test_pricing_for_point_release_does_not_inherit_predecessor():
    """The whole point of the gate: a point release must not quietly bill at its predecessor's
    rates (Opus 5.5 is 20% cheaper than Opus 5), so an unpriced one resolves to None, not a
    guess. Asserted against a shipped model + a fictional successor, so shipping any real .5
    can't turn this guard into a false pass."""
    assert pricing_for("claude-sonnet-5") is not None          # the predecessor IS priced
    assert pricing_for("claude-sonnet-5-9") is None            # its point release is not
    assert estimate_cost("claude-sonnet-5-9", input=1_000_000) is None


def test_configured_models_covers_every_billable_surface():
    """All four model-bearing config keys are reported, including both location resolutions —
    which one applies is per-job, so both are reachable and both must be priced."""
    cfg = {"ai": {"model": "claude-haiku-4-5"},
           "viability": {"model": "claude-sonnet-5"},
           "descriptions": {"model": "claude-opus-5"}}
    found = configured_models(cfg)
    # viability model, descriptions model, and the geo sub-call's two resolutions
    # (escalated → the viability model; plain → [ai].model).
    assert set(found) == {"claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"}
    assert "[descriptions].model" in found["claude-opus-5"]
    assert any("location_model" in w for w in found["claude-haiku-4-5"])


def test_unpriced_models_flags_only_the_unpriced():
    priced = {"ai": {"model": "claude-haiku-4-5"}, "viability": {"model": "claude-sonnet-5"}}
    assert unpriced_models(priced) == {}
    broken = {"ai": {"model": "claude-haiku-4-5"}, "viability": {"model": "claude-fictional-9"}}
    assert list(unpriced_models(broken)) == ["claude-fictional-9"]


def test_require_priced_models_passes_when_all_priced():
    require_priced_models([("__default__", {"viability": {"model": "claude-sonnet-5"}})])


def test_require_priced_models_names_model_search_and_remediation():
    """The error has to be actionable on its own: which model, which lens, and both fixes."""
    with pytest.raises(UnpricedModelError) as exc:
        require_priced_models([
            ("midatl_tpm", {"viability": {"model": "claude-sonnet-5"}}),      # fine
            ("europe_tpm", {"viability": {"model": "claude-fictional-9"}}),  # unpriced
        ])
    msg = str(exc.value)
    assert "claude-fictional-9" in msg and "europe_tpm" in msg
    assert "[viability].model" in msg
    assert "model_pricing.local.json" in msg   # remediation: add the model
    assert "switching the config" in msg   # remediation: or use a priced one
    assert "midatl_tpm" not in msg         # the healthy lens isn't dragged into the error


def test_require_priced_models_accepts_a_dated_snapshot_pin():
    """Pinning a snapshot is legitimate config, not an error — it inherits the base rates."""
    require_priced_models([("s", {"viability": {"model": "claude-sonnet-5-20260101"}})])


# ── newer-model advisory (describe_model_change) ──────────────────────────────
# The currency notice used to say only "something newer exists". These two facts decide whether
# the upgrade is worth taking, and neither is visible in a bare model id.
def test_cheaper_successor_is_reported_with_the_delta():
    lines = " ".join(describe_model_change("claude-sonnet-4-6", "claude-sonnet-5"))
    assert "cheaper" in lines and "-33%" in lines
    assert "$2/$10 vs $3/$15" in lines


def test_more_expensive_successor_is_also_reported():
    """'Newer and dearer' is just as decision-relevant as the reverse — a one-directional hint
    would quietly push users toward upgrades that cost them more."""
    lines = " ".join(describe_model_change("claude-sonnet-5", "claude-sonnet-4-6"))
    assert "costs MORE" in lines and "+50%" in lines


def test_same_price_successor_says_so():
    lines = " ".join(describe_model_change("claude-opus-4-8", "claude-opus-5"))
    assert "unchanged" in lines


def test_crossing_into_adaptive_thinking_is_flagged():
    """The case where 'cheaper per token' is most likely to be wrong: the newer model bills
    reasoning tokens the old one never produced, which no price table can show."""
    lines = " ".join(describe_model_change("claude-sonnet-4-5", "claude-sonnet-5"))
    assert "adaptive thinking" in lines and "NOT necessarily cheaper per job" in lines
    # ...and it is NOT claimed when both sides already think.
    assert "adaptive thinking" not in " ".join(
        describe_model_change("claude-sonnet-4-6", "claude-sonnet-5"))


def test_opus_4_5_upgrade_is_a_pure_cost_increase():
    """Same rates AND newly-billed thinking tokens — the concrete answer to 'why would I ever
    stay on an older model'."""
    lines = " ".join(describe_model_change("claude-opus-4-5", "claude-opus-5"))
    assert "unchanged" in lines and "adaptive thinking" in lines


def test_unpriced_side_produces_no_price_claim():
    """Guessing a delta is worse than staying quiet, so an unpriced model yields no price line."""
    lines = " ".join(describe_model_change("claude-sonnet-5", "claude-fictional-9"))
    assert "cheaper" not in lines and "costs MORE" not in lines and "unchanged" not in lines


def test_advice_points_at_the_comparison_harness_by_base_id():
    """The suggested command must name a model you'd actually put in config — not the dated
    build the models API hands back."""
    lines = describe_model_change("claude-sonnet-4-6", "claude-sonnet-5-5-20260929")
    assert lines[-1].endswith("./compare_scoring.sh --model claude-sonnet-5-5")
