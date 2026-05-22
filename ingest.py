#!/usr/bin/env python3
# requires Python 3.11+
"""Ingest Apify LinkedIn job search results into a local SQLite database."""

import argparse
import json
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
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
    linkedin_url    TEXT,
    apply_url       TEXT,
    easy_apply      INTEGER,
    salary_min      INTEGER,
    salary_max      INTEGER,
    salary_currency TEXT,
    labels          TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'new',
    notes           TEXT,
    job_description TEXT,
    first_seen      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company    ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migrate: rename regions → labels if the old column still exists.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "regions" in cols and "labels" not in cols:
        conn.execute("ALTER TABLE jobs RENAME COLUMN regions TO labels")
        conn.commit()
    return conn


def fetch_dataset_items(username: str, task_name: str, api_token: str) -> list[dict]:
    task_id = f"{username}~{task_name}"
    headers = {"Authorization": f"Bearer {api_token}"}

    run_url = f"{APIFY_BASE}/actor-tasks/{task_id}/runs/last"
    resp = requests.get(run_url, headers=headers, params={"status": "SUCCEEDED"}, timeout=30)
    resp.raise_for_status()
    dataset_id = resp.json()["data"]["defaultDatasetId"]

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


def _scalar(val: object) -> object:
    """Return the first element if val is a list, otherwise val as-is."""
    return val[0] if isinstance(val, list) else val


AUTO_CLOSE_STATUSES = {"new", "reviewed"}


def is_expired(item: dict) -> bool:
    val = _scalar(item.get("date_validthrough"))
    if not val:
        return False
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def extract_fields(item: dict) -> dict:
    # Field names from fantastic-jobs/advanced-linkedin-job-search-api (verified against real output).
    # `id` is Apify-internal; `linkedin_id` is the actual LinkedIn job ID used as our PK.
    # `directapply` = LinkedIn Easy Apply (apply without leaving LinkedIn).
    # Salary fields are AI-extracted by the actor and may be absent.
    # _scalar() guards against fields that are arrays in JSON for multi-value records.
    salary_min = _scalar(item.get("ai_salary_minvalue"))
    salary_max = _scalar(item.get("ai_salary_maxvalue"))
    return {
        "job_id": str(_scalar(item.get("linkedin_id")) or "").strip(),
        "title": _scalar(item.get("title")),
        "company": _scalar(item.get("organization")),
        "location": _scalar(item.get("locations_derived")),
        "posted_date": _scalar(item.get("date_posted")),
        "linkedin_url": f"https://www.linkedin.com/jobs/view/{_scalar(item.get('linkedin_id'))}",
        "apply_url": _scalar(item.get("external_apply_url")) or None,
        "easy_apply": 1 if str(_scalar(item.get("directapply", "")) or "").lower() == "true" else 0,
        "salary_min": int(salary_min) if salary_min not in (None, "", "null") else None,
        "salary_max": int(salary_max) if salary_max not in (None, "", "null") else None,
        "salary_currency": _scalar(item.get("ai_salary_currency")) or None,
        "job_description": _scalar(item.get("description_text")),
    }


def ingest(conn: sqlite3.Connection, items: list[dict], label: str) -> tuple[int, int, int]:
    inserted = updated = unchanged = 0

    for item in items:
        fields = extract_fields(item)
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
            initial_status = "closed" if expired else "new"
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, title, company, location, posted_date,
                     linkedin_url, apply_url, easy_apply, salary_min, salary_max, salary_currency,
                     labels, status, job_description, raw)
                VALUES
                    (:job_id, :title, :company, :location, :posted_date,
                     :linkedin_url, :apply_url, :easy_apply, :salary_min, :salary_max, :salary_currency,
                     :labels, :status, :job_description, :raw)
                """,
                {**fields, "labels": json.dumps([label]), "status": initial_status, "raw": raw},
            )
            inserted += 1
        else:
            current_status = row["status"]
            existing_labels: list[str] = json.loads(row["labels"])
            new_labels = existing_labels if label in existing_labels else existing_labels + [label]

            desc_changed = fields["job_description"] != row["job_description"]
            if expired and current_status in AUTO_CLOSE_STATUSES:
                new_status = "closed"
            elif desc_changed and current_status == "skipped":
                new_status = "new"
                print(f"  NOTE: description changed for job {fields['job_id']} ({fields['title']}), resetting from skipped → new")
            else:
                new_status = current_status

            something_changed = (
                new_labels != existing_labels
                or new_status != current_status
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
                    posted_date = :posted_date, linkedin_url = :linkedin_url,
                    apply_url = :apply_url, easy_apply = :easy_apply,
                    salary_min = :salary_min, salary_max = :salary_max,
                    salary_currency = :salary_currency,
                    job_description = :job_description,
                    labels = :labels, status = :status, raw = :raw
                WHERE job_id = :job_id
                """,
                {**fields, "labels": json.dumps(new_labels), "status": new_status, "raw": raw},
            )
            if something_changed:
                updated += 1
            else:
                unchanged += 1

    conn.commit()
    return inserted, updated, unchanged


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Apify LinkedIn job results into SQLite.")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config file (default: config.toml)")
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

    conn = open_db(db_path)
    total_inserted = total_updated = total_unchanged = 0

    for task in tasks:
        task_name: str = task["name"]
        label: str = task["label"]
        print(f"Fetching '{task_name}' (label: {label}) ...")
        try:
            items = fetch_dataset_items(username, task_name, api_token)
            print(f"  {len(items)} items retrieved from Apify")
            ins, upd, unch = ingest(conn, items, label)
            print(f"  {ins} inserted, {upd} updated, {unch} already existed")
            total_inserted += ins
            total_updated += upd
            total_unchanged += unch
        except requests.HTTPError as exc:
            print(f"  ERROR fetching '{task_name}': {exc}", file=sys.stderr)

    conn.close()
    print(f"\nDone. {total_inserted} inserted, {total_updated} updated, {total_unchanged} unchanged.")


if __name__ == "__main__":
    main()
