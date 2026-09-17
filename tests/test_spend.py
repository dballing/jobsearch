"""The spend_ledger cost ledger: recording billed calls and the per-lens cost queries.

All hermetic — an in-memory DB with the real schema, fake usage objects, and a pinned `now`
so the trailing-window math is deterministic. See spend.py for the accounting rules these
pin down (additive spend, tracked-job denominators, initial-vs-rescore, trailing windows).
"""
from types import SimpleNamespace

import pytest

import spend
from ingest import DescriptionFormatter

APPLIED = ("applied", "rejected", "ghosted", "interviewing", "offered", "withdrawn")
NOW = "2026-09-17 12:00:00"


def _usage(i=100, o=10, w=0, r=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o,
                           cache_creation_input_tokens=w, cache_read_input_tokens=r)


def _row(conn, ts, sid, job, feature, cost, model="claude-haiku-4-5"):
    conn.execute(
        "INSERT INTO spend_ledger (ts, search_id, job_id, feature, model, input_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)", (ts, sid, job, feature, model, cost))


def _jss(conn, job, sid, status="new", viability=None, applied_at=None):
    conn.execute(
        "INSERT INTO job_search_state (job_id, search_id, status, viability, applied_at) "
        "VALUES (?, ?, ?, ?, ?)", (job, sid, status, viability, applied_at))


# ── table + recording ─────────────────────────────────────────────────────────────────────
def test_ensure_table_is_idempotent(jobs_db):
    spend.ensure_spend_ledger(jobs_db)
    spend.ensure_spend_ledger(jobs_db)
    cols = {r[1] for r in jobs_db.execute("PRAGMA table_info(spend_ledger)")}
    assert {"ts", "search_id", "job_id", "feature", "model", "cost_usd"} <= cols


def test_usage_counts_none_and_zero_record_nothing():
    assert spend.usage_counts(None) is None
    assert spend.usage_counts(_usage(0, 0)) is None
    # Missing / None attributes read as 0, like the log tallies.
    assert spend.usage_counts(SimpleNamespace(input_tokens=5, output_tokens=None)) == \
        {"input": 5, "output": 0, "cache_write": 0, "cache_read": 0}


def test_record_usage_writes_priced_row(jobs_db):
    assert spend.record_usage(jobs_db, feature="viability", model="claude-haiku-4-5",
                                 usage=_usage(1_000_000, 0), search_id="s1", job_id="j1")
    row = jobs_db.execute("SELECT search_id, job_id, feature, input_tokens, cost_usd FROM spend_ledger").fetchone()
    assert tuple(row[:4]) == ("s1", "j1", "viability", 1_000_000)
    assert row[4] == pytest.approx(1.0)      # haiku-4-5 input is $1 / MTok


def test_record_usage_skips_empty_and_nulls_unpriced_cost(jobs_db):
    assert not spend.record_usage(jobs_db, feature="viability", model="claude-haiku-4-5",
                                     usage=None, search_id="s1", job_id="j1")
    assert spend.record_usage(jobs_db, feature="viability", model="some-unpriced-model",
                                 usage=_usage(), search_id="s1", job_id="j1")
    rows = jobs_db.execute("SELECT cost_usd FROM spend_ledger").fetchall()
    assert len(rows) == 1 and rows[0][0] is None


def test_record_usage_tolerates_missing_table():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    assert not spend.record_usage(conn, feature="viability", model="claude-haiku-4-5",
                                     usage=_usage(), search_id="s", job_id="j")


def _legacy_ai_usage(conn):
    conn.execute("CREATE TABLE ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, search_id TEXT, job_id TEXT, "
                    "feature TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, "
                    "output_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0, "
                 "cache_read_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL)")
    conn.execute("CREATE INDEX idx_ai_usage_job ON ai_usage(job_id, search_id)")
    conn.execute("INSERT INTO ai_usage (search_id, job_id, feature, model, cost_usd) "
                 "VALUES ('a', 'j1', 'viability', 'claude-haiku-4-5', 0.5)")


def test_ensure_migrates_the_legacy_ai_usage_table(jobs_db):
    # DBs created before Apify spend joined the ledger have it as `ai_usage`; its rows must
    # come across rather than being stranded.
    jobs_db.execute("DROP TABLE spend_ledger")
    _legacy_ai_usage(jobs_db)
    spend.ensure_spend_ledger(jobs_db)
    assert [tuple(r) for r in jobs_db.execute("SELECT cost_usd FROM spend_ledger")] == [(0.5,)]
    assert not jobs_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_usage'").fetchall()


def test_ensure_migrates_even_when_the_new_table_already_exists(jobs_db):
    # The live case: an auto-reloading app had already created the (empty) new table beside the
    # old one, so a plain RENAME would no-op and lose every historical row.
    _legacy_ai_usage(jobs_db)          # jobs_db already has an empty spend_ledger from SCHEMA
    spend.ensure_spend_ledger(jobs_db)
    assert [tuple(r) for r in jobs_db.execute("SELECT cost_usd FROM spend_ledger")] == [(0.5,)]
    assert not jobs_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_usage'").fetchall()


# ── Apify run pro-rating ──────────────────────────────────────────────────────────────────
def test_apify_cost_splits_evenly_over_items(jobs_db):
    # 4 items, one job seen twice (a re-sighting costs a share like any other item).
    spend.record_apify_run(jobs_db, search_id="a", task_name="t", cost_usd=0.04,
                           item_job_ids=["j1", "j1", "j2", "j3"])
    rows = dict(jobs_db.execute(
        "SELECT job_id, cost_usd FROM spend_ledger WHERE feature = 'apify'").fetchall())
    assert rows == {"j1": pytest.approx(0.02), "j2": pytest.approx(0.01), "j3": pytest.approx(0.01)}
    assert sum(rows.values()) == pytest.approx(0.04)   # nothing lost


def test_apify_unattributable_items_become_one_overhead_row(jobs_db):
    # An item that produced no job (ATS duplicate) has no owner → the lens's overhead row.
    spend.record_apify_run(jobs_db, search_id="a", task_name="t", cost_usd=0.02,
                           item_job_ids=["j1", None])
    rows = [tuple(r) for r in jobs_db.execute(
        "SELECT job_id, cost_usd FROM spend_ledger WHERE feature = 'apify' ORDER BY job_id")]
    assert rows == [(None, pytest.approx(0.01)), ("j1", pytest.approx(0.01))]


def test_apify_empty_run_is_all_overhead_and_accumulates_in_place(jobs_db):
    # The common case on a frequent schedule: a run that found nothing still cost money, and
    # thousands of them must not become thousands of rows.
    for _ in range(5):
        spend.record_apify_run(jobs_db, search_id="a", task_name="t", cost_usd=0.001,
                               item_job_ids=[])
    rows = jobs_db.execute("SELECT job_id, cost_usd FROM spend_ledger").fetchall()
    assert len(rows) == 1
    assert rows[0][0] is None and rows[0][1] == pytest.approx(0.005)


def test_apify_overhead_is_per_lens(jobs_db):
    spend.record_apify_run(jobs_db, search_id="a", task_name="t", cost_usd=0.01, item_job_ids=[])
    spend.record_apify_run(jobs_db, search_id="b", task_name="t", cost_usd=0.01, item_job_ids=[])
    assert jobs_db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 2


def test_apify_divisor_splits_a_shared_run_between_lenses(jobs_db):
    # One run, ingested by two lenses that share an unscoped task: Apify billed it once.
    for sid in ("a", "b"):
        spend.record_apify_run(jobs_db, search_id=sid, task_name="t", cost_usd=0.02,
                               item_job_ids=["j1"], divisor=2)
    assert jobs_db.execute("SELECT SUM(cost_usd) FROM spend_ledger").fetchone()[0] == pytest.approx(0.02)


def test_apify_zero_cost_records_nothing(jobs_db):
    assert spend.record_apify_run(jobs_db, search_id="a", task_name="t", cost_usd=None,
                                  item_job_ids=["j1"]) == 0
    assert jobs_db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0


def test_apify_spend_is_summarized_separately_from_ai(jobs_db):
    _jss(jobs_db, "j1", "a", status="applied", viability="high", applied_at="2026-09-10 09:00:00")
    _row(jobs_db, "2026-09-10 08:00:00", "a", "j1", "viability", 0.30)
    _row(jobs_db, "2026-09-11 08:00:00", "a", "j1", "viability", 0.10)   # a rescore
    _row(jobs_db, "2026-09-12 08:00:00", "a", "j1", "apify", 0.05)
    _row(jobs_db, "2026-09-12 08:00:00", "a", None, "apify", 0.01)       # overhead
    a = spend.lens_cost_summary(jobs_db, applied_statuses=APPLIED, now=NOW)["a"]
    assert a["total_usd"] == pytest.approx(0.46)
    assert a["ai_usd"] == pytest.approx(0.40) and a["apify_usd"] == pytest.approx(0.06)
    # The initial/tuning split covers AI only — re-scraping isn't re-deciding.
    assert a["initial_usd"] == pytest.approx(0.30) and a["rescore_usd"] == pytest.approx(0.10)
    # Attributed Apify rides with its job's rating; overhead lands in 'other'.
    assert a["by_viability"]["high"] == pytest.approx(0.45)
    assert a["by_viability"]["other"] == pytest.approx(0.01)
    # Cost per application counts everything, overhead included.
    assert a["cost_per_applied"] == pytest.approx(0.46)


def test_ingest_reports_item_job_ids(jobs_db):
    # ingest() fills the out-param with one entry per item, so the run's charge can be pro-rated.
    import ingest as ing
    items = [{"id": "1", "title": "A", "organization": "Acme", "url": "http://x/1"},
             {"id": "2", "title": "B", "organization": "Acme", "url": "http://x/2"}]
    seen: list = []
    ing.ingest(jobs_db, items, "label", "careersite", fuzzy_dedup=False, item_job_ids=seen)
    assert len(seen) == 2 and all(seen)


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
    rows = jobs_db.execute("SELECT search_id, job_id, feature FROM spend_ledger").fetchall()
    assert [tuple(r) for r in rows] == [("lensA", "j1", "reformat")]


# ── daily_spend_by_lens ───────────────────────────────────────────────────────────────────
def test_daily_spend_zero_fills_and_aligns(jobs_db):
    _row(jobs_db, "2026-09-15 10:00:00", "a", "j1", "viability", 0.5)
    _row(jobs_db, "2026-09-15 11:00:00", "a", "j2", "viability", 0.25)
    _row(jobs_db, "2026-09-17 09:00:00", "b", "j3", "reformat", 1.0)
    _row(jobs_db, "2026-08-01 09:00:00", "a", "j9", "viability", 9.0)   # outside the span
    d = spend.daily_spend_by_lens(jobs_db, days=3, now=NOW)
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
    s = spend.lens_cost_summary(ledger, applied_statuses=APPLIED, now=NOW)
    a = s["a"]
    assert a["total_usd"] == pytest.approx(1.20)                    # additive, rescore included
    assert a["initial_usd"] == pytest.approx(1.00)
    assert a["rescore_usd"] == pytest.approx(0.20)
    assert a["by_feature"]["reformat"] == pytest.approx(0.10)
    assert a["by_viability"] == pytest.approx({"high": 0.50, "medium": 0.0, "low": 0.30, "other": 0.40})
    assert a["first_ts"] == "2026-06-01 08:00:00"
    assert a["calls"] == 5 and a["unpriced_calls"] == 0


def test_summary_denominators_count_only_tracked_jobs(ledger):
    a = spend.lens_cost_summary(ledger, applied_statuses=APPLIED, now=NOW)["a"]
    # 'old' is applied + high but has no ledger spend — excluded from both denominators.
    assert a["applied_jobs"] == 1 and a["cost_per_applied"] == pytest.approx(1.20)
    assert a["high_jobs"] == 1 and a["cost_per_high"] == pytest.approx(1.20)


def test_summary_is_per_lens_and_none_on_zero_denominator(ledger):
    b = spend.lens_cost_summary(ledger, applied_statuses=APPLIED, now=NOW)["b"]
    assert b["total_usd"] == pytest.approx(1.00)
    assert b["applied_jobs"] == 0 and b["cost_per_applied"] is None
    assert b["cost_per_high"] == pytest.approx(1.00)


def test_summary_empty_ledger(jobs_db):
    assert spend.lens_cost_summary(jobs_db, applied_statuses=APPLIED, now=NOW) == {}


def test_summary_counts_unpriced(jobs_db):
    _row(jobs_db, "2026-09-16 08:00:00", "a", "j1", "viability", None)
    a = spend.lens_cost_summary(jobs_db, applied_statuses=APPLIED, now=NOW)["a"]
    assert a["unpriced_calls"] == 1 and a["total_usd"] == 0


# ── lens_cost_summary: trailing window ────────────────────────────────────────────────────
def test_summary_window_excludes_old_spend_and_keeps_rescore_classification(ledger):
    a = spend.lens_cost_summary(ledger, applied_statuses=APPLIED, window_days=30, now=NOW)["a"]
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
    a = spend.lens_cost_summary(jobs_db, applied_statuses=APPLIED, window_days=30, now=NOW)["a"]
    assert a["total_usd"] == 0 and a["cost_per_applied"] is None


# ── rolling_ratio + trend series ──────────────────────────────────────────────────────────
def test_rolling_ratio_sums_windows():
    assert spend.rolling_ratio([1, 2, 3, 4], [1, 0, 1, 0], window=2) == [3.0, 5.0, 7.0]


def test_rolling_ratio_none_without_applications():
    assert spend.rolling_ratio([1, 1, 1], [0, 0, 1], window=2) == [None, 2.0]


def test_rolling_ratio_none_before_full_window_of_tracking():
    # Tracking started at index 1, so the window starting at 0 is partial → None.
    assert spend.rolling_ratio([0, 2, 2], [0, 1, 1], window=2, first_index=1) == [None, 2.0]


def test_trend_series_shape_and_gaps(jobs_db):
    c = jobs_db
    _jss(c, "j1", "a", status="applied", applied_at="2026-09-16 09:00:00")
    _row(c, "2026-09-10 08:00:00", "a", "j1", "viability", 0.6)
    _row(c, "2026-09-16 08:00:00", "a", "j1", "viability", 0.3)
    t = spend.trailing_cost_per_applied_series(c, applied_statuses=APPLIED,
                                                  window_days=3, span_days=5, now=NOW)
    assert t["days"] == ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
    # Windows ending 09-16 and 09-17 contain the application; the 09-16 spend is in both.
    assert t["series"]["a"] == [None, None, None, pytest.approx(0.3), pytest.approx(0.3)]


def test_trend_omits_lens_without_full_window(jobs_db):
    c = jobs_db
    _jss(c, "j1", "a", status="applied", applied_at="2026-09-17 09:00:00")
    _row(c, "2026-09-16 08:00:00", "a", "j1", "viability", 0.3)   # only 2 days of tracking
    t = spend.trailing_cost_per_applied_series(c, applied_statuses=APPLIED,
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
    # Sample ledger: ln_root (interviewing, high) has initial + rescore spend; ln_new_hot is high;
    # plus one pro-rated Apify share and a day of Apify overhead.
    assert lens["ai_usd"] == pytest.approx(0.0166)
    assert lens["apify_usd"] == pytest.approx(0.0028)
    assert lens["total_usd"] == pytest.approx(0.0194)
    assert lens["rescore_usd"] == pytest.approx(0.0035)
    assert lens["applied_jobs"] == 1 and lens["high_jobs"] == 2
