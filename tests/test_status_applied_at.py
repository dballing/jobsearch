"""applied_at management on a status change (both status routes share _APPLIED_AT_CASE_SQL).

The rule: moving into an "early" status (no application yet) clears applied_at; moving into an
applied-family status stamps now() *when the date is empty* — from ANY prior status — but never
overwrites an existing date. This closes the gap where a job that reached the applied family by
a path that never stamped (e.g. new→interviewing directly, or an applied step after the date was
cleared) would sit at applied_at=NULL forever. Regression: a job manually created as
'interviewing', toggled through new/interviewing, then set to 'applied' from 'interviewing' —
the last transition previously left applied_at NULL because 'interviewing' isn't an early status.
"""
import sqlite3

import app


def _set(job_id, status, applied_at):
    """Force a job's status + applied_at directly, to seed a starting state for a transition.
    Status/applied_at are per-lens now — seed the __default__ state row."""
    con = sqlite3.connect(app.DB_PATH)
    con.execute("UPDATE job_search_state SET status = ?, applied_at = ? "
                "WHERE job_id = ? AND search_id = '__default__'",
                (status, applied_at, job_id))
    con.commit(); con.close()


def _applied_at(job_id):
    con = sqlite3.connect(app.DB_PATH)
    v = con.execute("SELECT applied_at FROM job_search_state "
                    "WHERE job_id = ? AND search_id = '__default__'", (job_id,)).fetchone()[0]
    con.close()
    return v


def _transition(job_id, status):
    return app.app.test_client().post(f"/job/{job_id}/status", data={"status": status})


# ── the reported bug: →applied from a non-early status with no date ───────────
def test_applied_from_interviewing_stamps_when_empty(sample_app_db):
    """interviewing (no date) → applied stamps now(). Previously this fell through to ELSE
    because 'interviewing' isn't an early status, leaving applied_at NULL."""
    _set("cs_review", "interviewing", None)
    assert _transition("cs_review", "applied").status_code == 204
    assert _applied_at("cs_review") is not None


# ── the root cause: entering the family by a non-stamping path ────────────────
def test_new_to_interviewing_fills_empty_date(sample_app_db):
    """new → interviewing directly stamps now() (you can't interview without applying), so the
    job never sits in the applied family with a NULL date."""
    _set("cs_review", "new", None)
    assert _transition("cs_review", "interviewing").status_code == 204
    assert _applied_at("cs_review") is not None


# ── never overwrite a genuine application date ────────────────────────────────
def test_family_transition_does_not_overwrite_existing_date(sample_app_db):
    """A real applied date survives a back-and-forth: applied→interviewing→applied keeps it."""
    _set("cs_review", "applied", "2026-07-01 09:00:00")
    _transition("cs_review", "interviewing")
    assert _applied_at("cs_review") == "2026-07-01 09:00:00"   # kept through interviewing
    _transition("cs_review", "applied")
    assert _applied_at("cs_review") == "2026-07-01 09:00:00"   # not re-stamped to now()


# ── early statuses clear the date ─────────────────────────────────────────────
def test_early_status_clears_date(sample_app_db):
    _set("cs_review", "applied", "2026-07-01 09:00:00")
    assert _transition("cs_review", "new").status_code == 204
    assert _applied_at("cs_review") is None


def test_full_reported_sequence_ends_with_a_date(sample_app_db):
    """The exact history that surfaced the bug: created 'interviewing' (with a date), then
    interviewing→new→interviewing→applied→interviewing. It must not end at NULL."""
    _set("cs_review", "interviewing", "2026-07-01 09:00:00")  # manual-created-as-interviewing state
    for s in ["new", "interviewing", "applied", "interviewing"]:
        _transition("cs_review", s)
    assert _applied_at("cs_review") is not None


# ── the bulk route shares the same rule ───────────────────────────────────────
def test_bulk_route_stamps_empty_date_on_family(sample_app_db):
    _set("cs_review", "interviewing", None)
    resp = app.app.test_client().post("/jobs/status",
                                      data={"status": "applied", "job_ids": "cs_review"})
    assert resp.status_code == 204
    assert _applied_at("cs_review") is not None
