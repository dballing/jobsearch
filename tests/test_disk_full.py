"""What happens when a write fails because the disk is full.

SQLITE_FULL is not an ordinary per-statement error. SQLite aborts the ENTIRE open
transaction, and a later ``commit()`` on that connection then returns *success* with
nothing in it (test_premise_* below pins both halves). That combination is what makes a
full disk dangerous in a way a normal error isn't: any handler that catches it and carries
on can report success having silently discarded the caller's work. These tests pin the
places that used to be able to do that, plus the file-side cleanups.

The full disk is simulated with ``PRAGMA max_page_count``, which raises exactly the same
``sqlite3.OperationalError: database or disk is full``; the handler branches themselves use
a stub connection so they're deterministic rather than dependent on page arithmetic.
test_real_disk_full_error_is_not_mistaken_for_a_missing_table ties the two together, so a
change in SQLite's wording can't leave the stubs testing a message that no longer occurs.
"""
import errno
import io
import os
import pathlib
import sqlite3
import sys
import tomllib
from types import SimpleNamespace

import pytest

import app
import ingest
import spend

CONFIG = (
    '[ai]\n'
    'api_key = "sk-secret-xyz"\n\n'
    '[company_aliases]\n'
    '"Amazon Web Services (AWS)" = "Amazon"\n'
    '"Sirius XM"                 = "Sirius XM Radio"\n\n'
    '[[tasks]]\n'
    'name = "x"\n'
)

FULL = "database or disk is full"


class _FullDisk:
    """A connection on a full disk: every statement raises SQLITE_FULL.

    Stands in for the real thing so the except-branches under test are exercised without
    depending on how many pages a given row happens to need.
    """

    def execute(self, *args):
        raise sqlite3.OperationalError(FULL)


def _wedge(conn, headroom=2):
    """Cap the DB a couple of pages above its current size — a nearly-full disk.

    A small row still fits; the next sizeable one raises SQLITE_FULL. Deliberately does NOT
    commit first: these tests need the earlier write still *pending* when the limit is hit,
    which is the whole scenario.
    """
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count={pages + headroom}")


def _insert(conn, job_id, title="t"):
    conn.execute("INSERT INTO jobs (job_id, title, company, raw, labels) "
                 "VALUES (?, ?, 'c', '{}', '[]')", (job_id, title))


# ── the premise ───────────────────────────────────────────────────────────────────────────
def test_premise_disk_full_discards_the_whole_transaction(jobs_db):
    """A full disk doesn't just fail the statement that hit it — it rolls back everything
    uncommitted on that connection, including writes that already succeeded."""
    _wedge(jobs_db)
    _insert(jobs_db, "j1")            # succeeds, and is still uncommitted
    assert jobs_db.in_transaction
    with pytest.raises(sqlite3.OperationalError, match=FULL):
        _insert(jobs_db, "j2", "x" * 200_000)
    # Not "the failing statement was skipped" — the transaction itself is gone.
    assert not jobs_db.in_transaction


def test_premise_commit_after_disk_full_reports_success(jobs_db):
    """And the commit that follows does NOT raise: there is no transaction left to fail.

    This is the whole reason the recorders must not swallow SQLITE_FULL — a caller that
    catches it and commits anyway gets a clean return and an empty database."""
    _wedge(jobs_db)
    _insert(jobs_db, "keeper")
    with pytest.raises(sqlite3.OperationalError):
        _insert(jobs_db, "big", "x" * 200_000)
    jobs_db.commit()                      # no exception — looks like it worked
    assert jobs_db.execute("SELECT COUNT(*) FROM jobs WHERE job_id = 'keeper'").fetchone()[0] == 0


# ── spend recorders: tolerate a missing table, never a full disk ──────────────────────────
def test_record_usage_reraises_disk_full():
    """Swallowing this would discard the caller's uncommitted work (an ingest item's
    job_search_state row, a fuzzy re-link) and let ingest report the item as written."""
    with pytest.raises(sqlite3.OperationalError, match=FULL):
        spend.record_usage(_FullDisk(), feature="viability", model="claude-haiku-4-5",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=10,
                                                 cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0),
                           search_id="s1", job_id="j1")


def test_record_apify_run_reraises_disk_full():
    """A run's Apify charge is bookmarked as consumed in the same transaction, so losing it
    quietly would mean nothing ever re-records it."""
    with pytest.raises(sqlite3.OperationalError, match=FULL):
        spend.record_apify_run(_FullDisk(), search_id="s1", task_name="t",
                               cost_usd=0.04, item_job_ids=["j1"])


def test_record_apify_run_still_tolerates_missing_table():
    """The one survivable case is unchanged: accounting must not be able to break ingest."""
    conn = sqlite3.connect(":memory:")
    assert spend.record_apify_run(conn, search_id="s", task_name="t",
                                  cost_usd=0.04, item_job_ids=["j"]) == 0


def test_real_disk_full_error_is_not_mistaken_for_a_missing_table(jobs_db):
    """Bridge between the stubs above and reality: a genuine SQLITE_FULL from SQLite itself
    must fall on the re-raise side of the predicate, and a genuine missing table on the
    tolerated side. Guards the message-matching in spend._is_missing_table."""
    _wedge(jobs_db)
    with pytest.raises(sqlite3.OperationalError) as full:
        _insert(jobs_db, "big", "x" * 200_000)
    assert not spend._is_missing_table(full.value)

    with pytest.raises(sqlite3.OperationalError) as missing:
        sqlite3.connect(":memory:").execute("INSERT INTO spend_ledger (feature) VALUES ('x')")
    assert spend._is_missing_table(missing.value)


# ── ingest: the run's cost rows are committed, not left pending at close() ─────────────────
def test_apify_cost_row_survives_the_end_of_an_ingest_run(tmp_path, monkeypatch, capsys):
    """Regression: ingest.main() used to record the Apify charge AFTER record_state (the call
    that commits), so the last ingested run's cost rows were still pending when main() did
    conn.close() — which rolls an open transaction back rather than committing it. They
    vanished silently, every run, with no disk-full needed. A fresh connection must see the
    row after main() returns.

    One run returning an empty dataset: ingest() writes nothing, so the charge lands in the
    lens's unattributed overhead row — the same code path, with no feed-item shape to mock.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'api_token = "x"\n'
        'username = "u"\n'
        f'db_path = "{tmp_path / "jobs.db"}"\n'
        "[[tasks]]\n"
        'name = "derek-career-site-generic"\n'
        'label = "test"\n'
    )
    run = {"id": "run-1", "startedAt": "2026-10-08T00:00:00.000Z",
           "defaultDatasetId": "ds-1", "usageTotalUsd": 0.04}
    monkeypatch.setattr(ingest, "fetch_task_runs", lambda *a, **k: [run])
    monkeypatch.setattr(ingest, "fetch_dataset_items", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--config", str(cfg)])
    ingest.main()
    capsys.readouterr()

    con = sqlite3.connect(tmp_path / "jobs.db")
    cost = con.execute(
        "SELECT cost_usd FROM spend_ledger WHERE feature = 'apify'").fetchall()
    bookmarked = con.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0]
    con.close()
    assert [r[0] for r in cost] == [pytest.approx(0.04)]
    # The bookmark and the charge are one transaction, so the run is only marked consumed
    # if its cost was recorded too.
    assert bookmarked == 1


# ── app: a failed write must not strand an upload or mislabel the cause ───────────────────
def test_upload_leaves_no_orphan_file_when_the_db_write_fails(sample_app_db, monkeypatch):
    """The bytes land on disk before the row that references them. A full disk mid-request
    (classically a truncated file from file.save itself) would otherwise leave a file under a
    UUID nothing points at: invisible in the UI, never reached by the refcounted delete, and
    still occupying the space that just ran out."""
    os.makedirs(app.UPLOADS_DIR, exist_ok=True)
    before = set(os.listdir(app.UPLOADS_DIR))

    def _full(*a, **k):
        raise sqlite3.OperationalError(FULL)
    monkeypatch.setattr(app, "group_member_ids", _full)

    resp = app.app.test_client().post(
        "/job/cs_review/attachment",
        data={"file": (io.BytesIO(b"resume bytes"), "resume.pdf")},
        content_type="multipart/form-data")
    assert resp.status_code == 507
    assert set(os.listdir(app.UPLOADS_DIR)) == before


def test_db_error_handler_separates_a_full_disk_from_a_busy_db():
    """Both are environmental, and neither is an app bug — but they need opposite advice
    ("retry in a moment" vs "free some space"), so a full disk gets its own answer instead
    of a bare 500. 507 is safe to be definite about: SQLITE_FULL already rolled the
    request's transaction back, so nothing was half-written."""
    body, code = app.handle_db_busy(sqlite3.OperationalError(FULL))
    assert code == 507 and "disk is full" in body and "Nothing from this request was saved" in body

    body, code = app.handle_db_busy(sqlite3.OperationalError("database is locked"))
    assert code == 503 and "busy" in body

    # Anything else is a real error and must not be dressed up as either one.
    with pytest.raises(sqlite3.OperationalError):
        app.handle_db_busy(sqlite3.OperationalError("no such column: sprocket"))


# ── config writers: the live file survives, and the temp file doesn't linger ───────────────
def _enospc_on_temp(monkeypatch):
    """Make writes to a *.tmp path behave like ENOSPC: a partial write, then the error.

    Faithful to the real thing — the failure surfaces at flush/close, after bytes have
    already landed — which is why the temp file needs cleaning up at all.
    """
    real = pathlib.Path.write_text

    def _write(self, data, *args, **kwargs):
        if self.name.endswith(".tmp"):
            real(self, data[: len(data) // 2], *args, **kwargs)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", _write)


def test_alias_writer_keeps_config_intact_and_removes_the_temp_file(config_file, monkeypatch):
    """temp-then-rename already protects config.toml (ENOSPC raises before the rename), so the
    only exposure is the half-written temp sitting next to a file people edit by hand — and a
    500 instead of the writer's normal "wrote nothing" refusal."""
    p = config_file(CONFIG)
    _enospc_on_temp(monkeypatch)

    added, err = app.add_company_alias("X, LLC", "Xenon")
    assert added is False and "could not write config.toml" in err
    assert tomllib.loads(p.read_text())["company_aliases"] == {
        "Amazon Web Services (AWS)": "Amazon", "Sirius XM": "Sirius XM Radio"}
    assert 'api_key = "sk-secret-xyz"' in p.read_text()   # the whole file, not just the aliases
    assert not p.with_name(p.name + ".tmp").exists()


def test_basics_migrator_keeps_config_intact_and_removes_the_temp_file(tmp_path, monkeypatch):
    """Same contract for the other temp-then-rename writer (config.migrate_config_to_basics,
    behind ingest.sh --fixbasics)."""
    import config

    p = tmp_path / "config.toml"
    p.write_text('db_path = "/tmp/x.db"\n\n[[tasks]]\nname = "x"\n', encoding="utf-8")
    original = p.read_text()
    _enospc_on_temp(monkeypatch)

    changed, msg = config.migrate_config_to_basics(p)
    assert changed is False and "could not write" in msg
    assert p.read_text() == original
    assert not p.with_name(p.name + ".tmp").exists()
