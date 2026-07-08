"""Tests for ingest.find_canonical — member-aware fuzzy dedup with root resolution."""
import ingest

DESC_A = ("We are hiring a Staff Technical Program Manager to lead cross-functional "
          "infrastructure programs across many teams at global scale. " * 6)
DESC_B = ("About Our Client: the organization operates in telemetry infrastructure for "
          "AI, bridging ambition and operational reality with a flexible platform. " * 6)


def _insert(conn, job_id, title, desc, canonical_id=None, first_seen="2026-01-01"):
    conn.execute(
        "INSERT INTO jobs (job_id, title, company, job_description, canonical_id, "
        "first_seen, raw) VALUES (?,?,?,?,?,?,?)",
        (job_id, title, "Acme", desc, canonical_id, first_seen, "{}"),
    )
    conn.commit()


def test_member_match_resolves_to_root(jobs_db):
    # Group: root A (early) + member B pointing at A. B's prose differs from A's.
    _insert(jobs_db, "A", "Staff TPM", DESC_A, first_seen="2026-01-01")
    _insert(jobs_db, "B", "Staff TPM", DESC_B, canonical_id="A", first_seen="2026-02-01")
    # A new posting identical to member B (but unlike root A) must link to root A.
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_B, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_direct_root_match(jobs_db):
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["A"]


def test_title_prefilter_blocks_match(jobs_db):
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    # Same description, wildly different title -> title pre-filter rejects.
    matches = ingest.find_canonical(jobs_db, "P", "Warehouse Forklift Operator",
                                    "Acme", DESC_A, 0.85)
    assert matches == []


def test_description_threshold_blocks_match(jobs_db):
    _insert(jobs_db, "A", "Staff TPM", DESC_A)
    # Same title, unrelated description -> below desc threshold.
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_B, 0.85)
    assert matches == []


def test_returns_distinct_roots_oldest_first(jobs_db):
    # Two separate canonicals that both match the query resolve to two roots, oldest first.
    _insert(jobs_db, "OLD", "Staff TPM", DESC_A, first_seen="2026-01-01")
    _insert(jobs_db, "NEW", "Staff TPM", DESC_A, first_seen="2026-05-01")
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_A, 0.85)
    assert [m["job_id"] for m in matches] == ["OLD", "NEW"]


def test_does_not_match_itself(jobs_db):
    _insert(jobs_db, "P", "Staff TPM", DESC_A)
    matches = ingest.find_canonical(jobs_db, "P", "Staff TPM", "Acme", DESC_A, 0.85)
    assert matches == []
