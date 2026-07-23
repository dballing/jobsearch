"""Tests for rescore_viability's job-selection matrix and --autoskipped helpers.

build_selection() decides which jobs a run touches — the SQL is executed against the live
DB in main(), but the clause/param construction is pure and worth locking down: a wrong
filter silently rescoring the wrong set (or nothing) is the kind of bug that hides. The
--autoskipped promotion predicate and the argparse type validators are pure too.
"""
import argparse

import pytest

import app
import rescore_viability as rv


HASH = "abc123"


# ── build_selection: staleness axis ───────────────────────────────────────────
def test_default_includes_staleness_and_active_filter():
    where, params = rv.build_selection(current_hash=HASH)
    assert "viability_prompt_hash != ?" in where          # staleness gate present
    assert "status NOT IN" in where                        # default active set
    assert params == [HASH]                                # only the hash param


def test_force_drops_staleness_gate():
    where, params = rv.build_selection(current_hash=HASH, force=True)
    assert "viability_prompt_hash" not in where
    assert params == []                                    # no hash param when forced


# ── build_selection: status axis ──────────────────────────────────────────────
def test_all_statuses_drops_status_filter():
    where, _ = rv.build_selection(current_hash=HASH, all_statuses=True)
    assert "status" not in where                           # no status clause at all
    assert "viability_prompt_hash" in where                # staleness still there


def test_early_stage_filter():
    where, _ = rv.build_selection(current_hash=HASH, early_stage=True)
    assert "status IN ('new', 'reviewing', 'deferred')" in where


def test_autoskipped_targets_only_autoskipped_not_skipped():
    where, _ = rv.build_selection(current_hash=HASH, autoskipped=True)
    assert "status = 'autoskipped'" in where
    # Must NOT fall back to the active-exclusion clause (which also names 'autoskipped').
    assert "status NOT IN" not in where
    # And must not accidentally sweep in plain 'skipped'.
    assert "'skipped'" not in where.replace("'autoskipped'", "")


def test_force_all_no_date_is_empty_where():
    where, params = rv.build_selection(current_hash=HASH, force=True, all_statuses=True)
    assert where == "" and params == []                    # truly everything


def test_explicit_status_is_parameterized_and_has_no_escape():
    """--status=skipped selects exactly that status, parameterized (no NULL/needs_rescored
    escape that would sweep in other statuses)."""
    where, params = rv.build_selection(current_hash=HASH, status="skipped")
    assert "status = ?" in where
    assert "status NOT IN" not in where and "viability IS NULL OR needs_rescored" not in where
    assert params == [HASH, "skipped"]                     # staleness hash first, then status


# ── build_selection: current-viability axis + the target composition ──────────
def test_current_viability_adds_score_filter():
    where, params = rv.build_selection(current_hash=HASH, current_viability="high")
    assert "viability = ?" in where
    assert params == [HASH, "high"]


def test_status_and_current_viability_compose_with_param_order():
    """The headline use case: skipped-but-high. Forced (so no staleness gate), the two
    filters AND together with params in placeholder order (status then viability)."""
    where, params = rv.build_selection(
        current_hash=HASH, force=True, status="skipped", current_viability="high")
    assert "status = ?" in where and "viability = ?" in where and " AND " in where
    assert params == ["skipped", "high"]                   # no hash (forced), status, viability


def test_status_current_viability_keep_staleness_when_not_forced():
    """Without --force the staleness gate still applies, so the explicit selection only
    reaches the stale subset; param order stays hash → status → viability."""
    where, params = rv.build_selection(
        current_hash=HASH, status="skipped", current_viability="high")
    assert "viability_prompt_hash != ?" in where
    assert params == [HASH, "skipped", "high"]


# ── build_selection: ingest-date axis + param ordering ────────────────────────
def test_since_adds_date_floor_and_param_order():
    where, params = rv.build_selection(current_hash=HASH, since="2026-07-01")
    assert "date(first_seen) >= ?" in where
    # Params must line up with placeholder order: staleness hash first, then the date.
    assert params == [HASH, "2026-07-01"]


def test_previous_days_uses_rolling_window():
    where, params = rv.build_selection(current_hash=HASH, previous_days=7)
    assert "first_seen >= datetime('now', ?)" in where
    assert params == [HASH, "-7 days"]


def test_autoskipped_with_since_combines():
    where, params = rv.build_selection(
        current_hash=HASH, force=True, autoskipped=True, since="2026-07-05")
    assert "status = 'autoskipped'" in where and "date(first_seen) >= ?" in where
    assert params == ["2026-07-05"]                        # forced → no hash, just the date


# ── should_unskip: --autoskipped promotion predicate ──────────────────────────
def test_should_unskip_low_threshold():
    # threshold "low" (rank 0): anything above low (medium/high) is promoted; low stays.
    thr = rv.VIABILITY_RANK["low"]
    assert rv.should_unskip("high", thr) is True
    assert rv.should_unskip("medium", thr) is True
    assert rv.should_unskip("low", thr) is False


def test_should_unskip_medium_threshold():
    # threshold "medium" (rank 1): only high clears it.
    thr = rv.VIABILITY_RANK["medium"]
    assert rv.should_unskip("high", thr) is True
    assert rv.should_unskip("medium", thr) is False
    assert rv.should_unskip("low", thr) is False


def test_should_unskip_unknown_rating_never_promotes():
    assert rv.should_unskip("garbage", rv.VIABILITY_RANK["low"]) is False


# ── canonical_promotion_applies: don't promote against a stale canonical ───────
CUR = "hash-current"


def test_promotion_requires_current_canonical_hash():
    # New 'medium' beats canonical 'low', but the canonical's score is STALE (old prompt),
    # so it must NOT promote — this is the Chime OMX SOTO case.
    assert rv.canonical_promotion_applies(
        new_rating="medium", prev_rating=None, canon_rating="low",
        canon_hash="hash-OLD", current_hash=CUR) is False


def test_promotion_fires_when_canonical_current_and_strictly_better():
    assert rv.canonical_promotion_applies(
        new_rating="medium", prev_rating=None, canon_rating="low",
        canon_hash=CUR, current_hash=CUR) is True


def test_no_promotion_when_not_beating_current_canonical():
    # Current canonical, but the duplicate only ties it (medium vs medium) → no promotion.
    assert rv.canonical_promotion_applies(
        new_rating="medium", prev_rating=None, canon_rating="medium",
        canon_hash=CUR, current_hash=CUR) is False


def test_no_promotion_when_not_beating_own_prior():
    # Beats the canonical but not its own prior score (already high) → no churn.
    assert rv.canonical_promotion_applies(
        new_rating="high", prev_rating="high", canon_rating="low",
        canon_hash=CUR, current_hash=CUR) is False


def test_no_promotion_when_canonical_never_scored():
    # Canonical has no hash yet (NULL) → not a valid yardstick → hold off.
    assert rv.canonical_promotion_applies(
        new_rating="high", prev_rating=None, canon_rating=None,
        canon_hash=None, current_hash=CUR) is False


# ── argparse type validators ──────────────────────────────────────────────────
def test_valid_since_date_accepts_iso_and_rejects_junk():
    assert rv.valid_since_date("2026-07-10") == "2026-07-10"
    for bad in ("07-10-2026", "2026/07/10", "yesterday", "2026-13-01"):
        with pytest.raises(argparse.ArgumentTypeError):
            rv.valid_since_date(bad)


def test_positive_int_accepts_positive_rejects_rest():
    assert rv.positive_int("5") == 5
    for bad in ("0", "-3", "x", "2.5"):
        with pytest.raises(argparse.ArgumentTypeError):
            rv.positive_int(bad)


def test_valid_status_accepts_known_normalizes_case_rejects_junk():
    assert rv.valid_status("skipped") == "skipped"
    assert rv.valid_status(" SKIPPED ") == "skipped"       # trimmed + lowercased
    for bad in ("skip", "", "activeish", "high"):
        with pytest.raises(argparse.ArgumentTypeError):
            rv.valid_status(bad)


def test_valid_viability_accepts_tiers_rejects_rest():
    for good in ("high", "Medium", " low "):
        assert rv.valid_viability(good) == good.strip().lower()
    for bad in ("unscored", "", "hi", "skipped"):
        with pytest.raises(argparse.ArgumentTypeError):
            rv.valid_viability(bad)


# ── cross-module constant sync ────────────────────────────────────────────────
# rescore_viability duplicates the status/viability vocabularies (VALID_STATUSES /
# VALID_VIABILITIES) rather than importing app, so the batch script stays standalone
# (importing app spins up a Flask app). That duplication can silently drift — a status
# added in app.STATUSES but not here would make --status reject a real status. These lock
# the two definitions together so a future edit to one side fails loudly until both agree.
def test_rescore_statuses_match_app():
    assert set(rv.VALID_STATUSES) == set(app.STATUSES), (
        "rescore_viability.VALID_STATUSES has drifted from app.STATUSES; update both.")


def test_rescore_viabilities_match_app():
    # app enumerates the scored tiers as the VIABILITY_COLORS keys (NULL/unscored is implicit).
    assert set(rv.VALID_VIABILITIES) == set(app.VIABILITY_COLORS), (
        "rescore_viability.VALID_VIABILITIES has drifted from app.VIABILITY_COLORS; update both.")
