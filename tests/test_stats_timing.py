"""Tests for app.transition_time_stats — application-pipeline timing from status histories.

Pure timeline math (parse status events, pick endpoints, average the deltas), so it's worth
locking down: an off-by-one in endpoint selection or a swallowed unordered pair would quietly
skew the numbers a job-seeker reads as fact.
"""
import app


def _st(to, ts):
    return {"event": "status", "to": to, "ts": ts}


def test_applied_to_interviewing():
    hist = [_st("applied", "2026-01-01T00:00:00Z"), _st("interviewing", "2026-01-04T00:00:00Z")]
    r = app.transition_time_stats([hist])
    assert r["applied_to_interviewing"] == {"avg_days": 3.0, "n": 1}


def test_interviewing_to_outcome_uses_first_outcome():
    hist = [_st("applied", "2026-01-01T00:00:00Z"),
            _st("interviewing", "2026-01-02T00:00:00Z"),
            _st("offered", "2026-01-04T00:00:00Z")]
    r = app.transition_time_stats([hist])
    assert r["interviewing_to_outcome"] == {"avg_days": 2.0, "n": 1}


def test_applied_to_rejected():
    hist = [_st("applied", "2026-02-01T00:00:00Z"), _st("rejected", "2026-02-06T00:00:00Z")]
    r = app.transition_time_stats([hist])
    assert r["applied_to_rejected"] == {"avg_days": 5.0, "n": 1}


def test_average_across_jobs():
    h1 = [_st("applied", "2026-01-01T00:00:00Z"), _st("interviewing", "2026-01-03T00:00:00Z")]  # 2d
    h2 = [_st("applied", "2026-01-01T00:00:00Z"), _st("interviewing", "2026-01-05T00:00:00Z")]  # 4d
    r = app.transition_time_stats([h1, h2])
    assert r["applied_to_interviewing"] == {"avg_days": 3.0, "n": 2}


def test_missing_endpoint_not_counted():
    # Applied but never interviewed → contributes to nothing but leaves n=0 averages None.
    r = app.transition_time_stats([[_st("applied", "2026-01-01T00:00:00Z")]])
    assert r["applied_to_interviewing"] == {"avg_days": None, "n": 0}
    assert r["applied_to_rejected"] == {"avg_days": None, "n": 0}
    assert r["interviewing_to_outcome"] == {"avg_days": None, "n": 0}


def test_out_of_order_pair_ignored():
    # interviewing recorded *before* applied (data oddity) must not produce a negative delta.
    hist = [_st("interviewing", "2026-01-01T00:00:00Z"), _st("applied", "2026-01-03T00:00:00Z")]
    r = app.transition_time_stats([hist])
    assert r["applied_to_interviewing"]["n"] == 0


def test_non_status_and_bad_ts_events_ignored():
    hist = [{"event": "viability", "rating": "high", "ts": "2026-01-01T00:00:00Z"},
            _st("applied", "not-a-date"),          # unparseable → dropped
            _st("applied", "2026-01-01T00:00:00Z"),
            _st("rejected", "2026-01-01 00:00:00")]  # space form, same day → 0 days
    r = app.transition_time_stats([hist])
    assert r["applied_to_rejected"] == {"avg_days": 0.0, "n": 1}


def test_empty_input():
    r = app.transition_time_stats([])
    assert all(v == {"avg_days": None, "n": 0} for v in r.values())


def test_fractional_days():
    hist = [_st("applied", "2026-01-01T00:00:00Z"), _st("interviewing", "2026-01-01T12:00:00Z")]
    r = app.transition_time_stats([hist])
    assert r["applied_to_interviewing"] == {"avg_days": 0.5, "n": 1}


def test_stats_route_returns_pipeline_keys():
    # Smoke test: the /stats route wires the new fields without erroring on an empty DB.
    resp = app.app.test_client().get("/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "transition_times" in data and "outcome_split" in data
    assert set(data["transition_times"]) == {
        "applied_to_interviewing", "interviewing_to_outcome", "applied_to_rejected"}
    assert set(data["outcome_split"]) == {"rejected", "ghosted", "polite_pct"}


# ── viability_day_series: aligned per-day high/medium/low arrays ───────────────
def test_viability_day_series_aligns_and_fills_zeros():
    rows = [
        ("2026-01-02", "high", 3), ("2026-01-02", "low", 1),   # no medium this day → 0
        ("2026-01-01", "medium", 5),                            # only medium this day
    ]
    s = app.viability_day_series(rows)
    assert s["days"] == ["2026-01-01", "2026-01-02"]            # sorted ascending
    assert s["high"]   == [0, 3]
    assert s["medium"] == [5, 0]
    assert s["low"]    == [0, 1]


def test_viability_day_series_empty():
    assert app.viability_day_series([]) == {"days": [], "high": [], "medium": [], "low": []}


def test_viability_by_day_route_smoke():
    resp = app.app.test_client().get("/stats/viability_by_day")
    assert resp.status_code == 200
    assert set(resp.get_json()) == {"days", "high", "medium", "low"}


def test_viability_by_day_label_filter():
    # Seed two freshly-ingested scored jobs on different labels, then confirm ?label= narrows.
    import os
    import sqlite3
    con = sqlite3.connect(os.environ["JOBSEARCH_DB"])
    con.executemany(
        "INSERT INTO jobs (job_id, viability, labels, raw) VALUES (?, ?, ?, '{}')",
        [("vt_nc", "high", '["nc"]'), ("vt_sc", "low", '["sc"]')])
    con.commit(); con.close()
    try:
        cl = app.app.test_client()
        allv = cl.get("/stats/viability_by_day").get_json()
        ncv  = cl.get("/stats/viability_by_day?label=nc").get_json()
        assert sum(allv["high"]) >= 1 and sum(allv["low"]) >= 1   # unfiltered sees both
        assert sum(ncv["high"]) >= 1 and sum(ncv["low"]) == 0     # nc filter: only the high nc job
    finally:
        con = sqlite3.connect(os.environ["JOBSEARCH_DB"])
        con.execute("DELETE FROM jobs WHERE job_id IN ('vt_nc', 'vt_sc')")
        con.commit(); con.close()
