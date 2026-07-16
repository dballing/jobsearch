"""Tests for app._merge_group_into — merging one posting's whole group into another group.

The link/merge picker calls this from any member of the source group, so it must move the
ENTIRE source group (root + all members) to the target root, keep the one-hop/no-chain
invariant, and inherit the target's status for still-early members.
"""
import json

import app


def _insert(db, job_id, canonical_id=None, status="new", applied_at=None):
    db.execute(
        "INSERT INTO jobs (job_id, title, canonical_id, status, applied_at, raw) "
        "VALUES (?, 'T', ?, ?, ?, '{}')",
        (job_id, canonical_id, status, applied_at),
    )


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
            for r in jobs_db.execute("SELECT job_id, status, applied_at FROM jobs").fetchall()}
    assert stat["B"] == ("applied", "2026-06-01 09:00:00")
    assert stat["b1"] == ("applied", "2026-06-01 09:00:00")
    assert stat["b2"][0] == "rejected"          # left alone


def test_merge_logs_history_on_source_root(jobs_db):
    _insert(jobs_db, "A")
    _insert(jobs_db, "B")
    _insert(jobs_db, "b1", canonical_id="B")
    app._merge_group_into(jobs_db, "b1", "A", "t")
    hist = json.loads(jobs_db.execute("SELECT history FROM jobs WHERE job_id='B'").fetchone()["history"])
    linked = [e for e in hist if e["event"] == "linked"]
    assert linked and linked[-1]["canonical_id"] == "A" and "merged group" in linked[-1]["note"]


def test_merge_excludes_target_from_repoint(jobs_db):
    # Degenerate guard: target must never be re-pointed onto itself.
    _insert(jobs_db, "A")
    _insert(jobs_db, "B")
    app._merge_group_into(jobs_db, "B", "A", "t")
    assert _links(jobs_db)["A"] is None
