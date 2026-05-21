#!/usr/bin/env python3
"""Flask web UI for browsing ingested LinkedIn jobs."""

import json
import math
import sqlite3
import tomllib
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, url_for

app = Flask(__name__)
PER_PAGE = 25

SORTABLE_COLS = {
    "title", "company", "location", "salary_min",
    "status", "posted_date", "first_seen",
}
DEFAULT_SORT          = "first_seen"
DEFAULT_DIR           = "desc"
DEFAULT_VIEW          = "grouped"
DEFAULT_STATUS_FILTER = "active"

STATUSES = [
    "new", "reviewed", "applied", "interviewing",
    "offered", "rejected", "withdrawn", "skipped",
]

STATUS_COLORS = {
    "new":          "primary",
    "reviewed":     "secondary",
    "applied":      "info",
    "interviewing": "warning",
    "offered":      "success",
    "rejected":     "danger",
    "withdrawn":    "secondary",
    "skipped":      "dark",
}

REGION_LABELS = {
    "dc": "DC/DMV",
    "nc": "NC",
}

STATUS_FILTERS = {
    "active":  ("Active",  "status NOT IN ('skipped', 'rejected', 'withdrawn')"),
    "applied": ("Applied", "status IN ('applied', 'interviewing', 'offered')"),
    "all":     ("All",     None),
}

# Grouped header query — one row per (title, company).
GROUPED_HEADERS = """
    SELECT title, company, COUNT(*) AS location_count,
           MIN(first_seen)      AS first_seen,
           MIN(posted_date)     AS posted_date,
           MIN(salary_min)      AS salary_min,
           MAX(salary_max)      AS salary_max,
           MIN(salary_currency) AS salary_currency,
           MIN(status)          AS status
    FROM jobs {where}
    GROUP BY title, company
    {order}
    LIMIT ? OFFSET ?
"""
GROUPED_COUNT = "SELECT COUNT(*) FROM (SELECT 1 FROM jobs {where} GROUP BY title, company)"
FLAT_COUNT    = "SELECT COUNT(*) FROM jobs {where}"
FLAT_SELECT   = "SELECT * FROM jobs {where} {order} LIMIT ? OFFSET ?"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        config_path = Path("config.toml")
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        g.db = sqlite3.connect(config.get("db_path", "jobs.db"))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def build_where(region: str, status_filter: str) -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []
    if region:
        conditions.append("regions LIKE ?")
        params.append(f'%"{region}"%')
    sql_condition = STATUS_FILTERS.get(status_filter, (None, None))[1]
    if sql_condition:
        conditions.append(sql_condition)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def format_salary(row: dict) -> str:
    lo, hi = row.get("salary_min"), row.get("salary_max")
    if lo and hi:
        return f"${lo // 1000}k – ${hi // 1000}k"
    if lo:
        return f"${lo // 1000}k+"
    if hi:
        return f"up to ${hi // 1000}k"
    return ""


def decode_regions(raw: str | None) -> list[str]:
    return [REGION_LABELS.get(r, r.upper()) for r in json.loads(raw or "[]")]


def process_job_row(row: sqlite3.Row | dict) -> dict:
    j = dict(row)
    j["regions"]        = decode_regions(j.get("regions"))
    j["status_color"]   = STATUS_COLORS.get(j.get("status", "new"), "secondary")
    j["salary_display"] = format_salary(j)
    return j


def fetch_sub_rows(db: sqlite3.Connection, title: str, company: str,
                   where: str, params: list) -> list[dict]:
    and_clause = f"{where} AND " if where else "WHERE "
    rows = db.execute(
        f"SELECT * FROM jobs {and_clause}title = ? AND company = ? ORDER BY location",
        params + [title, company],
    ).fetchall()
    return [process_job_row(r) for r in rows]


def build_grouped_job(header: sqlite3.Row, sub_rows: list[dict]) -> dict:
    h     = dict(header)
    multi = h["location_count"] > 1
    job   = {
        "title":          h["title"],
        "company":        h["company"],
        "location_count": h["location_count"],
        "multi":          multi,
        "sub_rows":       sub_rows,
    }
    if not multi and sub_rows:
        s = sub_rows[0]
        job.update({
            "job_id":           s.get("job_id"),
            "linkedin_url":     s.get("linkedin_url"),
            "location_primary": s.get("location") or "—",
            "salary_display":   s["salary_display"],
            "regions":          s["regions"],
            "status":           s.get("status", "new"),
            "status_color":     s["status_color"],
            "posted_date":      s.get("posted_date", ""),
            "first_seen":       s.get("first_seen", ""),
        })
    return job


def available_regions(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT DISTINCT je.value FROM jobs, json_each(jobs.regions) je ORDER BY je.value"
    ).fetchall()
    return [
        {"value": r["value"], "label": REGION_LABELS.get(r["value"], r["value"].upper())}
        for r in rows
    ]


def sort_url(col: str, current_sort: str, current_dir: str,
             region: str, view: str, status_filter: str) -> str:
    new_dir = "asc" if (current_sort != col or current_dir == "desc") else "desc"
    return url_for("index", sort=col, dir=new_dir,
                   region=region or None, view=view,
                   status_filter=status_filter, page=1)


@app.route("/")
def index():
    db = get_db()

    region        = request.args.get("region", "")
    sort          = request.args.get("sort", DEFAULT_SORT)
    direction     = request.args.get("dir", DEFAULT_DIR)
    view          = request.args.get("view", DEFAULT_VIEW)
    status_filter = request.args.get("status_filter", DEFAULT_STATUS_FILTER)
    page          = max(1, request.args.get("page", 1, type=int))

    if sort not in SORTABLE_COLS:
        sort = DEFAULT_SORT
    if direction not in ("asc", "desc"):
        direction = DEFAULT_DIR
    if view not in ("grouped", "flat"):
        view = DEFAULT_VIEW
    if status_filter not in STATUS_FILTERS:
        status_filter = DEFAULT_STATUS_FILTER
    if view == "grouped" and sort == "location":
        sort = DEFAULT_SORT

    where, params = build_where(region, status_filter)
    order  = f"ORDER BY {sort} {direction.upper()} NULLS LAST"
    offset = (page - 1) * PER_PAGE

    if view == "grouped":
        total   = db.execute(GROUPED_COUNT.format(where=where), params).fetchone()[0]
        headers = db.execute(GROUPED_HEADERS.format(where=where, order=order),
                             params + [PER_PAGE, offset]).fetchall()
        jobs = [
            build_grouped_job(h, fetch_sub_rows(db, h["title"], h["company"], where, params))
            for h in headers
        ]
    else:
        total = db.execute(FLAT_COUNT.format(where=where), params).fetchone()[0]
        rows  = db.execute(FLAT_SELECT.format(where=where, order=order),
                           params + [PER_PAGE, offset]).fetchall()
        jobs  = [process_job_row(r) for r in rows]

    total_pages = max(1, math.ceil(total / PER_PAGE))
    regions     = available_regions(db)
    col_urls    = {
        col: sort_url(col, sort, direction, region, view, status_filter)
        for col in SORTABLE_COLS
    }

    return render_template(
        "jobs.html",
        jobs=jobs,
        page=page,
        total_pages=total_pages,
        total=total,
        region=region,
        sort=sort,
        direction=direction,
        view=view,
        status_filter=status_filter,
        status_filters=STATUS_FILTERS,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        regions=regions,
        col_urls=col_urls,
    )


@app.route("/job/<job_id>/status", methods=["POST"])
def update_status(job_id: str):
    new_status = request.form.get("status", "")
    if new_status not in STATUSES:
        return "Invalid status", 400
    db = get_db()
    db.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (new_status, job_id))
    db.commit()
    return "", 204


if __name__ == "__main__":
    app.run(debug=True)
