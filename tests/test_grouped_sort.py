"""Grouped-header identity columns (title/company/status/dates) reflect the canonical
ROOT so the grouped view sorts by the canonical, not by an arbitrary MIN() member — and
matches the displayed representative title. Falls back to the group aggregate only when the
root is filtered out of the view. Exercises the real GROUPED_HEADERS SQL against a DB."""
import app


def _insert(db, job_id, title, canonical_id=None, status="new",
            first_seen="2026-01-01 00:00:00", company="Co",
            salary_min=None, salary_max=None):
    db.execute(
        "INSERT INTO jobs (job_id, title, company, status, canonical_id, first_seen, "
        "salary_min, salary_max, raw, source, labels, history) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', 'linkedin', '[]', '[]')",
        (job_id, title, company, status, canonical_id, first_seen, salary_min, salary_max))


def _grouped(db, where="", order="ORDER BY title", params=None):
    sql = app.GROUPED_HEADERS.format(where=where, order=order)
    return db.execute(sql, (params or []) + [-1, 0]).fetchall()


def test_header_title_is_root_not_min(jobs_db):
    """Root's title wins even when it isn't the alphabetical MIN of the group."""
    _insert(jobs_db, "root", "ZZZ Root Title")                       # canonical root
    _insert(jobs_db, "m1", "AAA Member Title", canonical_id="root")  # MIN would pick this
    rows = _grouped(jobs_db)
    assert len(rows) == 1
    assert rows[0]["title"] == "ZZZ Root Title"


def test_header_status_and_company_follow_root(jobs_db):
    # Values chosen so root != MIN, so the assertions actually distinguish the two.
    _insert(jobs_db, "root", "T", status="new", company="Zebra Inc")
    _insert(jobs_db, "m1", "T", canonical_id="root", status="applied", company="Acme")
    row = _grouped(jobs_db)[0]
    assert row["status"] == "new"            # root, not MIN('applied','new') == 'applied'
    assert row["company_eff"] == "Zebra Inc" # root, not MIN('Acme','Zebra Inc') == 'Acme'


def test_header_falls_back_to_min_when_root_filtered_out(jobs_db):
    """If the active filter excludes the root (e.g. a closed root under applied members),
    the header falls back to the group aggregate rather than returning NULL."""
    _insert(jobs_db, "root", "ZZZ Root", status="closed")
    _insert(jobs_db, "m1", "AAA Member", canonical_id="root", status="applied")
    _insert(jobs_db, "m2", "BBB Member", canonical_id="root", status="applied")
    rows = _grouped(jobs_db, where="WHERE status = 'applied'")
    assert len(rows) == 1
    assert rows[0]["title"] == "AAA Member"   # MIN of the surviving members


def test_header_salary_is_root_band_not_group_envelope(jobs_db):
    """Salary sorts/searches on the canonical root's band, not a synthetic MIN-low/MAX-high
    envelope spanning the group."""
    _insert(jobs_db, "root", "T", salary_min=200000, salary_max=250000)
    _insert(jobs_db, "m1", "T", canonical_id="root", salary_min=150000, salary_max=300000)
    row = _grouped(jobs_db)[0]
    assert row["salary_min"] == 200000   # root's, not MIN(150000, 200000)
    assert row["salary_max"] == 250000   # root's, not MAX(250000, 300000)


def test_grouped_sort_orders_by_root_title_not_min(jobs_db):
    """The whole point: sort order uses the root's title. A group whose root sorts late
    must not jump early just because a member has an alphabetically-early title."""
    # Group A: root "Middle Root", member "Aardvark" (MIN would be "Aardvark").
    _insert(jobs_db, "rootA", "Middle Root")
    _insert(jobs_db, "mA", "Aardvark Member", canonical_id="rootA")
    # Standalone group B: "Beta".
    _insert(jobs_db, "rootB", "Beta Standalone")
    titles = [r["title"] for r in _grouped(jobs_db, order="ORDER BY title COLLATE NOCASE ASC")]
    # Root-based: Beta < Middle. MIN-based would have put "Aardvark" first.
    assert titles == ["Beta Standalone", "Middle Root"]
