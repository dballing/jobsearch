"""Per-lens salary-currency override (job_search_state.salary_currency_actual): the candidate
correcting a feed that stamped a €/£ band as USD (or any mislabeled currency). Covered here as
the write path (the /salary_actual endpoint, which now carries an optional currency independent
of the min/max band) plus its precedence/display via app.effective_currency + format_salary
(unit-tested in test_helpers). The score itself needs a live AI call, so it isn't exercised."""
import json
import sqlite3

import app


def _post_salary(job_id: str, **fields):
    return app.app.test_client().post(f"/job/{job_id}/salary_actual", data=fields)


def _jss(job_id: str):
    con = sqlite3.connect(app.DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT salary_min_actual, salary_max_actual, salary_currency_actual, "
        "needs_rescored, history FROM job_search_state "
        "WHERE job_id = ? AND search_id = '__default__'", (job_id,)
    ).fetchone()
    con.close()
    return row


def test_currency_only_override_stored_and_flags_rescore(sample_app_db):
    """A currency correction with no band still records — the numbers stay from the feed
    (min/max_actual NULL), the currency override lands, and it flags a rescore (the scorer
    now sees the currency)."""
    resp = _post_salary("cs_review", salary_min="", salary_max="", salary_currency="EUR")
    assert resp.status_code == 204
    row = _jss("cs_review")
    assert row["salary_currency_actual"] == "EUR"
    assert row["salary_min_actual"] is None and row["salary_max_actual"] is None
    assert row["needs_rescored"] == 1


def test_band_and_currency_override_together(sample_app_db):
    resp = _post_salary("cs_review", salary_min="90000", salary_max="110000", salary_currency="gbp")
    assert resp.status_code == 204
    row = _jss("cs_review")
    assert (row["salary_min_actual"], row["salary_max_actual"]) == (90000, 110000)
    assert row["salary_currency_actual"] == "GBP"   # normalized to upper-case


def test_blank_currency_clears_override(sample_app_db):
    client = app.app.test_client()
    client.post("/job/cs_review/salary_actual",
                data={"salary_min": "", "salary_max": "", "salary_currency": "EUR"})
    resp = client.post("/job/cs_review/salary_actual",
                       data={"salary_min": "", "salary_max": "", "salary_currency": ""})
    assert resp.status_code == 204
    assert _jss("cs_review")["salary_currency_actual"] is None


def test_unknown_currency_rejected(sample_app_db):
    """Only a renderable currency code is accepted, so the display always has a glyph and no
    stray code can be stored."""
    resp = _post_salary("cs_review", salary_min="", salary_max="", salary_currency="XYZ")
    assert resp.status_code == 400
    assert _jss("cs_review")["salary_currency_actual"] is None


def test_history_records_currency_change(sample_app_db):
    _post_salary("cs_review", salary_min="", salary_max="", salary_currency="EUR")
    events = [e for e in json.loads(_jss("cs_review")["history"]) if e.get("event") == "salary_actual"]
    assert events and events[-1]["cur_to"] == "EUR" and events[-1]["cur_from"] is None
