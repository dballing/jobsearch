"""A grouped header whose members disappear between the two listing queries.

The grouped view runs one query for the headers and a second per group for its members, with
no transaction spanning them. A writer landing in that gap — ingest's auto-ghost/close/reset
flipping a status out of the active filter, or a fuzzy relink re-pointing canonical_id — leaves
a header whose sub-row fetch comes back empty. That used to build a row with no job_id and no
locations_count, and the template died on it ('dict object' has no attribute 'locations_count'),
500-ing the whole page over one vanished group.
"""
import app
import ingest


def _header(group_key="root", n=1):
    """Minimal grouped-header stand-in; location_count == 1 is the single-posting (flattened)
    shape, which is the one that reaches the flat-row template."""
    return {"group_key": group_key, "location_count": n,
            "source": "linkedin", "source_max": "linkedin",
            "salary_min": None, "salary_max": None}


def test_empty_group_still_builds_a_renderable_row():
    """build_grouped_job must stay total: even with no sub-rows it returns every key the
    flat-row template reads unconditionally, so no caller can hand the template a landmine."""
    job = app.build_grouped_job(_header(), [])
    # The key the crash reported, plus the others the same template branch dereferences.
    assert job["locations_count"] == 1
    assert job["locations_all"] == ""
    assert job["job_id"] is None
    assert job["labels"] == []
    assert job["salary_display"] == ""
    assert job["status_color"] == "secondary"


def test_populated_group_is_unaffected():
    """The normal single-posting path must keep taking its values from the member row — the
    empty-group tolerance is a fallback, not a new default."""
    sub = {"job_id": "root", "title": "Eng", "status": "new", "labels": ["x"],
           "salary_display": "$100k", "status_color": "primary", "source_display": "LinkedIn",
           "locations_count": 3, "locations_all": "NYC\nSF\nRemote", "location": "NYC"}
    job = app.build_grouped_job(_header(), [sub])
    assert job["locations_count"] == 3
    assert job["job_id"] == "root"
    assert job["labels"] == ["x"]
    assert job["salary_display"] == "$100k"


def test_build_grouped_jobs_drops_vanished_groups(jobs_db):
    """A header whose members no longer match the filter is dropped entirely: the group has
    nothing to show in this view, so a phantom row would be wrong even if it rendered."""
    jobs_db.execute(
        "INSERT INTO jobs (job_id, title, company, location, first_seen, raw) "
        "VALUES ('live', 'Engineer', 'Acme', 'NYC', '2026-09-01', '{}')")
    # Creates the per-lens table and backfills a __default__ row for the job above, which is
    # what makes 'live' a member of the search the listing queries join against.
    ingest.ensure_job_search_state(jobs_db)
    jobs_db.commit()

    # 'gone' has no row at all — the same end state as a member that got relinked or filtered
    # out after the header query counted it.
    headers = [_header("live"), _header("gone")]
    jobs = app.build_grouped_jobs(jobs_db, headers, "", [])
    assert [j["job_id"] for j in jobs] == ["live"]


def test_index_survives_a_group_emptying_mid_request(sample_app_db, monkeypatch):
    """End-to-end shape of the reported 500: every group's members vanish between the header
    query and the sub-row fetch. The page must still render (empty), not error."""
    monkeypatch.setattr(app, "fetch_sub_rows", lambda *a, **k: [])
    resp = app.app.test_client().get("/?group_match=1")
    assert resp.status_code == 200
