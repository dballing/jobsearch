"""Historical repricing of the spend ledger (reprice.py).

``cost_usd`` is a cache of tokens × rates, so a price we had wrong or learned about late is
repairable — provided the pricing file records what the rate was at the time. These cover the
row selection and the plan, which is where the judgment lives; the CLI wrapper around them is
thin.
"""
import json
import sqlite3

import pytest

import ai_config
import reprice
from spend import SCHEMA


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """An in-memory ledger plus an isolated pricing override file."""
    monkeypatch.setenv(ai_config.LOCAL_PRICING_ENV, str(tmp_path / "pricing.json"))
    monkeypatch.setattr(ai_config, "_pricing_cache", None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn, tmp_path / "pricing.json"
    monkeypatch.setattr(ai_config, "_pricing_cache", None)


def add(conn, *, ts, model, feature="viability", search_id="s1", tokens=1_000_000, cost=0.0):
    conn.execute(
        "INSERT INTO spend_ledger (ts, search_id, job_id, feature, model, input_tokens, "
        "output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) "
        "VALUES (?, ?, 'j1', ?, ?, ?, 0, 0, 0, ?)",
        (ts, search_id, feature, model, tokens, cost))
    conn.commit()


def price(path, models):
    path.write_text(json.dumps({"models": models}))
    ai_config._pricing_cache = None


def test_apify_rows_are_never_repriced(ledger):
    """Apify rows carry a run charge that was never derived from tokens, so there is nothing to
    recompute — and recomputing would zero them, since their 'model' is a task name."""
    conn, path = ledger
    add(conn, ts="2026-09-20 10:00:00", model="claude-sonnet-5")
    add(conn, ts="2026-09-20 10:00:00", model="derek-linkedin-tpm", feature="apify", cost=4.2)
    rows = reprice.rows_to_reprice(conn)
    assert [r["model"] for r in rows] == ["claude-sonnet-5"]


def test_reprices_each_row_at_the_rate_effective_at_its_own_timestamp(ledger):
    """The whole point of the temporal table: one run repairs a mixed-era ledger correctly,
    without a cutoff flag supplied by hand."""
    conn, path = ledger
    add(conn, ts="2026-09-20 10:00:00", model="m-1", cost=2.00)   # old era, wrongly priced
    add(conn, ts="2026-09-25 10:00:00", model="m-1", cost=2.00)   # new era, already right
    price(path, {"m-1": [
        {"input": 3.00, "output": 15.00, "effective_until": "2026-09-23"},
        {"input": 2.00, "output": 10.00, "effective_start": "2026-09-23"},
    ]})
    rows = reprice.rows_to_reprice(conn)
    updates, uncovered = reprice.plan_reprice(rows)
    assert uncovered == {}
    assert len(updates) == 1                      # only the pre-cut row moves
    assert updates[0][1] == pytest.approx(3.00)


def test_rows_outside_every_period_are_reported_not_silently_skipped(ledger):
    """'We don't know what this cost' is the same failure as not knowing today's price. Leaving
    them alone would quietly ship a half-repriced ledger."""
    conn, path = ledger
    add(conn, ts="2026-08-01 10:00:00", model="m-1")
    add(conn, ts="2026-09-25 10:00:00", model="m-1")
    price(path, {"m-1": {"input": 2.00, "output": 10.00, "effective_start": "2026-09-01"}})
    updates, uncovered = reprice.plan_reprice(reprice.rows_to_reprice(conn))
    assert uncovered == {("m-1", "2026-08"): 1}   # grouped per model-month, so the hole is obvious
    assert len(updates) == 1


def test_already_correct_rows_produce_no_updates(ledger):
    """A no-op reprice must write nothing — float noise shouldn't churn thousands of rows."""
    conn, path = ledger
    price(path, {"m-1": {"input": 2.00, "output": 10.00}})
    add(conn, ts="2026-09-20 10:00:00", model="m-1", cost=2.00)
    updates, uncovered = reprice.plan_reprice(reprice.rows_to_reprice(conn))
    assert updates == [] and uncovered == {}


def test_null_cost_rows_are_repriced(ledger):
    """Rows written while the model was unpriced carry NULL; they're exactly what repricing is
    for once a price arrives."""
    conn, path = ledger
    conn.execute("INSERT INTO spend_ledger (ts, search_id, job_id, feature, model, input_tokens, "
                 "output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) "
                 "VALUES ('2026-09-20 10:00:00', 's1', 'j1', 'viability', 'm-1', 1000000, 0, 0, 0, NULL)")
    conn.commit()
    price(path, {"m-1": {"input": 2.00, "output": 10.00}})
    updates, _ = reprice.plan_reprice(reprice.rows_to_reprice(conn))
    assert len(updates) == 1 and updates[0][1] == pytest.approx(2.00)


def test_all_four_token_kinds_contribute(ledger):
    conn, path = ledger
    conn.execute("INSERT INTO spend_ledger (ts, search_id, job_id, feature, model, input_tokens, "
                 "output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) "
                 "VALUES ('2026-09-20 10:00:00', 's1', 'j1', 'viability', 'm-1', "
                 "1000000, 1000000, 1000000, 1000000, 0.0)")
    conn.commit()
    price(path, {"m-1": {"input": 2.00, "output": 10.00}})   # write 1.25x, read 0.10x
    updates, _ = reprice.plan_reprice(reprice.rows_to_reprice(conn))
    assert updates[0][1] == pytest.approx(2.00 + 10.00 + 2.50 + 0.20)


def test_filters_narrow_the_rows(ledger):
    conn, path = ledger
    add(conn, ts="2026-09-20 10:00:00", model="m-1", search_id="s1")
    add(conn, ts="2026-09-20 10:00:00", model="m-2", search_id="s2")
    assert len(reprice.rows_to_reprice(conn)) == 2
    assert [r["model"] for r in reprice.rows_to_reprice(conn, model="m-1")] == ["m-1"]
    assert [r["search_id"] for r in reprice.rows_to_reprice(conn, search_id="s2")] == ["s2"]


def test_summary_reports_before_and_after_per_lens_and_model(ledger):
    conn, path = ledger
    add(conn, ts="2026-09-20 10:00:00", model="m-1", search_id="s1", cost=2.00)
    price(path, {"m-1": {"input": 3.00, "output": 15.00}})
    rows = reprice.rows_to_reprice(conn)
    updates, _ = reprice.plan_reprice(rows)
    line = "\n".join(reprice.summarize(rows, updates))
    assert "s1" in line and "m-1" in line and "2.0000" in line and "3.0000" in line
