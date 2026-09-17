"""The ai_usage cost ledger: recording billed calls and the per-lens cost queries.

All hermetic — an in-memory DB with the real schema, fake usage objects, and a pinned `now`
so the trailing-window math is deterministic. See ai_usage.py for the accounting rules these
pin down (additive spend, tracked-job denominators, initial-vs-rescore, trailing windows).
"""
from types import SimpleNamespace

import pytest

import ai_usage
from ingest import DescriptionFormatter

APPLIED = ("applied", "rejected", "ghosted", "interviewing", "offered", "withdrawn")
NOW = "2026-09-17 12:00:00"


def _usage(i=100, o=10, w=0, r=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o,
                           cache_creation_input_tokens=w, cache_read_input_tokens=r)


def _row(conn, ts, sid, job, feature, cost, model="claude-haiku-4-5"):
    conn.execute(
        "INSERT INTO ai_usage (ts, search_id, job_id, feature, model, input_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)", (ts, sid, job, feature, model, cost))


def _jss(conn, job, sid, status="new", viability=None, applied_at=None):
    conn.execute(
        "INSERT INTO job_search_state (job_id, search_id, status, viability, applied_at) "
        "VALUES (?, ?, ?, ?, ?)", (job, sid, status, viability, applied_at))


# ── table + recording ─────────────────────────────────────────────────────────────────────
def test_ensure_table_is_idempotent(jobs_db):
    ai_usage.ensure_ai_usage_table(jobs_db)
    ai_usage.ensure_ai_usage_table(jobs_db)
    cols = {r[1] for r in jobs_db.execute("PRAGMA table_info(ai_usage)")}
    assert {"ts", "search_id", "job_id", "feature", "model", "cost_usd"} <= cols


def test_usage_counts_none_and_zero_record_nothing():
    assert ai_usage.usage_counts(None) is None
    assert ai_usage.usage_counts(_usage(0, 0)) is None
    # Missing / None attributes read as 0, like the log tallies.
    assert ai_usage.usage_counts(SimpleNamespace(input_tokens=5, output_tokens=None)) == \
        {"input": 5, "output": 0, "cache_write": 0, "cache_read": 0}


def test_record_usage_writes_priced_row(jobs_db):
    assert ai_usage.record_usage(jobs_db, feature="viability", model="claude-haiku-4-5",
                                 usage=_usage(1_000_000, 0), search_id="s1", job_id="j1")
    row = jobs_db.execute("SELECT search_id, job_id, feature, input_tokens, cost_usd FROM ai_usage").fetchone()
    assert tuple(row[:4]) == ("s1", "j1", "viability", 1_000_000)
    assert row[4] == pytest.approx(1.0)      # haiku-4-5 input is $1 / MTok


def test_record_usage_skips_empty_and_nulls_unpriced_cost(jobs_db):
    assert not ai_usage.record_usage(jobs_db, feature="viability", model="claude-haiku-4-5",
                                     usage=None, search_id="s1", job_id="j1")
    assert ai_usage.record_usage(jobs_db, feature="viability", model="some-unpriced-model",
                                 usage=_usage(), search_id="s1", job_id="j1")
    rows = jobs_db.execute("SELECT cost_usd FROM ai_usage").fetchall()
    assert len(rows) == 1 and rows[0][0] is None


def test_record_usage_tolerates_missing_table():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    assert not ai_usage.record_usage(conn, feature="viability", model="claude-haiku-4-5",
                                     usage=_usage(), search_id="s", job_id="j")


# ── DescriptionFormatter ledgering ─────────────────────────────────────────────────────────
class _ReformatClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        text = kwargs["messages"][0]["content"].split("\n\n", 1)[1]
        msg = SimpleNamespace(stop_reason="end_turn", usage=_usage(),
                              content=[SimpleNamespace(type="text", text=text)])
        return msg


def test_formatter_records_real_call_but_not_cache_hit(jobs_db):
    fmt = DescriptionFormatter(client=_ReformatClient())
    desc = "Build rockets. Requirements: patience."
    assert fmt.format(jobs_db, desc, "h1", "j1", search_id="lensA", job_id="j1")
    assert fmt.format(jobs_db, desc, "h1", "j2", search_id="lensB", job_id="j2")   # in-run cache hit
    rows = jobs_db.execute("SELECT search_id, job_id, feature FROM ai_usage").fetchall()
    assert [tuple(r) for r in rows] == [("lensA", "j1", "reformat")]


# ── daily_spend_by_lens ───────────────────────────────────────────────────────────────────
def test_daily_spend_zero_fills_and_aligns(jobs_db):
    _row(jobs_db, "2026-09-15 10:00:00", "a", "j1", "viability", 0.5)
    _row(jobs_db, "2026-09-15 11:00:00", "a", "j2", "viability", 0.25)
    _row(jobs_db, "2026-09-17 09:00:00", "b", "j3", "reformat", 1.0)
    _row(jobs_db, "2026-08-01 09:00:00", "a", "j9", "viability", 9.0)   # outside the span
    d = ai_usage.daily_spend_by_lens(jobs_db, days=3, now=NOW)
    assert d["days"] == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert d["series"] == {"a": [0.75, 0.0, 0.0], "b": [0.0, 0.0, 1.0]}


# ── lens_cost_summary: all time ───────────────────────────────────────────────────────────
@pytest.fixture
def ledger(jobs_db):
    """Two lenses. Lens a: j1 applied+high (initial score + reformat + one rescore), j2 low, j3 an
    unscored job with spend (→ other), plus an OLD applied job with no ledger spend (must not count).
    Lens b: one high job only, to prove per-lens isolation."""
    c = jobs_db
    _jss(c, "j1", "a", status="interviewing", viability="high", applied_at="2026-09-10 09:00:00")
    _jss(c, "j2", "a", status="new", viability="low")
    _jss(c, "j3", "a", status="new")
    _jss(c, "old", "a", status="applied", viability="high", applied_at="2026-01-01 09:00:00")
    _jss(c, "k1", "b", status="new", viability="high")
    _row(c, "2026-06-01 08:00:00", "a", "j1", "reformat", 0.10)
    _row(c, "2026-06-01 08:01:00", "a", "j1", "viability", 0.20)
    _row(c, "2026-09-12 08:00:00", "a", "j1", "viability", 0.20)   # rescore, inside a 30d window
    _row(c, "2026-06-02 08:00:00", "a", "j2", "viability", 0.30)
    _row(c, "2026-09-16 08:00:00", "a", "j3", "viability", 0.40)
    _row(c, "2026-09-16 08:00:00", "b", "k1", "viability", 1.00)
    return c


def test_summary_all_time_totals_split_and_buckets(ledger):
    s = ai_usage.lens_cost_summary(ledger, applied_statuses=APPLIED, now=NOW)
    a = s["a"]
    assert a["total_usd"] == pytest.approx(1.20)                    # additive, rescore included
    assert a["initial_usd"] == pytest.approx(1.00)
    assert a["rescore_usd"] == pytest.approx(0.20)
    assert a["by_feature"]["reformat"] == pytest.approx(0.10)
    assert a["by_viability"] == pytest.approx({"high": 0.50, "medium": 0.0, "low": 0.30, "other": 0.40})
    assert a["first_ts"] == "2026-06-01 08:00:00"
    assert a["calls"] == 5 and a["unpriced_calls"] == 0


def test_summary_denominators_count_only_tracked_jobs(ledger):
    a = ai_usage.lens_cost_summary(ledger, applied_statuses=APPLIED, now=NOW)["a"]
    # 'old' is applied + high but has no ledger spend — excluded from both denominators.
    assert a["applied_jobs"] == 1 and a["cost_per_applied"] == pytest.approx(1.20)
    assert a["high_jobs"] == 1 and a["cost_per_high"] == pytest.approx(1.20)


def test_summary_is_per_lens_and_none_on_zero_denominator(ledger):
    b = ai_usage.lens_cost_summary(ledger, applied_statuses=APPLIED, now=NOW)["b"]
    assert b["total_usd"] == pytest.approx(1.00)
    assert b["applied_jobs"] == 0 and b["cost_per_applied"] is None
    assert b["cost_per_high"] == pytest.approx(1.00)


def test_summary_empty_ledger(jobs_db):
    assert ai_usage.lens_cost_summary(jobs_db, applied_statuses=APPLIED, now=NOW) == {}


def test_summary_counts_unpriced(jobs_db):
    _row(jobs_db, "2026-09-16 08:00:00", "a", "j1", "viability", None)
    a = ai_usage.lens_cost_summary(jobs_db, applied_statuses=APPLIED, now=NOW)["a"]
    assert a["unpriced_calls"] == 1 and a["total_usd"] == 0


# ── lens_cost_summary: trailing window ────────────────────────────────────────────────────
def test_summary_window_excludes_old_spend_and_keeps_rescore_classification(ledger):
    a = ai_usage.lens_cost_summary(ledger, applied_statuses=APPLIED, window_days=30, now=NOW)["a"]
    # In window: j1's rescore (0.20) and j3's initial score (0.40).
    assert a["total_usd"] == pytest.approx(0.60)
    # j1's in-window row is still a rescore — initial is decided over the job's whole history.
    assert a["rescore_usd"] == pytest.approx(0.20) and a["initial_usd"] == pytest.approx(0.40)
    # j1 was ingested/scored months ago but applied inside the window → counts by applied_at.
    assert a["applied_jobs"] == 1 and a["cost_per_applied"] == pytest.approx(0.60)
    # j1's FIRST score is outside the window, so it isn't an in-window High.
    assert a["high_jobs"] == 0 and a["cost_per_high"] is None


def test_summary_window_still_lists_lens_with_no_recent_spend(jobs_db):
    _row(jobs_db, "2026-01-01 08:00:00", "a", "j1", "viability", 0.5)
    a = ai_usage.lens_cost_summary(jobs_db, applied_statuses=APPLIED, window_days=30, now=NOW)["a"]
    assert a["total_usd"] == 0 and a["cost_per_applied"] is None


# ── rolling_ratio + trend series ──────────────────────────────────────────────────────────
def test_rolling_ratio_sums_windows():
    assert ai_usage.rolling_ratio([1, 2, 3, 4], [1, 0, 1, 0], window=2) == [3.0, 5.0, 7.0]


def test_rolling_ratio_none_without_applications():
    assert ai_usage.rolling_ratio([1, 1, 1], [0, 0, 1], window=2) == [None, 2.0]


def test_rolling_ratio_none_before_full_window_of_tracking():
    # Tracking started at index 1, so the window starting at 0 is partial → None.
    assert ai_usage.rolling_ratio([0, 2, 2], [0, 1, 1], window=2, first_index=1) == [None, 2.0]


def test_trend_series_shape_and_gaps(jobs_db):
    c = jobs_db
    _jss(c, "j1", "a", status="applied", applied_at="2026-09-16 09:00:00")
    _row(c, "2026-09-10 08:00:00", "a", "j1", "viability", 0.6)
    _row(c, "2026-09-16 08:00:00", "a", "j1", "viability", 0.3)
    t = ai_usage.trailing_cost_per_applied_series(c, applied_statuses=APPLIED,
                                                  window_days=3, span_days=5, now=NOW)
    assert t["days"] == ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
    # Windows ending 09-16 and 09-17 contain the application; the 09-16 spend is in both.
    assert t["series"]["a"] == [None, None, None, pytest.approx(0.3), pytest.approx(0.3)]


def test_trend_omits_lens_without_full_window(jobs_db):
    c = jobs_db
    _jss(c, "j1", "a", status="applied", applied_at="2026-09-17 09:00:00")
    _row(c, "2026-09-16 08:00:00", "a", "j1", "viability", 0.3)   # only 2 days of tracking
    t = ai_usage.trailing_cost_per_applied_series(c, applied_statuses=APPLIED,
                                                  window_days=30, span_days=10, now=NOW)
    assert t["series"] == {}


# ── route smoke test ──────────────────────────────────────────────────────────────────────
def test_stats_cost_route(sample_app_db):
    import app
    resp = app.app.test_client().get("/stats/cost")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data) >= {"daily", "lenses", "trend", "names", "colors", "current"}
    assert set(data["lenses"]) == {"all", "90", "30"}
    lens = data["lenses"]["all"]["__default__"]
    # Sample ledger: ln_root (interviewing, high) has initial + rescore spend; ln_new_hot is high.
    assert lens["total_usd"] == pytest.approx(0.0166)
    assert lens["rescore_usd"] == pytest.approx(0.0035)
    assert lens["applied_jobs"] == 1 and lens["high_jobs"] == 2
