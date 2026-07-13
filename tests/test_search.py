"""Tests for app.search_token_where — the shared free-text search clause.

The jobs list and the link-picker both use it. The key property is whitespace-insensitivity:
"Search all" builds its query from a displayed (whitespace-normalized) title, so a single
whole-string LIKE would miss a raw stored title with irregular spacing (e.g. a double space).
Tokenizing fixes that — verified both as a clause builder and functionally.
"""
import app


def _cols():
    return ["title", "company", "company_actual"]


def test_empty_query_returns_no_clause():
    assert app.search_token_where("", _cols()) == ("", [])
    assert app.search_token_where("   ", _cols()) == ("", [])


def test_single_token_matches_each_column():
    clause, params = app.search_token_where("zillow", _cols())
    assert clause == "((title LIKE ? OR company LIKE ? OR company_actual LIKE ?))"
    assert params == ["%zillow%", "%zillow%", "%zillow%"]


def test_multiple_tokens_are_anded():
    clause, params = app.search_token_where("staff tpm", _cols())
    assert clause.count("LIKE ?") == 6          # 2 tokens × 3 columns
    assert " AND " in clause
    assert params == ["%staff%"] * 3 + ["%tpm%"] * 3


def test_quoted_phrase_is_one_token():
    clause, params = app.search_token_where('"senior tpm" zillow', _cols())
    # Two tokens: the quoted phrase and "zillow" → 2 × 3 params, phrase kept whole.
    assert params == ["%senior tpm%"] * 3 + ["%zillow%"] * 3


def test_unclosed_quote_falls_back_to_split():
    # Must not raise; the stray quote just splits normally.
    clause, params = app.search_token_where('senior "tpm', _cols())
    assert clause and params  # produced a clause without error


def test_whitespace_insensitive_match_against_double_spaced_title(jobs_db):
    # The regression: a normalized single-space query must find a raw double-space title.
    jobs_db.execute(
        "INSERT INTO jobs (job_id, title, raw) VALUES "
        "('a', 'Senior Director, Technical Program Management - AI  (Remote Eligible)', '{}')")
    jobs_db.execute(
        "INSERT INTO jobs (job_id, title, raw) VALUES ('b', 'Unrelated Analyst Role', '{}')")

    q = "Senior Director, Technical Program Management - AI (Remote Eligible)"  # single space
    clause, params = app.search_token_where(q, _cols())
    hits = {r["job_id"] for r in
            jobs_db.execute(f"SELECT job_id FROM jobs WHERE {clause}", params).fetchall()}
    assert hits == {"a"}          # the double-spaced original matches; the unrelated one doesn't
