"""The file-backed pricing table: shipped model_pricing.json plus an optional local override.

Pricing moved out of ai_config's source so a model can be priced without a code change — the
point being that a user can add a just-announced model themselves. These cover the merge rules,
the failure modes (loud, never fail-soft to $0), and the override disclosure/suppression split.

Every test points JOBSEARCH_MODEL_PRICING at a tmp file and resets the module cache, so a real
model_pricing.local.json sitting in the repo can't change what any of them see.
"""
import json

import pytest

import ai_config
import pricing_check as pc


@pytest.fixture(autouse=True)
def isolated_overrides(tmp_path, monkeypatch):
    """Point the override path at a non-existent tmp file and clear the mtime cache."""
    monkeypatch.setenv(ai_config.LOCAL_PRICING_ENV, str(tmp_path / "model_pricing.local.json"))
    monkeypatch.setattr(ai_config, "_pricing_cache", None)
    yield tmp_path / "model_pricing.local.json"
    monkeypatch.setattr(ai_config, "_pricing_cache", None)


def write_local(path, models):
    path.write_text(json.dumps({"models": models}))


# ── shipped table ─────────────────────────────────────────────────────────────
def test_shipped_table_loads_and_derives_cache_rates():
    """input/output are stored; the two cache rates are derived by multiplier."""
    sonnet = ai_config.model_pricing()["claude-sonnet-5"]
    assert sonnet["input"] == 2.00 / 1_000_000
    assert sonnet["output"] == 10.00 / 1_000_000
    assert sonnet["cache_write"] == 2.00 * 1.25 / 1_000_000
    assert sonnet["cache_read"] == 2.00 * 0.10 / 1_000_000


def test_shipped_table_honors_a_non_standard_cache_multiplier():
    """Fable/Mythos 5.1 bill cache hits at 0.025x, a page footnote rather than a column. The
    JSON has to be able to express that or the move off the Python table loses it."""
    assert ai_config.model_pricing()["claude-fable-5-1"]["cache_read"] == 10.00 * 0.025 / 1_000_000


def test_no_overrides_by_default():
    assert ai_config.pricing_overrides() == {}
    assert ai_config.describe_pricing_overrides() == []


# ── merge rules ───────────────────────────────────────────────────────────────
def test_local_file_adds_a_model_the_shipped_table_lacks(isolated_overrides):
    """The headline use case: price a just-announced model before we ship it.

    Uses a deliberately fictional id: pointing this at a real unshipped model makes the test
    fail the day we ship it, which is exactly what happened with claude-sonnet-5-5."""
    assert ai_config.pricing_for("claude-fictional-9") is None
    write_local(isolated_overrides, {"claude-fictional-9": {"input": 2.00, "output": 10.00}})
    ai_config._pricing_cache = None
    assert ai_config.estimate_cost("claude-fictional-9", input=1_000_000) == 2.00


def test_local_override_wins_over_a_shipped_price(isolated_overrides):
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 1.11, "output": 2.22}})
    ai_config._pricing_cache = None
    assert ai_config.estimate_cost("claude-sonnet-5", input=1_000_000) == 1.11
    assert ai_config.pricing_overrides()["claude-sonnet-5"]["_shipped"] is True


def test_override_replaces_an_entry_rather_than_patching_fields(isolated_overrides):
    """A local entry is a complete price, not a partial patch — a half-inherited hybrid (new
    input rate, stale output rate) would be a silently wrong number with no way to spot it."""
    write_local(isolated_overrides, {"claude-fable-5-1": {"input": 10.00, "output": 50.00}})
    ai_config._pricing_cache = None
    # The shipped entry's 0.025x cache multiplier is NOT inherited; the default applies.
    assert ai_config.model_pricing()["claude-fable-5-1"]["cache_read"] == 10.00 * 0.10 / 1_000_000


def test_shipped_table_is_unaffected_by_overrides(isolated_overrides):
    """shipped_pricing() is what the drift check validates, so an override must not leak in."""
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 99.0, "output": 99.0}})
    ai_config._pricing_cache = None
    assert ai_config.shipped_pricing()["claude-sonnet-5"]["input"] == 2.00 / 1_000_000
    assert ai_config.model_pricing()["claude-sonnet-5"]["input"] == 99.0 / 1_000_000


def test_override_unblocks_the_unpriced_model_gate(isolated_overrides):
    """The override and the startup gate have to compose, or adding a model locally still can't
    be used. Before: refused. After: accepted."""
    cfg = [("s", {"viability": {"model": "claude-fictional-9"}})]
    with pytest.raises(ai_config.UnpricedModelError):
        ai_config.require_priced_models(cfg)
    write_local(isolated_overrides, {"claude-fictional-9": {"input": 2.00, "output": 10.00}})
    ai_config._pricing_cache = None
    ai_config.require_priced_models(cfg)          # no raise


def test_edit_is_picked_up_without_a_restart(isolated_overrides):
    """app.py hot-reloads config; pricing that needed a bounce would be a trap. Cache is keyed
    on (path, mtime_ns, size), so a rewrite is seen."""
    write_local(isolated_overrides, {"claude-fictional-9": {"input": 2.00, "output": 10.00}})
    ai_config._pricing_cache = None
    assert ai_config.estimate_cost("claude-fictional-9", input=1_000_000) == 2.00
    write_local(isolated_overrides, {"claude-fictional-9": {"input": 4.00, "output": 20.00}})
    assert ai_config.estimate_cost("claude-fictional-9", input=1_000_000) == 4.00


# ── failure modes: loud, never fail-soft ──────────────────────────────────────
def test_malformed_local_json_raises(isolated_overrides):
    """Fail-soft here would silently revert to shipped prices the user deliberately overrode."""
    isolated_overrides.write_text("{ not json")
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError):
        ai_config.model_pricing()


def test_local_file_without_a_models_object_raises(isolated_overrides):
    isolated_overrides.write_text(json.dumps({"claude-fictional-9": {"input": 2.0}}))
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError) as exc:
        ai_config.model_pricing()
    assert "models" in str(exc.value)


def test_entry_missing_rates_raises_and_names_the_model(isolated_overrides):
    write_local(isolated_overrides, {"claude-fictional-9": {"input": 2.00}})   # no output
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError) as exc:
        ai_config.model_pricing()
    assert "claude-fictional-9" in str(exc.value)


def test_non_numeric_rate_raises(isolated_overrides):
    write_local(isolated_overrides, {"claude-fictional-9": {"input": "two bucks", "output": 10}})
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError):
        ai_config.model_pricing()


# ── disclosure and suppression ────────────────────────────────────────────────
def test_disclosure_names_model_rates_and_whether_it_replaces_a_shipped_price(isolated_overrides):
    write_local(isolated_overrides, {"claude-sonnet-5":   {"input": 1.0, "output": 2.0},
                                     "claude-fictional-9": {"input": 2.0, "output": 10.0}})
    ai_config._pricing_cache = None
    lines = "\n".join(ai_config.describe_pricing_overrides())
    assert "claude-sonnet-5:" in lines and "replaces the shipped price" in lines
    assert "claude-fictional-9:" in lines and "not in the shipped table" in lines


def test_suppression_silences_drift_warnings_but_not_the_disclosure(isolated_overrides):
    """The switch exists for a negotiated rate that will never match the public page. It stops
    the nag; it must not hide that the ledger is priced off a local file."""
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 1.0, "output": 2.0,
                                                        "suppress_drift_warning": True,
                                                        "note": "negotiated rate"}})
    ai_config._pricing_cache = None
    assert ai_config.override_is_silenced("claude-sonnet-5") is True
    disclosure = "\n".join(ai_config.describe_pricing_overrides())
    assert "claude-sonnet-5" in disclosure          # still disclosed
    assert "negotiated rate" in disclosure          # the note is echoed, so "why" survives
    assert "silenced" in disclosure


# ── drift check interaction ───────────────────────────────────────────────────
def _live(table):
    """A pricing table ($/token) in the live parser's shape ($/MTok)."""
    return {m: {k: v * 1_000_000 for k, v in r.items()} for m, r in table.items()}


def test_override_disagreeing_with_live_is_advisory_not_fatal(isolated_overrides):
    """Case C: user typo'd, or the rate moved. compare_to_repo (the hard failure) must stay
    clean, while override_problems reports it."""
    live = _live(ai_config.shipped_pricing())
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 3.00, "output": 15.00}})
    ai_config._pricing_cache = None
    assert pc.compare_to_repo(live) == []                    # shipped table still agrees
    notes = pc.override_problems(live)
    assert any("claude-sonnet-5" in n and "local override" in n for n in notes)


def test_redundant_override_is_flagged_for_deletion(isolated_overrides):
    """Case B: the shipped table caught up. Harmless today, but it keeps winning — so the next
    price change would be silently overridden by the stale copy."""
    live = _live(ai_config.shipped_pricing())
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 2.00, "output": 10.00}})
    ai_config._pricing_cache = None
    notes = pc.override_problems(live)
    assert any("redundant" in n for n in notes)


def test_suppressed_override_produces_no_drift_notes(isolated_overrides):
    live = _live(ai_config.shipped_pricing())
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 3.00, "output": 15.00,
                                                        "suppress_drift_warning": True}})
    ai_config._pricing_cache = None
    assert pc.override_problems(live) == []


def test_locally_priced_model_is_not_reported_as_missing(isolated_overrides):
    """missing_from_repo reads the MERGED table: a model the user already priced is solved, and
    nagging anyway trains them to ignore the warning."""
    live = dict(_live(ai_config.shipped_pricing()),
                **{"claude-fictional-9": {"input": 2.0, "cache_write": 2.5,
                                         "cache_read": 0.2, "output": 10.0}})
    assert "claude-fictional-9" in pc.missing_from_repo(live, set())
    write_local(isolated_overrides, {"claude-fictional-9": {"input": 2.00, "output": 10.00}})
    ai_config._pricing_cache = None
    assert "claude-fictional-9" not in pc.missing_from_repo(live, set())


# ── temporal pricing: effective_start / effective_until ───────────────────────
# A price is a sequence of half-open [start, until) periods, not a single number. Modelling it
# as "the current price" made a historical ledger row unrepriceable, because nothing recorded
# what the rate was when the row was written.
def test_bare_object_means_one_epoch_to_forever_period(isolated_overrides):
    """The common case — a model with one price — must not pay for the temporal machinery."""
    write_local(isolated_overrides, {"m-1": {"input": 2.00, "output": 10.00}})
    ai_config._pricing_cache = None
    assert ai_config.estimate_cost("m-1", input=1_000_000, at="1999-01-01") == 2.00
    assert ai_config.estimate_cost("m-1", input=1_000_000, at="2099-01-01") == 2.00


def test_rate_is_chosen_by_the_instant_asked_for(isolated_overrides):
    write_local(isolated_overrides, {"m-1": [
        {"input": 3.00, "output": 15.00, "effective_until": "2026-09-23"},
        {"input": 2.00, "output": 10.00, "effective_start": "2026-09-23"},
    ]})
    ai_config._pricing_cache = None
    assert ai_config.estimate_cost("m-1", input=1_000_000, at="2026-09-22 23:59:59") == 3.00
    # Half-open: the boundary instant belongs to the LATER period, so adjacent periods can
    # share a date without overlapping.
    assert ai_config.estimate_cost("m-1", input=1_000_000, at="2026-09-23 00:00:00") == 2.00
    assert ai_config.estimate_cost("m-1", input=1_000_000, at="2026-09-24 12:00:00") == 2.00


def test_ledger_timestamp_format_is_accepted_directly(isolated_overrides):
    """Rows carry SQLite's 'YYYY-MM-DD HH:MM:SS'; reprice passes that straight in, so the
    space-vs-T difference must not silently miss the window."""
    write_local(isolated_overrides, {"m-1": [
        {"input": 3.00, "output": 15.00, "effective_until": "2026-09-23"},
        {"input": 2.00, "output": 10.00, "effective_start": "2026-09-23"},
    ]})
    ai_config._pricing_cache = None
    assert ai_config.pricing_for("m-1", "2026-09-20 10:11:12")["input"] == 3.00 / 1_000_000


def test_overlapping_periods_are_fatal(isolated_overrides):
    """An overlap makes the rate at an instant ambiguous — there is no safe pick."""
    write_local(isolated_overrides, {"m-1": [
        {"input": 3.00, "output": 15.00, "effective_until": "2026-09-25"},
        {"input": 2.00, "output": 10.00, "effective_start": "2026-09-23"},
    ]})
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError) as exc:
        ai_config.model_pricing()
    assert "overlapping" in str(exc.value)


def test_interior_gap_is_fatal(isolated_overrides):
    """A gap makes the rate unknown, which is the same failure as not knowing today's price —
    and we already refuse to run on that. Picking a neighbouring period would invent a number."""
    write_local(isolated_overrides, {"m-1": [
        {"input": 3.00, "output": 15.00, "effective_until": "2026-09-20"},
        {"input": 2.00, "output": 10.00, "effective_start": "2026-09-23"},
    ]})
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError) as exc:
        ai_config.model_pricing()
    assert "gap" in str(exc.value)


def test_period_ending_before_it_starts_is_fatal(isolated_overrides):
    write_local(isolated_overrides, {"m-1": {"input": 2.0, "output": 10.0,
                                             "effective_start": "2026-09-23",
                                             "effective_until": "2026-09-01"}})
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError):
        ai_config.model_pricing()


def test_unparseable_date_is_fatal_and_names_the_field(isolated_overrides):
    write_local(isolated_overrides, {"m-1": {"input": 2.0, "output": 10.0,
                                             "effective_start": "September 2026"}})
    ai_config._pricing_cache = None
    with pytest.raises(ai_config.PricingError) as exc:
        ai_config.model_pricing()
    assert "effective_start" in str(exc.value)


def test_model_outside_its_timeline_reads_as_unpriced(isolated_overrides):
    """'No period covers this instant' and 'unknown model' are the same thing to every caller,
    so a lapsed timeline trips the startup gate rather than silently pricing at a stale rate."""
    write_local(isolated_overrides, {"m-1": {"input": 2.0, "output": 10.0,
                                             "effective_until": "2026-01-01"}})
    ai_config._pricing_cache = None
    assert ai_config.pricing_for("m-1", "2025-06-01")["input"] == 2.0 / 1_000_000
    assert ai_config.pricing_for("m-1", "2026-06-01") is None
    assert list(ai_config.unpriced_models({"viability": {"model": "m-1"}})) == ["m-1"]


def test_future_period_does_not_price_today(isolated_overrides):
    """Pre-loading an announced future price must not change what today's calls are billed."""
    write_local(isolated_overrides, {"claude-sonnet-5": [
        {"input": 2.00, "output": 10.00, "effective_until": "2099-01-01"},
        {"input": 4.00, "output": 20.00, "effective_start": "2099-01-01"},
    ]})
    ai_config._pricing_cache = None
    assert ai_config.estimate_cost("claude-sonnet-5", input=1_000_000) == 2.00


def test_override_shrinking_the_timeline_warns(isolated_overrides):
    """An override replaces the model's WHOLE timeline, so declaring only 'from today' silently
    orphans older ledger rows. Detected rather than left to whether the docs were read."""
    write_local(isolated_overrides, {"claude-sonnet-5": {"input": 2.0, "output": 10.0,
                                                        "effective_start": "2026-09-23"}})
    ai_config._pricing_cache = None
    warns = ai_config.override_coverage_warnings()
    assert any("claude-sonnet-5" in w and "2026-09-23" in w for w in warns)


def test_override_covering_full_history_does_not_warn(isolated_overrides):
    write_local(isolated_overrides, {"claude-sonnet-5": [
        {"input": 3.0, "output": 15.0, "effective_until": "2026-09-23"},
        {"input": 2.0, "output": 10.0, "effective_start": "2026-09-23"},
    ]})
    ai_config._pricing_cache = None
    assert ai_config.override_coverage_warnings() == []
