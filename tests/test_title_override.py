"""Manual job-title override (title_actual): the effective title (override wins over the
scraped title) flows through display, search, and scoring, and the endpoint persists it and
flags a rescore. The scoring effect is covered as pure logic in test_viability_message.py;
here we exercise process_job_row, the search clause, and the route."""
import sqlite3

import app


# ── process_job_row: effective title ──────────────────────────────────────────
def test_process_job_row_override_wins_and_keeps_original():
    j = app.process_job_row({"title": "Raw Feed Title", "title_actual": "Corrected Title",
                             "labels": "[]"})
    assert j["title"] == "Corrected Title"          # override wins for display
    assert j["title_original"] == "Raw Feed Title"  # original kept for the "*" tooltip
    assert j["has_title_override"] is True


def test_process_job_row_no_override_uses_feed_title():
    j = app.process_job_row({"title": "Raw Feed Title", "title_actual": None, "labels": "[]"})
    assert j["title"] == "Raw Feed Title"
    assert j["has_title_override"] is False


# ── search matches the override text too ──────────────────────────────────────
def test_search_where_includes_title_actual():
    where, params = app.build_where("", "all", q="widget")
    assert "title_actual" in where                  # an overridden title is findable


# ── endpoint: persist + flag rescore ──────────────────────────────────────────
def test_route_sets_title_override_and_marks_rescore(sample_app_db):
    resp = app.app.test_client().post("/job/cs_review/title_actual",
                                      data={"title_actual": "Real Title"})
    assert resp.status_code == 204
    con = sqlite3.connect(app.DB_PATH)
    row = con.execute("SELECT j.title_actual, s.needs_rescored FROM jobs j "
                      "JOIN job_search_state s ON s.job_id = j.job_id AND s.search_id = '__default__' "
                      "WHERE j.job_id = ?",
                      ("cs_review",)).fetchone()
    con.close()
    assert row[0] == "Real Title"
    assert row[1] == 1                               # title feeds the scorer → rescore


def test_route_blank_clears_override(sample_app_db):
    client = app.app.test_client()
    client.post("/job/cs_review/title_actual", data={"title_actual": "X"})
    resp = client.post("/job/cs_review/title_actual", data={"title_actual": "   "})
    assert resp.status_code == 204
    con = sqlite3.connect(app.DB_PATH)
    val = con.execute("SELECT title_actual FROM jobs WHERE job_id = ?", ("cs_review",)).fetchone()[0]
    con.close()
    assert val is None                              # whitespace-only → cleared


def test_route_unknown_job_is_404(sample_app_db):
    resp = app.app.test_client().post("/job/nope/title_actual", data={"title_actual": "X"})
    assert resp.status_code == 404


def test_single_job_group_flatten_keeps_title_original():
    """A single-job 'group' (grouped view, not a fuzzy group) is flattened to a flat-style
    row that the template renders with the same 'originally: …' override tooltip. That
    tooltip reads job.title_original, so the flatten must carry it through — otherwise a
    grouped-view single job shows an empty 'originally:' even though its '*' is present."""
    sub = app.process_job_row({"job_id": "j1", "title": "Raw Feed Title",
                               "title_actual": "Corrected Title", "labels": "[]",
                               "status": "new"})
    header = {"group_key": "j1", "location_count": 1,
              "source": "linkedin", "source_max": "linkedin",
              "salary_min": None, "salary_max": None}
    job = app.build_grouped_job(header, [sub])
    assert job["multi"] is False                       # single job → flattened row
    assert job["has_title_override"] is True
    assert job["title"] == "Corrected Title"           # effective title shown
    assert job["title_original"] == "Raw Feed Title"   # original kept for the "*" tooltip


def test_grouped_view_single_job_shows_original_in_tooltip(sample_app_db):
    """End-to-end in grouped view: an overridden single job's '*' tooltip names the original
    scraped title (regression — the grouped flatten previously dropped title_original)."""
    client = app.app.test_client()
    client.post("/job/cs_review/title_actual", data={"title_actual": "Overridden Title XYZ"})
    html = client.get("/?status_filter=all&group_match=1&per_page=200").get_data(as_text=True)
    assert "Overridden Title XYZ" in html
    assert "originally: Photocopier Repair Technician" in html


def test_override_renders_effective_title_and_original_tooltip(sample_app_db):
    """End-to-end: after overriding, the table shows the effective title and keeps the
    original in the '*' tooltip."""
    client = app.app.test_client()
    client.post("/job/cs_review/title_actual", data={"title_actual": "Overridden Title XYZ"})
    html = client.get("/?status_filter=all&group_match=0&per_page=200").get_data(as_text=True)
    assert "Overridden Title XYZ" in html                        # effective title shown
    assert "Photocopier Repair Technician" in html               # original, in the tooltip
    assert "originally: Photocopier Repair Technician" in html
