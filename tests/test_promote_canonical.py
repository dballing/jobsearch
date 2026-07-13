"""Tests for app.promote_to_canonical — making a grouped member the group's root.

The re-pointing must preserve the one-hop / no-chain invariant (exactly one root with
canonical_id IS NULL; every other member points straight at it) and log the change, so
it's worth exercising against a real (in-memory) schema rather than trusting the SQL.
"""
import json

import app


def _insert(db, job_id, canonical_id=None, title="T"):
    db.execute(
        "INSERT INTO jobs (job_id, title, canonical_id, raw) VALUES (?, ?, ?, '{}')",
        (job_id, title, canonical_id),
    )


def _group(db, root):
    """Return {job_id: canonical_id} for the whole group rooted (originally) at `root`."""
    rows = db.execute(
        "SELECT job_id, canonical_id FROM jobs "
        "WHERE job_id = ? OR canonical_id = ? "
        "OR canonical_id IN (SELECT job_id FROM jobs WHERE canonical_id = ?)",
        (root, root, root),
    ).fetchall()
    return {r["job_id"]: r["canonical_id"] for r in rows}


def _history_events(db, job_id):
    row = db.execute("SELECT history FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return [e["event"] for e in json.loads(row["history"])]


def test_promote_member_repoints_whole_group(jobs_db):
    # Group: root R with members A, B, X (the one we promote).
    _insert(jobs_db, "R")
    for m in ("A", "B", "X"):
        _insert(jobs_db, m, canonical_id="R")

    ok, old_root = app.promote_to_canonical(jobs_db, "X", "2026-07-13T00:00:00Z")
    assert ok and old_root == "R"

    links = {r["job_id"]: r["canonical_id"]
             for r in jobs_db.execute("SELECT job_id, canonical_id FROM jobs").fetchall()}
    assert links["X"] is None                       # X is now the root
    assert links["R"] == "X"                         # former root re-pointed at X
    assert links["A"] == "X" and links["B"] == "X"   # siblings re-pointed at X


def test_promote_preserves_single_root_no_chain(jobs_db):
    _insert(jobs_db, "R")
    for m in ("A", "B", "X"):
        _insert(jobs_db, m, canonical_id="R")
    app.promote_to_canonical(jobs_db, "X", "2026-07-13T00:00:00Z")

    all_links = {r["job_id"]: r["canonical_id"]
                 for r in jobs_db.execute("SELECT job_id, canonical_id FROM jobs").fetchall()}
    roots = [j for j, c in all_links.items() if c is None]
    assert roots == ["X"]                            # exactly one root
    # No chains: every non-root points directly at the root (a root itself).
    for j, c in all_links.items():
        if c is not None:
            assert all_links[c] is None


def test_promote_logs_history_on_both_ends(jobs_db):
    _insert(jobs_db, "R")
    _insert(jobs_db, "X", canonical_id="R")
    app.promote_to_canonical(jobs_db, "X", "2026-07-13T00:00:00Z")
    assert "promoted_canonical" in _history_events(jobs_db, "X")
    assert "canonical_demoted" in _history_events(jobs_db, "R")


def test_promote_two_job_group(jobs_db):
    # Minimal group: just root + one member. Promoting the member swaps the roles.
    _insert(jobs_db, "R")
    _insert(jobs_db, "X", canonical_id="R")
    ok, old_root = app.promote_to_canonical(jobs_db, "X", "2026-07-13T00:00:00Z")
    assert ok and old_root == "R"
    links = {r["job_id"]: r["canonical_id"]
             for r in jobs_db.execute("SELECT job_id, canonical_id FROM jobs").fetchall()}
    assert links == {"X": None, "R": "X"}


def test_promote_rejects_existing_root(jobs_db):
    # R is already the canonical (canonical_id IS NULL) — nothing to promote.
    _insert(jobs_db, "R")
    _insert(jobs_db, "X", canonical_id="R")
    ok, msg = app.promote_to_canonical(jobs_db, "R", "2026-07-13T00:00:00Z")
    assert ok is False and "already the canonical" in msg
    # State untouched.
    assert jobs_db.execute("SELECT canonical_id FROM jobs WHERE job_id='X'").fetchone()[0] == "R"


def test_promote_rejects_missing_job(jobs_db):
    ok, msg = app.promote_to_canonical(jobs_db, "nope", "2026-07-13T00:00:00Z")
    assert ok is False and msg == "Job not found."
