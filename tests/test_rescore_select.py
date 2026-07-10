"""Tests for rescore_viability's job-selection matrix and --autoskipped helpers.

build_selection() decides which jobs a run touches — the SQL is executed against the live
DB in main(), but the clause/param construction is pure and worth locking down: a wrong
filter silently rescoring the wrong set (or nothing) is the kind of bug that hides. The
--autoskipped promotion predicate and the argparse type validators are pure too.
"""
import argparse

import pytest

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
