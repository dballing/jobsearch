"""Company reject-list: employers the candidate will never work for, whose pre-decision jobs
are forced to low/autoskipped WITHOUT an AI call.

Covers the pure helpers (parse/match/reason), that matching runs on the ALREADY alias-normalized
company field (exact, case-insensitive, whole-field), and that build_selection's default-run
reject-OR actually pulls the right jobs — non-stale listed pre-decision jobs in, acted-on jobs
out — when executed against a live SQLite DB. The AI-scoring loop itself isn't unit-tested (it
needs a live model); the deny branch reuses these helpers, which are locked down here.
"""
import sqlite3

import rescore_viability as rv
from viability import (REJECT_DENYABLE_STATUSES, build_reject_set, is_rejected_company,
                       reject_reason)


# ── build_reject_set: parsing ─────────────────────────────────────────────────
def test_build_reject_set_lowercases_trims_and_drops_blanks():
    rs = build_reject_set({"viability": {"reject_companies": ["  Initech ", "GLOBEX", "", "  "]}})
    assert rs == frozenset({"initech", "globex"})


def test_build_reject_set_absent_or_off_is_empty():
    assert build_reject_set({}) == frozenset()
    assert build_reject_set({"viability": {}}) == frozenset()
    assert build_reject_set({"viability": {"reject_companies": []}}) == frozenset()


def test_build_reject_set_tolerates_non_list():
    # A scalar (misconfigured) must not crash or iterate characters — treat as no list.
    assert build_reject_set({"viability": {"reject_companies": "Initech"}}) == frozenset()


# ── is_rejected_company: matching ─────────────────────────────────────────────
def test_is_rejected_company_case_insensitive_and_trimmed():
    rs = frozenset({"initech"})
    assert is_rejected_company("Initech", rs)
    assert is_rejected_company("  initech ", rs)
    assert is_rejected_company("INITECH", rs)


def test_is_rejected_company_is_whole_field_not_substring():
    # "Foo" must not match "Foobar Inc" — the whole-field rule is what keeps a reject entry from
    # bleeding onto an adjacent/competitor employer (the same footgun we avoid in scoring).
    rs = frozenset({"initech"})
    assert not is_rejected_company("Initech Corp", rs)
    assert not is_rejected_company("The Initech", rs)


def test_is_rejected_company_empty_inputs():
    assert not is_rejected_company("Initech", frozenset())
    assert not is_rejected_company("", frozenset({"initech"}))
    assert not is_rejected_company(None, frozenset({"initech"}))


def test_reject_reason_names_the_employer():
    assert reject_reason("Initech") == "Autoskipped: Initech is on your company reject-list."
    # Blank company still yields a sensible sentence rather than a dangling name.
    assert "this employer" in reject_reason("")


# ── build_selection reject-OR, executed against a live DB ─────────────────────
def _db_with_jobs(rows):
    # Shared fields on jobs; per-lens status/viability/staleness on job_search_state (the schema
    # build_selection now targets via a join). Row tuple stays (id, company, status, viability,
    # hash, needs_rescored, first_seen) for readability at the call sites.
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE jobs (job_id TEXT, company TEXT, first_seen TEXT)")
    con.execute(
        "CREATE TABLE job_search_state (job_id TEXT, search_id TEXT, status TEXT, viability TEXT, "
        "viability_prompt_hash TEXT, needs_rescored INT)"
    )
    for job_id, company, status, viability, phash, nr, first_seen in rows:
        con.execute("INSERT INTO jobs VALUES (?,?,?)", (job_id, company, first_seen))
        con.execute("INSERT INTO job_search_state VALUES (?, '__default__', ?, ?, ?, ?)",
                    (job_id, status, viability, phash, nr))
    return con


_JSS_JOIN = ("JOIN job_search_state jss ON jss.job_id = jobs.job_id "
             "AND jss.search_id = '__default__'")


def _selected(con, **kwargs):
    where, params = rv.build_selection(**kwargs)
    return sorted(r["job_id"] for r in
                  con.execute(f"SELECT jobs.job_id AS job_id FROM jobs {_JSS_JOIN} {where}", params))


def test_default_run_pulls_in_nonstale_listed_predecision_jobs():
    # A listed employer's 'new' job that's ALREADY scored under the current hash (so the staleness
    # gate would skip it) must still be selected via the reject-OR, so newly listing a company
    # takes effect on its existing jobs — while acted-on and non-listed jobs stay untouched.
    con = _db_with_jobs([
        ("A", "Initech", "new",     "medium", "H", 0, "2026-08-01"),  # listed, non-stale  → in
        ("B", "Initech", "applied", "high",   "H", 0, "2026-08-01"),  # listed, acted on   → out
        ("C", "Acme",    "new",     "medium", "H", 0, "2026-08-01"),  # not listed         → out
        ("D", "GLOBEX",  "new",     None,     None, 1, "2026-08-01"),  # listed + stale     → in
        ("E", "Initech", "interviewing", "high", "H", 0, "2026-08-01"),  # acted on        → out
    ])
    rs = build_reject_set({"viability": {"reject_companies": ["Initech", "Globex"]}})
    assert _selected(con, current_hash="H", reject_companies=rs) == ["A", "D"]


def test_reject_or_does_not_widen_narrow_modes():
    # An explicit narrowing (--autoskipped/--status/--early-stage/date/current-viability) is a
    # deliberate, bounded selection; the reject-OR must not silently widen it.
    con = _db_with_jobs([
        ("A", "Initech", "new",         "medium", "H", 0, "2026-08-01"),  # listed pre-decision
        ("Z", "Initech", "autoskipped", "low",    "H", 1, "2026-08-01"),  # listed autoskipped, stale
    ])
    rs = build_reject_set({"viability": {"reject_companies": ["Initech"]}})
    # --autoskipped selects only the (stale) autoskipped job, NOT the 'new' listed one — the
    # reject-OR that would pull in A is suppressed in this narrow mode.
    assert _selected(con, current_hash="H", autoskipped=True, reject_companies=rs) == ["Z"]
    # --status skipped selects neither (no such status here).
    assert _selected(con, current_hash="H", status="skipped", reject_companies=rs) == []


def test_reject_or_absent_when_no_list_configured():
    con = _db_with_jobs([("A", "Initech", "new", "medium", "H", 0, "2026-08-01")])
    # No reject set → the non-stale listed job is NOT force-selected (behaves as before the feature).
    assert _selected(con, current_hash="H", reject_companies=frozenset()) == []
    assert _selected(con, current_hash="H") == []


def test_denyable_statuses_are_the_pre_decision_set_plus_autoskipped():
    # Guards the shared constant both drivers key off: acted-on statuses must be excluded so the
    # list never yanks a job you're actively pursuing.
    assert set(REJECT_DENYABLE_STATUSES) == {"new", "reviewing", "deferred", "autoskipped"}
    for acted in ("applied", "interviewing", "rejected", "withdrawn", "ghosted", "closed"):
        assert acted not in REJECT_DENYABLE_STATUSES


# ── the web-UI single-job deny path writes low/autoskipped without any AI call ─
def test_score_one_job_denies_listed_company_without_ai(jobs_db, config_file):
    # The "rescore this job" button short-circuits BEFORE the anthropic import for a listed
    # employer, so this exercises the real persist path (viability→low, reason, status→autoskipped,
    # history, current hash stamped) with no network — the api_key below is never used.
    import app
    config_file(
        '[viability]\n'
        'enabled = true\n'
        'api_key = "sk-ant-test"\n'
        'prompt = "candidate profile"\n'
        'reject_companies = ["Initech"]\n'
    )
    jobs_db.execute(
        "INSERT INTO jobs (job_id, title, company, raw) VALUES (?,?,?,?)",
        ("j1", "Engineer", "Initech", "{}"),
    )
    # Per-lens state row (status/viability now live here) under the default search.
    jobs_db.execute(
        "INSERT INTO job_search_state (job_id, search_id, status) VALUES ('j1', '__default__', 'new')"
    )
    jobs_db.commit()

    ok, msg = app._score_one_job(jobs_db, "j1")
    assert ok and "reject-listed" in msg

    row = jobs_db.execute(
        "SELECT viability, viability_reason, status, viability_prompt_hash "
        "FROM job_search_state WHERE job_id = 'j1' AND search_id = '__default__'"
    ).fetchone()
    assert row["viability"] == "low"
    assert "reject-list" in row["viability_reason"]
    assert row["status"] == "autoskipped"
    # Stamped with the current hash so it isn't perpetually re-selected as stale.
    assert row["viability_prompt_hash"]
