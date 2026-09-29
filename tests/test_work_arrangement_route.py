"""Work-arrangement override endpoint: valid values (incl. the manual geo-POOR flags) are
stored and flag the job for rescoring; invalid values are rejected. The scoring effect of
the flags is covered as pure logic in test_viability_message.py (clamp / manual_geo_poor_flag);
here we only exercise the route's accept/reject + persistence, since the score itself needs
a live AI call."""
import sqlite3

import pytest

import app
import viability


def _post_arrangement(job_id: str, value: str):
    return app.app.test_client().post(
        f"/job/{job_id}/work_arrangement", data={"work_arrangement": value})


def test_manual_geo_poor_flags_are_valid_options():
    """Both manual geo-POOR sentinels ride the same dropdown/validation set as the real work
    arrangements, so the endpoint accepts them."""
    assert viability.GEO_UNSUPPORTED_ARRANGEMENT in app.WORK_ARRANGEMENTS
    assert viability.GEO_BAD_FEED_LOCATION in app.WORK_ARRANGEMENTS


@pytest.mark.parametrize("flag", [viability.GEO_UNSUPPORTED_ARRANGEMENT,
                                  viability.GEO_BAD_FEED_LOCATION])
def test_route_stores_manual_flag_and_marks_rescore(sample_app_db, flag):
    """POSTing either flag persists it verbatim and sets needs_rescored so the next rescore
    re-evaluates the job (and clamps it low)."""
    resp = _post_arrangement("cs_review", flag)
    assert resp.status_code == 204

    con = sqlite3.connect(app.DB_PATH)
    row = con.execute(
        "SELECT j.work_arrangement_actual, s.needs_rescored FROM jobs j "
        "JOIN job_search_state s ON s.job_id = j.job_id AND s.search_id = '__default__' "
        "WHERE j.job_id = ?",
        ("cs_review",)).fetchone()
    con.close()
    assert row[0] == flag
    assert row[1] == 1


def test_route_rejects_unknown_arrangement(sample_app_db):
    """A value outside WORK_ARRANGEMENTS is a 400 — the dropdown is the only source of
    truth, so free-text can't slip a bogus arrangement into scoring."""
    resp = _post_arrangement("cs_review", "Remote on the Moon")
    assert resp.status_code == 400
