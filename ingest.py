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
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

APIFY_BASE = "https://api.apify.com/v2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    posted_date     TEXT,
    job_url         TEXT,
    apply_url       TEXT,
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
    applied_at            TEXT,
    history               TEXT NOT NULL DEFAULT '[]',
    company_actual        TEXT,
    first_seen      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company    ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);

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
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent reads/writes with the Flask app and rescore script.
    # busy_timeout retries on lock contention instead of raising immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
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
    return conn


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


def runs_to_process(
    conn: sqlite3.Connection,
    task_name: str,
    all_runs: list[dict],
) -> list[dict]:
    """Return the subset of runs not yet ingested, in chronological order.

    On first ever run (no state) all available runs are returned so that a
    newly configured task picks up its full backlog.  Use --dry-run first to
    preview what will be ingested.
    """
    state = conn.execute(
        "SELECT last_run_id FROM ingest_state WHERE task_name = ?",
        (task_name,),
    ).fetchone()

    if state is None:
        # No prior state — process all available runs.
        return list(all_runs)

    last_run_id = state["last_run_id"]
    seen = False
    pending = []
    for run in all_runs:
        if seen:
            pending.append(run)
        if run["id"] == last_run_id:
            seen = True

    if not seen:
        # Last known run has aged off (unlikely) — fall back to latest only.
        return all_runs[-1:] if all_runs else []

    return pending


def _scalar(val: object) -> object:
    """Return the first element if val is a list, otherwise val as-is."""
    return val[0] if isinstance(val, list) else val


def _now_iso() -> str:
    """Return current UTC time as an ISO 8601 string ending in Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_history(conn: sqlite3.Connection, job_id: str, entry: dict) -> None:
    """Append one event dict to a job's history JSON array (atomic, no read-modify-write)."""
    conn.execute(
        "UPDATE jobs SET history = json_insert(COALESCE(history, '[]'), '$[#]', json(?)) "
        "WHERE job_id = ?",
        (json.dumps(entry, ensure_ascii=False), job_id),
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


AUTO_CLOSE_STATUSES = {"new", "reviewing"}


def is_expired(item: dict) -> bool:
    val = _scalar(item.get("date_valid_through") or item.get("date_validthrough"))
    if not val:
        return False
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def find_canonical(
    conn: sqlite3.Connection,
    job_id: str,
    title: str | None,
    company: str | None,
    description: str | None,
    threshold: float,
    title_threshold: float = 0.6,
) -> list[sqlite3.Row]:
    """Return all existing canonical jobs that are near-duplicates, sorted oldest-first.

    Only considers jobs with canonical_id IS NULL (i.e. canonical candidates,
    not already-linked duplicates) to prevent chaining.  No company filter is
    applied — the same job can appear under different company names when posted
    by recruiters or aggregators.  A title similarity >= title_threshold pre-filter
    keeps the search efficient; description similarity >= threshold is the final gate.

    The caller should treat matches[0] as the canonical (oldest first_seen) and
    link all remaining matches to it, preventing future fragmentation.
    """
    if not description or not title:
        return []
    candidates = conn.execute(
        "SELECT * FROM jobs WHERE canonical_id IS NULL AND job_id != ?",
        (job_id,),
    ).fetchall()
    matches: list[sqlite3.Row] = []
    for candidate in candidates:
        if not candidate["title"] or not candidate["job_description"]:
            continue
        # Title pre-filter: quick_ratio is an upper bound on ratio()
        title_m = SequenceMatcher(None, title.lower(), candidate["title"].lower())
        if title_m.quick_ratio() < title_threshold:
            continue
        if title_m.ratio() < title_threshold:
            continue
        # Description check
        desc_m = SequenceMatcher(None, description, candidate["job_description"])
        if desc_m.quick_ratio() < threshold:
            continue
        if desc_m.ratio() >= threshold:
            matches.append(candidate)
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
    """(min, max) annual salary from an Apify item, normalized by its unit text.
    New/old field-name variants are both checked, matching the value fields."""
    unit = _scalar(item.get("ai_salary_unit_text") or item.get("ai_salary_unittext"))
    lo = _normalize_salary(
        _scalar(item.get("ai_salary_min_value") or item.get("ai_salary_minvalue")), unit)
    hi = _normalize_salary(
        _scalar(item.get("ai_salary_max_value") or item.get("ai_salary_maxvalue")), unit)
    return lo, hi


def extract_fields_linkedin(item: dict) -> dict:
    # Field names from fantastic-jobs/advanced-linkedin-job-search-api.
    # `linkedin_id` is the actual LinkedIn job ID used as our PK (type changed to int June 2026;
    #   str() conversion handles both old string and new integer values).
    # `direct_apply` (was `directapply`) = LinkedIn Easy Apply.
    # Salary fields are AI-extracted by the actor and may be absent.
    # `external_apply_url` was removed June 2026 with no replacement; apply_url will be None.
    # New name checked first with old name as fallback during the transition window.
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
        "easy_apply": 1 if str(
            _scalar(item.get("direct_apply") or item.get("directapply") or "") or ""
        ).lower() == "true" else 0,
        "source": "linkedin",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": _scalar(item.get("ai_salary_currency")) or None,
        "job_description": _scalar(item.get("description_text")),
    }


def extract_fields_careersite(item: dict) -> dict:
    # Field names from fantastic-jobs/career-site-job-listing-api.
    # `id` is the actor's internal job ID; we prefix it with "cs_" to avoid
    # any collision with numeric LinkedIn IDs stored in the same table.
    # `url` is both the canonical job page and the apply URL (career sites have no
    # separate apply link). Easy Apply is not applicable.
    salary_min, salary_max = extract_salary(item)
    raw_id  = str(_scalar(item.get("id")) or "").strip()
    job_url = _scalar(item.get("url")) or None
    return {
        "job_id": f"cs_{raw_id}" if raw_id else "",
        "title": _scalar(item.get("title")),
        "company": _scalar(item.get("organization")),
        "location": _scalar(item.get("locations_derived")),
        "posted_date": _scalar(item.get("date_posted")),
        "job_url": job_url,
        "apply_url": job_url,
        "easy_apply": 0,
        "source": "careersite",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": _scalar(item.get("ai_salary_currency")) or None,
        "job_description": _scalar(item.get("description_text")),
    }


def ingest(conn: sqlite3.Connection, items: list[dict], label: str,
           actor_type: str = "linkedin", exclude_ats_dups: bool = False,
           reset_on_change: bool = True,
           fuzzy_dedup: bool = True, fuzzy_desc_threshold: float = 0.85,
           fuzzy_title_threshold: float = 0.6,
           inherit_canonical_status: bool = True) -> tuple[int, int, int, int, int]:
    inserted = updated = unchanged = skipped_ats = fuzzy_linked = 0

    for item in items:
        if exclude_ats_dups and item.get("ats_duplicate") is True:
            skipped_ats += 1
            continue
        fields = extract_fields_careersite(item) if actor_type == "careersite" else extract_fields_linkedin(item)
        if not fields["job_id"]:
            print(f"  WARNING: item missing job_id, skipping: {list(item.keys())}", file=sys.stderr)
            continue

        raw = json.dumps(item, ensure_ascii=False)

        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (fields["job_id"],),
        ).fetchone()

        expired = is_expired(item)

        if row is None:
            canonical_id = None
            initial_status = "closed" if expired else "new"
            if fuzzy_dedup and not expired:
                matches = find_canonical(
                    conn, fields["job_id"], fields["title"], fields["company"],
                    fields["job_description"], fuzzy_desc_threshold, fuzzy_title_threshold,
                )
                if matches:
                    canonical = matches[0]
                    canonical_id = canonical["job_id"]
                    if inherit_canonical_status:
                        initial_status = canonical["status"]
                    print(
                        f"  NOTE: fuzzy match: {fields['job_id']} ({fields['title']}) "
                        f"→ canonical {canonical_id} ({canonical['title']}), "
                        f"status: {initial_status}"
                    )
                    fuzzy_linked += 1
                    # Also link any other orphaned matches to the same canonical so
                    # future jobs find one group rather than many.
                    for other in matches[1:]:
                        conn.execute(
                            "UPDATE jobs SET canonical_id = ? WHERE job_id = ?",
                            (canonical_id, other["job_id"]),
                        )
                        print(
                            f"  NOTE: also linking orphan {other['job_id']} ({other['title']}) "
                            f"→ canonical {canonical_id}"
                        )
                        fuzzy_linked += 1
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, title, company, location, posted_date,
                     job_url, apply_url, easy_apply, salary_min, salary_max, salary_currency,
                     labels, source, status, job_description, canonical_id, raw)
                VALUES
                    (:job_id, :title, :company, :location, :posted_date,
                     :job_url, :apply_url, :easy_apply, :salary_min, :salary_max, :salary_currency,
                     :labels, :source, :status, :job_description, :canonical_id, :raw)
                """,
                {**fields, "labels": json.dumps([label]), "status": initial_status,
                 "canonical_id": canonical_id, "raw": raw},
            )
            inserted += 1
            ts = _now_iso()
            append_history(conn, fields["job_id"], {
                "ts": ts, "event": "ingested", "label": label, "source": actor_type,
            })
            if initial_status == "closed":
                append_history(conn, fields["job_id"], {
                    "ts": ts, "event": "status", "from": "new", "to": "closed",
                })
            elif canonical_id:
                append_history(conn, fields["job_id"], {
                    "ts": ts, "event": "linked", "canonical_id": canonical_id,
                })
        else:
            current_status = row["status"]
            existing_labels: list[str] = json.loads(row["labels"])
            new_labels = existing_labels if label in existing_labels else existing_labels + [label]

            desc_changed = fields["job_description"] != row["job_description"]
            now = datetime.now(timezone.utc).isoformat()
            refreshed_at = row["refreshed_at"]  # preserve unless we're setting it now
            canonical_id = row["canonical_id"]  # preserve existing link by default

            if expired and current_status in AUTO_CLOSE_STATUSES:
                new_status = "closed"
            elif desc_changed and current_status in ("skipped", "autoskipped") and reset_on_change:
                new_status = "new"
                refreshed_at = now
                print(f"  NOTE: description changed for job {fields['job_id']} ({fields['title']}), resetting from {current_status} → new")
            else:
                new_status = current_status

            # Check for a fuzzy canonical on previously-unlinked jobs.
            if fuzzy_dedup and canonical_id is None:
                matches = find_canonical(
                    conn, fields["job_id"], fields["title"], fields["company"],
                    fields["job_description"], fuzzy_desc_threshold, fuzzy_title_threshold,
                )
                if matches:
                    canonical = matches[0]
                    canonical_id = canonical["job_id"]
                    print(
                        f"  NOTE: fuzzy match: {fields['job_id']} ({fields['title']}) "
                        f"→ canonical {canonical_id} ({canonical['title']})"
                    )
                    fuzzy_linked += 1
                    for other in matches[1:]:
                        conn.execute(
                            "UPDATE jobs SET canonical_id = ? WHERE job_id = ?",
                            (canonical_id, other["job_id"]),
                        )
                        print(
                            f"  NOTE: also linking orphan {other['job_id']} ({other['title']}) "
                            f"→ canonical {canonical_id}"
                        )
                        fuzzy_linked += 1

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

            conn.execute(
                """
                UPDATE jobs SET
                    title = :title, company = :company, location = :location,
                    posted_date = :posted_date, job_url = :job_url,
                    apply_url = :apply_url, easy_apply = :easy_apply,
                    salary_min = :salary_min, salary_max = :salary_max,
                    salary_currency = :salary_currency,
                    job_description = :job_description,
                    labels = :labels, source = :source, status = :status,
                    refreshed_at = :refreshed_at, canonical_id = :canonical_id, raw = :raw
                WHERE job_id = :job_id
                """,
                {**fields, "labels": json.dumps(new_labels), "status": new_status,
                 "refreshed_at": refreshed_at, "canonical_id": canonical_id, "raw": raw},
            )
            if something_changed:
                updated += 1
                ts = _now_iso()
                if new_status != current_status:
                    append_history(conn, fields["job_id"], {
                        "ts": ts, "event": "status", "from": current_status, "to": new_status,
                    })
                    if desc_changed and new_status == "new":
                        append_history(conn, fields["job_id"], {"ts": ts, "event": "refreshed"})
                elif desc_changed:
                    append_history(conn, fields["job_id"], {"ts": ts, "event": "refreshed"})
                if canonical_id != row["canonical_id"] and canonical_id is not None:
                    append_history(conn, fields["job_id"], {
                        "ts": ts, "event": "linked", "canonical_id": canonical_id,
                    })
            else:
                unchanged += 1

    conn.commit()
    return inserted, updated, unchanged, skipped_ats, fuzzy_linked


def auto_ghost_applied(conn: sqlite3.Connection, days: int) -> int:
    """Move stale 'applied' jobs to 'ghosted' based on applied_at age.

    Only affects jobs with status = 'applied' — interviewing/offered are
    intentionally excluded since those warrant a deliberate human decision.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = conn.execute(
        "SELECT job_id FROM jobs "
        "WHERE status = 'applied' AND applied_at IS NOT NULL "
        "AND substr(applied_at, 1, 10) <= ?",
        (cutoff,),
    ).fetchall()
    now_iso = _now_iso()
    for row in rows:
        conn.execute("UPDATE jobs SET status = 'ghosted' WHERE job_id = ?", (row["job_id"],))
        append_history(conn, row["job_id"], {
            "ts":    now_iso,
            "event": "status",
            "from":  "applied",
            "to":    "ghosted",
            "note":  f"auto-ghosted after {days} days without response",
        })
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
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}")

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    api_token: str = config["api_token"]
    username: str = config["username"]
    db_path: str = config.get("db_path", "jobs.db")
    tasks: list[dict] = config["tasks"]
    reset_on_change_global: bool = config.get("reset_on_change", True)
    auto_ghost: bool             = config.get("auto_ghost", False)
    auto_ghost_days: int         = config.get("auto_ghost_days", 180)
    fuzzy_dedup_global: bool     = config.get("fuzzy_dedup", True)
    fuzzy_desc_threshold: float = config.get("fuzzy_desc_threshold", 0.85)
    fuzzy_title_threshold: float = config.get("fuzzy_title_threshold", 0.6)
    inherit_canonical_status: bool = config.get("inherit_canonical_status", True)

    conn = open_db(db_path)
    total_inserted = total_updated = total_unchanged = total_skipped = total_fuzzy = 0
    start_time = datetime.now(timezone.utc)
    dry_run_note = " (DRY RUN)" if args.dry_run else ""
    print(f"Starting ingestion at {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}{dry_run_note}")

    for task in tasks:
        task_name:        str       = task["name"]
        default_label:    str       = task.get("label", "unknown")
        label_from_input: str | None = task.get("label_from_input")
        actor_type:       str       = task.get("actor", "linkedin")
        exclude_ats_dups: bool      = task.get("exclude_ats_duplicates", False)
        reset_on_change:  bool      = task.get("reset_on_change", reset_on_change_global)
        fuzzy_dedup:      bool      = task.get("fuzzy_dedup", fuzzy_dedup_global)
        label_desc = f"label_from_input={label_from_input!r}" if label_from_input else f"label: {default_label}"
        print(f"Fetching runs for '{task_name}' ({label_desc}, actor: {actor_type}) ...")
        try:
            all_runs = fetch_task_runs(username, task_name, api_token)
            pending = runs_to_process(conn, task_name, all_runs)

            if not pending:
                print(f"  No new runs since last ingestion.")
                if not args.dry_run:
                    touch_synced(conn, task_name)
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

            task_inserted = task_updated = task_unchanged = task_skipped = task_fuzzy = 0
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
                ins, upd, unch, skip, fuzzy = ingest(
                    conn, items, label, actor_type, exclude_ats_dups, reset_on_change,
                    fuzzy_dedup, fuzzy_desc_threshold, fuzzy_title_threshold, inherit_canonical_status,
                )
                skip_msg    = f", {skip} ATS duplicates skipped" if skip else ""
                fuzzy_msg   = f", {fuzzy} fuzzy-linked" if fuzzy else ""
                reset_note  = " (resets disabled)" if not reset_on_change else ""
                print(f"    {ins} inserted, {upd} updated{reset_note}, {unch} already existed{skip_msg}{fuzzy_msg}")
                task_inserted += ins
                task_updated  += upd
                task_unchanged += unch
                task_skipped  += skip
                task_fuzzy    += fuzzy
                record_state(conn, task_name, run, ins, upd, unch)

            if len(pending) > 1:
                task_skip_msg   = f", {task_skipped} ATS duplicates skipped" if task_skipped else ""
                task_fuzzy_msg  = f", {task_fuzzy} fuzzy-linked" if task_fuzzy else ""
                task_reset_note = " (resets disabled)" if not reset_on_change else ""
                print(f"  Task total: {task_inserted} inserted, {task_updated} updated{task_reset_note}, {task_unchanged} already existed{task_skip_msg}{task_fuzzy_msg}")

            total_inserted += task_inserted
            total_updated  += task_updated
            total_unchanged += task_unchanged
            total_skipped  += task_skipped
            total_fuzzy    += task_fuzzy

        except requests.HTTPError as exc:
            print(f"  ERROR fetching '{task_name}': {exc}", file=sys.stderr)

    ghosted_count = 0
    if auto_ghost and not args.dry_run:
        ghosted_count = auto_ghost_applied(conn, auto_ghost_days)
        if ghosted_count:
            print(f"Auto-ghosted {ghosted_count} applied job(s) with no activity in {auto_ghost_days}+ days.")

    conn.close()
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    total_fuzzy_msg  = f", {total_fuzzy} fuzzy-linked" if total_fuzzy else ""
    ghost_msg        = f", {ghosted_count} auto-ghosted" if ghosted_count else ""
    dry_run_prefix   = "[DRY-RUN] " if args.dry_run else ""
    print(f"{dry_run_prefix}Done in {elapsed:.1f}s. {total_inserted} inserted, {total_updated} updated, {total_unchanged} unchanged, {total_skipped} ATS duplicates skipped{total_fuzzy_msg}{ghost_msg}.\n")


if __name__ == "__main__":
    main()
