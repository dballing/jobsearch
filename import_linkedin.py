#!/usr/bin/env python3
# requires Python 3.11+
"""Import LinkedIn job listings by URL into the job tracker database.

Fetches job details via the apimaestro/linkedin-job-detail Apify actor
and inserts them as if they had been ingested normally.

Usage:
    python3 import_linkedin.py [--status STATUS] [--label LABEL] [--config PATH]
                               [--dry-run] [--debug] URL [URL ...]

    URLs may also be piped via stdin (one per line).

Flags:
    --status STATUS   Initial status for imported jobs (default: applied)
    --label LABEL     Label key to tag imported jobs (must exist in config [labels])
    --config PATH     Path to TOML config (default: config.toml)
    --dry-run         Print what would be imported without writing anything
    --debug           Print raw Apify response (useful for calibrating field mapping)
"""

import argparse
import json
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import requests

from ingest import APIFY_BASE, _scalar, _now_iso, append_history, find_canonical, open_db

ACTOR_ID = "apimaestro~linkedin-job-detail"
JOB_URL_RE = re.compile(r"linkedin\.com/jobs/view/(\d+)", re.IGNORECASE)

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

_CITY_STATE_RE = re.compile(r'^(.+),\s*([A-Z]{2})$')


def normalize_location(location: str | None) -> str | None:
    """Expand abbreviated US locations to match the format of the main ingest actor.

    "Leesburg, VA"  →  "Leesburg, Virginia, United States"
    "Remote"        →  "Remote"   (unchanged)
    """
    if not location:
        return location
    location = location.strip()
    m = _CITY_STATE_RE.match(location)
    if m:
        city, abbr = m.group(1).strip(), m.group(2)
        state = US_STATES.get(abbr)
        if state:
            return f"{city}, {state}, United States"
    return location


VALID_STATUSES = [
    "new", "skipped", "autoskipped", "reviewing",
    "applied", "rejected", "ghosted", "interviewing", "offered",
    "withdrawn", "closed",
]


def parse_job_id(url: str) -> str | None:
    """Extract the numeric LinkedIn job ID from a URL or a bare numeric ID."""
    url = url.strip()
    if url.isdigit():
        return url
    m = JOB_URL_RE.search(url)
    return m.group(1) if m else None


def _parse_salary_string(s: str) -> tuple[int | None, int | None, str | None]:
    """Parse a salary range string like '$120,000 – $150,000' or '$85K/yr'.

    Returns (salary_min, salary_max, currency).  Hourly values (< $1,000)
    are converted to annual by multiplying by 2,080.
    """
    currency = "USD" if "$" in s else None
    numbers: list[int] = []
    for m in re.finditer(r"[\d,]+(?:\.\d*)?[Kk]?", s):
        raw = m.group().replace(",", "")
        try:
            if raw.lower().endswith("k"):
                val = int(float(raw[:-1]) * 1_000)
            else:
                val = int(float(raw))
        except ValueError:
            continue
        if val > 0:
            numbers.append(val)
    if not numbers:
        return None, None, currency
    lo, hi = min(numbers), max(numbers)
    # Treat as hourly if values look hourly (< $1,000)
    if hi < 1_000:
        lo, hi = lo * 2_080, hi * 2_080
    return lo, (hi if hi != lo else None), currency


def extract_fields_import(item: dict, job_id: str) -> dict:
    """Map apimaestro/linkedin-job-detail output to our DB schema.

    The actor returns a nested structure: job_info, company_info,
    salary_info, apply_details.
    """
    job_info     = item.get("job_info",     {}) or {}
    company_info = item.get("company_info", {}) or {}
    salary_info  = item.get("salary_info",  {}) or {}
    apply_det    = item.get("apply_details",{}) or {}

    title       = str(job_info.get("title")    or "").strip() or None
    company     = str(company_info.get("name") or "").strip() or None
    location    = normalize_location(str(job_info.get("location") or "").strip() or None)
    description = str(job_info.get("description") or "").strip() or None

    posted_raw  = job_info.get("listed_at") or job_info.get("original_listed_at")
    posted_date = str(posted_raw)[:10] if posted_raw else None

    # Salary — actor provides structured min/max/currency
    salary_min = salary_max = salary_currency = None
    sal_min_raw = salary_info.get("min_salary")
    sal_max_raw = salary_info.get("max_salary")
    try:
        salary_min = int(float(sal_min_raw)) if sal_min_raw not in (None, "") else None
    except (ValueError, TypeError):
        pass
    try:
        salary_max = int(float(sal_max_raw)) if sal_max_raw not in (None, "") else None
    except (ValueError, TypeError):
        pass
    salary_currency = salary_info.get("currency_code") or None

    apply_url  = str(apply_det.get("application_url") or "").strip() or None
    easy_apply = 1 if apply_det.get("is_easy_apply") else 0

    return {
        "job_id":          job_id,
        "title":           title,
        "company":         company,
        "location":        location,
        "posted_date":     posted_date,
        "job_url":         f"https://www.linkedin.com/jobs/view/{job_id}",
        "apply_url":       apply_url,
        "easy_apply":      easy_apply,
        "source":          "linkedin",
        "salary_min":      salary_min,
        "salary_max":      salary_max,
        "salary_currency": salary_currency,
        "job_description": description,
    }


def run_actor(api_token: str, urls: list[str], debug: bool = False) -> list[dict]:
    """Run apimaestro/linkedin-job-detail for the given URLs; return output items."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type":  "application/json",
    }
    # Actor input: list of numeric job IDs (extracted from the URLs).
    job_ids = [pid for u in urls if (pid := parse_job_id(u))]
    payload = {"job_id": job_ids}

    print(f"  Calling Apify actor {ACTOR_ID} for {len(job_ids)} job ID(s)...", flush=True)

    # waitForFinish blocks until the run completes (up to 300 s).
    resp = requests.post(
        f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
        headers=headers,
        json=payload,
        params={"waitForFinish": 300},
        timeout=360,
    )
    resp.raise_for_status()
    run = resp.json()["data"]

    if run["status"] != "SUCCEEDED":
        raise RuntimeError(
            f"Actor run {run['id']!r} ended with status {run['status']!r}. "
            "Check the Apify console for details."
        )

    dataset_id = run["defaultDatasetId"]
    items_resp = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {api_token}"},
        params={"limit": 1000},
        timeout=30,
    )
    items_resp.raise_for_status()
    items: list[dict] = items_resp.json()

    if debug:
        print("\n--- DEBUG: raw actor output (first 5,000 chars) ---")
        print(json.dumps(items, indent=2, ensure_ascii=False)[:5_000])
        print("--- END DEBUG ---\n")

    return items


def correlate_items(items: list[dict], url_to_id: dict[str, str]) -> dict[str, dict]:
    """Build a job_id → item map from the actor's output.

    The apimaestro actor returns nested objects; the job ID lives at
    job_info.job_posting_id or can be parsed from job_info.job_url.
    """
    id_to_item: dict[str, dict] = {}
    for item in items:
        item_id: str | None = None
        job_info = item.get("job_info") or {}

        # Preferred: explicit numeric ID field
        raw_id = str(job_info.get("job_posting_id") or "").strip()
        if raw_id.isdigit():
            item_id = raw_id

        # Fallback: parse from the URL in job_info
        if not item_id:
            item_id = parse_job_id(str(job_info.get("job_url") or ""))

        # Last resort: top-level flat fields (other actor conventions)
        if not item_id:
            for key in ("url", "link", "jobUrl", "job_url", "linkedinUrl"):
                val = item.get(key)
                if val:
                    item_id = parse_job_id(str(val))
                    if item_id:
                        break
        if not item_id:
            for key in ("jobId", "id", "linkedin_id", "linkedinId"):
                raw = str(item.get(key) or "").strip()
                if raw.isdigit():
                    item_id = raw
                    break

        if item_id:
            id_to_item[item_id] = item
    return id_to_item


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import LinkedIn job listings by URL into the job tracker."
    )
    parser.add_argument("urls", nargs="*", help="LinkedIn job URLs to import")
    parser.add_argument(
        "--status", default="applied",
        help=f"Initial status (default: applied). Valid: {', '.join(VALID_STATUSES)}",
    )
    parser.add_argument("--label", help="Label key to apply (must exist in config [labels])")
    parser.add_argument("--config", default="config.toml",
                        help="Path to TOML config (default: config.toml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be imported without writing")
    parser.add_argument("--debug", action="store_true",
                        help="Print raw Apify response for field mapping inspection")
    args = parser.parse_args()

    # Collect URLs from positional args and/or stdin
    all_urls = list(args.urls)
    if not sys.stdin.isatty():
        all_urls.extend(line.strip() for line in sys.stdin if line.strip())
    if not all_urls:
        parser.error("No URLs provided. Pass URLs as arguments or pipe them via stdin.")

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}")
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    api_token: str = config.get("api_token", "")
    if not api_token:
        sys.exit("No Apify API token found. Set 'api_token' in config.toml.")

    db_path: str = config.get("db_path", "jobs.db")

    if args.status not in VALID_STATUSES:
        sys.exit(f"Invalid status {args.status!r}. Valid: {', '.join(VALID_STATUSES)}")

    # Validate label if given
    labels_json = "[]"
    if args.label:
        known_labels = set(config.get("labels", {}).keys())
        if known_labels and args.label not in known_labels:
            print(
                f"WARNING: label {args.label!r} not found in config [labels]. "
                f"Known: {', '.join(sorted(known_labels))}",
                file=sys.stderr,
            )
        labels_json = json.dumps([args.label])

    # Parse job IDs from URLs
    url_to_id: dict[str, str] = {}
    for url in all_urls:
        job_id = parse_job_id(url)
        if job_id:
            url_to_id[url] = job_id
        else:
            print(f"WARNING: could not parse job ID from URL, skipping: {url}", file=sys.stderr)

    if not url_to_id:
        sys.exit("No valid LinkedIn job URLs found.")

    print(f"Importing {len(url_to_id)} LinkedIn job(s) with status '{args.status}'...")

    if args.dry_run:
        print("DRY RUN — would attempt to import:")
        for url, job_id in url_to_id.items():
            print(f"  {job_id}: {url}")
        return

    # Fetch from Apify
    try:
        items = run_actor(api_token, list(url_to_id.keys()), debug=args.debug)
    except Exception as exc:
        sys.exit(f"Apify call failed: {exc}")

    id_to_item = correlate_items(items, url_to_id)

    # Fallback: if correlation found nothing but counts match, map positionally.
    # LinkedIn sometimes redirects a job URL to a different job_posting_id;
    # in that case we keep the user's original job_id but use the returned data.
    if not id_to_item and items and len(items) == len(url_to_id):
        print(
            "  Note: returned job IDs didn't match requested URLs "
            "(LinkedIn may have redirected). Mapping positionally.",
            file=sys.stderr,
        )
        for (url, job_id), item in zip(url_to_id.items(), items):
            id_to_item[job_id] = item
    elif not id_to_item and items:
        print(
            "WARNING: Could not correlate Apify results back to job IDs. "
            "Run with --debug to inspect the raw output.",
            file=sys.stderr,
        )

    # Fuzzy dedup settings from config
    desc_threshold:  float = config.get("fuzzy_desc_threshold",  0.85)
    title_threshold: float = config.get("fuzzy_title_threshold", 0.6)

    conn = open_db(db_path)
    inserted = updated = stubbed = failed = 0

    for url, job_id in url_to_id.items():
        item     = id_to_item.get(job_id)
        existing = conn.execute(
            "SELECT job_id FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()

        if existing:
            old_row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            old_status = old_row["status"] if old_row else None
            conn.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?", (args.status, job_id)
            )
            ts = _now_iso()
            if old_status and old_status != args.status:
                append_history(conn, job_id, {"ts": ts, "event": "status", "from": old_status, "to": args.status})
            conn.commit()
            print(f"  Updated   {job_id}: status → {args.status}")
            updated += 1
            continue

        if item is None:
            # Job no longer exists on LinkedIn — insert a stub so the application is tracked
            print(f"  Stubbed   {job_id}: no data returned (posting may be expired)")
            conn.execute(
                "INSERT INTO jobs (job_id, job_url, status, source, labels, raw) "
                "VALUES (?, ?, ?, 'linkedin', ?, ?)",
                (job_id, url, args.status, labels_json, json.dumps({})),
            )
            ts = _now_iso()
            append_history(conn, job_id, {"ts": ts, "event": "imported", "status": args.status, "stub": True})
            conn.commit()
            stubbed += 1
            continue

        try:
            fields = extract_fields_import(item, job_id)
        except Exception as exc:
            print(f"  FAILED    {job_id}: field extraction error — {exc}", file=sys.stderr)
            failed += 1
            continue

        # Fuzzy dedup — link to existing canonical if description matches
        canon_id     = None
        canon_status = None
        if fields.get("title") and fields.get("job_description"):
            matches = find_canonical(
                conn, job_id,
                fields["title"], fields["company"], fields["job_description"],
                desc_threshold, title_threshold,
            )
            if matches:
                canon_id     = matches[0]["job_id"]
                canon_status = matches[0]["status"]
                print(f"  Linked    {job_id} as duplicate of {canon_id}")

        effective_status = canon_status if canon_status else args.status

        applied_statuses = {"applied", "interviewing", "offered", "rejected", "withdrawn", "ghosted"}
        conn.execute(
            """
            INSERT INTO jobs
                (job_id, title, company, location, posted_date, job_url, apply_url,
                 easy_apply, salary_min, salary_max, salary_currency,
                 labels, source, status, job_description, canonical_id, applied_at, raw)
            VALUES
                (:job_id, :title, :company, :location, :posted_date, :job_url, :apply_url,
                 :easy_apply, :salary_min, :salary_max, :salary_currency,
                 :labels, :source, :status, :job_description, :canonical_id, :applied_at, :raw)
            """,
            {
                **fields,
                "labels":       labels_json,
                "status":       effective_status,
                "canonical_id": canon_id,
                "applied_at":   datetime.now(timezone.utc).date().isoformat() if effective_status in applied_statuses else None,
                "raw":          json.dumps(item, ensure_ascii=False),
            },
        )
        ts = _now_iso()
        append_history(conn, job_id, {"ts": ts, "event": "imported", "status": effective_status})
        if canon_id:
            append_history(conn, job_id, {"ts": ts, "event": "linked", "canonical_id": canon_id})
        conn.commit()
        title_str   = fields.get("title")   or "(no title)"
        company_str = fields.get("company") or "(unknown company)"
        print(f"  Inserted  {job_id}: {title_str} at {company_str} → {effective_status}")
        inserted += 1

    conn.close()

    parts = []
    if inserted: parts.append(f"{inserted} inserted")
    if updated:  parts.append(f"{updated} updated")
    if stubbed:  parts.append(f"{stubbed} stubbed (no data)")
    if failed:   parts.append(f"{failed} failed")
    print("Done." + (f" {', '.join(parts)}." if parts else ""))


if __name__ == "__main__":
    main()
