#!/usr/bin/env python3
# requires Python 3.11+
"""Ingest Apify LinkedIn job search results into a local SQLite database.

Usage:
    python3 ingest.py [--config PATH] [--dry-run]

Flags:
    --config PATH  Path to TOML config (default: config.toml).
    --dry-run      Fetch pending runs and report item counts per run (with
                   resolved labels) without writing anything to the database.
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

from config import ConfigError, load_config, migrate_config_to_basics

from ai_config import (DEFAULT_EFFORT, format_token_summary, resolve_ai_settings,
                       resolve_effort, warn_effort_ignored)
from reformat import content_preserved, description_hash, reformat_description
from runlock import acquire_run_lock

APIFY_BASE = "https://api.apify.com/v2"

# ── Database schema ───────────────────────────────────────────────────────────
# The `jobs` table is the heart of the app — one row per posting (PK job_id; career-
# site IDs are prefixed "cs_" to avoid collision with numeric LinkedIn IDs). Columns
# worth calling out:
#   canonical_id              NULL for a canonical/standalone job; otherwise the job_id
#                             of the canonical this is a fuzzy duplicate of (one hop only).
#   company_actual/salary_*_actual  manual UI overrides that win over scraped values.
#   company_url               employer's own site (extract_company_url): prefers the
#                             feed's linkedin_org_url/domain_derived, else organization_url.
#   needs_rescored            set when a viability-relevant field changed, so the next
#                             rescore re-evaluates even if the prompt itself is unchanged.
#   job_description_formatted optional AI-cleaned Markdown; NULL → heuristic renderer.
#   description_hash          sha256 of job_description; powers the reformat cache.
#   first_seen vs applied_at  first_seen = when WE ingested it; applied_at = when the
#                             user applied (drives auto-ghost).
#   raw                       full Apify item JSON, kept for reprocessing/debugging.
# ingest_state records the last-processed run per task (feeds the run summary/stats). The
# per-run rows in ingest_history are the source of truth for "already ingested": run-selection
# filters on run_id membership within a search (see runs_to_process), not the ingest_state
# pointer, which makes ingest robust to Apify task renames. ingest_history also feeds the stats
# charts; job_attachments links one uploaded file to N jobs (refcounted on delete).
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    posted_date     TEXT,
    job_url         TEXT,
    apply_url       TEXT,
    company_url     TEXT,
    easy_apply      INTEGER,
    salary_min      INTEGER,
    salary_max      INTEGER,
    salary_currency TEXT,
    labels          TEXT NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL DEFAULT 'linkedin',
    status          TEXT NOT NULL DEFAULT 'new',
    notes           TEXT,
    job_description TEXT,
    refreshed_at          TIMESTAMP,
    canonical_id          TEXT,
    viability             TEXT,
    viability_reason      TEXT,
    viability_prompt_hash TEXT,
    viability_factors     TEXT,
    applied_at            TEXT,
    history               TEXT NOT NULL DEFAULT '[]',
    company_actual        TEXT,
    title_actual          TEXT,
    salary_min_actual     INTEGER,
    salary_max_actual     INTEGER,
    work_arrangement_actual TEXT,
    geo_fit_actual        TEXT,
    description_actual    TEXT,
    needs_rescored        INTEGER NOT NULL DEFAULT 0,
    description_truncated  INTEGER NOT NULL DEFAULT 0,
    job_description_formatted TEXT,
    description_hash          TEXT,
    first_seen      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status           ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company          ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen       ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_description_hash ON jobs(description_hash);

CREATE TABLE IF NOT EXISTS ingest_state (
    task_name      TEXT PRIMARY KEY,
    last_run_id    TEXT NOT NULL,
    last_run_at    TEXT NOT NULL,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name   TEXT    NOT NULL,
    run_id      TEXT    NOT NULL,
    run_at      TEXT    NOT NULL,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(task_name, run_id)
);

-- File attachments: one physical file (attachment_id, stored on disk under a
-- UUID name) linked to N jobs. Refcount = COUNT(*) by attachment_id.
CREATE TABLE IF NOT EXISTS job_attachments (
    job_id        TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    stored_name   TEXT NOT NULL,
    original_name TEXT NOT NULL,
    content_type  TEXT,
    size          INTEGER,
    uploaded_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, attachment_id)
);
CREATE INDEX IF NOT EXISTS idx_attach_aid ON job_attachments(attachment_id);

-- Per-(job, search) state for multi-search support. A row's existence IS the job's
-- membership in that search; the row carries ALL per-lens state — status, viability,
-- the manual salary/geo overrides that feed that lens's scorer, and the per-lens event
-- history. The matching columns on `jobs` are dormant (kept for rollback; migrated here
-- once). See config.py for the search model and __default__ (the single/legacy search id).
CREATE TABLE IF NOT EXISTS job_search_state (
    job_id                TEXT NOT NULL,
    search_id             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'new',
    viability             TEXT,
    viability_reason      TEXT,
    viability_factors     TEXT,
    viability_prompt_hash TEXT,
    needs_rescored        INTEGER NOT NULL DEFAULT 0,
    salary_min_actual     INTEGER,
    salary_max_actual     INTEGER,
    geo_fit_actual        TEXT,
    applied_at            TEXT,
    history               TEXT NOT NULL DEFAULT '[]',
    first_seen            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP,
    PRIMARY KEY (job_id, search_id)
);
CREATE INDEX IF NOT EXISTS idx_jss_search ON job_search_state(search_id, status);
"""

# Legacy/default search id — the single implicit search (Path A) and the id every
# pre-multi-search row is backfilled under. Mirrors config.DEFAULT_SEARCH_ID (kept here
# too so the DB layer needn't import config).
DEFAULT_SEARCH_ID = "__default__"


def state_key(search_id: str, task_name: str) -> str:
    """Run-tracking key for ingest_state/ingest_history. The default search keys tasks bare
    (preserving single-search history labels exactly); named searches namespace as
    ``<search_id>:<task_name>`` so two searches referencing the same Apify task can't clobber
    each other's last-run bookmark."""
    return task_name if search_id == DEFAULT_SEARCH_ID else f"{search_id}:{task_name}"


def open_db(path: str) -> sqlite3.Connection:
    """Open the DB (WAL + busy_timeout), create the schema if missing, and run the
    idempotent migrations that bring an older DB up to the current column set.

    Every migration below is guarded by a PRAGMA table_info check, so it ALTERs only
    when the column/rename is actually missing — safe to call on every startup, on a
    fresh DB, or on an already-current one. (app.py and rescore_viability.py re-declare
    the columns they touch so they can run standalone against an untouched DB.)
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent reads/writes with the Flask app and rescore script.
    # busy_timeout retries on lock contention instead of raising immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # ── Idempotent migrations (oldest → newest). Re-reads `cols` between groups
    # because earlier ALTERs change the table. ──
    # Migrate: rename regions → labels if the old column still exists.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "regions" in cols and "labels" not in cols:
        conn.execute("ALTER TABLE jobs RENAME COLUMN regions TO labels")
        conn.commit()
    # Migrate: rename linkedin_url → job_url if old column still exists.
    if "linkedin_url" in cols and "job_url" not in cols:
        conn.execute("ALTER TABLE jobs RENAME COLUMN linkedin_url TO job_url")
        conn.commit()
    # Migrate: add source column if not present (existing rows default to 'linkedin').
    if "source" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'linkedin'")
        conn.commit()
    # Migrate: add last_synced_at to ingest_state if not present.
    state_cols = [row[1] for row in conn.execute("PRAGMA table_info(ingest_state)").fetchall()]
    if state_cols and "last_synced_at" not in state_cols:
        conn.execute("ALTER TABLE ingest_state ADD COLUMN last_synced_at TEXT")
        conn.commit()
    # Migrate: rename 'reviewed' → 'reviewing'.
    conn.execute("UPDATE jobs SET status = 'reviewing' WHERE status = 'reviewed'")
    conn.commit()
    # Migrate: add refreshed_at and canonical_id columns if not present.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "refreshed_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN refreshed_at TIMESTAMP")
        conn.commit()
    if "canonical_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN canonical_id TEXT")
        conn.commit()
    # Migrate: add viability scoring columns if not present.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "viability" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability TEXT")
        conn.commit()
    if "viability_reason" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability_reason TEXT")
        conn.commit()
    if "viability_prompt_hash" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability_prompt_hash TEXT")
        conn.commit()
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "applied_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
        # Backfill: jobs already in applied/interviewing/offered/rejected/withdrawn/ghosted
        # use first_seen as a reasonable approximation of when the application was made.
        conn.execute(
            "UPDATE jobs SET applied_at = first_seen "
            "WHERE status IN ('applied','interviewing','offered','rejected','withdrawn','ghosted') "
            "AND applied_at IS NULL"
        )
        conn.commit()
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "history" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN history TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
        bootstrap_history(conn)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "company_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN company_actual TEXT")
        conn.commit()
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "salary_min_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN salary_min_actual INTEGER")
        conn.commit()
    if "salary_max_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN salary_max_actual INTEGER")
        conn.commit()
    if "needs_rescored" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN needs_rescored INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "job_description_formatted" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN job_description_formatted TEXT")
        conn.commit()
    if "description_hash" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN description_hash TEXT")
        conn.commit()
    if "company_url" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN company_url TEXT")
        conn.commit()
    if "work_arrangement_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN work_arrangement_actual TEXT")
        conn.commit()
    if "description_actual" not in cols:
        # Manual paste-in full job description that supersedes a wrong/partial feed one (see
        # viability.effective_description). NULL = no override, use the feed's job_description.
        conn.execute("ALTER TABLE jobs ADD COLUMN description_actual TEXT")
        conn.commit()
    # Flags a careersite posting whose feed description looks truncated to a teaser (see
    # feed_description_truncated). Backfilled from the stored raw feed JSON the one time the
    # column is added — done inside the guard so whichever module (ingest/app/rescore) first
    # migrates an existing DB both adds AND populates it; the others then see it present and
    # skip. A fresh DB gets the column from SCHEMA (no rows to backfill).
    if "description_truncated" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN description_truncated INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        backfill_description_truncated(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_description_hash ON jobs(description_hash)"
    )
    conn.commit()
    ensure_job_search_state(conn)
    return conn


# Columns that live on both `jobs` (dormant after migration) and job_search_state — the
# per-lens state carried into the __default__ backfill. Kept as one list so the CREATE, the
# backfill SELECT, and any future reader stay in lockstep.
_JSS_MIGRATED_COLS = (
    "status", "viability", "viability_reason", "viability_factors", "viability_prompt_hash",
    "needs_rescored", "salary_min_actual", "salary_max_actual", "geo_fit_actual",
    "applied_at", "history", "first_seen",
)


def ensure_job_search_state(conn: sqlite3.Connection) -> None:
    """Create job_search_state (idempotent) and, the first time, seed one __default__ row per
    existing job from the (now dormant) per-lens columns on `jobs`. Gated on a read so it takes
    no write lock once seeded. Shared by every migration site (ingest open_db, app._migrate,
    rescore open_db) so the table + backfill can't drift between entry points."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS job_search_state (
               job_id                TEXT NOT NULL,
               search_id             TEXT NOT NULL,
               status                TEXT NOT NULL DEFAULT 'new',
               viability             TEXT,
               viability_reason      TEXT,
               viability_factors     TEXT,
               viability_prompt_hash TEXT,
               needs_rescored        INTEGER NOT NULL DEFAULT 0,
               salary_min_actual     INTEGER,
               salary_max_actual     INTEGER,
               geo_fit_actual        TEXT,
               applied_at            TEXT,
               history               TEXT NOT NULL DEFAULT '[]',
               first_seen            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
               updated_at            TIMESTAMP,
               PRIMARY KEY (job_id, search_id)
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jss_search ON job_search_state(search_id, status)")
    # First-time backfill: one __default__ row per job, copying the dormant per-lens columns.
    if not conn.execute("SELECT 1 FROM job_search_state LIMIT 1").fetchone():
        cols = ", ".join(_JSS_MIGRATED_COLS)
        conn.execute(
            f"INSERT OR IGNORE INTO job_search_state (job_id, search_id, {cols}) "
            f"SELECT job_id, ?, {cols} FROM jobs",
            (DEFAULT_SEARCH_ID,),
        )
    conn.commit()


def adopt_legacy(conn: sqlite3.Connection, adopter_id: str | None) -> int:
    """One-time single→multi transition: re-point every pre-split __default__ row (state +
    history + membership, all one row) and its ingest_state run-tracking key onto the search
    that declared adopts_legacy. Gated on a read (no-op once done) and idempotent — after the
    re-point there are no __default__ rows left. Returns the number of state rows moved.

    ``adopter_id`` is the id of the adopting search (config.py enforces at most one). When None
    and __default__ rows still exist under a Path-B config, warns loudly rather than silently
    orphaning them under lenses the UI can't show."""
    has_legacy = conn.execute(
        "SELECT 1 FROM job_search_state WHERE search_id = ? LIMIT 1", (DEFAULT_SEARCH_ID,)
    ).fetchone()
    if not has_legacy:
        return 0
    if not adopter_id:
        print(
            f"WARNING: {DEFAULT_SEARCH_ID!r} job rows exist but no search sets "
            "adopts_legacy=true — they are invisible under the named searches. Mark one "
            "[[searches]] entry `adopts_legacy = true` to adopt them.",
            file=sys.stderr,
        )
        return 0
    # Refuse to merge onto a search that already has its own rows for the same jobs (would
    # collide on the (job_id, search_id) PK) — adoption is meant to run before the first
    # multi-search ingest creates any adopter rows.
    collision = conn.execute(
        "SELECT 1 FROM job_search_state a JOIN job_search_state b "
        "ON a.job_id = b.job_id WHERE a.search_id = ? AND b.search_id = ? LIMIT 1",
        (DEFAULT_SEARCH_ID, adopter_id),
    ).fetchone()
    if collision:
        raise RuntimeError(
            f"cannot adopt {DEFAULT_SEARCH_ID!r} into {adopter_id!r}: the adopter already has "
            "state rows for some of those jobs. Run adoption before the first multi-search ingest."
        )
    cur = conn.execute(
        "UPDATE job_search_state SET search_id = ? WHERE search_id = ?",
        (adopter_id, DEFAULT_SEARCH_ID),
    )
    moved = cur.rowcount
    # Re-point run-tracking keys. The default search keys them bare (state_key), so at the
    # single→multi transition every existing (un-namespaced) key belongs to the default and
    # gets the adopter's prefix. Run-tracking is low-stakes (a miss just re-scans idempotently),
    # so the bare-vs-namespaced test (no ':') is deliberately simple.
    for tbl in ("ingest_state", "ingest_history"):
        conn.execute(
            f"UPDATE OR IGNORE {tbl} SET task_name = ? || task_name WHERE task_name NOT LIKE '%:%'",
            (adopter_id + ":",),
        )
    conn.commit()
    print(f"Adopted {moved} {DEFAULT_SEARCH_ID!r} job state row(s) into search {adopter_id!r}.")
    return moved


def fetch_task_runs(username: str, task_name: str, api_token: str) -> list[dict]:
    """Return all SUCCEEDED runs for a task, sorted oldest-first."""
    task_id = f"{username}~{task_name}"
    headers = {"Authorization": f"Bearer {api_token}"}

    runs: list[dict] = []
    offset = 0
    limit = 100
    while True:
        resp = requests.get(
            f"{APIFY_BASE}/actor-tasks/{task_id}/runs",
            headers=headers,
            params={"status": "SUCCEEDED", "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()["data"]["items"]
        if not batch:
            break
        runs.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    runs.sort(key=lambda r: r["startedAt"])
    return runs


def fetch_run_input(run: dict, api_token: str) -> dict:
    """Fetch the INPUT record from a run's default key-value store.

    Used to retrieve per-run label overrides (e.g. _jobsearch_label) set via
    Apify schedule input overrides.  Returns an empty dict on any failure so
    the caller can fall back gracefully.
    """
    store_id = run.get("defaultKeyValueStoreId")
    if not store_id:
        return {}
    try:
        resp = requests.get(
            f"{APIFY_BASE}/key-value-stores/{store_id}/records/INPUT",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=15,
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json() or {}
    except Exception:
        return {}


def fetch_dataset_items(dataset_id: str, api_token: str) -> list[dict]:
    """Fetch all items from a dataset by ID."""
    headers = {"Authorization": f"Bearer {api_token}"}
    items: list[dict] = []
    offset = 0
    limit = 1000
    while True:
        resp = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            headers=headers,
            params={"offset": offset, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return items


def search_history_run_ids(conn: sqlite3.Connection, search_id: str) -> set[str]:
    """The Apify run IDs this SEARCH has already ingested, read from ingest_history.

    History rows are keyed by ``state_key(search_id, task_name)`` — bare task names for the
    default search, ``"<search_id>:<task_name>"`` otherwise — so a search's rows are exactly
    those whose key carries its prefix. The prefix is matched with LIKE, and LIKE
    metacharacters in the search_id are escaped: ``_`` is a single-char wildcard and is
    plausible in a slug (e.g. ``mid_atlantic``), so an unescaped prefix could over-match a
    sibling search. Scope is the whole search, not one task, on purpose — see runs_to_process.
    """
    if search_id == DEFAULT_SEARCH_ID:
        # Default-search keys are bare (no colon); this mirrors the namespace-migration guard.
        cur = conn.execute("SELECT run_id FROM ingest_history WHERE task_name NOT LIKE '%:%'")
    else:
        like = search_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%"
        cur = conn.execute(
            "SELECT run_id FROM ingest_history WHERE task_name LIKE ? ESCAPE '\\'", (like,)
        )
    return {row[0] for row in cur}


def runs_to_process(
    conn: sqlite3.Connection,
    search_id: str,
    all_runs: list[dict],
) -> list[dict]:
    """Return the runs this SEARCH hasn't ingested yet, in chronological order.

    "Already ingested" is decided by run_id membership in this search's ingest_history —
    NOT by the task name. Apify run IDs are globally unique and survive a task rename
    (renaming a task on Apify re-labels its runs but keeps their IDs), so keying off the
    run_id makes ingest rename-proof: a task renamed A→B, renamed back to A, or replaced by
    a brand-new task reusing the name A all resolve correctly, because the only question ever
    asked is "has this search already processed this run_id?" Keying off the mutable task
    name instead would treat every historical run as new the first time a renamed task is
    fetched and reprocess the entire backlog (churning triaged status via reset_on_change /
    auto-close replay, and re-billing AI reformatting).

    Scope is the *search*, not the individual task, so the old task's already-seen run IDs
    still count after a rename. It stays per-search (via the history-key prefix) so two
    searches sharing one Apify task track it independently — search B still ingests a shared
    run after search A has.

    With no history for this search yet, nothing is filtered, so a new search/task picks up
    its full backlog. Use --dry-run first to preview what will be ingested.
    """
    seen = search_history_run_ids(conn, search_id)
    return [run for run in all_runs if run["id"] not in seen]


def _scalar(val: object) -> object:
    """Return the first element if val is a list, otherwise val as-is."""
    return val[0] if isinstance(val, list) else val


def _now_iso() -> str:
    """Return current UTC time as an ISO 8601 string ending in Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_history(conn: sqlite3.Connection, job_id: str, entry: dict,
                   search_id: str = DEFAULT_SEARCH_ID) -> None:
    """Append one event dict to a job's per-lens history JSON array (atomic, no
    read-modify-write). History is per-(job, search): it lives on job_search_state, so a
    status change under one lens doesn't interleave with another's. Defaults to the single
    ``__default__`` search, so single-search callers need no change. Requires the (job,
    search) membership row to exist (ingest creates it before its first append)."""
    conn.execute(
        "UPDATE job_search_state SET history = json_insert(COALESCE(history, '[]'), '$[#]', json(?)) "
        "WHERE job_id = ? AND search_id = ?",
        (json.dumps(entry, ensure_ascii=False), job_id, search_id),
    )


def bootstrap_history(conn: sqlite3.Connection) -> None:
    """Populate approximate history for jobs that have none (run once on migration).

    Constructs a logically coherent event chain from available data.
    All entries are marked approx=true since timestamps are estimated.
    """
    from datetime import timedelta

    today = datetime.now(timezone.utc).date().isoformat()
    applied_family = {"applied", "interviewing", "offered", "rejected", "withdrawn", "ghosted"}

    rows = conn.execute(
        "SELECT job_id, first_seen, applied_at, status FROM jobs "
        "WHERE history IS NULL OR history = '[]'"
    ).fetchall()

    for row in rows:
        history: list[dict] = []
        status    = row["status"]
        applied_at = row["applied_at"]

        # Normalise first_seen to a full ISO datetime with Z
        fs_raw = row["first_seen"] or ""
        if fs_raw:
            fs_date = fs_raw[:10]
            fs_time = fs_raw[11:19] if len(fs_raw) > 10 else "12:00:00"
            fs_dt   = f"{fs_date}T{fs_time}Z"
        else:
            fs_date = today
            fs_dt   = today + "T12:00:00Z"

        # 1. Ingested
        history.append({"ts": fs_dt, "event": "ingested", "approx": True})

        def _after(base_dt: str, minutes: int = 1) -> str:
            """Return base_dt + N minutes, guaranteed to be >= fs_dt."""
            try:
                dt = datetime.fromisoformat(base_dt.replace("Z", "+00:00"))
                result = dt + timedelta(minutes=minutes)
            except ValueError:
                result = datetime.fromisoformat(fs_dt.replace("Z", "+00:00")) + timedelta(minutes=minutes)
            # Never let an approximate status timestamp precede ingestion
            fs_parsed = datetime.fromisoformat(fs_dt.replace("Z", "+00:00"))
            if result <= fs_parsed:
                result = fs_parsed + timedelta(minutes=minutes)
            return result.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 2. Status at the time of ingestion (best guess)
        if status in ("reviewing", "skipped", "autoskipped"):
            history.append({
                "ts": _after(fs_dt, 1),
                "event": "status", "from": "new", "to": status, "approx": True,
            })

        # 3. Application-path events — timestamps must be >= ingestion time
        if applied_at and status in (applied_family | {"closed"}):
            at_date = applied_at[:10]
            # If applied_at is the same day as first_seen, anchor after ingestion;
            # otherwise use noon of the applied date (safe since it's a different day).
            if at_date == fs_date:
                applied_ts = _after(fs_dt, 1)
            else:
                applied_ts = at_date + "T12:02:00Z"
            history.append({
                "ts": applied_ts,
                "event": "status", "from": "new", "to": "applied", "approx": True,
            })
            if status != "applied":
                try:
                    next_date = (
                        datetime.fromisoformat(at_date) + timedelta(days=1)
                    ).date().isoformat()
                except ValueError:
                    next_date = at_date
                history.append({
                    "ts": _after(applied_ts, 1),
                    "event": "status", "from": "applied", "to": status, "approx": True,
                })
        elif status == "closed" and not applied_at:
            history.append({
                "ts": _after(fs_dt, 1),
                "event": "status", "from": "new", "to": "closed", "approx": True,
            })

        conn.execute(
            "UPDATE jobs SET history = ? WHERE job_id = ?",
            (json.dumps(history, ensure_ascii=False), row["job_id"]),
        )
    conn.commit()


# Statuses safe to auto-close when a posting expires: only jobs the user hasn't acted
# on. An applied/interviewing/etc. job is left as-is — the application outcome still
# matters even after the listing comes down.
AUTO_CLOSE_STATUSES = {"new", "reviewing"}


def is_expired(item: dict) -> bool:
    """True if the posting's validity date is in the past.

    Reads date_valid_through. Used to insert an arrived-expired posting straight as
    'closed', and to auto-close an active job whose listing has since lapsed.
    """
    val = _scalar(item.get("date_valid_through"))
    if not val:
        return False
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


# How close to the match threshold the first description-direction must score before we
# bother computing the reverse direction (the autojunk-asymmetry guard). The measured
# asymmetry gap between the two directions is small (< 0.07), so this margin is generous
# headroom: a candidate scoring further than this below the threshold can't be a match.
_REVERSE_MARGIN = 0.15

# Word-shingle Jaccard floor: a cheap, order-sensitive pre-gate before the O(n*m) description
# compare. Genuine near-duplicates (SequenceMatcher >= 0.85) share almost all their word
# 3-grams (measured Jaccard >= 0.87); postings that merely reuse the same role boilerplate
# share vocabulary but not phrase order, which fools the char-multiset quick_ratio but not
# shingles. Set FAR below the true-match floor so it only discards candidates with virtually
# no shared phrasing — anything with real overlap still goes to the full SequenceMatcher.
_SHINGLE_K = 3
_JACCARD_GATE = 0.2

# Title word-overlap floor: a token-level gate layered on top of the char-ratio pre-filter.
# Character similarity rewards a shared tail ("... Project Manager"), so distinct roles that
# differ only by a leading qualifier — "Engineering Project Manager" vs "Technical Project
# Manager" — score 0.73 char-wise and merge even when their descriptions are near-identical
# boilerplate. Comparing the *word sets* instead (Jaccard on lowercased alnum tokens) makes the
# distinguishing word count: that pair shares 2 of 4 distinct words (0.5) and is now kept
# separate, while a same-role suffix variant an aggregator produces ("Software Engineer" vs
# "Software Engineer - Remote", 0.67) still clears it. This is a stricter AND on top of the
# char-ratio pre-filter — it can only reject more, never rescue a pair that pre-filter already
# dropped (a heavy word *reorder* fails the char ratio first and never reaches this gate).
# The cost is that pure abbreviation reworites ("Sr." vs "Senior") no longer merge; that's rare
# and the safe direction (a spurious duplicate row beats hiding a genuinely different opening).
_TITLE_WORD_GATE = 0.6


def _title_words(title: str) -> set[str]:
    """Lowercased alphanumeric word tokens of a job title (punctuation split out and dropped),
    used for the word-overlap gate. Empty set for a title with no alnum tokens — the caller then
    skips the gate rather than blocking, since Jaccard is undefined without any words to compare."""
    return {w for w in re.split(r"[^0-9a-z]+", title.lower()) if w}


# Candidate identifier tokens: alphanumeric runs optionally joined by - or / (so a req code like
# "AQ-14258" or "2024-1234" stays one token instead of splitting on the hyphen), plus a 4+-digit
# run test for bare numeric IDs.
_ID_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-/][a-z0-9]+)*")
_LONG_DIGIT_RE = re.compile(r"\d{4,}")


def _title_id_codes(title: str) -> set[str]:
    """Identifier codes embedded in a job title — req/posting IDs like "AQ-14258", a bare "2024",
    or a level tag like "L5". A token qualifies only when it carries a real code signal: it mixes
    letters and digits, or it contains a 4+-digit run. Bare short numbers ("Level 3") are excluded
    because they're rarely IDs and single digits are noisy. The word-overlap gate can't catch a
    differing req ID — "[AQ-14258]" vs "[AQ-15000]" share the "aq" prefix and all the role words,
    so they score 0.67 and merge — but two postings whose titles carry *different* codes are
    different requisitions, so the caller refuses to merge them even when everything else (often a
    byte-identical ATS template) is identical. Missing an occasional same-req repost that reworded
    its code is the safe direction: a visible duplicate row beats hiding a distinct opening."""
    codes = set()
    for tok in _ID_TOKEN_RE.findall(title.lower()):
        if not any(c.isdigit() for c in tok):
            continue
        if any(c.isalpha() for c in tok) or _LONG_DIGIT_RE.search(tok):
            codes.add(tok)
    return codes


def _word_shingles(text: str, k: int = _SHINGLE_K) -> set | None:
    """Set of contiguous word k-grams in `text`, or None when it has fewer than k words
    (too short for a meaningful shingle overlap — the caller then skips the gate)."""
    words = text.split()
    if len(words) < k:
        return None
    return {tuple(words[i:i + k]) for i in range(len(words) - k + 1)}


def find_canonical(
    conn: sqlite3.Connection,
    job_id: str,
    title: str | None,
    company: str | None,
    description: str | None,
    threshold: float,
    title_threshold: float = 0.6,
    title_word_threshold: float = _TITLE_WORD_GATE,
    title_id_gate: bool = True,
    search_id: str | None = None,
) -> list[sqlite3.Row]:
    """Return the canonical-root jobs that are near-duplicates, sorted oldest-first.

    Matches against *every* posting — both canonical roots and already-linked
    members — then resolves each hit to its canonical root (one hop:
    canonical_id or job_id) and returns the distinct roots.  Matching members,
    not just roots, is essential for aggregators (Jobgether, RemoteHunter) that
    rewrite a posting's prose: their reposts share ~0 description overlap with
    the original ATS canonical but are near-identical to a *sibling* repost
    already in the group — which is always a member.  Resolving to the root
    keeps the no-chain invariant (roots have canonical_id IS NULL).  No company
    filter is applied — the same job appears under different aggregator names.
    A title similarity >= title_threshold char-ratio pre-filter keeps the search
    efficient; a title word-overlap (Jaccard) >= title_word_threshold gate then
    rejects distinct roles that merely share a tail phrase (e.g. "Engineering
    Project Manager" vs "Technical Project Manager").  When title_id_gate is set,
    a differing req/posting ID baked into the two titles (e.g. "[AQ-14258]" vs
    "[AQ-15000]") is an outright disqualifier — different requisitions never
    merge no matter how identical the rest is.  Description similarity >=
    threshold is the final gate.

    The caller should treat matches[0] as the canonical (oldest first_seen) and
    link all remaining matches to it, preventing future fragmentation.
    """
    if not description or not title:
        return []
    # Project only the columns the matcher and the caller need — NOT SELECT * — so we don't
    # marshal the large per-row blobs (raw JSON, history, formatted description) for all
    # ~thousands of postings on every new job. The caller reads job_id/title/company/
    # company_actual/status/applied_at off the returned canonical; the rest are for matching.
    # When search_id is given, restrict candidates to that search's members (automatic dedup
    # never crosses searches) and source status/applied_at from that lens's job_search_state
    # row so an inherited status reflects the search being ingested. search_id=None keeps the
    # legacy unrestricted behavior (reads the dormant jobs.status) for callers/tests that pass none.
    if search_id is None:
        candidates = conn.execute(
            "SELECT job_id, title, company, company_actual, status, applied_at, "
            "job_description, canonical_id, first_seen FROM jobs WHERE job_id != ?",
            (job_id,),
        ).fetchall()
    else:
        candidates = conn.execute(
            "SELECT jobs.job_id, jobs.title, jobs.company, jobs.company_actual, "
            "jss.status AS status, jss.applied_at AS applied_at, jobs.job_description, "
            "jobs.canonical_id, jobs.first_seen "
            "FROM jobs JOIN job_search_state jss "
            "ON jss.job_id = jobs.job_id AND jss.search_id = ? "
            "WHERE jobs.job_id != ?",
            (search_id, job_id),
        ).fetchall()
    desc_len = len(description)
    # Distinct canonical roots we matched, keyed by root job_id. A member match
    # contributes its root; the first time we see a root we resolve and cache its row.
    roots: dict[str, sqlite3.Row] = {}
    row_by_id: dict[str, sqlite3.Row] = {c["job_id"]: c for c in candidates}
    # Cache the NEW description as seq2 once. difflib builds its expensive b2j chain only on
    # seq2, so setting it once and swapping seq1 per candidate (its recommended one-vs-many
    # pattern) avoids rebuilding that chain for every one of thousands of candidates.
    new_sm = SequenceMatcher(None)
    new_sm.set_seq2(description)
    new_shingles = _word_shingles(description)  # None if the new desc is too short to gate
    new_words = _title_words(title)  # word set for the title-overlap gate (loop-invariant)
    # Req/posting-ID codes in the new title (loop-invariant); empty disables the ID gate for it.
    new_ids = _title_id_codes(title) if title_id_gate else set()
    for candidate in candidates:
        if not candidate["title"] or not candidate["job_description"]:
            continue
        # Title pre-filter: quick_ratio is an upper bound on ratio()
        title_m = SequenceMatcher(None, title.lower(), candidate["title"].lower())
        if title_m.quick_ratio() < title_threshold:
            continue
        if title_m.ratio() < title_threshold:
            continue
        # Title word-overlap gate: char-ratio rewards a shared tail phrase, so distinct roles
        # with the same suffix ("… Project Manager") slip through it. Require the word sets to
        # overlap too, which makes the differing qualifier count. Skip when either title has no
        # alnum tokens (Jaccard undefined) — the char check already vetted those.
        cand_words = _title_words(candidate["title"])
        if new_words and cand_words:
            union = len(new_words | cand_words)
            if len(new_words & cand_words) / union < title_word_threshold:
                continue
        # Req/posting-ID gate: when both titles carry identifier codes and they share none, the
        # postings are different requisitions — disqualify outright, even if descriptions are a
        # byte-identical ATS template. A shared code (or one side lacking a code) falls through
        # to the normal gates: an equal req is a match, and we can't infer a difference from a
        # code an aggregator stripped.
        if new_ids:
            cand_ids = _title_id_codes(candidate["title"])
            if cand_ids and not (new_ids & cand_ids):
                continue
        cand_desc = candidate["job_description"]
        # Cheap length-ratio pre-gate before the O(n*m) description compare. ratio() = 2*M/
        # (la+lb) with M (matched chars) <= min(la, lb), so 2*min/(la+lb) is a hard upper
        # bound (difflib's own real_quick_ratio). If even that can't reach the threshold, the
        # real ratio can't either — skip without constructing a SequenceMatcher. This prunes
        # the many same-title, different-length descriptions a common title (e.g. "…Program
        # Manager") surfaces, which otherwise dominate ingest time as the DB grows.
        cl = len(cand_desc)
        if 2.0 * min(desc_len, cl) / (desc_len + cl) < threshold:
            continue
        # Word-shingle Jaccard pre-gate: discards candidates with virtually no shared phrase
        # order (role boilerplate that shares vocabulary but isn't a near-duplicate) before
        # the expensive compare. Skipped when either description is too short for reliable
        # shingles — those are cheap to compare directly anyway.
        if new_shingles is not None:
            cand_shingles = _word_shingles(cand_desc)
            if cand_shingles is not None:
                union = len(new_shingles | cand_shingles)
                if union and len(new_shingles & cand_shingles) / union < _JACCARD_GATE:
                    continue
        # Swap only seq1 (the candidate); the new description stays cached as seq2. quick_ratio
        # is a cheap symmetric upper bound on ratio — skip before the O(n*m) work when it can't
        # reach the threshold.
        new_sm.set_seq1(cand_desc)
        if new_sm.quick_ratio() < threshold:
            continue
        # Description check. SequenceMatcher.ratio() is asymmetric — autojunk (difflib's
        # default speed heuristic) only applies to the *second* sequence — so the same pair
        # can score differently depending on argument order, which let cross-source duplicates
        # slip through based on ingest order. Neutralize it by taking either direction: with
        # the new desc as seq2, new_sm.ratio() is one direction; on a near-miss compute the
        # other (candidate as seq2). Match if EITHER reaches the threshold — same result as
        # the old ratio-then-reverse, just with seq2 cached for the common direction.
        ratio = new_sm.ratio()
        # Only pay for the reverse direction when the first is within a margin of the
        # threshold. The autojunk asymmetry gap is small (measured < 0.07), so a candidate
        # scoring well below the threshold one way can't cross it the other — computing the
        # reverse for the many clear non-matches is pure waste. The 0.15 margin is generous
        # headroom over the observed gap.
        if threshold - _REVERSE_MARGIN <= ratio < threshold:
            ratio = SequenceMatcher(None, description, cand_desc).ratio()
        if ratio < threshold:
            continue
        # Resolve a matched member to its canonical root so we return roots, not members.
        root_id = candidate["canonical_id"] or candidate["job_id"]
        if root_id not in roots:
            # The root row is normally in this run's candidate set; fall back to a lookup if a
            # member's root wasn't itself a candidate. With search restriction that happens for
            # a manually cross-search-merged group whose root lives in another search — the
            # exact-job_id lookup still resolves it (preserving the one-hop invariant); source
            # status/applied_at from this lens's state row (NULL if the root isn't a member here).
            fallback = row_by_id.get(root_id)
            if fallback is None:
                if search_id is None:
                    fallback = conn.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (root_id,)).fetchone()
                else:
                    fallback = conn.execute(
                        "SELECT jss.status AS status, jss.applied_at AS applied_at, jobs.* "
                        "FROM jobs LEFT JOIN job_search_state jss "
                        "ON jss.job_id = jobs.job_id AND jss.search_id = ? "
                        "WHERE jobs.job_id = ?", (search_id, root_id)).fetchone()
            roots[root_id] = fallback
    matches = [r for r in roots.values() if r is not None]
    # Sort oldest-first so matches[0] is the most-canonical candidate.
    matches.sort(key=lambda r: r["first_seen"] or "")
    return matches


# Annualize AI-extracted salary figures by their unit, so hourly/monthly bands
# are stored and compared on the same scale as the common annual case.
# Full-time-equivalent assumption: 40h × 52wk = 2080h/yr.
SALARY_PERIOD_MULTIPLIER = {
    "HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1,
}


def _normalize_salary(value: object, unit: object) -> int | None:
    """Convert one AI-extracted salary figure to an annual amount based on its
    unit (HOUR/DAY/WEEK/MONTH/YEAR). Unknown or missing units are left as-is
    (treated as already annual — the prior behaviour)."""
    if value in (None, "", "null"):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    mult = SALARY_PERIOD_MULTIPLIER.get(str(unit or "").strip().upper(), 1)
    return round(amount * mult)


def extract_salary(item: dict) -> tuple[int | None, int | None]:
    """(min, max) annual salary from an Apify item, normalized by its unit text."""
    unit = _scalar(item.get("ai_salary_unit_text"))
    lo = _normalize_salary(_scalar(item.get("ai_salary_min_value")), unit)
    hi = _normalize_salary(_scalar(item.get("ai_salary_max_value")), unit)
    return lo, hi


def build_company_alias_map(raw: "dict | None") -> dict:
    """Build the company-name normalization lookup from the config [company_aliases]
    table. The table maps each variant spelling → the canonical name to store, e.g.
    {"Sirius XM": "SiriusXM", "Sirius XM Radio": "SiriusXM"}.

    Matching is case-insensitive (user's choice), so we key the lookup on the trimmed,
    lower-cased variant; the canonical *value* is kept verbatim as written in config
    (that exact casing is what gets stored). Returns {} when unset, so callers can treat
    "no aliases" and "feature off" identically. A later duplicate key (two variants that
    differ only in case) simply wins — harmless, and not worth a warning."""
    if not raw:
        return {}
    return {str(variant).strip().lower(): str(canonical) for variant, canonical in raw.items()}


def normalize_company(name: object, alias_map: dict) -> object:
    """Return the canonical company name for `name` per `alias_map`, else `name` as-is.

    Match is case-insensitive on the trimmed name. We only rewrite on a hit — a
    non-matching name is returned completely untouched (original casing/whitespace), so
    this never silently mangles companies that aren't in the list. None/empty pass
    through unchanged."""
    if not name or not alias_map:
        return name
    return alias_map.get(str(name).strip().lower(), name)


def extract_company_url(item: dict) -> str | None:
    """Best-effort URL for the *employer's* own site from a feed item, or None.

    Preference order, best first:
      1. `linkedin_org_url`  — the employer's real website (LinkedIn feed), e.g. hdrinc.com
      2. `domain_derived`    — the employer's bare domain (careersite feed), e.g. acme.net
      3. `organization_url`  — fallback: the org's page *on the feed source* (an ATS or
                               LinkedIn company page), not the employer's own site
    A bare domain (no scheme) is promoted to https://. Feed values are frequently absent
    or the literal string "None"/"null"; those are treated as missing.
    """
    def _clean(v: object) -> str | None:
        s = str(_scalar(v) or "").strip()
        return s if s and s.lower() not in ("none", "null") and "." in s else None

    for key in ("linkedin_org_url", "domain_derived", "organization_url"):
        url = _clean(item.get(key))
        if url:
            return url if "://" in url else f"https://{url}"
    return None


# careersite (ATS) feeds — Oracle HCM, Workable, ADP, Paycom, Greenhouse, … — sometimes
# expose only a short teaser in `description_text` while the full body (responsibilities,
# qualifications, benefits) is rendered client-side and never reaches the feed. The actor
# still AI-extracts a requirements summary from the fuller content it scraped, so a short
# `description_text` paired with a populated `ai_requirements_summary` is our signal that the
# stored description is partial. LinkedIn's `description_text` carries the whole body, so this
# never applies there. Tuned from real data (see the truncation blast-radius analysis): genuine
# full careersite descriptions run ~5.7k chars median, and the truncated tail sits well under
# this cap, so the bound is deliberately generous — a false positive only costs a manual review
# (the row is surfaced, not hidden), which is the safe direction to err.
_TRUNCATED_DESC_MAXLEN = 2000


def feed_description_truncated(item: dict, description: "str | None", actor_type: str) -> bool:
    """True when a careersite feed item's `description_text` looks truncated to a teaser.

    Only careersite/ATS feeds are affected (LinkedIn carries the full body). The heuristic:
    a short stored description AND the actor populated `ai_requirements_summary` — evidence it
    saw a fuller posting than the feed exposed. Used to flag the row so viability scoring won't
    silently auto-skip a possibly-good role judged on half a posting, and the UI can badge it.
    """
    if actor_type != "careersite":
        return False
    if len(description or "") >= _TRUNCATED_DESC_MAXLEN:
        return False
    return bool(_scalar(item.get("ai_requirements_summary")))


def backfill_description_truncated(conn: sqlite3.Connection) -> int:
    """Populate `description_truncated` for careersite rows predating the column, from each
    row's stored raw feed JSON. Returns the number of rows flagged.

    Runs once, right after the column is added (see open_db) — every other row already defaults
    to 0, so we only need to flip the careersite postings that the heuristic catches. A row whose
    raw JSON won't parse is left at its default (unflagged). Kept a standalone helper so app.py
    and rescore_viability.py can call it from their own migrations, and so it's unit-testable."""
    flagged = 0
    rows = conn.execute(
        "SELECT job_id, job_description, raw FROM jobs WHERE source = 'careersite'"
    ).fetchall()
    for r in rows:
        try:
            item = json.loads(r["raw"])
        except (ValueError, TypeError):
            continue
        if feed_description_truncated(item, r["job_description"], "careersite"):
            conn.execute(
                "UPDATE jobs SET description_truncated = 1 WHERE job_id = ?", (r["job_id"],)
            )
            flagged += 1
    conn.commit()
    return flagged


def extract_fields_linkedin(item: dict) -> dict:
    """Map one fantastic-jobs LinkedIn actor item to our jobs-table field dict."""
    # Field names from fantastic-jobs/advanced-linkedin-job-search-api.
    # `linkedin_id` is the actual LinkedIn job ID used as our PK (type changed to int June 2026;
    #   str() conversion handles both old string and new integer values).
    # `direct_apply` = LinkedIn Easy Apply.
    # Salary fields are AI-extracted by the actor and may be absent.
    # `external_apply_url` was removed June 2026 with no replacement; apply_url will be None.
    # _scalar() guards against fields that are arrays in JSON for multi-value records.
    salary_min, salary_max = extract_salary(item)
    return {
        "job_id": str(_scalar(item.get("linkedin_id")) or "").strip(),
        "title": _scalar(item.get("title")),
        "company": _scalar(item.get("organization")),
        "location": _scalar(item.get("locations_derived")),
        "posted_date": _scalar(item.get("date_posted")),
        "job_url": f"https://www.linkedin.com/jobs/view/{_scalar(item.get('linkedin_id'))}",
        "apply_url": _scalar(item.get("external_apply_url")) or None,
        "company_url": extract_company_url(item),
        "easy_apply": 1 if str(
            _scalar(item.get("direct_apply") or "") or ""
        ).lower() == "true" else 0,
        "source": "linkedin",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": _scalar(item.get("ai_salary_currency")) or None,
        "job_description": _scalar(item.get("description_text")),
        # LinkedIn feeds carry the full body, so never truncated (see feed_description_truncated).
        "description_truncated": 0,
    }


def extract_fields_careersite(item: dict) -> dict:
    """Map one fantastic-jobs career-site actor item to our jobs-table field dict."""
    # Field names from fantastic-jobs/career-site-job-listing-api.
    # `id` is the actor's internal job ID; we prefix it with "cs_" to avoid
    # any collision with numeric LinkedIn IDs stored in the same table.
    # `url` is both the canonical job page and the apply URL (career sites have no
    # separate apply link). Easy Apply is not applicable.
    salary_min, salary_max = extract_salary(item)
    raw_id  = str(_scalar(item.get("id")) or "").strip()
    job_url = _scalar(item.get("url")) or None
    description = _scalar(item.get("description_text"))
    return {
        "job_id": f"cs_{raw_id}" if raw_id else "",
        "title": _scalar(item.get("title")),
        "company": _scalar(item.get("organization")),
        "location": _scalar(item.get("locations_derived")),
        "posted_date": _scalar(item.get("date_posted")),
        "job_url": job_url,
        "apply_url": job_url,
        "company_url": extract_company_url(item),
        "easy_apply": 0,
        "source": "careersite",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": _scalar(item.get("ai_salary_currency")) or None,
        "job_description": description,
        # Flag a teaser-only ATS feed description so it isn't silently auto-skipped or scored
        # as if complete (see feed_description_truncated).
        "description_truncated": 1 if feed_description_truncated(item, description, "careersite") else 0,
    }


class DescriptionFormatter:
    """Optional AI reformatting of descriptions, with an exact-match cache.

    Created once per ingest run. When disabled (no client) ``format()`` returns
    None so the heuristic renderer is used. The cache skips the AI call for any
    byte-identical description already formatted — within this run (in-memory) or
    in a prior run (DB lookup) — which is the common "same posting in N locations"
    case. Tracks token usage and per-run counts for the summary line.
    """

    def __init__(self, client=None, model: str = "claude-haiku-4-5",
                 effort: str = DEFAULT_EFFORT):
        self.client = client
        self.model = model
        self.effort = effort   # thinking effort, applied only if `model` is a reasoning model
        self._cache: dict[str, str] = {}
        self.via_ai = 0
        self.reused = 0
        self.discarded = 0   # AI returned text but it failed the integrity check
        self.failed = 0      # AI call errored / returned nothing
        self.tok_input = self.tok_output = self.tok_write = self.tok_read = 0

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def format(self, conn: sqlite3.Connection, description: str,
               desc_hash: str | None, label: str = "") -> str | None:
        """Return formatted Markdown for a description, or None.

        `label` (e.g. "<job_id> (<title>)") is used only in the rejected/failed
        log lines so it's clear which posting fell back to the heuristic renderer.

        Skips the AI call on an exact-match cache hit (run-local dict, then a
        cross-run DB lookup keyed on hash + exact text). On a miss, calls the AI
        and accepts the result only if it passes the content-integrity check.
        """
        if not self.enabled or not desc_hash or not (description or "").strip():
            return None
        cached = self._cache.get(desc_hash)
        if cached is not None:
            self.reused += 1
            return cached
        hit = conn.execute(
            "SELECT job_description_formatted FROM jobs "
            "WHERE description_hash = ? AND job_description = ? "
            "AND job_description_formatted IS NOT NULL LIMIT 1",
            (desc_hash, description),
        ).fetchone()
        if hit and hit[0]:
            self._cache[desc_hash] = hit[0]
            self.reused += 1
            return hit[0]
        md, usage = reformat_description(self.client, description, self.model, self.effort)
        if usage is not None:
            self.tok_input  += getattr(usage, "input_tokens",                0) or 0
            self.tok_output += getattr(usage, "output_tokens",               0) or 0
            self.tok_write  += getattr(usage, "cache_creation_input_tokens", 0) or 0
            self.tok_read   += getattr(usage, "cache_read_input_tokens",     0) or 0
        if md and content_preserved(description, md):
            self._cache[desc_hash] = md
            self.via_ai += 1
            return md
        suffix = f" for {label}" if label else ""
        if md:
            # The model altered content (not just formatting) — a prompt-quality
            # signal worth investigating, so flag it loudly and ask for a bug report.
            self.discarded += 1
            print(f"  WARNING: AI reformat altered content{suffix} and was rejected "
                  "(used heuristic formatter). If this recurs, please file a bug so the "
                  "reformatting prompt can be tightened.", file=sys.stderr)
        else:
            # Transient/operational — API error or empty response.
            self.failed += 1
            print(f"  NOTE: AI reformat failed{suffix} (API error or empty response; "
                  "using heuristic formatter)", file=sys.stderr)
        return None

    def summary(self) -> str | None:
        """One-line run summary, or None if no formatting work happened."""
        if not (self.via_ai or self.reused or self.discarded or self.failed):
            return None
        parts = [f"{self.via_ai} via AI", f"{self.reused} reused"]
        if self.discarded:
            parts.append(f"{self.discarded} discarded")
        if self.failed:
            parts.append(f"{self.failed} failed")
        line = "Description formatting: " + ", ".join(parts)
        toks = format_token_summary(
            self.model, input=self.tok_input, output=self.tok_output,
            cache_write=self.tok_write, cache_read=self.tok_read,
        )
        if toks:
            line += " — " + toks
        return line


def ingest(conn: sqlite3.Connection, items: list[dict], label: str,
           actor_type: str = "linkedin", exclude_ats_dups: bool = False,
           reset_on_change: bool = True,
           fuzzy_dedup: bool = True, fuzzy_desc_threshold: float = 0.85,
           fuzzy_title_threshold: float = 0.6,
           fuzzy_title_word_threshold: float = _TITLE_WORD_GATE,
           fuzzy_title_id_gate: bool = True,
           inherit_canonical_status: bool = True,
           company_aliases: "dict | None" = None,
           formatter: "DescriptionFormatter | None" = None,
           search_id: str = DEFAULT_SEARCH_ID) -> Counter:
    """Process one run's items for one search. Returns a Counter with these keys:
        inserted_clean / inserted_grouped / inserted_expired  — new postings, by kind
        updated / unchanged / skipped_ats                     — existing / skipped
        relinked / orphan_merges / reset_new / auto_closed    — side-ops on existing rows

    Per-lens: the shared posting fields go on `jobs` (one physical row per external id), while
    status/applied_at/history live in this ``search_id``'s ``job_search_state`` row (created on
    first membership). The same posting seen under two searches is one `jobs` row with two state
    rows. Automatic fuzzy dedup is restricted to this search's members.
    """
    c: Counter = Counter()

    # Upsert each item. For each: skip ATS dupes; extract our field dict; then branch on
    # whether the job_id already exists — new rows go through fuzzy-dedup + INSERT, existing
    # rows go through a change check + UPDATE. The existence check happens BEFORE any AI
    # reformat call, so reformatting only runs for genuinely new/changed descriptions.
    for item in items:
        if exclude_ats_dups and item.get("ats_duplicate") is True:
            c["skipped_ats"] += 1
            continue
        fields = extract_fields_careersite(item) if actor_type == "careersite" else extract_fields_linkedin(item)
        if not fields["job_id"]:
            print(f"  WARNING: item missing job_id, skipping: {list(item.keys())}", file=sys.stderr)
            continue

        # Normalize the company name before anything reads it, so the canonical spelling
        # is what gets stored, deduped, grouped, searched, and scored. Done per item (not
        # as a backfill sweep) by design: only newly-ingested or re-seen jobs are
        # normalized — a job already in the DB under an old spelling is fixed when its
        # posting next reappears (the new normalized name then differs from the stored
        # one, tripping the change check below into an UPDATE).
        # Capture the feed's spelling first so we can log the rewrite to the job's
        # History (audit trail) at the end of the loop, once the row is guaranteed to
        # exist. company_was_normalized is True only when the alias actually fired.
        original_company = fields["company"]
        fields["company"] = normalize_company(original_company, company_aliases or {})
        company_was_normalized = fields["company"] != original_company

        raw = json.dumps(item, ensure_ascii=False)
        desc = fields["job_description"] or ""
        desc_hash = description_hash(desc) if desc.strip() else None

        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (fields["job_id"],),
        ).fetchone()

        expired = is_expired(item)

        if row is None:
            canonical_id = None
            default_status = "closed" if expired else "new"
            initial_status = default_status
            initial_applied_at = None
            initial_company_actual = None
            if fuzzy_dedup and not expired:
                matches = find_canonical(
                    conn, fields["job_id"], fields["title"], fields["company"],
                    fields["job_description"], fuzzy_desc_threshold, fuzzy_title_threshold,
                    fuzzy_title_word_threshold, fuzzy_title_id_gate, search_id=search_id,
                )
                if matches:
                    canonical = matches[0]
                    canonical_id = canonical["job_id"]
                    # Show the repost under the canonical's *effective* employer name
                    # (e.g. "Cribl" behind an aggregator like RemoteHunter/Jobgether) so
                    # it doesn't need a manual re-override every time. The override lives
                    # on the aggregator member, but the name to adopt is the root's
                    # effective one (its own company, or its override if it has one) —
                    # and only when the repost's scraped company actually differs, so a
                    # genuine copy already naming the employer isn't given a no-op override.
                    # Unconditional w.r.t. the status-inheritance toggle: naming is a
                    # display concern, not application status.
                    canonical_company = canonical["company_actual"] or canonical["company"]
                    if canonical_company and canonical_company != fields["company"]:
                        initial_company_actual = canonical_company
                    if inherit_canonical_status:
                        # Inherit the canonical's applied date alongside its status,
                        # so an auto-linked duplicate of an applied role isn't left
                        # 'applied' with a NULL applied_at.
                        initial_status = canonical["status"]
                        initial_applied_at = canonical["applied_at"]
                    print(
                        f"  NOTE: fuzzy match: {fields['job_id']} ({fields['title']}) "
                        f"→ canonical {canonical_id} ({canonical['title']}), "
                        f"status: {initial_status}"
                    )
                    # (the new posting itself is tallied as inserted_grouped below)
                    # Also merge any other matched roots into matches[0] so future jobs
                    # find one group rather than many. Re-point each other root AND its
                    # existing members (canonical_id = other) to preserve the no-chain
                    # invariant — leaving members pointing at a now-demoted root chains.
                    for other in matches[1:]:
                        conn.execute(
                            "UPDATE jobs SET canonical_id = ? "
                            "WHERE job_id = ? OR canonical_id = ?",
                            (canonical_id, other["job_id"], other["job_id"]),
                        )
                        print(
                            f"  NOTE: also linking orphan {other['job_id']} ({other['title']}) "
                            f"→ canonical {canonical_id}"
                        )
                        c["orphan_merges"] += 1
            formatted = (
                formatter.format(conn, desc, desc_hash,
                                 f"{fields['job_id']} ({fields['title']})")
                if formatter else None
            )
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, title, company, company_actual, location, posted_date,
                     job_url, apply_url, company_url, easy_apply, salary_min, salary_max, salary_currency,
                     labels, source, status, applied_at, job_description, canonical_id, raw,
                     description_hash, job_description_formatted, description_truncated)
                VALUES
                    (:job_id, :title, :company, :company_actual, :location, :posted_date,
                     :job_url, :apply_url, :company_url, :easy_apply, :salary_min, :salary_max, :salary_currency,
                     :labels, :source, :status, :applied_at, :job_description, :canonical_id, :raw,
                     :description_hash, :job_description_formatted, :description_truncated)
                """,
                {**fields, "labels": json.dumps([label]), "status": initial_status,
                 "applied_at": initial_applied_at, "company_actual": initial_company_actual,
                 "canonical_id": canonical_id, "raw": raw,
                 "description_hash": desc_hash, "job_description_formatted": formatted},
            )
            # Per-lens state row (membership + authoritative status/applied_at). Created before
            # any append_history below, which targets this search's history. jobs.status/applied_at
            # above are the dormant rollback copy; this row is what the app reads.
            conn.execute(
                "INSERT OR IGNORE INTO job_search_state "
                "(job_id, search_id, status, applied_at) VALUES (?, ?, ?, ?)",
                (fields["job_id"], search_id, initial_status, initial_applied_at),
            )
            if initial_status == "closed":
                c["inserted_expired"] += 1
            elif canonical_id:
                c["inserted_grouped"] += 1
            else:
                c["inserted_clean"] += 1
            ts = _now_iso()
            append_history(conn, fields["job_id"], {
                "ts": ts, "event": "ingested", "label": label, "source": actor_type,
            }, search_id)
            # Record the inherited status so the paper trail shows when it became e.g.
            # 'applied', matching the UI link route's behaviour.
            if initial_status != default_status:
                append_history(conn, fields["job_id"], {
                    "ts": ts, "event": "status", "from": default_status,
                    "to": initial_status, "note": "inherited from canonical on ingest",
                }, search_id)
            # Record the inherited company-name override, mirroring the UI link route's
            # "company_actual" event so the override's provenance is visible in history.
            if initial_company_actual:
                append_history(conn, fields["job_id"], {
                    "ts": ts, "event": "company_actual", "from": None,
                    "to": initial_company_actual, "note": "inherited from canonical on ingest",
                }, search_id)
            if initial_status == "closed":
                append_history(conn, fields["job_id"], {
                    "ts": ts, "event": "status", "from": "new", "to": "closed",
                }, search_id)
            elif canonical_id:
                append_history(conn, fields["job_id"], {
                    "ts": ts, "event": "linked", "canonical_id": canonical_id,
                }, search_id)
        else:
            # jobs row exists. Read this search's per-lens state; if the posting is new to THIS
            # search (same external id already present via another search), create a fresh 'new'
            # membership row rather than updating a nonexistent one.
            jss_row = conn.execute(
                "SELECT status, applied_at FROM job_search_state WHERE job_id = ? AND search_id = ?",
                (fields["job_id"], search_id),
            ).fetchone()
            new_membership = jss_row is None
            if new_membership:
                conn.execute(
                    "INSERT OR IGNORE INTO job_search_state (job_id, search_id, status) "
                    "VALUES (?, ?, 'new')",
                    (fields["job_id"], search_id),
                )
                append_history(conn, fields["job_id"], {
                    "ts": _now_iso(), "event": "ingested", "label": label, "source": actor_type,
                }, search_id)
            current_status = jss_row["status"] if jss_row else "new"
            existing_labels: list[str] = json.loads(row["labels"])
            new_labels = existing_labels if label in existing_labels else existing_labels + [label]

            desc_changed = fields["job_description"] != row["job_description"]
            now = datetime.now(timezone.utc).isoformat()
            refreshed_at = row["refreshed_at"]  # preserve unless we're setting it now
            canonical_id = row["canonical_id"]  # preserve existing link by default

            if expired and current_status in AUTO_CLOSE_STATUSES:
                new_status = "closed"
                c["auto_closed"] += 1
            elif desc_changed and current_status in ("skipped", "autoskipped") and reset_on_change:
                new_status = "new"
                refreshed_at = now
                c["reset_new"] += 1
                print(f"  NOTE: description changed for job {fields['job_id']} ({fields['title']}), resetting from {current_status} → new")
            else:
                new_status = current_status

            # Check for a fuzzy canonical on previously-unlinked jobs.
            if fuzzy_dedup and canonical_id is None:
                matches = find_canonical(
                    conn, fields["job_id"], fields["title"], fields["company"],
                    fields["job_description"], fuzzy_desc_threshold, fuzzy_title_threshold,
                    fuzzy_title_word_threshold, fuzzy_title_id_gate, search_id=search_id,
                )
                if matches:
                    canonical = matches[0]
                    canonical_id = canonical["job_id"]
                    print(
                        f"  NOTE: fuzzy match: {fields['job_id']} ({fields['title']}) "
                        f"→ canonical {canonical_id} ({canonical['title']})"
                    )
                    c["relinked"] += 1
                    for other in matches[1:]:
                        # Re-point each other root AND its members (see the insert-path
                        # merge above) so merging two groups never leaves a chain.
                        conn.execute(
                            "UPDATE jobs SET canonical_id = ? "
                            "WHERE job_id = ? OR canonical_id = ?",
                            (canonical_id, other["job_id"], other["job_id"]),
                        )
                        print(
                            f"  NOTE: also linking orphan {other['job_id']} ({other['title']}) "
                            f"→ canonical {canonical_id}"
                        )
                        c["orphan_merges"] += 1

            something_changed = (
                new_labels != existing_labels
                or new_status != current_status
                or canonical_id != row["canonical_id"]
                or fields["title"] != row["title"]
                or fields["company"] != row["company"]
                or fields["location"] != row["location"]
                or fields["salary_min"] != row["salary_min"]
                or fields["salary_max"] != row["salary_max"]
                or fields["job_description"] != row["job_description"]
            )

            # Regenerate the formatted version only when the description changed; an
            # unchanged description keeps its existing formatting (no token spend).
            # When the description changed but AI is off, format() returns None,
            # clearing a now-stale formatting so the heuristic renderer takes over.
            if desc_changed:
                formatted = (
                    formatter.format(conn, desc, desc_hash,
                                     f"{fields['job_id']} ({fields['title']})")
                    if formatter else None
                )
            else:
                formatted = row["job_description_formatted"]
            conn.execute(
                """
                UPDATE jobs SET
                    title = :title, company = :company, location = :location,
                    posted_date = :posted_date, job_url = :job_url,
                    apply_url = :apply_url, company_url = :company_url, easy_apply = :easy_apply,
                    salary_min = :salary_min, salary_max = :salary_max,
                    salary_currency = :salary_currency,
                    job_description = :job_description,
                    labels = :labels, source = :source, status = :status,
                    refreshed_at = :refreshed_at, canonical_id = :canonical_id, raw = :raw,
                    description_hash = :description_hash,
                    job_description_formatted = :job_description_formatted,
                    description_truncated = :description_truncated
                WHERE job_id = :job_id
                """,
                {**fields, "labels": json.dumps(new_labels), "status": new_status,
                 "refreshed_at": refreshed_at, "canonical_id": canonical_id, "raw": raw,
                 "description_hash": desc_hash, "job_description_formatted": formatted},
            )
            # Per-lens status write (authoritative). refreshed_at stays a shared jobs column.
            if new_status != current_status:
                conn.execute(
                    "UPDATE job_search_state SET status = ? WHERE job_id = ? AND search_id = ?",
                    (new_status, fields["job_id"], search_id),
                )
            if something_changed:
                c["updated"] += 1
                ts = _now_iso()
                if new_status != current_status:
                    append_history(conn, fields["job_id"], {
                        "ts": ts, "event": "status", "from": current_status, "to": new_status,
                    }, search_id)
                    if desc_changed and new_status == "new":
                        append_history(conn, fields["job_id"], {"ts": ts, "event": "refreshed"}, search_id)
                elif desc_changed:
                    append_history(conn, fields["job_id"], {"ts": ts, "event": "refreshed"}, search_id)
                if canonical_id != row["canonical_id"] and canonical_id is not None:
                    append_history(conn, fields["job_id"], {
                        "ts": ts, "event": "linked", "canonical_id": canonical_id,
                    }, search_id)
            else:
                c["unchanged"] += 1

        # Audit trail: record when the company-alias map rewrote the feed's spelling to
        # the canonical form. Logged for both new and re-seen jobs (the row exists by now,
        # so this UPDATE-based append is safe). auto=true distinguishes it from a manual
        # company_actual override event. Recorded against the feed value → stored value,
        # i.e. exactly what the alias did this run.
        if company_was_normalized:
            append_history(conn, fields["job_id"], {
                "ts": _now_iso(), "event": "company_normalized",
                "from": original_company, "to": fields["company"], "auto": True,
            }, search_id)

        # Commit after each item rather than once per run. SQLite holds the write lock
        # from a row's INSERT/UPDATE until commit; a single per-run commit kept that lock
        # for the ENTIRE run — including every item's AI reformat call (1-2s each) — which
        # starved the web UI's writes (→ 503s) and any concurrent ingest/rescore. Per-item
        # commit releases the lock between items, so it's held only for the brief
        # INSERT/UPDATE + history writes of one row; the AI call for the next item happens
        # with no lock held. Safe: find_canonical reads over this same connection, which
        # sees this run's earlier writes regardless of commit, so cross-source grouping is
        # unchanged. A mid-run crash now leaves already-processed items persisted, which is
        # harmless and idempotent — the Apify run isn't marked consumed (record_state) until
        # ingest() returns cleanly, so a reprocess simply re-UPSERTs the same rows.
        conn.commit()

    conn.commit()  # Final flush for the empty-items / all-skipped case (otherwise a no-op).
    return c


# ── Run-summary formatting ──────────────────────────────────────────────────
def _new_total(c: Counter) -> int:
    return c["inserted_clean"] + c["inserted_grouped"] + c["inserted_expired"]


def _seen_total(c: Counter) -> int:
    """Every item processed: new postings + existing seen-again + ATS skips."""
    return _new_total(c) + c["updated"] + c["unchanged"] + c["skipped_ats"]


def _sideops(c: Counter, ghosted: int = 0) -> str:
    parts = []
    for key, lbl in (("relinked", "re-linked"), ("orphan_merges", "orphan merges"),
                     ("reset_new", "reset→new"), ("auto_closed", "auto-closed")):
        if c[key]:
            parts.append(f"{c[key]} {lbl}")
    if ghosted:
        parts.append(f"{ghosted} auto-ghosted")
    return ", ".join(parts)


def summary_compact(c: Counter, reset_on_change: bool = True) -> str:
    """One-line per-run / per-task summary."""
    reset_note = "" if reset_on_change else " (resets disabled)"
    line = (f"{c['inserted_clean']} new + {c['inserted_grouped']} grouped, "
            f"{c['updated']} updated{reset_note}, {c['unchanged']} unchanged")
    if c["inserted_expired"]:
        line += f", {c['inserted_expired']} arrived-expired"
    if c["skipped_ats"]:
        line += f", {c['skipped_ats']} ATS dupes"
    side = _sideops(c)
    if side:
        line += f" | {side}"
    return line


def summary_detailed(c: Counter, ghosted: int, elapsed: float, dry_run: bool) -> str:
    """Multi-line grand-total breakdown."""
    prefix = "[DRY-RUN] " if dry_run else ""
    exp = f", {c['inserted_expired']} arrived-expired" if c["inserted_expired"] else ""
    total = _seen_total(c)
    # Per-posting average, for spotting a slow fetch/dedup pass at a glance in a log.
    avg = f" (avg {elapsed / total:.2f}s/posting)" if total else ""
    return (
        f"{prefix}Done in {elapsed:.1f}s{avg}. {total} postings seen.\n"
        f"  New:      {c['inserted_clean']} standalone, {c['inserted_grouped']} grouped{exp}\n"
        f"  Existing: {c['updated']} updated, {c['unchanged']} unchanged, "
        f"{c['skipped_ats']} ATS duplicates skipped\n"
        f"  Side-ops: {_sideops(c, ghosted) or 'none'}"
    )


def auto_ghost_applied(conn: sqlite3.Connection, days: int) -> int:
    """Move stale 'applied' jobs to 'ghosted' based on applied_at age — per lens.

    Only affects (job, search) state rows with status = 'applied' — interviewing/offered are
    intentionally excluded since those warrant a deliberate human decision. Ghosting is
    per-lens: a job applied-to under one search ghosts only that search's state row.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = conn.execute(
        "SELECT job_id, search_id FROM job_search_state "
        "WHERE status = 'applied' AND applied_at IS NOT NULL "
        "AND substr(applied_at, 1, 10) <= ?",
        (cutoff,),
    ).fetchall()
    now_iso = _now_iso()
    for row in rows:
        conn.execute(
            "UPDATE job_search_state SET status = 'ghosted' WHERE job_id = ? AND search_id = ?",
            (row["job_id"], row["search_id"]),
        )
        append_history(conn, row["job_id"], {
            "ts":    now_iso,
            "event": "status",
            "from":  "applied",
            "to":    "ghosted",
            "note":  f"auto-ghosted after {days} days without response",
        }, row["search_id"])
    if rows:
        conn.commit()
    return len(rows)


def touch_synced(conn: sqlite3.Connection, task_name: str) -> None:
    """Record that ingest ran for this task, even if no new data was found."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE ingest_state SET last_synced_at = ? WHERE task_name = ?",
        (now, task_name),
    )
    conn.commit()


def record_state(conn: sqlite3.Connection, task_name: str, run: dict,
                 inserted: int, updated: int, unchanged: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO ingest_state (task_name, last_run_id, last_run_at, last_synced_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(task_name) DO UPDATE SET
            last_run_id    = excluded.last_run_id,
            last_run_at    = excluded.last_run_at,
            last_synced_at = excluded.last_synced_at
        """,
        (task_name, run["id"], run["startedAt"], now),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ingest_history
            (task_name, run_id, run_at, inserted, updated, unchanged)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_name, run["id"], run["startedAt"], inserted, updated, unchanged),
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Apify LinkedIn job results into SQLite.")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config file (default: config.toml)")
    parser.add_argument("--dry-run", action="store_true", help="Show pending run counts without fetching items or writing to the database")
    parser.add_argument("--fixbasics", action="store_true",
                        help="Migrate the config's bare top-level settings under a [basics] table and exit")
    args = parser.parse_args()

    # One-shot config maintenance: nest deprecated bare top-level keys under [basics]. Runs
    # before anything else touches the DB or network; exits immediately after.
    if args.fixbasics:
        _changed, msg = migrate_config_to_basics(args.config)
        print(msg)
        sys.exit(0)

    # Line-buffer stdout so each line is flushed on its newline. When output is
    # redirected to a file (e.g. cron `>> ingest.log`), Python block-buffers stdout,
    # which hides progress from a `tail -f` until the buffer fills or the run ends.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    config_path = Path(args.config)
    try:
        app_cfg = load_config(config_path)
    except ConfigError as exc:
        sys.exit(str(exc))

    # Globals are shared across all searches (one DB, one alias namespace, one API key).
    shared = app_cfg.shared
    api_token: str = shared["api_token"]
    username: str = shared["username"]
    db_path: str = app_cfg.db_path
    reset_on_change_global: bool = shared.get("reset_on_change", True)
    auto_ghost: bool             = shared.get("auto_ghost", False)
    auto_ghost_days: int         = shared.get("auto_ghost_days", 180)
    fuzzy_dedup_global: bool     = shared.get("fuzzy_dedup", True)
    fuzzy_desc_threshold: float = shared.get("fuzzy_desc_threshold", 0.85)
    fuzzy_title_threshold: float = shared.get("fuzzy_title_threshold", 0.6)
    fuzzy_title_word_threshold: float = shared.get("fuzzy_title_word_threshold", _TITLE_WORD_GATE)
    fuzzy_title_id_gate: bool = shared.get("fuzzy_title_id_gate", True)
    inherit_canonical_status: bool = shared.get("inherit_canonical_status", True)
    # Case-insensitive variant→canonical company-name map (empty if [company_aliases] unset).
    company_alias_map = build_company_alias_map(shared.get("company_aliases"))
    if company_alias_map:
        print(f"Company-name normalization: {len(company_alias_map)} alias(es) configured.")

    # Optional AI description reformatting (engine settings shared via [ai]).
    descriptions_cfg = shared.get("descriptions", {})
    formatter = DescriptionFormatter()  # disabled by default → heuristic renderer
    if descriptions_cfg.get("use_ai_on_descriptions", False) and not args.dry_run:
        api_key, model = resolve_ai_settings(shared, "descriptions")
        effort, effort_explicit = resolve_effort(shared, "descriptions")
        if api_key:
            import anthropic
            warn_effort_ignored("descriptions", model, effort, effort_explicit)
            formatter = DescriptionFormatter(anthropic.Anthropic(api_key=api_key), model, effort)
            print(f"AI description reformatting enabled (model: {model}).")
        else:
            print("WARNING: use_ai_on_descriptions is set but no API key resolved "
                  "(set api_key under [ai] or ANTHROPIC_API_KEY); skipping reformatting.",
                  file=sys.stderr)

    # Serialize against any concurrent ingest/rescore: ingest holds the SQLite write
    # lock for the whole run (across the AI reformat calls), so an overlapping writer
    # would crash on the busy_timeout. A dry run writes nothing, so it needn't lock —
    # and shouldn't be blocked by (or block) a real run. Held until the process exits.
    if not args.dry_run:
        _run_lock = acquire_run_lock(db_path, label="ingest")  # noqa: F841 (held for lifetime)

    conn = open_db(db_path)
    # One-time single→multi transition: fold pre-split __default__ state into the search that
    # declared adopts_legacy. Path B only (a single __default__ search needs no adoption).
    if not args.dry_run and app_cfg.is_multi_search:
        adopt_legacy(conn, app_cfg.adopter.id if app_cfg.adopter else None)
    grand_total: Counter = Counter()
    start_time = datetime.now(timezone.utc)
    dry_run_note = " (DRY RUN)" if args.dry_run else ""
    print(f"Starting ingestion at {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}{dry_run_note}")

    # One (search_id, task) per feed across all configured searches. Iterating tuples keeps the
    # loop body flat while tagging every ingest + run-tracking key with its owning search.
    task_specs: list[tuple[str, dict]] = [(s.id, t) for s in app_cfg.searches for t in s.tasks]
    for search_id, task in task_specs:
        task_name:        str       = task["name"]
        default_label:    str       = task.get("label", "unknown")
        label_from_input: str | None = task.get("label_from_input")
        actor_type:       str       = task.get("actor", "linkedin")
        exclude_ats_dups: bool      = task.get("exclude_ats_duplicates", False)
        reset_on_change:  bool      = task.get("reset_on_change", reset_on_change_global)
        fuzzy_dedup:      bool      = task.get("fuzzy_dedup", fuzzy_dedup_global)
        # Run-tracking key: bare for the default search (preserves single-search history labels),
        # namespaced <search_id>:<task> otherwise so two searches can share an Apify task name.
        skey = state_key(search_id, task_name)
        label_desc = f"label_from_input={label_from_input!r}" if label_from_input else f"label: {default_label}"
        print(f"Fetching runs for '{task_name}' ({label_desc}, actor: {actor_type}) ...")
        try:
            all_runs = fetch_task_runs(username, task_name, api_token)
            # Rename-proof: selection is keyed on run_id within this search, not the task
            # name (see runs_to_process). skey is still what record_state writes below.
            pending = runs_to_process(conn, search_id, all_runs)

            if not pending:
                print(f"  No new runs since last ingestion.")
                if not args.dry_run:
                    touch_synced(conn, skey)
                continue

            if args.dry_run:
                print(f"  {len(pending)} pending run(s):")
                for run in pending:
                    run_time = run["startedAt"][:16].replace("T", " ")
                    if label_from_input:
                        run_input = fetch_run_input(run, api_token)
                        run_label = str(run_input.get(label_from_input) or "").strip() or default_label
                    else:
                        run_label = default_label
                    items = fetch_dataset_items(run["defaultDatasetId"], api_token)
                    print(f"    {run_time} [{run_label}]: {len(items)} item(s)")
                continue

            if len(pending) > 1:
                print(f"  Catching up: {len(pending)} runs to process.")

            task_total: Counter = Counter()
            for run in pending:
                run_time = run["startedAt"][:16].replace("T", " ")
                # Resolve the label for this specific run.
                if label_from_input:
                    run_input = fetch_run_input(run, api_token)
                    label = str(run_input.get(label_from_input) or "").strip() or default_label
                    if label == default_label and label_from_input not in run_input:
                        print(f"  WARNING: '{label_from_input}' not found in run input; using fallback label '{label}'",
                              file=sys.stderr)
                else:
                    label = default_label
                items = fetch_dataset_items(run["defaultDatasetId"], api_token)
                print(f"  Run {run_time} [{label}]: {len(items)} items retrieved")
                result = ingest(
                    conn, items, label, actor_type, exclude_ats_dups, reset_on_change,
                    fuzzy_dedup, fuzzy_desc_threshold, fuzzy_title_threshold,
                    fuzzy_title_word_threshold, fuzzy_title_id_gate, inherit_canonical_status,
                    company_aliases=company_alias_map,
                    formatter=formatter, search_id=search_id,
                )
                print(f"    {summary_compact(result, reset_on_change)}")
                task_total += result
                record_state(conn, skey, run,
                             _new_total(result), result["updated"], result["unchanged"])

            if len(pending) > 1:
                print(f"  Task total: {summary_compact(task_total, reset_on_change)}")

            grand_total += task_total

        except requests.RequestException as exc:
            # Any network-layer failure for this task — an HTTP error from the Apify API, or
            # (the common case during an internet/DNS outage) a ConnectionError/Timeout raised
            # before we ever reach the server. RequestException is the base of all of them, so
            # catching it logs one clean line per task instead of dumping a urllib3 stacktrace,
            # and the loop moves on to the next task (during an outage every task fails alike).
            print(f"  ERROR fetching '{task_name}': {exc}", file=sys.stderr)

    ghosted_count = 0
    if auto_ghost and not args.dry_run:
        ghosted_count = auto_ghost_applied(conn, auto_ghost_days)
        if ghosted_count:
            print(f"Auto-ghosted {ghosted_count} applied job(s) with no activity in {auto_ghost_days}+ days.")

    conn.close()
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(summary_detailed(grand_total, ghosted_count, elapsed, args.dry_run))
    desc_summary = formatter.summary()
    if desc_summary:
        print("  " + desc_summary)
    print()


if __name__ == "__main__":
    main()
