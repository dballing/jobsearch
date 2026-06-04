#!/usr/bin/env python3
"""Flask web UI for browsing ingested LinkedIn jobs."""

import json
import math
import shlex
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, render_template, request, url_for
from ingest import append_history, bootstrap_history
from viability import prompt_hash

app = Flask(__name__)
PER_PAGE = 25

# Load config once at startup.
_config_path = Path("config.toml")
with open(_config_path, "rb") as _f:
    _cfg = tomllib.load(_f)

DB_PATH: str = _cfg.get("db_path", "jobs.db")

# Viability prompt hash — recomputed whenever config.toml changes on disk.
# Stored as a module-level mtime cache so a page refresh picks up edits
# without requiring an app restart.
_cfg_mtime: float = 0.0
_viability_hash_cache: str | None = None


def _current_viability_hash() -> str | None:
    """Return the viability prompt hash, refreshing if config.toml has changed."""
    global _cfg_mtime, _viability_hash_cache
    try:
        mtime = _config_path.stat().st_mtime
        if mtime != _cfg_mtime:
            _cfg_mtime = mtime
            with open(_config_path, "rb") as _rf:
                _live_cfg = tomllib.load(_rf)
            p = _live_cfg.get("viability", {}).get("prompt", "").strip()
            _viability_hash_cache = prompt_hash(p) if p else None
    except OSError:
        pass
    return _viability_hash_cache

# Build label → display-name mapping.
# Preferred source: top-level [labels] table in config.toml.
# Backward-compat fallback: per-task `display` key (older config format).
# Any label not covered by either defaults to the label uppercased at use-time.
_label_names: dict[str, str] = dict(_cfg.get("labels", {}))
for _t in _cfg.get("tasks", []):
    _lbl = _t["label"]
    if _lbl not in _label_names and "display" in _t:
        _label_names[_lbl] = _t["display"]
LABEL_NAMES: dict[str, str] = _label_names

SORTABLE_COLS = {
    "title", "company", "location", "salary_min",
    "status", "applied_at", "posted_date", "first_seen",
}
DEFAULT_SORT          = "first_seen"
DEFAULT_DIR           = "desc"
DEFAULT_VIEW          = "grouped"
DEFAULT_STATUS_FILTER = "active"

STATUSES = [
    "new", "skipped", "autoskipped", "reviewing",
    "applied", "rejected", "ghosted", "interviewing", "offered",
    "withdrawn", "closed",
]

STATUS_COLORS = {
    "new":          "primary",
    "reviewing":    "secondary",
    "applied":      "info",
    "interviewing": "warning",
    "offered":      "success",
    "rejected":     "danger",
    "withdrawn":    "secondary",
    "skipped":      "dark",
    "autoskipped":  "dark",
    "ghosted":      "secondary",
    "closed":       "dark",
}

SOURCE_NAMES = {
    "linkedin":   "LinkedIn",
    "careersite": "Career Sites",
}

GROUP_VARIED = "(varied)"  # Displayed whenever a grouped field differs across sub-rows.

VIABILITY_COLORS = {
    "high":   "success",
    "medium": "warning",
    "low":    "danger",
}

STATUS_FILTERS = {
    "new":       ("New",       "status = 'new'"),
    "reviewing": ("Reviewing", "status = 'reviewing'"),
    "active":    ("Active",    "status NOT IN ('skipped', 'autoskipped', 'rejected', 'withdrawn', 'ghosted', 'closed')"),
    "applied":   ("Applied",   "status IN ('applied', 'interviewing', 'offered', 'ghosted')"),
    "rejected":  ("Rejected",  "status = 'rejected'"),
    "all":       ("All",       None),
}

# Grouped header query — one row per canonical group.
# Jobs linked via canonical_id are grouped together; others are their own group.
GROUPED_HEADERS = """
    SELECT COALESCE(canonical_id, job_id) AS group_key,
           MIN(title)           AS title,
           MAX(title)           AS title_max,
           MIN(company)         AS company,
           MAX(company)         AS company_max,
           MIN(COALESCE(company_actual, company)) AS company_eff,
           MAX(COALESCE(company_actual, company)) AS company_eff_max,
           COUNT(*)             AS location_count,
           MIN(first_seen)      AS first_seen,
           MIN(posted_date)     AS posted_date,
           MIN(salary_min)      AS salary_min,
           MAX(salary_max)      AS salary_max,
           MIN(salary_currency) AS salary_currency,
           MIN(status)          AS status,
           MIN(source)          AS source,
           MAX(source)          AS source_max
    FROM jobs {where}
    GROUP BY COALESCE(canonical_id, job_id)
    {order}
    LIMIT ? OFFSET ?
"""
GROUPED_COUNT = "SELECT COUNT(*) FROM (SELECT 1 FROM jobs {where} GROUP BY COALESCE(canonical_id, job_id))"
FLAT_COUNT    = "SELECT COUNT(*) FROM jobs {where}"
FLAT_SELECT   = "SELECT * FROM jobs {where} {order} LIMIT ? OFFSET ?"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # WAL mode allows concurrent reads/writes with the rescore script.
        # busy_timeout retries on lock contention instead of raising immediately.
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        # Migrate: rename regions → labels if the old column still exists.
        cols = [row[1] for row in g.db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "regions" in cols and "labels" not in cols:
            g.db.execute("ALTER TABLE jobs RENAME COLUMN regions TO labels")
            g.db.commit()
        # Migrate: rename linkedin_url → job_url if old column still exists.
        if "linkedin_url" in cols and "job_url" not in cols:
            g.db.execute("ALTER TABLE jobs RENAME COLUMN linkedin_url TO job_url")
            g.db.commit()
        # Migrate: add source column if not present (existing rows default to 'linkedin').
        if "source" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'linkedin'")
            g.db.commit()
        # Migrate: add last_synced_at to ingest_state if not present.
        state_cols = [row[1] for row in g.db.execute("PRAGMA table_info(ingest_state)").fetchall()]
        if state_cols and "last_synced_at" not in state_cols:
            g.db.execute("ALTER TABLE ingest_state ADD COLUMN last_synced_at TEXT")
            g.db.commit()
        # Migrate: rename 'reviewed' → 'reviewing'.
        g.db.execute("UPDATE jobs SET status = 'reviewing' WHERE status = 'reviewed'")
        g.db.commit()
        # Migrate: add refreshed_at and canonical_id columns if not present.
        cols = [row[1] for row in g.db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "refreshed_at" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN refreshed_at TIMESTAMP")
            g.db.commit()
        if "canonical_id" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN canonical_id TEXT")
            g.db.commit()
        # Migrate: add viability scoring columns if not present.
        cols = [row[1] for row in g.db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "viability" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN viability TEXT")
            g.db.commit()
        if "viability_reason" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN viability_reason TEXT")
            g.db.commit()
        if "viability_prompt_hash" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN viability_prompt_hash TEXT")
            g.db.commit()
        cols = [row[1] for row in g.db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "applied_at" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
            g.db.execute(
                "UPDATE jobs SET applied_at = first_seen "
                "WHERE status IN ('applied','interviewing','offered','rejected','withdrawn','ghosted') "
                "AND applied_at IS NULL"
            )
            g.db.commit()
        cols = [row[1] for row in g.db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "history" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN history TEXT NOT NULL DEFAULT '[]'")
            g.db.commit()
            bootstrap_history(g.db)
        cols = [row[1] for row in g.db.execute("PRAGMA table_info(jobs)").fetchall()]
        if "company_actual" not in cols:
            g.db.execute("ALTER TABLE jobs ADD COLUMN company_actual TEXT")
            g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(e=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_nav_timestamps() -> dict:
    try:
        row = get_db().execute(
            "SELECT MAX(last_run_at) AS data_at, MAX(last_synced_at) AS synced_at FROM ingest_state"
        ).fetchone()
        data_iso   = row["data_at"]   if row and row["data_at"]   else None
        synced_iso = row["synced_at"] if row and row["synced_at"] else None
        fmt = lambda ts: ts[:16].replace("T", " ") + " UTC" if ts else None
    except sqlite3.OperationalError:
        data_iso = synced_iso = None
        fmt = lambda ts: None
    return {
        "last_data_at":    fmt(data_iso),
        "last_data_iso":   data_iso,
        "last_synced_at":  fmt(synced_iso),
        "last_synced_iso": synced_iso,
    }


def build_where(label: str, status_filter: str, q: str = "", source: str = "",
                viability: str = "") -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []
    if label:
        conditions.append("labels LIKE ?")
        params.append(f'%"{label}"%')
    sql_condition = STATUS_FILTERS.get(status_filter, (None, None))[1]
    if sql_condition:
        conditions.append(sql_condition)
    if source:
        conditions.append("source = ?")
        params.append(source)
    if viability == "unscored":
        conditions.append("viability IS NULL")
    elif viability in VIABILITY_COLORS:
        conditions.append("viability = ?")
        params.append(viability)
    if q:
        conditions.append("(title LIKE ? OR company LIKE ? OR company_actual LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
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


def decode_labels(raw: str | None) -> list[str]:
    return [LABEL_NAMES.get(r, r.upper()) for r in json.loads(raw or "[]")]


def process_job_row(row: sqlite3.Row | dict) -> dict:
    j = dict(row)
    j["labels"]           = decode_labels(j.get("labels"))
    j["company_actual"]   = j.get("company_actual")
    j["company_display"]  = j.get("company_actual") or j.get("company") or ""
    j["status_color"]     = STATUS_COLORS.get(j.get("status", "new"), "secondary")
    j["salary_display"]   = format_salary(j)
    j["source_display"]   = SOURCE_NAMES.get(j.get("source", "linkedin"), j.get("source", ""))
    j["applied_at"]       = (j.get("applied_at") or "")[:10] or None
    j["viability_color"]  = VIABILITY_COLORS.get(j.get("viability") or "", "")
    _cur_hash = _current_viability_hash()
    j["viability_stale"]  = bool(
        j.get("viability") is not None
        and _cur_hash is not None
        and j.get("viability_prompt_hash") != _cur_hash
    )
    # Extract full locations list from raw JSON for tooltip display.
    try:
        raw_data = json.loads(j.get("raw") or "{}")
        locs = raw_data.get("locations_derived") or []
        locs = locs if isinstance(locs, list) else []
    except (json.JSONDecodeError, TypeError):
        locs = []
    j["locations_count"] = len(locs)
    j["locations_all"]   = "\n".join(locs) if len(locs) > 1 else ""
    j["refreshed_at"]    = j.get("refreshed_at")
    return j


def _group_field(sub_rows: list[dict], key: str, fmt=None) -> object:
    """Return the uniform value across all sub-rows, or '(varied)' if they differ.

    An optional fmt callable is applied to each raw value before comparison
    (e.g. to truncate timestamps to date strings). Returns None if every value
    is falsy (template renders it as '—').
    """
    vals = [(fmt(s.get(key)) if fmt else s.get(key)) for s in sub_rows]
    if not vals:
        return None
    first = vals[0]
    return first if all(v == first for v in vals) else GROUP_VARIED


def fetch_sub_rows(db: sqlite3.Connection, group_key: str,
                   where: str, params: list) -> list[dict]:
    """Fetch all jobs belonging to a canonical group.

    A group contains:
      - the canonical job itself (canonical_id IS NULL AND job_id = group_key)
      - all fuzzy duplicates that point at it (canonical_id = group_key)
    """
    and_clause = f"{where} AND " if where else "WHERE "
    rows = db.execute(
        f"SELECT * FROM jobs {and_clause}"
        "(canonical_id = ? OR (canonical_id IS NULL AND job_id = ?)) ORDER BY location",
        params + [group_key, group_key],
    ).fetchall()
    return [process_job_row(r) for r in rows]


def build_grouped_job(header: sqlite3.Row, sub_rows: list[dict]) -> dict:
    h             = dict(header)
    # is_fuzzy_group: more than one job in this canonical group (canonical + ≥1 duplicate)
    is_fuzzy_group = h["location_count"] > 1
    multi          = is_fuzzy_group
    # Title/company may vary within a fuzzy group (different titles from different sources)
    title   = h["title"] if h["title"] == h.get("title_max")         else GROUP_VARIED
    company = h["company_eff"] if h["company_eff"] == h.get("company_eff_max") else GROUP_VARIED
    sub_statuses      = [s.get("status", "new") for s in sub_rows]
    unique_statuses   = set(sub_statuses)
    viability_vals         = [s.get("viability") for s in sub_rows]
    unique_viabilities     = set(viability_vals)
    group_viability        = viability_vals[0] if len(unique_viabilities) == 1 else GROUP_VARIED
    group_viability_color  = VIABILITY_COLORS.get(group_viability or "", "")
    group_viability_stale  = any(s.get("viability_stale") for s in sub_rows)
    # Build tooltip: one representative reason per distinct viability level.
    _seen_levels: dict[str, str] = {}
    for s in sub_rows:
        v, r = s.get("viability"), s.get("viability_reason")
        if v and r and v not in _seen_levels:
            _seen_levels[v] = r
    _tooltip_lines = []
    if group_viability_stale:
        _tooltip_lines.append("Stale — re-run rescore_viability.sh")
    for _level in ("high", "medium", "low"):
        if _level in _seen_levels:
            _tooltip_lines.append(f"{_level}: {_seen_levels[_level]}")
    group_viability_tooltip = "\n\n".join(_tooltip_lines)
    # group_source: the single source if all sub-rows agree, else GROUP_VARIED
    group_source = h["source"] if h.get("source") == h.get("source_max") else GROUP_VARIED

    job = {
        "title":            title,
        "company":          company,
        "company_display":  company,
        "location_count":   h["location_count"],
        "multi":            multi,
        "is_fuzzy_group":   is_fuzzy_group,
        "sub_rows":         sub_rows,
        "preview_job_id":   sub_rows[0]["job_id"] if sub_rows else None,
        "group_status":     next(iter(unique_statuses)) if len(unique_statuses) == 1 else None,
        "group_source":     group_source,
        "group_source_display": SOURCE_NAMES.get(group_source, "") if group_source else None,
        "sub_job_ids":      [s["job_id"] for s in sub_rows if s.get("job_id")],
        "has_company_override":    any(s.get("company_actual") for s in sub_rows),
        "group_viability":         group_viability,
        "group_viability_color":   group_viability_color,
        "group_viability_stale":   group_viability_stale,
        "group_viability_tooltip": group_viability_tooltip,
        "group_applied":    _group_field(sub_rows, "applied_at",
                                         fmt=lambda v: (v or "")[:10]),
        "group_salary":     _group_field(sub_rows, "salary_display"),
        "group_labels":     _group_field(sub_rows, "labels"),
        "group_posted":     _group_field(sub_rows, "posted_date",
                                         fmt=lambda v: (v or "")[:10]),
        "group_first_seen": _group_field(sub_rows, "first_seen",
                                         fmt=lambda v: (v or "")[:10]),
    }
    if not multi and sub_rows:
        s = sub_rows[0]
        job.update({
            "job_id":           s.get("job_id"),
            "job_url":          s.get("job_url"),
            "location_primary": s.get("location") or "—",
            "locations_all":    s.get("locations_all", ""),
            "locations_count":  s.get("locations_count", 1),
            "refreshed_at":     s.get("refreshed_at"),
            "salary_display":   s["salary_display"],
            "labels":           s["labels"],
            "source":           s.get("source", "linkedin"),
            "source_display":   s["source_display"],
            "status":           s.get("status", "new"),
            "status_color":     s["status_color"],
            "applied_at":       s.get("applied_at"),
            "posted_date":      s.get("posted_date", ""),
            "first_seen":       s.get("first_seen", ""),
            "viability":        s.get("viability"),
            "viability_reason": s.get("viability_reason"),
            "viability_color":  s.get("viability_color", ""),
            "viability_stale":  s.get("viability_stale", False),
            "company":          s.get("company", ""),
            "company_actual":   s.get("company_actual"),
            "company_display":  s.get("company_display", ""),
        })
    return job


def available_labels(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT DISTINCT je.value FROM jobs, json_each(jobs.labels) je ORDER BY je.value"
    ).fetchall()
    return [
        {"value": r["value"], "label": LABEL_NAMES.get(r["value"], r["value"].upper())}
        for r in rows
    ]


def has_viability_scores(db: sqlite3.Connection) -> bool:
    """Return True if any jobs have been scored for viability."""
    return db.execute(
        "SELECT COUNT(*) FROM jobs WHERE viability IS NOT NULL"
    ).fetchone()[0] > 0


def available_sources(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT DISTINCT source FROM jobs ORDER BY source").fetchall()
    return [
        {"value": r["source"], "label": SOURCE_NAMES.get(r["source"], r["source"])}
        for r in rows
    ]


def sort_url(col: str, current_sort: str, current_dir: str,
             label: str, view: str, status_filter: str, q: str = "",
             source: str = "", viability: str = "") -> str:
    if current_sort != col:
        # Not currently sorted by this column — start ascending.
        new_sort, new_dir = col, "asc"
    elif current_dir == "asc":
        # First click already done — go descending.
        new_sort, new_dir = col, "desc"
    else:
        # Second click already done — clear back to default.
        new_sort, new_dir = DEFAULT_SORT, DEFAULT_DIR
    return url_for("index", sort=new_sort, dir=new_dir,
                   label=label or None, view=view, source=source or None,
                   viability=viability or None,
                   status_filter=status_filter, q=q or None, page=1)


@app.route("/")
def index():
    db = get_db()

    label         = request.args.get("label", "")
    q             = request.args.get("q", "").strip()
    sort          = request.args.get("sort", DEFAULT_SORT)
    direction     = request.args.get("dir", DEFAULT_DIR)
    view          = request.args.get("view", DEFAULT_VIEW)
    status_filter = request.args.get("status_filter", DEFAULT_STATUS_FILTER)
    source        = request.args.get("source", "")
    viability     = request.args.get("viability", "")
    page          = max(1, request.args.get("page", 1, type=int))

    if sort not in SORTABLE_COLS:
        sort = DEFAULT_SORT
    if direction not in ("asc", "desc"):
        direction = DEFAULT_DIR
    if view not in ("grouped", "flat"):
        view = DEFAULT_VIEW
    if status_filter not in STATUS_FILTERS:
        status_filter = DEFAULT_STATUS_FILTER
    if source not in SOURCE_NAMES and source != "":
        source = ""
    if viability not in ("high", "medium", "low", "unscored", ""):
        viability = ""
    if view == "grouped" and sort == "location":
        sort = DEFAULT_SORT

    where, params = build_where(label, status_filter, q, source, viability)
    _TEXT_COLS = {"title", "company", "location", "status"}
    sort_expr = f"{sort} COLLATE NOCASE" if sort in _TEXT_COLS else sort
    # For applied_at, NULLs sort first when descending so unapplied jobs
    # bubble to the top — making it easy to find jobs still needing action.
    # All other columns keep NULLs last in both directions.
    _NULLS_FIRST_DESC = {"applied_at"}
    nulls = "NULLS FIRST" if (sort in _NULLS_FIRST_DESC and direction == "desc") else "NULLS LAST"
    order  = f"ORDER BY {sort_expr} {direction.upper()} {nulls}"
    offset = (page - 1) * PER_PAGE

    if view == "grouped":
        total   = db.execute(GROUPED_COUNT.format(where=where), params).fetchone()[0]
        headers = db.execute(GROUPED_HEADERS.format(where=where, order=order),
                             params + [PER_PAGE, offset]).fetchall()
        jobs = [
            build_grouped_job(h, fetch_sub_rows(db, h["group_key"], where, params))
            for h in headers
        ]
    else:
        total = db.execute(FLAT_COUNT.format(where=where), params).fetchone()[0]
        rows  = db.execute(FLAT_SELECT.format(where=where, order=order),
                           params + [PER_PAGE, offset]).fetchall()
        jobs  = [process_job_row(r) for r in rows]

    total_pages          = max(1, math.ceil(total / PER_PAGE))
    labels               = available_labels(db)
    sources              = available_sources(db)
    show_viability_filter = has_viability_scores(db)
    col_urls             = {
        col: sort_url(col, sort, direction, label, view, status_filter, q, source, viability)
        for col in SORTABLE_COLS
    }

    return render_template(
        "jobs.html",
        jobs=jobs,
        page=page,
        total_pages=total_pages,
        total=total,
        label=label,
        q=q,
        sort=sort,
        direction=direction,
        view=view,
        status_filter=status_filter,
        status_filters=STATUS_FILTERS,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        labels=labels,
        sources=sources,
        source=source,
        viability=viability,
        show_viability_filter=show_viability_filter,
        group_varied=GROUP_VARIED,
        col_urls=col_urls,
    )


@app.route("/stats")
def stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    by_status = {
        r["status"]: r["cnt"]
        for r in db.execute(
            "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
    }
    new_7d = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE first_seen >= datetime('now', '-7 days')"
    ).fetchone()[0]
    label_rows = db.execute(
        """SELECT je.value AS lbl, COUNT(*) AS cnt
           FROM jobs, json_each(jobs.labels) je
           GROUP BY je.value ORDER BY cnt DESC"""
    ).fetchall()
    by_label = [
        {"label": r["lbl"], "display": LABEL_NAMES.get(r["lbl"], r["lbl"].upper()), "count": r["cnt"]}
        for r in label_rows
    ]
    viability_rows = db.execute(
        "SELECT COALESCE(viability, 'unscored') AS level, COUNT(*) AS cnt "
        "FROM jobs GROUP BY level ORDER BY cnt DESC"
    ).fetchall()
    by_viability = {r["level"]: r["cnt"] for r in viability_rows}
    current_hash = _current_viability_hash()
    # Only flag stale scores on active jobs — old/closed/skipped jobs are
    # intentionally not rescored and would make this number misleadingly large.
    _, active_condition = STATUS_FILTERS["active"]
    viability_stale = db.execute(
        f"SELECT COUNT(*) FROM jobs WHERE viability IS NOT NULL "
        f"AND viability_prompt_hash != ? AND {active_condition}",
        (current_hash,),
    ).fetchone()[0] if current_hash else 0
    return {
        "total": total,
        "by_status": by_status,
        "new_last_7_days": new_7d,
        "by_label": by_label,
        "by_viability": by_viability,
        "viability_stale": viability_stale,
    }


@app.route("/stats/history")
def stats_history():
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT DATE(run_at) AS day,
                   SUM(inserted)  AS inserted,
                   SUM(updated)   AS updated,
                   SUM(unchanged) AS unchanged
            FROM ingest_history
            WHERE run_at >= datetime('now', '-7 days')
            GROUP BY DATE(run_at)
            ORDER BY day
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    return {
        "days":      [r["day"]       for r in rows],
        "inserted":  [r["inserted"]  for r in rows],
        "updated":   [r["updated"]   for r in rows],
        "unchanged": [r["unchanged"] for r in rows],
    }


@app.route("/job/<job_id>")
def get_job(job_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return "Not found", 404
    job = dict(row)
    return {
        "job_id":           job["job_id"],
        "title":            job["title"],
        "company":          job["company"],
        "company_actual":   job.get("company_actual"),
        "location":         job["location"],
        "job_url":          job["job_url"],
        "apply_url":        job["apply_url"],
        "easy_apply":       job["easy_apply"],
        "salary_display":   format_salary(job),
        "posted_date":      (job["posted_date"] or "")[:10],
        "job_description":  job["job_description"],
        "viability":        job.get("viability"),
        "viability_reason": job.get("viability_reason"),
        "applied_at":       (job.get("applied_at") or "")[:10] or None,
        "history":          json.loads(job.get("history") or "[]"),
    }


@app.route("/jobs/status", methods=["POST"])
def update_jobs_status():
    new_status = request.form.get("status", "")
    job_ids    = request.form.getlist("job_ids")
    if new_status not in STATUSES or not job_ids:
        return "Invalid request", 400
    db = get_db()
    # Capture old statuses before the bulk update so we can log accurate from→to.
    placeholders = ",".join("?" * len(job_ids))
    old_statuses = {
        r["job_id"]: r["status"]
        for r in db.execute(
            f"SELECT job_id, status FROM jobs WHERE job_id IN ({placeholders})", job_ids
        ).fetchall()
    }
    db.executemany(
        """UPDATE jobs SET status = ?, refreshed_at = NULL,
           applied_at = CASE
             WHEN ? = 'applied' AND status IN ('new','reviewing','skipped','autoskipped') THEN CURRENT_TIMESTAMP
             WHEN ? IN ('new','reviewing','skipped','autoskipped') THEN NULL
             ELSE applied_at
           END
           WHERE job_id = ?""",
        [(new_status, new_status, new_status, jid) for jid in job_ids],
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for jid in job_ids:
        old = old_statuses.get(jid)
        if old and old != new_status:
            append_history(db, jid, {"ts": ts, "event": "status", "from": old, "to": new_status})
    db.commit()
    return "", 204


@app.route("/job/<job_id>/status", methods=["POST"])
def update_status(job_id: str):
    new_status = request.form.get("status", "")
    if new_status not in STATUSES:
        return "Invalid status", 400
    db = get_db()
    row = db.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    old_status = row["status"] if row else None
    db.execute(
        """UPDATE jobs SET status = ?, refreshed_at = NULL,
           applied_at = CASE
             WHEN ? = 'applied' AND status IN ('new','reviewing','skipped','autoskipped') THEN CURRENT_TIMESTAMP
             WHEN ? IN ('new','reviewing','skipped','autoskipped') THEN NULL
             ELSE applied_at
           END
           WHERE job_id = ?""",
        (new_status, new_status, new_status, job_id),
    )
    if old_status and old_status != new_status:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        append_history(db, job_id, {"ts": ts, "event": "status", "from": old_status, "to": new_status})
    db.commit()
    return "", 204


@app.route("/job/<job_id>/company_actual", methods=["POST"])
def set_company_actual(job_id: str):
    value = request.form.get("company_actual", "").strip() or None
    db = get_db()
    row = db.execute("SELECT company_actual FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return "Not found", 404
    old = row["company_actual"]
    db.execute("UPDATE jobs SET company_actual = ? WHERE job_id = ?", (value, job_id))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "company_actual", "from": old, "to": value})
    db.commit()
    return "", 204


@app.route("/jobs/autocomplete")
def jobs_autocomplete():
    q       = request.args.get("q", "").strip()
    exclude = request.args.get("exclude", "")
    if not q:
        return []
    db = get_db()
    # Quoted phrases ("senior tpm") count as one token; unquoted words split normally.
    # Fall back to plain split if the user leaves a quote unclosed.
    try:
        tokens = shlex.split(q)
    except ValueError:
        tokens = q.split()
    if not tokens:
        return []
    # Each token must appear in title OR company (AND across tokens).
    token_clauses = " AND ".join(
        "(j.title LIKE ? OR j.company LIKE ? OR COALESCE(j.company_actual, j.company) LIKE ?)" for _ in tokens
    )
    token_params  = [p for t in tokens for p in (f"%{t}%", f"%{t}%", f"%{t}%")]
    rows = db.execute(
        f"""SELECT j.job_id, j.title, j.company, j.location, j.status, j.viability,
                   j.canonical_id,
                   c.title   AS canonical_title,
                   c.company AS canonical_company
            FROM jobs j
            LEFT JOIN jobs c ON c.job_id = j.canonical_id
            WHERE {token_clauses}
              AND j.job_id != ?
            ORDER BY j.first_seen DESC
            LIMIT 10""",
        token_params + [exclude or ""],
    ).fetchall()
    return [dict(r) for r in rows]


@app.route("/job/<job_id>/link", methods=["POST"])
def link_job(job_id: str):
    db             = get_db()
    target_raw     = request.form.get("canonical_id", "").strip()
    ts             = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Unlink ──────────────────────────────────────────────────────────────
    if not target_raw:
        db.execute("UPDATE jobs SET canonical_id = NULL WHERE job_id = ?", (job_id,))
        append_history(db, job_id, {"ts": ts, "event": "unlinked"})
        db.commit()
        return "", 204

    # ── Link ─────────────────────────────────────────────────────────────────
    if target_raw == job_id:
        return {"error": "A job cannot be linked to itself."}, 400

    target_row = db.execute(
        "SELECT job_id, canonical_id, title, company FROM jobs WHERE job_id = ?",
        (target_raw,),
    ).fetchone()
    if not target_row:
        return {"error": f"Job {target_raw!r} not found."}, 404

    # Follow one hop to the root (prevents chaining).
    resolved_root = target_row["canonical_id"] or target_row["job_id"]

    if resolved_root == job_id:
        return {"error": "Cannot create a circular link."}, 400

    # Update the job itself.
    db.execute("UPDATE jobs SET canonical_id = ? WHERE job_id = ?", (resolved_root, job_id))
    append_history(db, job_id, {"ts": ts, "event": "linked", "canonical_id": resolved_root})

    # Inherit the canonical's status if the job is still in an early state.
    job_row = db.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if job_row and job_row["status"] in ("new", "reviewing"):
        root_row = db.execute("SELECT status FROM jobs WHERE job_id = ?", (resolved_root,)).fetchone()
        if root_row and root_row["status"] not in ("new", "reviewing"):
            db.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (root_row["status"], job_id))
            append_history(db, job_id, {
                "ts": ts, "event": "status",
                "from": job_row["status"], "to": root_row["status"],
                "note": "inherited from canonical on link",
            })

    # Re-point any existing dependents of this job to the new root so no
    # two-hop chains are created (data model guarantees at most one hop).
    db.execute(
        "UPDATE jobs SET canonical_id = ? WHERE canonical_id = ?",
        (resolved_root, job_id),
    )

    db.commit()
    return {"canonical_id": resolved_root}, 200


if __name__ == "__main__":
    app.run(debug=True)
