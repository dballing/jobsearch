"""Shared test setup.

The app module runs `_init_db()` at import (migrations) and reads config.toml, both
keyed off env-overridable paths. We point those at a throwaway temp dir *before* any test
imports `app`, so the real jobs.db / config.toml are never touched by the suite.
"""
import os
import pathlib
import sqlite3
import tempfile

import pytest

# Must happen at conftest import — before test modules do `import app`.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="jobsearch-tests-"))
(_TMP / "config.toml").write_text(
    f'db_path = "{_TMP / "jobs.db"}"\nuploads_dir = "{_TMP / "uploads"}"\n'
)
os.environ["JOBSEARCH_CONFIG"] = str(_TMP / "config.toml")
os.environ["JOBSEARCH_DB"] = str(_TMP / "jobs.db")

# app._init_db() only runs ALTER-based migrations at import; it assumes ingest already
# created the base `jobs` table. Seed the throwaway DB with the schema first (ingest
# imports with no side effects) so that import succeeds against the temp DB.
import ingest  # noqa: E402
_seed = sqlite3.connect(_TMP / "jobs.db")
_seed.executescript(ingest.SCHEMA)
_seed.close()


@pytest.fixture
def jobs_db():
    """In-memory SQLite with the real jobs schema and Row access, for exercising the
    DB-level helpers (e.g. find_canonical) without any file I/O."""
    import ingest
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ingest.SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def sample_db(jobs_db):
    """In-memory DB populated with the deterministic sample dataset (see fixtures/sample_data).
    For DB-level tests that want a realistic, varied set of rows without hand-building them."""
    from fixtures.sample_data import build_sample_db
    build_sample_db(jobs_db)
    return jobs_db


@pytest.fixture
def sample_app_db():
    """Populate the *app's* configured DB with the sample dataset for one test, then clear it.

    Lets a test render the real app (via app.test_client) against known, varied data. Resets
    jobs / ingest_state / company_hotlist at setup and teardown so it stays isolated from the
    rest of the suite (which assumes an empty app DB)."""
    import sqlite3
    from fixtures.sample_data import build_sample_db

    path = os.environ["JOBSEARCH_DB"]

    def _clear():
        con = sqlite3.connect(path)
        for t in ("jobs", "ingest_state", "company_hotlist"):
            try:
                con.execute(f"DELETE FROM {t}")
            except sqlite3.OperationalError:
                pass
        con.commit(); con.close()

    _clear()
    con = sqlite3.connect(path)
    build_sample_db(con)
    con.close()
    yield
    _clear()


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Return a helper that writes a throwaway config.toml and points app at it.

    Usage: `path = config_file('[ai]\\napi_key = "x"\\n')` → subsequent app config
    reads/writes (e.g. add_company_alias) operate on that file, never the real one.
    """
    import app

    def _make(contents: str) -> pathlib.Path:
        p = tmp_path / "config.toml"
        p.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(app, "_config_path", p)
        return p

    return _make
