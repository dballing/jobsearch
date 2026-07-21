"""Work-arrangement override endpoint: valid values (incl. the manual geo-POOR flag) are
stored and flag the job for rescoring; invalid values are rejected. The scoring effect of
the flag is covered as pure logic in test_viability_message.py (clamp / is_manual_geo_poor);
here we only exercise the route's accept/reject + persistence, since the score itself needs
a live AI call."""
import sqlite3

import app
import viability


def _post_arrangement(job_id: str, value: str):
    return app.app.test_client().post(
        f"/job/{job_id}/work_arrangement", data={"work_arrangement": value})


def test_unsupported_location_flag_is_a_valid_option():
    """The manual geo-POOR sentinel rides the same dropdown/validation set as the real
    work arrangements, so the endpoint accepts it."""
    assert viability.GEO_UNSUPPORTED_ARRANGEMENT in app.WORK_ARRANGEMENTS


def test_route_stores_manual_flag_and_marks_rescore(sample_app_db):
    """POSTing the flag persists it verbatim and sets needs_rescored so the next rescore
    re-evaluates the job (and clamps it low)."""
    resp = _post_arrangement("cs_review", viability.GEO_UNSUPPORTED_ARRANGEMENT)
    assert resp.status_code == 204

    con = sqlite3.connect(app.DB_PATH)
    row = con.execute(
        "SELECT work_arrangement_actual, needs_rescored FROM jobs WHERE job_id = ?",
        ("cs_review",)).fetchone()
    con.close()
    assert row[0] == viability.GEO_UNSUPPORTED_ARRANGEMENT
    assert row[1] == 1


def test_route_rejects_unknown_arrangement(sample_app_db):
    """A value outside WORK_ARRANGEMENTS is a 400 — the dropdown is the only source of
    truth, so free-text can't slip a bogus arrangement into scoring."""
    resp = _post_arrangement("cs_review", "Remote on the Moon")
    assert resp.status_code == 400
