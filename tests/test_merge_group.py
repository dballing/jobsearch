"""Tests for app._merge_group_into — merging one posting's whole group into another group.

The link/merge picker calls this from any member of the source group, so it must move the
ENTIRE source group (root + all members) to the target root, keep the one-hop/no-chain
invariant, and inherit the target's status for still-early members. The opt-in
`adopt_company` rename (app.adopt_company_from_root, the modal's checkbox) is covered at the
bottom, including the route that drives it.
"""
import json
import sqlite3

import app


_SID = "__default__"


def _insert(db, job_id, canonical_id=None, status="new", applied_at=None,
            company=None, company_actual=None):
    db.execute(
        "INSERT INTO jobs (job_id, title, canonical_id, company, company_actual, raw) "
        "VALUES (?, 'T', ?, ?, ?, '{}')",
        (job_id, canonical_id, company, company_actual),
    )
    # Per-lens status/applied_at/history live on the __default__ state row now.
    db.execute(
        "INSERT INTO job_search_state (job_id, search_id, status, applied_at) VALUES (?, ?, ?, ?)",
        (job_id, _SID, status, applied_at),
    )


def _companies(db):
    return {r["job_id"]: (r["company"], r["company_actual"])
            for r in db.execute("SELECT job_id, company, company_actual FROM jobs").fetchall()}


def _links(db):
    return {r["job_id"]: r["canonical_id"]
            for r in db.execute("SELECT job_id, canonical_id FROM jobs").fetchall()}


def test_merge_moves_whole_source_group_from_a_member(jobs_db):
    # Group A (root A + a1, a2) and group B (root B + b1, b2). Merge from a MEMBER of B.
    for j in ("A", "a1", "a2"):
        _insert(jobs_db, j, canonical_id=None if j == "A" else "A")
    for j in ("B", "b1", "b2"):
        _insert(jobs_db, j, canonical_id=None if j == "B" else "B")

    moved = app._merge_group_into(jobs_db, "b1", "A", "2026-07-16T00:00:00Z")
    assert moved == 3                         # B, b1, b2

    links = _links(jobs_db)
    assert links["A"] is None                 # target stays the sole root
    for j in ("B", "b1", "b2", "a1", "a2"):
        assert links[j] == "A"


def test_merge_from_the_source_root_also_works(jobs_db):
    _insert(jobs_db, "A")
    _insert(jobs_db, "B")
    _insert(jobs_db, "b1", canonical_id="B")
    moved = app._merge_group_into(jobs_db, "B", "A", "t")
    assert moved == 2
    links = _links(jobs_db)
    assert links["A"] is None and links["B"] == "A" and links["b1"] == "A"


def test_merge_preserves_single_root_no_chain(jobs_db):
    for j in ("A", "a1"):
        _insert(jobs_db, j, canonical_id=None if j == "A" else "A")
    for j in ("B", "b1", "b2"):
        _insert(jobs_db, j, canonical_id=None if j == "B" else "B")
    app._merge_group_into(jobs_db, "b2", "A", "t")
    links = _links(jobs_db)
    roots = [j for j, c in links.items() if c is None]
    assert roots == ["A"]                      # exactly one root
    for j, c in links.items():                 # every non-root points straight at a root
        if c is not None:
            assert links[c] is None


def test_merge_inherits_status_for_early_members(jobs_db):
    # Target root is 'applied'; the merged group's new/reviewing members inherit applied+date.
    _insert(jobs_db, "A", status="applied", applied_at="2026-06-01 09:00:00")
    _insert(jobs_db, "B", status="new")
    _insert(jobs_db, "b1", canonical_id="B", status="reviewing")
    _insert(jobs_db, "b2", canonical_id="B", status="rejected")   # terminal → NOT overwritten
    app._merge_group_into(jobs_db, "B", "A", "t")
    stat = {r["job_id"]: (r["status"], r["applied_at"])
            for r in jobs_db.execute(
                "SELECT job_id, status, applied_at FROM job_search_state").fetchall()}
    assert stat["B"] == ("applied", "2026-06-01 09:00:00")
    assert stat["b1"] == ("applied", "2026-06-01 09:00:00")
    assert stat["b2"][0] == "rejected"          # left alone


def test_merge_logs_history_on_source_root(jobs_db):
    _insert(jobs_db, "A")
    _insert(jobs_db, "B")
    _insert(jobs_db, "b1", canonical_id="B")
    app._merge_group_into(jobs_db, "b1", "A", "t")
    hist = json.loads(jobs_db.execute(
        "SELECT history FROM job_search_state WHERE job_id='B'").fetchone()["history"])
    linked = [e for e in hist if e["event"] == "linked"]
    assert linked and linked[-1]["canonical_id"] == "A" and "merged group" in linked[-1]["note"]


def test_merge_excludes_target_from_repoint(jobs_db):
    # Degenerate guard: target must never be re-pointed onto itself.
    _insert(jobs_db, "A")
    _insert(jobs_db, "B")
    app._merge_group_into(jobs_db, "B", "A", "t")
    assert _links(jobs_db)["A"] is None


# ── adopt_company: rename the moved postings to the target root's employer ─────
def test_adopt_company_overrides_moved_postings_only(jobs_db):
    _insert(jobs_db, "A", company="Foo, Inc.")
    _insert(jobs_db, "B", company="Headhunter Site")
    _insert(jobs_db, "b1", canonical_id="B", company="Other Recruiter")
    company, changed = app.adopt_company_from_root(jobs_db, ["B", "b1"], "A", "t")
    assert company == "Foo, Inc." and set(changed) == {"B", "b1"}
    comps = _companies(jobs_db)
    assert comps["B"] == ("Headhunter Site", "Foo, Inc.")   # feed value kept, override added
    assert comps["b1"] == ("Other Recruiter", "Foo, Inc.")
    assert comps["A"] == ("Foo, Inc.", None)                # the root itself is untouched


def test_adopt_company_prefers_the_roots_own_override(jobs_db):
    # The root may itself be an aggregator posting corrected by hand — adopt its *effective* name.
    _insert(jobs_db, "A", company="Jobgether", company_actual="Foo, Inc.")
    _insert(jobs_db, "B", company="Headhunter Site")
    company, changed = app.adopt_company_from_root(jobs_db, ["B"], "A", "t")
    assert company == "Foo, Inc." and changed == ["B"]
    assert _companies(jobs_db)["B"][1] == "Foo, Inc."


def test_adopt_company_skips_postings_already_naming_the_employer(jobs_db):
    # No point writing an override that says what the feed already says (case/space-insensitive).
    _insert(jobs_db, "A", company="Foo, Inc.")
    _insert(jobs_db, "B", company=" foo, inc. ")
    company, changed = app.adopt_company_from_root(jobs_db, ["B"], "A", "t")
    assert company == "Foo, Inc." and changed == []
    assert _companies(jobs_db)["B"][1] is None


def test_adopt_company_noop_when_root_has_no_name(jobs_db):
    _insert(jobs_db, "A", company=None)
    _insert(jobs_db, "B", company="Headhunter Site")
    assert app.adopt_company_from_root(jobs_db, ["B"], "A", "t") == (None, [])
    assert _companies(jobs_db)["B"][1] is None


def test_adopt_company_flags_rescore_and_logs_history(jobs_db):
    _insert(jobs_db, "A", company="Foo, Inc.")
    _insert(jobs_db, "B", company="Headhunter Site")
    app.adopt_company_from_root(jobs_db, ["B"], "A", "t")
    row = jobs_db.execute("SELECT needs_rescored, history FROM job_search_state "
                          "WHERE job_id = 'B'").fetchone()
    assert row["needs_rescored"] == 1               # employer feeds the scorer
    ev = [e for e in json.loads(row["history"]) if e["event"] == "company_actual"]
    assert ev and ev[-1]["to"] == "Foo, Inc." and ev[-1]["from"] is None
    assert "adopted from canonical" in ev[-1]["note"]


def test_merge_adopts_company_only_when_asked(jobs_db):
    for flag, expected in ((False, None), (True, "Foo, Inc.")):
        jobs_db.execute("DELETE FROM jobs")
        jobs_db.execute("DELETE FROM job_search_state")
        _insert(jobs_db, "A", company="Foo, Inc.")
        _insert(jobs_db, "B", company="Headhunter Site")
        _insert(jobs_db, "b1", canonical_id="B", company="Headhunter Site")
        app._merge_group_into(jobs_db, "b1", "A", "t", _SID, adopt_company=flag)
        comps = _companies(jobs_db)
        assert comps["B"][1] == expected and comps["b1"][1] == expected


# ── the route wiring (checkbox → form field → rename) ─────────────────────────
def _company_actual(job_id):
    con = sqlite3.connect(app.DB_PATH)
    val = con.execute("SELECT company_actual FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
    con.close()
    return val


def test_link_route_adopts_company_with_the_checkbox(sample_app_db):
    # manual_recruiter ("Tyrell Corp") merged into the ln_root group ("Acme Corp").
    resp = app.app.test_client().post(
        "/job/manual_recruiter/link", data={"canonical_id": "ln_root", "adopt_company": "1"})
    assert resp.status_code == 200
    assert _company_actual("manual_recruiter") == "Acme Corp"


def test_link_route_leaves_company_alone_by_default(sample_app_db):
    resp = app.app.test_client().post(
        "/job/manual_recruiter/link", data={"canonical_id": "ln_root"})
    assert resp.status_code == 200
    assert _company_actual("manual_recruiter") is None
