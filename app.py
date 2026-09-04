#!/usr/bin/env python3
"""Flask web UI for browsing, filtering, and tracking ingested jobs.

Read-mostly companion to ingest.py. It serves the jobs table (filter / search / sort /
paginate), two grouping modes (matched-jobs via canonical_id, and by employer), the
per-job preview with status / notes / attachments, manual company & salary overrides,
manual fuzzy-link editing, and a stats dashboard. The SQLite DB is shared with ingest
and the rescore script via WAL: this process migrates once at startup (_init_db) and is
otherwise read-mostly, with a handful of small write endpoints for user actions.
"""

import json
import math
import os
import re
import shlex
import sqlite3
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, abort, g, has_request_context, make_response, render_template,
                   request, send_file, url_for)
from werkzeug.utils import secure_filename
from config import ConfigError, DEFAULT_SEARCH_ID, load_config
from ingest import (adopt_legacy, append_history, backfill_description_truncated,
                    bootstrap_history, ensure_job_search_state, history_scope)
from viability import (
    _work_arrangement, FACTOR_DIMENSIONS, GEO_UNSUPPORTED_ARRANGEMENT, MANUAL_GEO_FIT_CHOICES,
    assess_location_fit, clamp_viability_for_geo, description_is_truncated, effective_description,
    geo_note, has_description_override, manual_geo_verdict, parse_factors,
    score_job, scoring_hash_for_config,
)

app = Flask(__name__)
PER_PAGE = 25                                  # default page size
PER_PAGE_OPTIONS = ["25", "50", "100", "200", "all"]  # user-selectable page sizes

# Load config once at startup via the shared loader. The config and DB paths can be
# overridden via env vars (JOBSEARCH_CONFIG / JOBSEARCH_DB) — used by the test suite to point
# at throwaway files so importing this module never migrates the real jobs.db.
_config_path = Path(os.environ.get("JOBSEARCH_CONFIG", "config.toml"))
APP_CONFIG = load_config(_config_path)

DB_PATH: str = os.environ.get("JOBSEARCH_DB") or APP_CONFIG.db_path

# Where uploaded attachments live on disk (UUID filenames; real names in the DB).
UPLOADS_DIR: str = APP_CONFIG.uploads_dir
os.makedirs(UPLOADS_DIR, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB per upload

# Per-search viability prompt hash — recomputed whenever any config source file changes on
# disk, so a live prompt edit shows up without an app restart. Keyed by search_id (each lens
# scores under its own criteria), invalidated on the newest mtime across all config files.
_cfg_mtime: float = 0.0
_viability_hash_cache: dict[str, str | None] = {}


def _current_viability_hash(search_id: str | None = None) -> str | None:
    """The viability prompt hash for a search ("lens"), refreshing if any config file changed.

    Defaults to the request's current lens. Kept per-search so the web-UI staleness check matches
    exactly the hash the batch/on-demand rescore stamps for that search. None when the search has
    no viability prompt configured."""
    global _cfg_mtime, _viability_hash_cache
    sid = search_id or _current_search_id()
    try:
        mtime = max((p.stat().st_mtime for p in APP_CONFIG.source_files), default=0.0)
        if mtime != _cfg_mtime:
            _cfg_mtime = mtime
            live = load_config(_config_path)
            _viability_hash_cache = {s.id: scoring_hash_for_config(s.config) for s in live.searches}
    except (OSError, ConfigError):
        pass
    return _viability_hash_cache.get(sid)

# Label → display-name mapping, unioned across searches by the loader (preferred source:
# each search's [labels] table; backward-compat fallback: per-task `display` key). Any label
# not covered defaults to the label uppercased at use-time.
LABEL_NAMES: dict[str, str] = APP_CONFIG.label_names

SORTABLE_COLS = {
    "title", "company", "location", "salary_min",
    "status", "applied_at", "posted_date", "first_seen",
}
DEFAULT_SORT          = "first_seen"
DEFAULT_DIR           = "desc"
DEFAULT_VIEW          = "grouped"
DEFAULT_STATUS_FILTER = "active"

STATUSES = [
    "new", "skipped", "autoskipped", "reviewing", "deferred",
    "applied", "rejected", "ghosted", "interviewing", "offered",
    "withdrawn", "closed",
]

# Status buckets that drive applied_at when status changes, kept in one place so the per-job
# and bulk status routes (and the manual-add stamp) all agree. "Early" statuses mean no
# application has been submitted, so moving into one CLEARS applied_at. The "applied family"
# statuses all imply an application went out; moving into any of them STAMPS applied_at with
# now() — but only when it's still empty, so a real application date is never overwritten by a
# later toggle (e.g. applied→interviewing keeps the original date). This fills the gap where a
# job that reaches the family by a path that never stamped (e.g. new→interviewing directly, or
# an applied step after the date was cleared) would otherwise sit at applied_at=NULL forever.
_EARLY_STATUSES = ("new", "reviewing", "deferred", "skipped", "autoskipped")
_APPLIED_FAMILY = ("applied", "rejected", "ghosted", "interviewing", "offered", "withdrawn")


def _sql_str_list(values: "tuple[str, ...]") -> str:
    """Render our own status-bucket tuples as a SQL `('a', 'b')` literal list for an IN clause.
    Only ever called on the module constants above (never user input), so plain quoting is safe."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# Shared applied_at recomputation for a status change, parameterized by the NEW status (the two
# `?` are both the new status). Order: clear for early statuses; else stamp now() when entering
# the applied family with no date yet; else keep the existing date. Both status routes embed it.
_APPLIED_AT_CASE_SQL = (
    "CASE "
    f"WHEN ? IN {_sql_str_list(_EARLY_STATUSES)} THEN NULL "
    f"WHEN applied_at IS NULL AND ? IN {_sql_str_list(_APPLIED_FAMILY)} THEN CURRENT_TIMESTAMP "
    "ELSE applied_at END"
)

# Manual work-arrangement override options (self-explanatory phrasings sent verbatim to
# the viability scorer — see viability._work_arrangement). The preview panel serves these
# to its dropdown via /job/<id>; the endpoint validates against this exact set.
# GEO_UNSUPPORTED_ARRANGEMENT is the odd one out: it isn't a work-style the scorer reasons
# about but a deterministic "POOR geography" flag (see viability.is_manual_geo_poor) for
# remote roles restricted to states the candidate can't be in — kept in this list so it
# rides the same dropdown + validation path.
WORK_ARRANGEMENTS = ["On-site", "Hybrid", "Fully remote", "Remote (hybrid if near an office)",
                     GEO_UNSUPPORTED_ARRANGEMENT]

STATUS_COLORS = {
    # 'new' gets the brighter cyan (info) so "jobs I still need to look at" stand out most;
    # 'applied' takes the calmer blue (primary). Swapped deliberately — see below.
    "new":          "info",
    "reviewing":    "secondary",
    # "deferred" is parked-but-alive; a custom purple accent keeps it visually distinct
    # from the grey "reviewing"/"withdrawn"/"ghosted". status_color is only used for the
    # status-select left-border accent (see applySelectColor in jobs.html), never as a
    # Bootstrap text-bg-* badge, so a non-Bootstrap token is safe here.
    "deferred":     "deferred",
    "applied":      "primary",
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
    "manual":     "Manual",
}

# Bootstrap badge classes for the Source column, one distinct color per source.
# Unknown sources fall back to the careersite style.
SOURCE_BADGE_CLASSES = {
    "linkedin":   "bg-primary",
    "careersite": "bg-info text-dark",
    # A distinct purple (custom .source-manual class in base.html): unused by the other source
    # or label badges, and legible in both themes — bg-dark blended into the dark page bg.
    "manual":     "source-manual",
}
SOURCE_BADGE_DEFAULT = "bg-info text-dark"

GROUP_VARIED = "(varied)"  # Displayed whenever a grouped field differs across sub-rows.

VIABILITY_COLORS = {
    "high":   "success",
    "medium": "warning",
    "low":    "danger",
}

# Status predicates reference the per-lens jss.status (every listing query joins job_search_state).
STATUS_FILTERS = {
    "new":       ("New",       "jss.status = 'new'"),
    "reviewing": ("Reviewing", "jss.status = 'reviewing'"),
    # Parked-but-alive: on the radar to mention to recruiters, but not being acted on.
    # Deliberately NOT in the Active exclusion list below, so it also shows in Active.
    "deferred":  ("Deferred",  "jss.status = 'deferred'"),
    "active":    ("Active",    "jss.status NOT IN ('skipped', 'autoskipped', 'rejected', 'withdrawn', 'ghosted', 'closed')"),
    "applied":   ("Applied",   "jss.status IN ('applied', 'interviewing', 'offered', 'ghosted')"),
    "interview": ("Interview Process", "jss.status IN ('interviewing', 'offered')"),
    # Ghosted counts as a presumptive rejection (applied, no response, auto-aged out), so
    # it's included here alongside explicit rejections. It also still shows under Applied.
    "rejected":  ("Rejected",  "jss.status IN ('rejected', 'ghosted')"),
    # Manually skipped only (NOT 'autoskipped', which is the low-viability auto-decision) —
    # the point is to revisit *my own* skip decisions, e.g. pair with the High viability
    # filter to reconsider a strong role I passed on. Autoskipped jobs are always low
    # viability, so folding them in would only add noise to that pairing.
    "skipped":   ("Skipped",   "jss.status = 'skipped'"),
    "all":       ("All",       None),
}

# ── Weekly job-hunt-contact report ───────────────────────────────────────────
# Status transitions that represent an actual *contact* between the candidate and an
# employer — me initiating (withdrawing), the employer advancing the process, or the
# employer actively ending it. The application itself is a contact too, sourced from
# applied_at (not history) so it also covers jobs migrated before history existed.
# 'ghosted' is deliberately excluded: it's auto-inferred silence (the *absence* of a
# response), not a contact event. Backs the report VA unemployment can request.
CONTACT_STATUSES = {"interviewing", "offered", "rejected", "withdrawn"}
# Status-history note prefixes marking a *propagated* transition — a matched-group
# duplicate adopting the group's already-decided status when it's linked or re-ingested,
# or a migration backfill — not a fresh contact. Excluded from the report so one
# withdrawal/rejection isn't recounted once per posting in the group (each propagation
# lands at its own later timestamp, so the (ts, status) de-dup alone won't catch them).
_PROPAGATED_STATUS_NOTES = ("inherited from canonical", "reconstructed from canonical")
CONTACT_LABELS = {
    "applied":      "Applied",
    "interviewing": "Interviewing",
    "offered":      "Offer",
    "rejected":     "Rejected",
    "withdrawn":    "Withdrew",
}
# The report buckets contacts into Sun→Sat calendar weeks in the server's *local*
# timezone (where the candidate lives), so evening activity isn't misfiled into the
# next day's UTC date.

# Effective salary range. When a manual override (salary_*_actual) is set, that pair
# IS the salary — a blank bound is open-ended (e.g. "$175k+"). Coalescing each bound
# independently with the feed would mix an overridden min with a stale feed max (e.g.
# "$175k – $120k"), so resolve the override as an all-or-nothing pair. The override is
# per-lens (jss.salary_*_actual); the feed bounds are shared (jobs.salary_*). Every query
# using these joins job_search_state as `jss` (see _jss_join).
_SAL_OVERRIDDEN = "(jss.salary_min_actual IS NOT NULL OR jss.salary_max_actual IS NOT NULL)"
EFF_SALARY_MIN = f"(CASE WHEN {_SAL_OVERRIDDEN} THEN jss.salary_min_actual ELSE salary_min END)"
EFF_SALARY_MAX = f"(CASE WHEN {_SAL_OVERRIDDEN} THEN jss.salary_max_actual ELSE salary_max END)"


def effective_salary(row: dict) -> tuple[int | None, int | None]:
    """(min, max) after applying any manual override (the Python counterpart of
    EFF_SALARY_MIN/MAX). The override pair wins whole when either bound is set."""
    if row.get("salary_min_actual") is not None or row.get("salary_max_actual") is not None:
        return row.get("salary_min_actual"), row.get("salary_max_actual")
    return row.get("salary_min"), row.get("salary_max")


def _root_pref(col: str, agg: str = "MIN") -> str:
    """SQL for a grouped-header column that should reflect the canonical *root's* value.

    The header row's identity columns (title, company, status, salary) are used to SORT the
    grouped view, and their display shows the root's value — so sorting must order by the
    canonical root too, not by an arbitrary member picked by MIN(). Within each
    COALESCE(canonical_id, jobs.job_id) group the root is the row with canonical_id IS NULL, so
    MAX(CASE WHEN canonical_id IS NULL THEN col END) isolates its value (NULL for members,
    which the aggregate skips). Falls back to the plain group aggregate when the root row is
    filtered out of the current view (e.g. a closed root under an active member), mirroring
    _root_sub_row's fallback so sort and display stay in step. Applies to salary too — the
    grouped cell shows the root's band (not a synthetic MIN-low/MAX-high envelope across the
    group), so its comp-search range and column sort track that same root band.

    NOT for the date columns: 'first seen'/'first posted' are semantically MIN (the group was
    first seen/posted on its earliest member's date), so those stay plain MIN() in both
    display and sort — see _group_earliest_date."""
    return f"COALESCE(MAX(CASE WHEN canonical_id IS NULL THEN {col} END), {agg}({col}))"


# ── Per-lens state join ──
# Every list/count/employer/stats query joins the current search's job_search_state row as
# `jss`, so status / viability / salary+geo overrides come from the lens rather than the dormant
# jobs.* columns of the same name. The search id is validated against the configured searches
# before it reaches here (see _current_search_id), so inlining it as a literal is injection-safe
# and leaves the existing {where}/params threading untouched.
def _jss_join(search_id: str) -> str:
    return (f"JOIN job_search_state jss ON jss.job_id = jobs.job_id "
            f"AND jss.search_id = '{search_id}'")


# Per-lens columns, aliased to their canonical names and placed FIRST in a SELECT so they
# shadow the dormant jobs.* columns of the same name (sqlite3.Row and dict(row) both take the
# first match). process_job_row/build_grouped_job then read status/viability/salary_*_actual/
# geo_fit exactly as before, now sourced from the lens.
_JSS_COLS = (
    "jss.status AS status, jss.applied_at AS applied_at, jss.viability AS viability, "
    "jss.viability_reason AS viability_reason, jss.viability_factors AS viability_factors, "
    "jss.viability_prompt_hash AS viability_prompt_hash, jss.needs_rescored AS needs_rescored, "
    "jss.salary_min_actual AS salary_min_actual, jss.salary_max_actual AS salary_max_actual, "
    "jss.geo_fit_actual AS geo_fit_actual"
)

_VALID_SEARCH_IDS = {s.id for s in APP_CONFIG.searches}


def _current_search_id() -> str:
    """The search ("lens") the current request operates on: a validated ``search`` param (query
    string, or form body so POST actions carry it), else the default/first search. The result is
    always a configured id, so it's safe to inline into _jss_join. Falls back to the default
    outside a request context (e.g. a unit test calling process_job_row directly)."""
    if has_request_context():
        # Precedence: an explicit ?search= / form field wins; else the sticky lens cookie the
        # index route sets when you switch lenses (so POST actions from the page — which don't
        # carry the param — still target the lens you're viewing). Always validated.
        sid = (request.args.get("search") or request.form.get("search")
               or request.cookies.get("search") or "")
        if sid in _VALID_SEARCH_IDS:
            return sid
    return APP_CONFIG.default_search().id


# Grouped header query — one row per canonical group.
# Jobs linked via canonical_id are grouped together; others are their own group.
GROUPED_HEADERS = f"""
    SELECT COALESCE(canonical_id, jobs.job_id) AS group_key,
           {_root_pref("COALESCE(title_actual, title)")} AS title,
           {_root_pref("COALESCE(company_actual, company)")} AS company_eff,
           COUNT(*)             AS location_count,
           MIN(jobs.first_seen) AS first_seen,
           MIN(posted_date)     AS posted_date,
           {_root_pref(EFF_SALARY_MIN)} AS salary_min,
           {_root_pref(EFF_SALARY_MAX, "MAX")} AS salary_max,
           MIN(salary_currency) AS salary_currency,
           {_root_pref("jss.status")}      AS status,
           MIN(source)          AS source,
           MAX(source)          AS source_max
    FROM jobs {{join}} {{where}}
    GROUP BY COALESCE(canonical_id, jobs.job_id)
    {{order}}
    LIMIT ? OFFSET ?
"""
GROUPED_COUNT = "SELECT COUNT(*) FROM (SELECT 1 FROM jobs {join} {where} GROUP BY COALESCE(canonical_id, jobs.job_id))"
FLAT_COUNT    = "SELECT COUNT(*) FROM jobs {join} {where}"
FLAT_SELECT   = f"SELECT {_JSS_COLS}, jobs.* FROM jobs {{join}} {{where}} {{order}} LIMIT ? OFFSET ?"

# ── Employer grouping ──
# Effective company name: the override (company_actual) wins over the scraped company.
EMPLOYER_EXPR = "COALESCE(company_actual, company)"

# Page of distinct employers, A–Z. Two variants keep the employer set consistent
# with how jobs get assigned to an employer below (see scoped fetches in index()):
#   - flat mode:    one employer per raw posting's effective company.
#   - grouped mode: each canonical group filed under its MIN(effective company),
#                   so a fuzzy group spanning two company names belongs to exactly one.
EMPLOYER_PAGE_FLAT = f"""
    SELECT {EMPLOYER_EXPR} AS employer
    FROM jobs {{join}} {{where}}
    GROUP BY {EMPLOYER_EXPR} COLLATE NOCASE
    ORDER BY employer COLLATE NOCASE {{dir}}
    LIMIT ? OFFSET ?
"""
EMPLOYER_COUNT_FLAT = f"""
    SELECT COUNT(*) FROM (
        SELECT 1 FROM jobs {{join}} {{where}}
        GROUP BY {EMPLOYER_EXPR} COLLATE NOCASE
    )
"""
EMPLOYER_PAGE_GROUPED = f"""
    SELECT employer FROM (
        SELECT MIN({EMPLOYER_EXPR}) AS employer
        FROM jobs {{join}} {{where}}
        GROUP BY COALESCE(canonical_id, jobs.job_id)
    )
    GROUP BY employer COLLATE NOCASE
    ORDER BY employer COLLATE NOCASE {{dir}}
    LIMIT ? OFFSET ?
"""
EMPLOYER_COUNT_GROUPED = f"""
    SELECT COUNT(*) FROM (
        SELECT 1 FROM (
            SELECT MIN({EMPLOYER_EXPR}) AS employer
            FROM jobs {{join}} {{where}}
            GROUP BY COALESCE(canonical_id, jobs.job_id)
        )
        GROUP BY employer COLLATE NOCASE
    )
"""

# Grouped-header fetch scoped to one employer. The employer predicate lives in
# HAVING (on the group's MIN effective company), NOT WHERE — so a fuzzy group
# spanning two companies appears whole under its assigned employer, never split.
GROUPED_HEADERS_EMP = GROUPED_HEADERS.replace(
    "GROUP BY COALESCE(canonical_id, jobs.job_id)\n    {order}",
    "GROUP BY COALESCE(canonical_id, jobs.job_id)\n    {having}\n    {order}",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema/data migrations. Run ONCE at startup (see _init_db), not
    per request, so that opening a connection never takes a write lock — readers
    stay lock-free in WAL even while ingestion/scoring holds the write lock.

    On an already-migrated DB this performs no writes: column guards short-circuit,
    CREATE … IF NOT EXISTS no-ops, and the data fix is gated behind a SELECT.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "regions" in cols and "labels" not in cols:
        conn.execute("ALTER TABLE jobs RENAME COLUMN regions TO labels")
    if "linkedin_url" in cols and "job_url" not in cols:
        conn.execute("ALTER TABLE jobs RENAME COLUMN linkedin_url TO job_url")
    if "source" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'linkedin'")
    state_cols = [row[1] for row in conn.execute("PRAGMA table_info(ingest_state)").fetchall()]
    if state_cols and "last_synced_at" not in state_cols:
        conn.execute("ALTER TABLE ingest_state ADD COLUMN last_synced_at TEXT")
    # One-time data fix: rename 'reviewed' → 'reviewing'. Gated on a read so it
    # doesn't take a write lock on every startup once the data is clean.
    if conn.execute("SELECT 1 FROM jobs WHERE status = 'reviewed' LIMIT 1").fetchone():
        conn.execute("UPDATE jobs SET status = 'reviewing' WHERE status = 'reviewed'")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "refreshed_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN refreshed_at TIMESTAMP")
    if "canonical_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN canonical_id TEXT")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "viability" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability TEXT")
    if "viability_reason" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability_reason TEXT")
    if "viability_prompt_hash" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability_prompt_hash TEXT")
    # The scorer's self-reported factor breakdown (JSON array of {dimension, score, note}); NULL
    # for unscored jobs, older scores, and reject-list denials (no AI call). See viability.parse_factors.
    if "viability_factors" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN viability_factors TEXT")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "applied_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
        conn.execute(
            "UPDATE jobs SET applied_at = first_seen "
            "WHERE status IN ('applied','interviewing','offered','rejected','withdrawn','ghosted') "
            "AND applied_at IS NULL"
        )
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    needs_history_bootstrap = "history" not in cols
    if needs_history_bootstrap:
        conn.execute("ALTER TABLE jobs ADD COLUMN history TEXT NOT NULL DEFAULT '[]'")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "company_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN company_actual TEXT")
    if "title_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN title_actual TEXT")
    if "salary_min_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN salary_min_actual INTEGER")
    if "salary_max_actual" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN salary_max_actual INTEGER")
    if "needs_rescored" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN needs_rescored INTEGER NOT NULL DEFAULT 0")
    if "job_description_formatted" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN job_description_formatted TEXT")
    if "description_hash" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN description_hash TEXT")
    if "company_url" not in cols:
        # Employer's own site (from the feed's linkedin_org_url/domain_derived/
        # organization_url); populated at ingest by ingest.extract_company_url.
        conn.execute("ALTER TABLE jobs ADD COLUMN company_url TEXT")
    if "work_arrangement_actual" not in cols:
        # Manual override of the feed's ai_work_arrangement (e.g. recruiter says a role
        # is hybrid though the posting reads on-site); wins in viability scoring.
        conn.execute("ALTER TABLE jobs ADD COLUMN work_arrangement_actual TEXT")
    if "geo_fit_actual" not in cols:
        # Manual geographic-fit override (see viability.manual_geo_fit): the candidate
        # asserts the location is workable (ACCEPTABLE), skipping the billed location
        # sub-call and sparing the job the POOR→low clamp.
        conn.execute("ALTER TABLE jobs ADD COLUMN geo_fit_actual TEXT")
    if "description_actual" not in cols:
        # Manual paste-in full job description that supersedes a wrong/partial feed one
        # (see viability.effective_description). NULL = no override.
        conn.execute("ALTER TABLE jobs ADD COLUMN description_actual TEXT")
    if "description_truncated" not in cols:
        # Teaser-only careersite feed description (see ingest.feed_description_truncated).
        # Backfilled from stored raw JSON the one time it's added; whichever of app/ingest/
        # rescore migrates first both adds and populates it (needs a commit before the
        # backfill reads the new column).
        conn.execute("ALTER TABLE jobs ADD COLUMN description_truncated INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        backfill_description_truncated(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_description_hash ON jobs(description_hash)"
    )
    # File attachments: one physical file (attachment_id) linked to N jobs.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS job_attachments (
               job_id        TEXT NOT NULL,
               attachment_id TEXT NOT NULL,
               stored_name   TEXT NOT NULL,
               original_name TEXT NOT NULL,
               content_type  TEXT,
               size          INTEGER,
               uploaded_at   TEXT NOT NULL,
               PRIMARY KEY (job_id, attachment_id)
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attach_aid ON job_attachments(attachment_id)")
    # Company hotlist: employers the user is especially interested in. A new/reviewing job
    # from one is highlighted in the table. name_key is the lower-cased effective company
    # name (case-insensitive match); display_name keeps the casing it was starred under.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_hotlist (
               name_key     TEXT PRIMARY KEY,
               display_name TEXT NOT NULL,
               added_at     TEXT NOT NULL
           )"""
    )
    conn.commit()
    if needs_history_bootstrap:
        bootstrap_history(conn)
        conn.commit()
    # Per-lens state table (create + one-time backfill of the dormant per-lens jobs columns
    # into __default__ rows). Shared helper so app/ingest/rescore migrate it identically.
    ensure_job_search_state(conn)
    # One-time single→multi adoption (Path B only; no-op/warn-free for the single __default__
    # search). Writes at startup like the other migrations here — gated + idempotent.
    if APP_CONFIG.is_multi_search:
        adopt_legacy(conn, APP_CONFIG.adopter.id if APP_CONFIG.adopter else None)


def _init_db() -> None:
    """Open a dedicated connection and run migrations once, at startup."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _migrate(conn)
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # WAL lets reads run concurrently with ingestion/scoring writes; busy_timeout
        # retries briefly on write-lock contention. Migrations run once at startup
        # (_init_db), so opening a connection here never takes a write lock.
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


_init_db()  # run migrations once at import, before serving any request


@app.errorhandler(sqlite3.OperationalError)
def handle_db_busy(e: sqlite3.OperationalError):
    """A locked DB means ingestion/scoring is mid-write. Reads stay lock-free in
    WAL, so this only affects writes; surface a clear, retryable message."""
    if "locked" in str(e).lower() or "busy" in str(e).lower():
        return ("The database is busy — an ingestion or scoring run is in progress. "
                "Please try again in a moment.", 503)
    raise e  # other operational errors fall through to the normal 500 handler


@app.teardown_appcontext
def close_db(e=None) -> None:
    """Close the per-request DB connection (opened lazily by get_db) at request end."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_nav_timestamps() -> dict:
    """Expose the latest ingest data/sync timestamps to every template (the navbar
    shows when the data was last fetched vs. last reconciled with Apify)."""
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


def search_token_where(q: str, columns: list[str]) -> tuple[str, list]:
    """Build a free-text search clause: split ``q`` into tokens (quoted phrases stay whole)
    and require every token to substring-match at least one of ``columns`` (AND across
    tokens, OR across columns). Returns ("", []) when the query is empty.

    Tokenizing makes the search whitespace-insensitive — a query built from a displayed,
    whitespace-normalized title still matches its raw stored original even when that has
    irregular spacing (e.g. a double space "AI  (Remote)"), which a single whole-string
    LIKE '%…%' silently fails to find. Shared by the jobs list and the link-picker
    autocomplete so both search identically.
    """
    try:
        tokens = shlex.split(q)      # quoted phrases ("senior tpm") count as one token
    except ValueError:
        tokens = q.split()           # unclosed quote → fall back to a plain split
    tokens = [t for t in tokens if t]
    if not tokens:
        return "", []
    per_token = "(" + " OR ".join(f"{c} LIKE ?" for c in columns) + ")"
    clause    = "(" + " AND ".join(per_token for _ in tokens) + ")"
    params    = [f"%{t}%" for t in tokens for _ in columns]
    return clause, params


def build_where(label: str, status_filter: str, q: str = "", source: str = "",
                viability: str = "", comp_active: bool = False,
                comp_min: int | None = None, comp_max: int | None = None) -> tuple[str, list]:
    """Build the shared ``WHERE`` clause + bound-params for the listing queries.

    Combines all active filters (label, status, free-text search, source, viability,
    exact comp-range) into one parameterized clause reused by the flat, grouped, and
    employer query variants so every view filters identically. Returns ("", []) when
    no filters are active.
    """
    conditions: list[str] = []
    params: list = []
    if comp_active:
        # Exact comp-range match (null-safe via IS) — a strong "missed duplicate"
        # signal: two postings with the identical salary band but slightly different
        # descriptions that fuzzy dedup didn't catch.
        conditions.append(f"{EFF_SALARY_MIN} IS ? AND {EFF_SALARY_MAX} IS ?")
        params.extend([comp_min, comp_max])
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
        conditions.append("jss.viability IS NULL")
    elif viability in VIABILITY_COLORS:
        conditions.append("jss.viability = ?")
        params.append(viability)
    if q:
        # Tokenized (whitespace-insensitive) so a search built from a displayed,
        # whitespace-normalized title still finds its raw stored original.
        q_clause, q_params = search_token_where(q, ["title", "title_actual", "company", "company_actual"])
        if q_clause:
            conditions.append(q_clause)
            params.extend(q_params)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def format_salary(row: dict) -> str:
    """Compact "$120k – $150k" / "$120k+" / "up to $150k" string for a row's effective
    salary (override-aware), or "" when neither bound is known. Rounds to whole $k."""
    lo, hi = effective_salary(row)
    if lo and hi:
        return f"${lo // 1000}k – ${hi // 1000}k"
    if lo:
        return f"${lo // 1000}k+"
    if hi:
        return f"up to ${hi // 1000}k"
    return ""


# Tags allowed in AI-formatted descriptions. Anything else (script, style, event
# attributes, etc.) is stripped — this is the XSS boundary, since the client injects
# the result via innerHTML.
_DESC_ALLOWED_TAGS = ["p", "br", "strong", "em", "ul", "ol", "li"]


def format_description_html(md: str | None) -> str | None:
    """Render stored AI Markdown to sanitized HTML, or None if unavailable.

    markdown/bleach are imported lazily so users who never enable AI description
    formatting aren't required to have them installed; any import or render error
    falls back to None (the caller then uses the heuristic renderer).
    """
    if not md:
        return None
    try:
        import bleach
        import markdown as md_lib
    except ImportError:
        return None
    try:
        html = md_lib.markdown(md)
        return bleach.clean(html, tags=_DESC_ALLOWED_TAGS, attributes={}, strip=True)
    except Exception:
        return None


def decode_labels(raw: str | None) -> list[str]:
    """Turn the stored labels JSON array (e.g. '["dc","nc"]') into display names via
    LABEL_NAMES, falling back to the uppercased key for any label without a mapping."""
    return [LABEL_NAMES.get(r, r.upper()) for r in json.loads(raw or "[]")]


# A job is "hot" only while it's actionable at a hotlisted employer — once it leaves
# these statuses it reverts to the normal visual state.
HOT_STATUSES = {"new", "reviewing"}


def _company_key(company_actual: object, company: object) -> str:
    """Lower-cased, trimmed *effective* employer name — the hotlist match key."""
    return str(company_actual or company or "").strip().lower()


def get_hotlist(db: sqlite3.Connection) -> set[str]:
    """Set of hotlisted company keys (lower-cased effective names)."""
    return {r["name_key"] for r in db.execute("SELECT name_key FROM company_hotlist")}


def _viability_factors(raw: object) -> "list[dict] | None":
    """Parse a stored ``viability_factors`` JSON string into an ordered list for the preview panel.

    Reuses viability.parse_factors for defensive normalization, then orders the four fixed
    FACTOR_DIMENSIONS first (in their canonical order) followed by any extra model-surfaced axes in
    the order the model reported them — so the panel always shows the comparable dimensions first.
    Returns None when there's nothing to show (unscored, reject-listed, an older score, or malformed
    JSON) so the panel simply omits the breakdown section."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    factors = parse_factors(data)
    if not factors:
        return None
    order = {dim: i for i, dim in enumerate(FACTOR_DIMENSIONS)}
    # sorted() is stable: fixed dims sort into canonical order by their index; every extra maps to
    # the same trailing key, so extras keep the model's reported order, placed after the fixed set.
    return sorted(factors, key=lambda f: order.get(f["dimension"], len(order)))


def process_job_row(row: sqlite3.Row | dict, hotlist: "set[str] | frozenset" = frozenset()) -> dict:
    """Decorate a raw jobs row with everything the templates need for display.

    Adds decoded labels, effective + feed salary (with display strings and an override
    flag), status/source/viability colors, a "stale score" flag, a trimmed applied_at
    date, and the parsed locations list (for the "+N" tooltip). Used both for flat rows
    and as the per-sub-row builder inside grouped views.
    """
    j = dict(row)
    j["labels"]           = decode_labels(j.get("labels"))
    # Effective title: a manual override (title_actual) wins over the scraped title. Keep the
    # original under title_original so the UI can show "originally listed as", and expose it as
    # `title` directly so every display path (flat/grouped/preview) uses the effective value.
    j["title_original"]      = j.get("title")
    j["title_actual"]        = j.get("title_actual")
    j["has_title_override"]  = bool(j.get("title_actual"))
    j["title"]               = j.get("title_actual") or j.get("title")
    j["company_actual"]   = j.get("company_actual")
    j["company_display"]  = j.get("company_actual") or j.get("company") or ""
    j["status_color"]     = STATUS_COLORS.get(j.get("status", "new"), "secondary")
    # Hot = actionable (new/reviewing) job at a hotlisted employer → row tint.
    j["is_hot"] = (j.get("status") in HOT_STATUSES
                   and _company_key(j.get("company_actual"), j.get("company")) in hotlist)
    # Effective salary: manual override wins over the feed. Preserve the feed values
    # (salary_*_feed) so the UI can show "originally listed as" on an override.
    j["salary_min_feed"]  = j.get("salary_min")
    j["salary_max_feed"]  = j.get("salary_max")
    j["has_salary_override"] = (
        j.get("salary_min_actual") is not None or j.get("salary_max_actual") is not None
    )
    j["salary_feed_display"] = format_salary(
        {"salary_min": j.get("salary_min"), "salary_max": j.get("salary_max")}
    )
    j["salary_display"]   = format_salary(j)
    j["salary_min"], j["salary_max"] = effective_salary(j)
    j["source_display"]   = SOURCE_NAMES.get(j.get("source", "linkedin"), j.get("source", ""))
    # Effective description: a manual paste-in override (description_actual) wins over the feed
    # text, and it clears the "partial feed" state for badge/auto-skip purposes (we now have the
    # full posting). Compute the effective-truncated flag BEFORE overwriting job_description —
    # description_is_truncated keys off the flag + the override, not the text itself.
    j["has_description_override"] = has_description_override(j)
    j["description_original"]     = j.get("job_description")
    j["description_truncated"]    = 1 if description_is_truncated(j) else 0
    j["job_description"]          = effective_description(j)
    j["applied_at"]       = (j.get("applied_at") or "")[:10] or None
    j["viability_color"]  = VIABILITY_COLORS.get(j.get("viability") or "", "")
    _cur_hash = _current_viability_hash()
    # A score is "stale" if the prompt changed under it (hash mismatch) OR a
    # viability-relevant field was edited since (needs_rescored). Either way the
    # badge is subdued until the next rescore catches it.
    j["viability_stale"]  = bool(
        j.get("viability") is not None
        and (
            bool(j.get("needs_rescored"))
            or (_cur_hash is not None and j.get("viability_prompt_hash") != _cur_hash)
        )
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


def _norm_ws(val: object) -> object:
    """Collapse internal whitespace runs to a single space and strip ends.

    Used when comparing grouped fields (e.g. titles from different sources) so
    cosmetic whitespace differences — like a stray double space — don't make an
    otherwise-identical field render as '(varied)'. Non-string values pass through.
    """
    if not isinstance(val, str):
        return val
    return " ".join(val.split())


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


def _group_earliest_date(sub_rows: list[dict], key: str) -> "str | None":
    """Earliest date across the group for a 'first seen'/'first posted' column (as YYYY-MM-DD).

    Unlike other collapsed fields, dates have a meaningful group aggregate: the group was
    first seen / first posted on its EARLIEST member's date, so we show that MIN rather than
    an opaque '(varied)' when members differ. Dates are stored as ISO strings (date or
    timestamp), so a lexical MIN over the YYYY-MM-DD prefix is chronological. Ignores missing
    values; None (rendered '—') when the whole group lacks the date. Matches the SQL MIN() the
    grouped view sorts these columns by, so display and sort agree."""
    dates = [(s.get(key) or "")[:10] for s in sub_rows]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _root_sub_row(sub_rows: list[dict], group_key) -> "dict | None":
    """The canonical root's processed sub-row (job_id == group_key), or the first sub-row
    when the root is filtered out of the current view.

    This is the posting a grouped header speaks for: its title/salary/etc. stand in for the
    whole group, so a fuzzy group whose members carry slightly different values ('- AI' vs
    '-Ai/ML' titles, or feed-noise salary differences) shows one real, identifiable value
    instead of an opaque '(varied)' — the template appends a small '(varies)' note when the
    members disagree (see the *_varies flags in build_grouped_job). Falling back to the first
    sub-row keeps it non-null when the root itself doesn't pass the active filter."""
    return next((s for s in sub_rows if s.get("job_id") == group_key),
                sub_rows[0] if sub_rows else None)


def group_member_ids(db: sqlite3.Connection, job_id: str) -> list[str]:
    """All job_ids in this job's current fuzzy-match group (canonical root + members).

    Used to fan out notes and attachments across the group: writes propagate to
    every current member so each keeps its own copy if the group is later split.
    Returns just [job_id] if the job doesn't exist.
    """
    row = db.execute("SELECT canonical_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return [job_id]
    root = row["canonical_id"] or job_id
    rows = db.execute(
        "SELECT job_id FROM jobs WHERE canonical_id = ? OR (canonical_id IS NULL AND job_id = ?)",
        (root, root),
    ).fetchall()
    return [r["job_id"] for r in rows] or [job_id]


def fetch_sub_rows(db: sqlite3.Connection, group_key: str,
                   where: str, params: list,
                   hotlist: "set[str] | frozenset" = frozenset()) -> list[dict]:
    """Fetch all jobs belonging to a canonical group.

    A group contains:
      - the canonical job itself (canonical_id IS NULL AND job_id = group_key)
      - all fuzzy duplicates that point at it (canonical_id = group_key)
    """
    and_clause = f"{where} AND " if where else "WHERE "
    join = _jss_join(_current_search_id())
    rows = db.execute(
        f"SELECT {_JSS_COLS}, jobs.* FROM jobs {join} {and_clause}"
        "(canonical_id = ? OR (canonical_id IS NULL AND jobs.job_id = ?)) ORDER BY location",
        params + [group_key, group_key],
    ).fetchall()
    return [process_job_row(r, hotlist) for r in rows]


def build_grouped_job(header: sqlite3.Row, sub_rows: list[dict]) -> dict:
    """Assemble the display dict for one grouped (matched-jobs) header row.

    Combines the aggregate header with its processed sub-rows: a single title/company
    (or '(varied)' when sub-rows disagree), collapsed status/viability/salary/labels/
    applied/posted (each the shared value or '(varied)'), override flags, and the
    sub_rows for the expandable detail. A single-job "group" is flattened to behave like
    an ordinary flat row.
    """
    h             = dict(header)
    # is_fuzzy_group: more than one job in this canonical group (canonical + ≥1 duplicate)
    is_fuzzy_group = h["location_count"] > 1
    multi          = is_fuzzy_group
    # Title/salary may vary within a fuzzy group (different titles/salaries from different
    # sources). Rather than collapse a genuine variation to an unhelpful '(varied)' (which
    # hides *what* the group is / *what* it pays), show the canonical root's concrete value
    # and set a *_varies flag so the template appends a small '(varies)'. Compare
    # whitespace-normalized titles (and rounded salary_display strings) so cosmetic feed
    # noise doesn't count as a real variation. Company keeps the plain '(varied)' behaviour.
    root_row     = _root_sub_row(sub_rows, h["group_key"])
    title_norms  = {_norm_ws(s.get("title")) for s in sub_rows}
    title_varies = len(title_norms) > 1
    title        = root_row.get("title") if root_row else None
    salary_displays = {s.get("salary_display") for s in sub_rows}
    salary_varies   = len(salary_displays) > 1
    company = _group_field(sub_rows, "company_display", fmt=_norm_ws)
    sub_statuses      = [s.get("status", "new") for s in sub_rows]
    unique_statuses   = set(sub_statuses)
    unique_viabilities = {s.get("viability") for s in sub_rows}
    # All distinct reasons per level (in member order, deduped), so a level's badge tooltip
    # shows EVERY posting's reasoning at that level — not just one representative.
    _reasons_by_level: dict[str, list[str]] = {}
    for s in sub_rows:
        v, r = s.get("viability"), s.get("viability_reason")
        if v and r:
            lst = _reasons_by_level.setdefault(v, [])
            if r not in lst:
                lst.append(r)
    # Viability/labels/source are set-like "tags": a mixed group shows the UNION of the
    # distinct values (one badge each) rather than an opaque '(varied)'. Viability badges run
    # best→worst; each carries its colour, whether any posting at that level is stale (dimmed),
    # and every reason at that level (bulleted when more than one) as a tooltip.
    def _level_badge(lv: str) -> dict:
        stale   = any(s.get("viability_stale") for s in sub_rows if s.get("viability") == lv)
        reasons = _reasons_by_level.get(lv, [])
        if len(reasons) == 1:
            body = f"{lv}: {reasons[0]}"
        elif reasons:
            body = f"{lv}:\n" + "\n".join(f"• {r}" for r in reasons)
        else:
            body = ""
        title = ("Stale — re-run rescore_viability.sh\n\n" if stale else "") + body
        return {"level": lv, "color": VIABILITY_COLORS.get(lv, ""), "stale": stale, "title": title}
    group_viabilities = [_level_badge(lv) for lv in ("high", "medium", "low")
                         if lv in unique_viabilities]
    # Scalar hint for the status-select's data-viability: the level when the group agrees,
    # else '' (a mixed group has no single level to hint).
    group_viability = group_viabilities[0]["level"] if len(group_viabilities) == 1 else ""
    # Union of the distinct sources present, one badge each (display name + badge class).
    group_sources = [
        {"value": src, "display": SOURCE_NAMES.get(src, src),
         "badge": SOURCE_BADGE_CLASSES.get(src, SOURCE_BADGE_DEFAULT)}
        for src in sorted({s.get("source") for s in sub_rows if s.get("source")})
    ]

    job = {
        "title":            title,
        "title_varies":     title_varies,
        "salary_varies":    salary_varies,
        "company":          company,
        "company_display":  company,
        # Group-level employer site: first sub-row that has one (members of a real
        # employer group share it; a mixed fuzzy group just links whichever resolves).
        "company_url":      next((s.get("company_url") for s in sub_rows if s.get("company_url")), None),
        # Group is hot if any member is a new/reviewing job at a hotlisted employer.
        "is_hot":           any(s.get("is_hot") for s in sub_rows),
        "location_count":   h["location_count"],
        # Group's comp band is the canonical root's (see _root_pref) — feeds the salary-cell
        # comp-search icon and the salary column sort, matching the displayed group_salary.
        "salary_min":       h.get("salary_min"),
        "salary_max":       h.get("salary_max"),
        "multi":            multi,
        "is_fuzzy_group":   is_fuzzy_group,
        "sub_rows":         sub_rows,
        "preview_job_id":   sub_rows[0]["job_id"] if sub_rows else None,
        "group_status":     next(iter(unique_statuses)) if len(unique_statuses) == 1 else None,
        "group_sources":    group_sources,
        "sub_job_ids":      [s["job_id"] for s in sub_rows if s.get("job_id")],
        "has_company_override":    any(s.get("company_actual") for s in sub_rows),
        "has_title_override":      any(s.get("has_title_override") for s in sub_rows),
        "has_salary_override":     any(s.get("has_salary_override") for s in sub_rows),
        "group_viability":   group_viability,
        "group_viabilities": group_viabilities,
        "group_applied":    _group_field(sub_rows, "applied_at",
                                         fmt=lambda v: (v or "")[:10]),
        # Root's salary string (not '(varied)'): the canonical posting's band, with the
        # template flagging '(varies)' when members differ — see salary_varies.
        "group_salary":     root_row.get("salary_display") if root_row else None,
        # Union of every label across the group (sorted), rendered as one badge each.
        "group_labels":     sorted({lbl for s in sub_rows for lbl in (s.get("labels") or [])}),
        # Dates use the group's earliest member (MIN), the meaningful "first seen/posted"
        # value, rather than '(varied)' — matches the SQL MIN() these columns sort by.
        "group_posted":     _group_earliest_date(sub_rows, "posted_date"),
        "group_first_seen": _group_earliest_date(sub_rows, "first_seen"),
    }
    if not multi and sub_rows:
        s = sub_rows[0]
        job.update({
            "job_id":           s.get("job_id"),
            "job_url":          s.get("job_url"),
            # The scraped title, for the "originally: …" tooltip on the override "*". The
            # single-location row template reads job.title_original (same as a flat row);
            # without this it renders an empty "originally:" for an overridden single job.
            "title_original":   s.get("title_original"),
            "location_primary": s.get("location") or "—",
            "locations_all":    s.get("locations_all", ""),
            "locations_count":  s.get("locations_count", 1),
            "refreshed_at":     s.get("refreshed_at"),
            "salary_display":   s["salary_display"],
            "salary_min":       s.get("salary_min"),
            "salary_max":       s.get("salary_max"),
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
            "description_truncated": s.get("description_truncated", 0),
            "company":          s.get("company", ""),
            "company_actual":   s.get("company_actual"),
            "company_display":  s.get("company_display", ""),
            "company_url":      s.get("company_url"),
            "salary_min_feed":  s.get("salary_min_feed"),
            "salary_max_feed":  s.get("salary_max_feed"),
            "salary_feed_display": s.get("salary_feed_display", ""),
            "has_salary_override": s.get("has_salary_override", False),
        })
    return job


def employer_having(employer: str | None) -> tuple[str, list]:
    """HAVING clause + params filing a canonical group under one employer.

    A group is assigned to MIN(effective company), so a fuzzy group spanning two
    company names lands under exactly one employer (never split or double-counted).
    """
    if employer is None:
        return f"HAVING MIN({EMPLOYER_EXPR}) IS NULL", []
    return f"HAVING MIN({EMPLOYER_EXPR}) = ? COLLATE NOCASE", [employer]


def employer_where(where: str, params: list, employer: str | None) -> tuple[str, list]:
    """Append an effective-company predicate to a WHERE clause (flat fetch)."""
    clause = f"{where} AND " if where else "WHERE "
    if employer is None:
        return f"{clause}{EMPLOYER_EXPR} IS NULL", list(params)
    return f"{clause}{EMPLOYER_EXPR} = ? COLLATE NOCASE", list(params) + [employer]


def available_labels(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT DISTINCT je.value FROM jobs, json_each(jobs.labels) je ORDER BY je.value"
    ).fetchall()
    return [
        {"value": r["value"], "label": LABEL_NAMES.get(r["value"], r["value"].upper())}
        for r in rows
    ]


def has_viability_scores(db: sqlite3.Connection) -> bool:
    """Return True if any jobs have been scored for viability in the current lens."""
    return db.execute(
        f"SELECT COUNT(*) FROM jobs {_jss_join(_current_search_id())} WHERE jss.viability IS NOT NULL"
    ).fetchone()[0] > 0


def available_sources(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT DISTINCT source FROM jobs ORDER BY source").fetchall()
    return [
        {"value": r["source"], "label": SOURCE_NAMES.get(r["source"], r["source"])}
        for r in rows
    ]


def sort_url(col: str, current_sort: str, current_dir: str,
             label: str, group_match: bool, group_employer: bool,
             status_filter: str, q: str = "",
             source: str = "", viability: str = "", emp_dir: str = "asc",
             return_to: str = "", comp: str = "", per_page: str = "25") -> str:
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
                   label=label or None,
                   group_match="1" if group_match else "0",
                   group_employer="1" if group_employer else "0",
                   emp_dir=emp_dir if emp_dir != "asc" else None,
                   source=source or None, viability=viability or None,
                   status_filter=status_filter, q=q or None,
                   return_to=return_to or None, comp=comp or None,
                   per_page=per_page if per_page != str(PER_PAGE) else None, page=1)


@app.route("/")
def index():
    """The main jobs table. Reads all view state from query params (filters, sort,
    paging, the two grouping axes, free-text/comp search, and the search-all return
    target), builds the shared WHERE + ORDER, then renders jobs.html via one of three
    paths: flat rows, matched-jobs grouping (per canonical group), or employer grouping
    (sections keyed on effective company, each containing flat or grouped rows). All
    branches reuse build_where/process_job_row/build_grouped_job so they stay consistent.
    """
    db = get_db()
    hotlist = get_hotlist(db)  # employer keys to highlight new/reviewing jobs for

    label         = request.args.get("label", "")
    q             = request.args.get("q", "").strip()
    sort          = request.args.get("sort", DEFAULT_SORT)
    direction     = request.args.get("dir", DEFAULT_DIR)
    status_filter = request.args.get("status_filter", DEFAULT_STATUS_FILTER)
    source        = request.args.get("source", "")
    viability     = request.args.get("viability", "")
    page          = max(1, request.args.get("page", 1, type=int))

    # Two independent grouping axes (replacing the old single `view` toggle):
    #   group_match    — fuzzy/canonical "matched jobs" grouping (old view=grouped)
    #   group_employer — outer grouping by effective company name
    # Backward-compat: honour an old ?view=grouped|flat link when group_match is absent.
    legacy_view = request.args.get("view")
    match_default = "0" if legacy_view == "flat" else "1"
    group_match    = request.args.get("group_match", match_default) == "1"
    group_employer = request.args.get("group_employer", "0") == "1"
    # `view` still drives ~6 column-layout conditionals in the template.
    view = "grouped" if group_match else "flat"
    # Employer-section order direction (group-by-employer only). Independent of the
    # within-section column sort, so reversing employers doesn't reset the row sort.
    emp_dir = request.args.get("emp_dir", "asc")
    if emp_dir not in ("asc", "desc"):
        emp_dir = "asc"
    # "Search all" stashes the prior filtered view here so the search ✕ can restore
    # it. Only accept a local path (no open-redirect via //host or backslashes).
    return_to = request.args.get("return_to", "")
    if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to:
        return_to = ""
    # Exact comp-range search (from the salary-cell icon): comp = "min-max", either
    # bound optional. Matches the full salary signature, including a null bound.
    comp = request.args.get("comp", "")
    comp_min = comp_max = None
    comp_active = False
    if comp and "-" in comp:
        _lo, _, _hi = comp.partition("-")
        try:
            comp_min = int(_lo) if _lo else None
            comp_max = int(_hi) if _hi else None
            comp_active = comp_min is not None or comp_max is not None
        except ValueError:
            comp_active = False
    if not comp_active:
        comp = ""
    comp_display = format_salary({"salary_min": comp_min, "salary_max": comp_max}) if comp_active else ""
    # Page size: 25/50/100/200, or "all" (single page, no LIMIT).
    per_page = request.args.get("per_page", str(PER_PAGE))
    if per_page not in PER_PAGE_OPTIONS:
        per_page = str(PER_PAGE)

    if sort not in SORTABLE_COLS:
        sort = DEFAULT_SORT
    if direction not in ("asc", "desc"):
        direction = DEFAULT_DIR
    if status_filter not in STATUS_FILTERS:
        status_filter = DEFAULT_STATUS_FILTER
    if source not in SOURCE_NAMES and source != "":
        source = ""
    if viability not in ("high", "medium", "low", "unscored", ""):
        viability = ""
    if view == "grouped" and sort == "location":
        sort = DEFAULT_SORT

    where, params = build_where(label, status_filter, q, source, viability,
                                comp_active, comp_min, comp_max)
    # Per-lens state join for this request's search (Phase 1: the single/default search).
    join = _jss_join(_current_search_id())
    _TEXT_COLS = {"title", "company", "location"}
    if sort == "status":
        # Grouped view sorts by the header's status aggregate (alias); flat by the joined
        # per-lens status. Bare `status` is ambiguous in flat (dormant jobs.status + jss alias).
        sort_expr = ("status COLLATE NOCASE" if view == "grouped" else "jss.status COLLATE NOCASE")
    elif sort == "company":
        # Sort by the *effective* (override-aware) company so the order matches what
        # the table shows. A company_actual override otherwise sorts by the hidden
        # original name. Grouped queries expose it as the company_eff aggregate;
        # flat rows compute it inline.
        sort_expr = ("company_eff COLLATE NOCASE" if view == "grouped"
                     else "COALESCE(company_actual, company) COLLATE NOCASE")
    elif sort == "title":
        # Effective (override-aware) title, matching what the table shows. Grouped exposes
        # it via the `title` aggregate (already COALESCE'd); flat rows compute it inline.
        sort_expr = ("title COLLATE NOCASE" if view == "grouped"
                     else "COALESCE(title_actual, title) COLLATE NOCASE")
    elif sort == "salary_min":
        # Sort by effective (override-aware) salary. The grouped query already
        # aliases salary_min to the effective aggregate; flat rows compute it inline.
        sort_expr = ("salary_min" if view == "grouped" else EFF_SALARY_MIN)
    elif sort == "first_seen":
        # first_seen is on both jobs and jss now — grouped exposes the MIN(jobs.first_seen)
        # alias; flat qualifies to the shared jobs column.
        sort_expr = ("first_seen" if view == "grouped" else "jobs.first_seen")
    elif sort == "applied_at":
        # Per-lens (jss). Qualified so it's unambiguous; in grouped it's a lenient pick.
        sort_expr = "jss.applied_at"
    elif sort in _TEXT_COLS:
        sort_expr = f"{sort} COLLATE NOCASE"
    else:
        sort_expr = sort
    # For applied_at, NULLs sort first when descending so unapplied jobs
    # bubble to the top — making it easy to find jobs still needing action.
    # All other columns keep NULLs last in both directions.
    _NULLS_FIRST_DESC = {"applied_at"}
    nulls = "NULLS FIRST" if (sort in _NULLS_FIRST_DESC and direction == "desc") else "NULLS LAST"
    order  = f"ORDER BY {sort_expr} {direction.upper()} {nulls}"
    # "all" → single page, no LIMIT (SQLite treats LIMIT -1 as unlimited).
    if per_page == "all":
        page, limit, offset = 1, -1, 0
    else:
        limit  = int(per_page)
        offset = (page - 1) * limit

    employer_groups = None
    jobs = None

    if group_employer:
        # Paginate per employer; each employer is shown whole. The Company column
        # header re-orders the employer sections (emp_dir, asc/desc) independently
        # of the within-section column sort, which orders jobs inside each employer.
        if group_match:
            page_sql, count_sql = EMPLOYER_PAGE_GROUPED, EMPLOYER_COUNT_GROUPED
        else:
            page_sql, count_sql = EMPLOYER_PAGE_FLAT, EMPLOYER_COUNT_FLAT
        total    = db.execute(count_sql.format(join=join, where=where), params).fetchone()[0]
        emp_rows = db.execute(page_sql.format(join=join, where=where, dir=emp_dir.upper()),
                              params + [limit, offset]).fetchall()
        employer_groups = []
        for er in emp_rows:
            employer = er["employer"]
            if group_match:
                having, hparams = employer_having(employer)
                headers = db.execute(
                    GROUPED_HEADERS_EMP.format(join=join, where=where, having=having, order=order),
                    params + hparams + [-1, 0]).fetchall()
                emp_jobs = [
                    build_grouped_job(h, fetch_sub_rows(db, h["group_key"], where, params, hotlist))
                    for h in headers
                ]
                job_count = sum(j["location_count"] for j in emp_jobs)
            else:
                ewhere, eparams = employer_where(where, params, employer)
                rows = db.execute(FLAT_SELECT.format(join=join, where=ewhere, order=order),
                                  eparams + [-1, 0]).fetchall()
                emp_jobs = [process_job_row(r, hotlist) for r in rows]
                job_count = len(emp_jobs)
            employer_groups.append({
                "employer_name": employer or "(no company)",
                "job_count":     job_count,
                "jobs":          emp_jobs,
            })
    elif group_match:
        total   = db.execute(GROUPED_COUNT.format(join=join, where=where), params).fetchone()[0]
        headers = db.execute(GROUPED_HEADERS.format(join=join, where=where, order=order),
                             params + [limit, offset]).fetchall()
        jobs = [
            build_grouped_job(h, fetch_sub_rows(db, h["group_key"], where, params, hotlist))
            for h in headers
        ]
    else:
        total = db.execute(FLAT_COUNT.format(join=join, where=where), params).fetchone()[0]
        rows  = db.execute(FLAT_SELECT.format(join=join, where=where, order=order),
                           params + [limit, offset]).fetchall()
        jobs  = [process_job_row(r, hotlist) for r in rows]

    total_pages          = 1 if per_page == "all" else max(1, math.ceil(total / limit))
    labels               = available_labels(db)
    sources              = available_sources(db)
    show_viability_filter = has_viability_scores(db)
    col_urls             = {
        col: sort_url(col, sort, direction, label, group_match, group_employer,
                      status_filter, q, source, viability, emp_dir, return_to, comp, per_page)
        for col in SORTABLE_COLS
    }
    # Link on the Company header (employer mode only): flip the employer-section
    # order without disturbing the within-section column sort.
    emp_dir_url = url_for("index", sort=sort, dir=direction, label=label or None,
                          group_match="1" if group_match else "0",
                          group_employer="1" if group_employer else "0",
                          emp_dir="desc" if emp_dir == "asc" else "asc",
                          source=source or None, viability=viability or None,
                          status_filter=status_filter, q=q or None,
                          return_to=return_to or None, comp=comp or None,
                          per_page=per_page if per_page != str(PER_PAGE) else None, page=1)

    resp = make_response(render_template(
        "jobs.html",
        jobs=jobs,
        employer_groups=employer_groups,
        page=page,
        total_pages=total_pages,
        total=total,
        label=label,
        q=q,
        sort=sort,
        direction=direction,
        view=view,
        group_match=group_match,
        group_employer=group_employer,
        emp_dir=emp_dir,
        emp_dir_url=emp_dir_url,
        return_to=return_to,
        comp=comp,
        comp_display=comp_display,
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        status_filter=status_filter,
        status_filters=STATUS_FILTERS,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        source_badges=SOURCE_BADGE_CLASSES,
        source_badge_default=SOURCE_BADGE_DEFAULT,
        labels=labels,
        # All configured labels (not just those already present in the data) for the
        # manual "Add job" form's label checkboxes.
        all_labels=[{"value": k, "label": v}
                    for k, v in sorted(LABEL_NAMES.items(), key=lambda kv: kv[1].lower())],
        sources=sources,
        source=source,
        viability=viability,
        show_viability_filter=show_viability_filter,
        group_varied=GROUP_VARIED,
        col_urls=col_urls,
        # Multi-search lens selector: the configured searches and the active one. A single
        # (default) search means Path A — the selector is hidden in the template.
        searches=[{"id": s.id, "name": s.name} for s in APP_CONFIG.searches],
        current_search=_current_search_id(),
        is_multi_search=APP_CONFIG.is_multi_search,
    ))
    # Sticky lens cookie: POST actions from the page (status/notes/overrides) don't carry the
    # ?search= param, so persist the viewed lens here for _current_search_id to read back.
    resp.set_cookie("search", _current_search_id(), samesite="Lax")
    return resp


def _parse_utc(ts: str | None) -> datetime | None:
    """Parse a stored UTC timestamp into a tz-aware UTC datetime, or None.

    The DB holds two shapes: 'YYYY-MM-DD HH:MM:SS' (SQLite CURRENT_TIMESTAMP and
    first_seen) and ISO 'YYYY-MM-DDTHH:MM:SSZ' (history event `ts`). Both are UTC;
    naive values are assumed UTC.
    """
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)  # space form → ISO so fromisoformat accepts it
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# Statuses that terminate an application, for the "interviewing → outcome" timing. An
# outcome is any of these reached after the interviewing stage began.
_OUTCOME_STATUSES = {"offered", "rejected", "withdrawn", "ghosted"}


def _status_timeline(history: list) -> list[tuple]:
    """(datetime, to_status) for each status-change event in a job's history, ascending in
    time. Events whose ts is missing or unparseable are dropped so they can't distort deltas."""
    tl = []
    for ev in history:
        if ev.get("event") != "status":
            continue
        dt = _parse_utc(ev.get("ts"))
        to = ev.get("to")
        if dt and to:
            tl.append((dt, to))
    tl.sort(key=lambda x: x[0])
    return tl


def _first_reach(timeline: list, statuses: set, after: datetime | None = None) -> datetime | None:
    """Earliest datetime the job entered any of `statuses`, at/after `after` when given."""
    for dt, to in timeline:
        if after is not None and dt < after:
            continue
        if to in statuses:
            return dt
    return None


def transition_time_stats(histories) -> dict:
    """Average elapsed days for key application transitions, from job status histories.

    For each job's status-event timeline, when both endpoints exist and are correctly
    ordered we add its elapsed days to the relevant bucket; each average carries its sample
    size `n` (small samples are common early in a search, so surfacing n matters). A job
    missing an endpoint simply doesn't contribute to that bucket. Pure/self-contained so
    the timing logic is unit-testable without the DB. Transitions:
      - applied → interviewing
      - interviewing → outcome (first offered/rejected/withdrawn/ghosted after it)
      - applied → rejected
    """
    buckets: dict[str, list] = {
        "applied_to_interviewing": [],
        "interviewing_to_outcome": [],
        "applied_to_rejected": [],
    }
    for history in histories:
        tl = _status_timeline(history)
        if not tl:
            continue
        applied      = _first_reach(tl, {"applied"})
        interviewing = _first_reach(tl, {"interviewing"})
        rejected     = _first_reach(tl, {"rejected"})
        if applied and interviewing and interviewing >= applied:
            buckets["applied_to_interviewing"].append((interviewing - applied).total_seconds() / 86400)
        if applied and rejected and rejected >= applied:
            buckets["applied_to_rejected"].append((rejected - applied).total_seconds() / 86400)
        if interviewing:
            outcome = _first_reach(tl, _OUTCOME_STATUSES, after=interviewing)
            if outcome:
                buckets["interviewing_to_outcome"].append((outcome - interviewing).total_seconds() / 86400)

    def _summary(vals: list) -> dict:
        return {"avg_days": round(sum(vals) / len(vals), 1) if vals else None, "n": len(vals)}

    return {k: _summary(v) for k, v in buckets.items()}


def _local_naive(dt: datetime) -> datetime:
    """Convert an aware UTC datetime to the server's local wall-clock time, naive.

    astimezone() with no argument applies the OS local zone with the correct DST
    offset for that specific instant, so per-instant bucketing stays accurate across
    spring/fall transitions; we drop tzinfo to compare against naive week bounds.
    """
    return dt.astimezone().replace(tzinfo=None)


def _week_bounds(anchor: datetime) -> tuple[datetime, datetime]:
    """Return [start, end) naive-local datetimes for the Sun→Sat week containing
    `anchor` (a naive-local datetime). End is the next Sunday 00:00 (exclusive)."""
    # weekday(): Mon=0..Sun=6 → days since the most recent Sunday.
    days_since_sun = (anchor.weekday() + 1) % 7
    start = datetime(anchor.year, anchor.month, anchor.day) - timedelta(days=days_since_sun)
    return start, start + timedelta(days=7)


@app.route("/report/weekly")
def report_weekly():
    """Printable weekly job-hunt-contact report (Sun→Sat, local time).

    Lists every role with a *contact* in the selected week — an application I
    submitted (applied_at) or a status change I/the employer made (CONTACT_STATUSES) —
    grouped by employer, with the application URL, applied date/time, and each in-week
    contact's date/time. Intended to evidence job-search activity for VA unemployment.
    `?week=YYYY-MM-DD` selects any day in the target week; default is the current week.
    """
    db = get_db()

    # Resolve the requested week from a date param (any day in the week); fall back to
    # today (local) on absence or a malformed value.
    raw_week = request.args.get("week", "").strip()
    try:
        anchor = datetime.fromisoformat(raw_week) if raw_week else datetime.now()
    except ValueError:
        anchor = datetime.now()
    start, end = _week_bounds(anchor)

    # Narrow to jobs that could possibly contribute: anything applied-to, or with a
    # status change in its history. The week filter happens in Python (timestamps need
    # local-tz conversion). status/applied_at fan out across a matched group, so we
    # collapse to one entry per canonical group below.
    # Union per-lens state across ALL searches (the report is search-agnostic — you contacted
    # the employer regardless of which lens surfaced the job). One row per (job, search) with
    # activity; the (instant, status) de-dup below collapses a contact seen under two lenses.
    rows = db.execute(
        """SELECT jobs.job_id AS job_id, COALESCE(canonical_id, jobs.job_id) AS group_key,
                  canonical_id, title, title_actual, company, company_actual, company_url,
                  job_url, apply_url, jss.applied_at AS applied_at, jss.history AS history
           FROM jobs JOIN job_search_state jss ON jss.job_id = jobs.job_id
           WHERE jss.applied_at IS NOT NULL OR jss.history LIKE '%"event":"status"%'"""
    ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["group_key"], []).append(r)

    employers: dict[str, list[dict]] = {}
    n_apps = n_responses = 0
    for members in groups.values():
        # Representative posting for display: the canonical root if present (status and
        # applied_at fan out identically across members, so any member's timeline works).
        rep = next((m for m in members if m["canonical_id"] is None), members[0])

        # The application contact: applied_at is shared across the group; take any set one.
        applied_dt = next(
            (d for d in (_parse_utc(m["applied_at"]) for m in members) if d), None
        )

        # Union status-change contacts across members, de-duped by (instant, status) so a
        # real change fanned out to every member at one instant is counted once. Skip
        # *propagated* transitions (a duplicate adopting the group's already-decided status
        # on link/ingest, or a migration backfill): those carry their own later timestamp,
        # so they'd otherwise show as extra contacts the canonical's own log never recorded.
        contacts: dict[tuple[str, str], datetime] = {}
        for m in members:
            for ev in json.loads(m["history"] or "[]"):
                if ev.get("event") != "status" or ev.get("to") not in CONTACT_STATUSES:
                    continue
                if (ev.get("note") or "").startswith(_PROPAGATED_STATUS_NOTES):
                    continue
                dt = _parse_utc(ev.get("ts"))
                if dt:
                    contacts[(ev["ts"], ev["to"])] = dt

        # Keep only this week's contacts (local-time bucketed); the application counts as
        # a contact only if it, too, landed in the week.
        events: list[dict] = []
        applied_in_week = applied_dt is not None and start <= _local_naive(applied_dt) < end
        if applied_in_week:
            events.append({"dt": applied_dt, "label": CONTACT_LABELS["applied"], "kind": "applied"})
        for (_, to), dt in contacts.items():
            if start <= _local_naive(dt) < end:
                events.append({"dt": dt, "label": CONTACT_LABELS[to], "kind": to})
        if not events:
            continue

        events.sort(key=lambda e: e["dt"])
        n_apps += 1 if applied_in_week else 0
        n_responses += sum(1 for e in events if e["kind"] != "applied")

        company = (rep["company_actual"] or rep["company"] or "—").strip()
        # Application URL: prefer a real apply link from any member, else the listing URL.
        url = next((m["apply_url"] for m in members if (m["apply_url"] or "").strip()), None) \
            or rep["job_url"]
        company_url = next((m["company_url"] for m in members if (m["company_url"] or "").strip()), None)
        employers.setdefault(company, []).append({
            "title": rep["title_actual"] or rep["title"] or "—",
            "job_id": rep["job_id"],  # representative posting, for the description preview panel
            "url": url,
            "company_url": company_url,
            "applied_at": _fmt_local(applied_dt),
            "applied_in_week": applied_in_week,
            "events": [{"when": _fmt_local(e["dt"]), "label": e["label"], "kind": e["kind"]}
                       for e in events],
            # Sort by the actual instant of the earliest in-week contact (events is already
            # chronological); the formatted "when" string wouldn't order by date.
            "_sort": events[0]["dt"],
        })

    # Alphabetical by employer; roles within an employer earliest-contact first. The
    # employer's site is the first role's that resolved one (roles share an employer).
    report = [
        {"company": c,
         "company_url": next((r["company_url"] for r in roles if r["company_url"]), None),
         "roles": sorted(roles, key=lambda r: r["_sort"])}
        for c, roles in sorted(employers.items(), key=lambda kv: kv[0].lower())
    ]

    return render_template(
        "report_weekly.html",
        report=report,
        week_label=f"{start:%a %b %-d} – {end - timedelta(days=1):%a %b %-d, %Y}",
        week_start_iso=f"{start:%Y-%m-%d}",
        prev_week=f"{(start - timedelta(days=1)):%Y-%m-%d}",
        next_week=f"{end:%Y-%m-%d}",
        is_current_week=(start <= datetime.now() < end),
        n_apps=n_apps,
        n_responses=n_responses,
        n_employers=len(report),
    )


def _fmt_local(dt: datetime | None) -> str | None:
    """Format an aware UTC datetime as local wall time for the report, e.g.
    'Sun Jun 22, 2026 2:16 PM EDT'. Returns None for None."""
    if dt is None:
        return None
    return dt.astimezone().strftime("%a %b %-d, %Y %-I:%M %p %Z")


@app.route("/stats")
def stats():
    """JSON for the stats modal: total jobs, counts by status, new-in-last-7-days,
    counts by label and by viability, and a stale-score count (active jobs only)."""
    db = get_db()
    # Stats are per-lens: every count scopes to the current search's members via the join.
    join = _jss_join(_current_search_id())
    total = db.execute(f"SELECT COUNT(*) FROM jobs {join}").fetchone()[0]
    by_status = {
        r["status"]: r["cnt"]
        for r in db.execute(
            f"SELECT jss.status AS status, COUNT(*) AS cnt FROM jobs {join} "
            "GROUP BY jss.status ORDER BY cnt DESC"
        ).fetchall()
    }
    new_7d = db.execute(
        f"SELECT COUNT(*) FROM jobs {join} WHERE jobs.first_seen >= datetime('now', '-7 days')"
    ).fetchone()[0]
    label_rows = db.execute(
        f"""SELECT je.value AS lbl, COUNT(*) AS cnt
           FROM jobs {join}, json_each(jobs.labels) je
           GROUP BY je.value ORDER BY cnt DESC"""
    ).fetchall()
    by_label = [
        {"label": r["lbl"], "display": LABEL_NAMES.get(r["lbl"], r["lbl"].upper()), "count": r["cnt"]}
        for r in label_rows
    ]
    viability_rows = db.execute(
        f"SELECT COALESCE(jss.viability, 'unscored') AS level, COUNT(*) AS cnt "
        f"FROM jobs {join} GROUP BY level ORDER BY cnt DESC"
    ).fetchall()
    by_viability = {r["level"]: r["cnt"] for r in viability_rows}
    current_hash = _current_viability_hash()
    # Only flag stale scores on active jobs — old/closed/skipped jobs are
    # intentionally not rescored and would make this number misleadingly large.
    _, active_condition = STATUS_FILTERS["active"]
    viability_stale = db.execute(
        f"SELECT COUNT(*) FROM jobs {join} WHERE jss.viability IS NOT NULL "
        f"AND (jss.viability_prompt_hash != ? OR jss.needs_rescored = 1) AND {active_condition}",
        (current_hash,),
    ).fetchone()[0] if current_hash else 0
    # Application-pipeline timing, from status-event histories (per-lens). Narrow to jobs that
    # reached applied or interviewing (every measured transition starts at one of those) so we
    # don't parse history for the thousands of skipped/new postings that can't contribute.
    hist_rows = db.execute(
        f"SELECT jss.history AS history FROM jobs {join} "
        "WHERE jss.history LIKE '%\"to\":\"applied\"%' OR jss.history LIKE '%\"to\":\"interviewing\"%'"
    ).fetchall()
    transition_times = transition_time_stats(
        json.loads(r["history"] or "[]") for r in hist_rows
    )
    # Polite vs rude: of closed-out negative outcomes, how many employers said no (rejected)
    # vs went silent (ghosted). Withdrawn is your choice, not theirs, so it's excluded.
    rejected_n = by_status.get("rejected", 0)
    ghosted_n  = by_status.get("ghosted", 0)
    negatives  = rejected_n + ghosted_n
    outcome_split = {
        "rejected":   rejected_n,
        "ghosted":    ghosted_n,
        "polite_pct": round(rejected_n / negatives * 100, 1) if negatives else None,
    }
    return {
        "total": total,
        "by_status": by_status,
        "new_last_7_days": new_7d,
        "by_label": by_label,
        "by_viability": by_viability,
        "viability_stale": viability_stale,
        "transition_times": transition_times,
        "outcome_split": outcome_split,
    }


@app.route("/stats/history")
def stats_history():
    """JSON time series (last 7 days) of ingest_history inserted/updated/unchanged per
    day, for the activity chart. Scoped to the current lens (the ingest_history key carries
    the search_id prefix) so a freshly-created search doesn't show every search's activity.
    Returns empty arrays if the table doesn't exist yet."""
    db = get_db()
    scope_sql, scope_params = history_scope(_current_search_id())
    try:
        rows = db.execute(
            f"""
            SELECT DATE(run_at) AS day,
                   SUM(inserted)  AS inserted,
                   SUM(updated)   AS updated,
                   SUM(unchanged) AS unchanged
            FROM ingest_history
            WHERE {scope_sql} AND run_at >= datetime('now', '-7 days')
            GROUP BY DATE(run_at)
            ORDER BY day
            """,
            scope_params,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    return {
        "days":      [r["day"]       for r in rows],
        "inserted":  [r["inserted"]  for r in rows],
        "updated":   [r["updated"]   for r in rows],
        "unchanged": [r["unchanged"] for r in rows],
    }


def viability_day_series(rows) -> dict:
    """Turn (day, viability, count) rows into aligned per-day high/medium/low arrays for the
    viability-over-time chart. A (day, level) combo absent from the rows becomes 0 so the
    three series stay the same length and index-align with `days`. Pure/testable."""
    by_day: dict[str, dict] = {}
    for day, viability, cnt in rows:
        bucket = by_day.setdefault(day, {"high": 0, "medium": 0, "low": 0})
        if viability in bucket:
            bucket[viability] = cnt
    days = sorted(by_day)
    return {
        "days":   days,
        "high":   [by_day[d]["high"]   for d in days],
        "medium": [by_day[d]["medium"] for d in days],
        "low":    [by_day[d]["low"]    for d in days],
    }


@app.route("/stats/viability_by_day")
def stats_viability_by_day():
    """Per-day counts of scored jobs by current viability (high/medium/low), bucketed by
    first_seen over the last 30 days — data for the viability-over-time stacked chart.
    Absolute counts only; the client derives the percentage view. Unscored jobs excluded.

    Optional ?label=<key> narrows to jobs carrying that label (e.g. 'nc', 'remote'), so the
    chart can show how one geography/tag is scoring. Filtered via the same labels-LIKE
    pattern the jobs list uses."""
    db = get_db()
    label = request.args.get("label", "").strip()
    params: list = []
    label_clause = ""
    if label:
        label_clause = "AND labels LIKE ?"
        params.append(f'%"{label}"%')
    rows = db.execute(
        f"""SELECT DATE(jobs.first_seen) AS day, jss.viability AS viability, COUNT(*) AS cnt
            FROM jobs {_jss_join(_current_search_id())}
            WHERE jss.viability IN ('high', 'medium', 'low')
              AND jobs.first_seen >= datetime('now', '-30 days')
              {label_clause}
            GROUP BY DATE(jobs.first_seen), jss.viability""",
        params,
    ).fetchall()
    return viability_day_series([(r["day"], r["viability"], r["cnt"]) for r in rows])


@app.route("/job/<job_id>")
def get_job(job_id: str):
    """Return one job as JSON for the preview panel: meta, salary (display + override
    fields), the raw description and the sanitized AI-formatted HTML, viability + its
    staleness, notes, attachments, and the history timeline."""
    db = get_db()
    # Per-lens fetch: jss columns (status/viability/salary+geo overrides/history) first so they
    # shadow the dormant jobs.* columns; history isn't in _JSS_COLS so add it explicitly.
    row = db.execute(
        f"SELECT {_JSS_COLS}, jss.history AS history, jobs.* "
        f"FROM jobs {_jss_join(_current_search_id())} WHERE jobs.job_id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return "Not found", 404
    job = dict(row)
    attachments = [
        dict(a) for a in db.execute(
            "SELECT attachment_id, original_name, size, content_type, uploaded_at "
            "FROM job_attachments WHERE job_id = ? ORDER BY uploaded_at", (job_id,)
        ).fetchall()
    ]
    # Cross-lens info: the SAME posting's (viability, status) under every OTHER search it belongs
    # to, so the panel can surface "also appears in <search> — <viability>/<status>". Empty for a
    # single-search setup (there are no other lenses).
    other_searches = [
        {"search_id":   r["search_id"],
         "search_name": (APP_CONFIG.get_search(r["search_id"]).name
                         if APP_CONFIG.get_search(r["search_id"]) else r["search_id"]),
         "viability":   r["viability"],
         "status":      r["status"]}
        for r in db.execute(
            "SELECT search_id, viability, status FROM job_search_state "
            "WHERE job_id = ? AND search_id != ? ORDER BY search_id",
            (job_id, _current_search_id()))
    ]
    return {
        "job_id":           job["job_id"],
        # Non-NULL when this posting is a grouped member (points at a canonical root); the
        # preview pane shows the "Promote to canonical" control only in that case.
        "canonical_id":     job.get("canonical_id"),
        "title":            job["title"],
        "title_actual":     job.get("title_actual"),
        "company":          job["company"],
        "company_actual":   job.get("company_actual"),
        "company_url":      job.get("company_url"),
        "company_hotlisted": _company_key(job.get("company_actual"), job.get("company")) in get_hotlist(db),
        "work_arrangement_actual":  job.get("work_arrangement_actual"),
        # What the feed says (glossed), shown as a hint next to the override control.
        "work_arrangement_feed":    _work_arrangement({**job, "work_arrangement_actual": None}),
        "work_arrangement_options": WORK_ARRANGEMENTS,
        # Manual geographic-fit override ('acceptable' or None) — see set_geo_fit_actual.
        "geo_fit_actual":   job.get("geo_fit_actual"),
        "location":         job["location"],
        "job_url":          job["job_url"],
        "apply_url":        job["apply_url"],
        "easy_apply":       job["easy_apply"],
        "salary_display":   format_salary(job),
        "salary_min":       job.get("salary_min"),
        "salary_max":       job.get("salary_max"),
        "salary_min_actual": job.get("salary_min_actual"),
        "salary_max_actual": job.get("salary_max_actual"),
        "posted_date":      (job["posted_date"] or "")[:10],
        # Effective description (override wins). When overridden, suppress the AI-formatted HTML —
        # it was rendered from the now-superseded feed text — so the panel renders the pasted text.
        "job_description":  effective_description(job),
        "job_description_html": (None if has_description_override(job)
                                 else format_description_html(job.get("job_description_formatted"))),
        # Override provenance for the panel: whether one is set, the raw override, and the feed's
        # original text (so the user can review or revert to what the feed delivered).
        "has_description_override": has_description_override(job),
        "description_actual":      job.get("description_actual"),
        "description_feed":        job.get("job_description"),
        # Whether the (effective) description is a partial career-site teaser — drives the panel's
        # "paste the full text" nudge. Cleared once an override supersedes the feed text.
        "description_truncated":   description_is_truncated(job),
        "viability":        job.get("viability"),
        "viability_reason": job.get("viability_reason"),
        # The scorer's self-reported factor breakdown, parsed to a list for the preview panel to
        # render under the reason. None (no breakdown / unscored / reject-listed) → the panel omits it.
        "viability_factors": _viability_factors(job.get("viability_factors")),
        "viability_stale":  process_job_row(job).get("viability_stale", False),
        "applied_at":       (job.get("applied_at") or "")[:10] or None,
        "notes":            job.get("notes"),
        "attachments":      attachments,
        "history":          json.loads(job.get("history") or "[]"),
        "other_searches":   other_searches,
    }


@app.route("/jobs/status", methods=["POST"])
def update_jobs_status():
    """Bulk-set status for many jobs (checkbox selection). Logs a from→to history entry
    per changed job and auto-manages applied_at: stamp it when moving into 'applied'
    from an early status, clear it when moving back to an early status."""
    new_status = request.form.get("status", "")
    job_ids    = request.form.getlist("job_ids")
    if new_status not in STATUSES or not job_ids:
        return "Invalid request", 400
    db = get_db()
    sid = _current_search_id()
    # Capture old statuses (per-lens) before the bulk update so we can log accurate from→to.
    placeholders = ",".join("?" * len(job_ids))
    old_statuses = {
        r["job_id"]: r["status"]
        for r in db.execute(
            f"SELECT job_id, status FROM job_search_state "
            f"WHERE search_id = ? AND job_id IN ({placeholders})", [sid, *job_ids]
        ).fetchall()
    }
    # Status/applied_at are per-lens (job_search_state); refreshed_at is a shared jobs column.
    db.executemany(
        f"""UPDATE job_search_state SET status = ?, applied_at = {_APPLIED_AT_CASE_SQL}
           WHERE job_id = ? AND search_id = ?""",
        [(new_status, new_status, new_status, jid, sid) for jid in job_ids],
    )
    db.executemany("UPDATE jobs SET refreshed_at = NULL WHERE job_id = ?",
                   [(jid,) for jid in job_ids])
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for jid in job_ids:
        old = old_statuses.get(jid)
        if old and old != new_status:
            append_history(db, jid, {"ts": ts, "event": "status", "from": old, "to": new_status}, sid)
    db.commit()
    return "", 204


@app.route("/job/<job_id>/status", methods=["POST"])
def update_status(job_id: str):
    """Set status for one job from the preview panel / row dropdown. Status is a
    property of the role, so it's propagated to every current matched-group member
    (with per-row applied_at + history), the same way notes/attachments fan out."""
    new_status = request.form.get("status", "")
    if new_status not in STATUSES:
        return "Invalid status", 400
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone():
        return "Not found", 404
    # Status belongs to the role, not the individual posting: propagate to every
    # current group member so applied_at and the history log land on all of them
    # (and survive a later de-group). applied_at is per-row via the CASE below, so
    # each member transitions from its own previous status correctly.
    members = group_member_ids(db, job_id)
    sid = _current_search_id()
    placeholders = ",".join("?" * len(members))
    # Per-lens old statuses (only members that are members of THIS search appear, so the
    # fan-out stays within the current lens — a group can span searches).
    old_statuses = {
        r["job_id"]: r["status"]
        for r in db.execute(
            f"SELECT job_id, status FROM job_search_state "
            f"WHERE search_id = ? AND job_id IN ({placeholders})", [sid, *members]
        ).fetchall()
    }
    db.executemany(
        f"""UPDATE job_search_state SET status = ?, applied_at = {_APPLIED_AT_CASE_SQL}
           WHERE job_id = ? AND search_id = ?""",
        [(new_status, new_status, new_status, mid, sid) for mid in members],
    )
    db.executemany("UPDATE jobs SET refreshed_at = NULL WHERE job_id = ?",
                   [(mid,) for mid in members])
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for mid in members:
        old = old_statuses.get(mid)
        if old and old != new_status:
            append_history(db, mid, {"ts": ts, "event": "status", "from": old, "to": new_status}, sid)
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
    # Company is a shared posting fact; it feeds every lens's scorer, so the override lives on
    # jobs but dirties ALL of the job's per-lens state rows for rescoring.
    db.execute("UPDATE jobs SET company_actual = ? WHERE job_id = ?", (value, job_id))
    db.execute("UPDATE job_search_state SET needs_rescored = 1 WHERE job_id = ?", (job_id,))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "company_actual", "from": old, "to": value},
                   _current_search_id())
    db.commit()
    return "", 204


@app.route("/job/<job_id>/title_actual", methods=["POST"])
def set_title_actual(job_id: str):
    """Override the scraped job title for one posting (e.g. an aggregator's mangled/generic
    title). Empty clears it. Per-job (not fanned out across a matched group — postings in a
    group legitimately carry different titles). The title feeds the viability prompt, so flag
    for rescoring on change."""
    value = request.form.get("title_actual", "").strip() or None
    db = get_db()
    row = db.execute("SELECT title_actual FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return "Not found", 404
    old = row["title_actual"]
    # Shared posting fact → dirty every lens's state row for rescoring.
    db.execute("UPDATE jobs SET title_actual = ? WHERE job_id = ?", (value, job_id))
    db.execute("UPDATE job_search_state SET needs_rescored = 1 WHERE job_id = ?", (job_id,))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "title_actual", "from": old, "to": value},
                   _current_search_id())
    db.commit()
    return "", 204


@app.route("/job/<job_id>/description_actual", methods=["POST"])
def set_description_actual(job_id: str):
    """Override a posting's job description with full text pasted from the employer's site.

    The common case is repairing a career-site feed that delivered only a teaser (see
    ingest.feed_description_truncated): the override supersedes the feed text everywhere the
    description is consumed — viability scoring (build_score_message), the preview panel, and the
    cover-letter prompt — while the feed's original stays in job_description for posterity and
    revert. Empty clears the override. Per-job (not fanned out across a matched group — a pasted
    full posting is specific to the source it came from; group members carry their own prose).
    The override changes what the scorer sees, so flag the job for rescoring; the badge/auto-skip
    'partial description' state also clears once an override is present (we now have the full text).
    """
    value = request.form.get("description_actual", "").strip() or None
    db = get_db()
    row = db.execute("SELECT description_actual FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return "Not found", 404
    had = bool((row["description_actual"] or "").strip())
    # Shared posting fact → dirty every lens's state row for rescoring.
    db.execute("UPDATE jobs SET description_actual = ? WHERE job_id = ?", (value, job_id))
    db.execute("UPDATE job_search_state SET needs_rescored = 1 WHERE job_id = ?", (job_id,))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The full text is bulky and lives in the column; history records only that the override was
    # set/cleared (+ length) so the timeline stays legible.
    append_history(db, job_id, {
        "ts": ts, "event": "description_actual",
        "action": "set" if value else "cleared",
        "length": len(value) if value else 0,
        "replaced": had,
    }, _current_search_id())
    db.commit()
    return "", 204


@app.route("/job/<job_id>/geo_fit_actual", methods=["POST"])
def set_geo_fit_actual(job_id: str):
    """Manually override a posting's geographic fit (see viability.manual_geo_fit). The only
    accepted value is 'acceptable' — the candidate asserting they'd work this location, which
    skips the billed location sub-call and spares the job the POOR→low clamp. Empty clears it.
    Per-job (the assertion is about this specific posting/location, not the whole group). Feeds
    the viability verdict, so flag for rescoring on change."""
    value = request.form.get("geo_fit_actual", "").strip().lower() or None
    if value is not None and value not in MANUAL_GEO_FIT_CHOICES:
        return "Invalid geographic-fit override.", 400
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone():
        return "Not found", 404
    sid = _current_search_id()
    # Geo-fit is a per-lens assessment (each search has its own location criteria): write it to
    # THIS search's state row and dirty only that lens for rescoring.
    row = db.execute(
        "SELECT geo_fit_actual FROM job_search_state WHERE job_id = ? AND search_id = ?",
        (job_id, sid),
    ).fetchone()
    old = row["geo_fit_actual"] if row else None
    db.execute(
        "UPDATE job_search_state SET geo_fit_actual = ?, needs_rescored = 1 "
        "WHERE job_id = ? AND search_id = ?",
        (value, job_id, sid),
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "geo_fit_actual", "from": old, "to": value}, sid)
    db.commit()
    return "", 204


@app.route("/job/<job_id>/work_arrangement", methods=["POST"])
def set_work_arrangement(job_id: str):
    """Override the feed's work arrangement for a job (e.g. a recruiter confirms hybrid
    though the posting reads on-site). Empty clears it. The override wins in viability
    scoring, so flag the job for rescoring."""
    value = request.form.get("work_arrangement", "").strip() or None
    if value is not None and value not in WORK_ARRANGEMENTS:
        return "Invalid work arrangement.", 400
    db = get_db()
    row = db.execute("SELECT work_arrangement_actual FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return "Not found", 404
    old = row["work_arrangement_actual"]
    # Shared posting fact → dirty every lens's state row for rescoring.
    db.execute("UPDATE jobs SET work_arrangement_actual = ? WHERE job_id = ?", (value, job_id))
    db.execute("UPDATE job_search_state SET needs_rescored = 1 WHERE job_id = ?", (job_id,))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "work_arrangement", "from": old, "to": value},
                   _current_search_id())
    db.commit()
    return "", 204


def _toml_basic_string(s: str) -> str:
    """Encode s as a TOML basic (double-quoted) string with the standard escapes —
    needed because company names contain spaces/commas/quotes and can't be bare keys."""
    out = (s.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
    return f'"{out}"'


# One `key = value  # comment` line in [company_aliases]: quoted-or-bare key, quoted-or-
# bare value, optional trailing comment. Used to re-align the block on insert.
_ALIAS_LINE_RE = re.compile(
    r'^\s*(?P<key>"(?:[^"\\]|\\.)*"|[^\s=]+)\s*=\s*'
    r'(?P<val>"(?:[^"\\]|\\.)*"|[^\s#]+)\s*(?P<comment>#.*?)?\s*$'
)


def add_company_alias(variant: str, canonical: str) -> tuple[bool, str | None]:
    """Add `variant = canonical` to [company_aliases] in config.toml with an EOL
    "# Added YYYY-MM-DD via web app." comment, so future ingests normalize the variant.

    Re-emits the whole [company_aliases] block in the hand-maintained style: entries
    grouped by canonical (variants of one employer sit together), canonicals ordered
    A->Z, the `=` column-aligned, and the EOL comments aligned into their own column too.
    Every other line in the file (comments, prompts, the API key) is left untouched. Two
    guards before writing: the result must parse as TOML, and its parsed aliases must
    equal the old set plus exactly this one entry (so re-ordering/re-formatting can't drop
    or mangle a row). Then writes atomically (temp + os.replace).

    Returns (added, error): (True, None) on success; (False, None) if an equivalent alias
    was already present (idempotent no-op); (False, msg) if it refused and wrote nothing
    (variant already maps elsewhere, or a guard tripped)."""
    try:
        original = _config_path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"could not read config.toml: {e}"
    old_aliases = tomllib.loads(original).get("company_aliases", {})
    v_key = variant.strip().lower()
    for k, val in old_aliases.items():
        if str(k).strip().lower() == v_key:
            if str(val).strip() == canonical.strip():
                return False, None  # already aliased to the same name — nothing to write
            return False, (f"'{variant}' already maps to '{val}' in config.toml; "
                           "edit it there to change the target.")

    new_key, new_val = _toml_basic_string(variant), _toml_basic_string(canonical)
    new_comment = f"# Added {datetime.now().strftime('%Y-%m-%d')} via web app."
    lines = original.splitlines(keepends=True)
    nl = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    header_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "[company_aliases]"), None)

    if header_idx is None:  # no table yet — create one at EOF
        block = f"{nl}[company_aliases]{nl}{new_key} = {new_val}  {new_comment}{nl}"
        new_text = original + ("" if not original or original.endswith("\n") else nl) + block
    else:
        end = next((i for i in range(header_idx + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))
        body = lines[header_idx + 1:end]
        matched = {i: (m["key"], m["val"], m["comment"])
                   for i, ln in enumerate(body) if (m := _ALIAS_LINE_RE.match(ln))}
        pos = sorted(matched)
        # All aliases (existing + the new one) grouped by canonical then variant — both
        # case-insensitive — so an employer's variants sit together and canonicals go A->Z.
        # Sorting the quoted text is fine: the leading quote is constant across entries.
        rows = list(matched.values()) + [(new_key, new_val, new_comment)]
        rows.sort(key=lambda e: (e[1].lower(), e[0].lower()))
        width = max(len(k) for k, _, _ in rows)
        prefixes = [f"{k}{' ' * (width - len(k))} = {v}" for k, v, _ in rows]
        comment_col = max(len(p) for p in prefixes) + 2  # aligned comment column
        emitted = [
            (p + (" " * (comment_col - len(p)) + c if c else "")) + nl
            for p, (_, _, c) in zip(prefixes, rows)
        ]
        # Preserve any non-entry lines (blank/standalone-comment) around the block; only
        # the alias rows are re-ordered.
        preamble  = body[:pos[0]] if pos else []
        between   = [body[j] for j in range(pos[0] + 1, pos[-1]) if j not in matched] if pos else []
        trailing  = body[pos[-1] + 1:] if pos else body
        new_body  = preamble + emitted + between + trailing
        new_text = "".join(lines[:header_idx + 1] + new_body + lines[end:])

    try:
        parsed = tomllib.loads(new_text)  # never write something that won't parse back
    except tomllib.TOMLDecodeError as e:
        return False, f"refused: edit would not parse as TOML ({e})"
    # Semantic guard: the alias set must be exactly what it was plus this one entry.
    if parsed.get("company_aliases", {}) != {**old_aliases, variant: canonical}:
        return False, "refused: re-format would have changed existing aliases"
    tmp = _config_path.with_name(_config_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, _config_path)
    return True, None


@app.route("/job/<job_id>/company_rename", methods=["POST"])
def company_rename(job_id: str):
    """Canonically rename an employer — the "change the underlying name" fork of the
    company editor (vs. the per-job "on behalf of" company_actual override).

    Rewrites the scraped `company` on *every* job with this name and adds a permanent
    [company_aliases] entry so future ingests normalize it too (that alias is what keeps
    the rewrite from reverting on the next re-scrape). Each affected job gets a
    `company_renamed` history event and is flagged for rescoring. The feed's original
    name is still preserved in each job's `raw`."""
    to_name = request.form.get("new_name", "").strip()
    if not to_name:
        return "A new company name is required.", 400
    db = get_db()
    row = db.execute("SELECT company FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return "Not found", 404
    from_name = (row["company"] or "").strip()
    if not from_name:
        return "This job has no scraped company name to rename.", 400
    if from_name.lower() == to_name.lower():
        return "The new name matches the current company name.", 400
    # Add the alias first: if it refuses (variant already maps elsewhere), change nothing.
    added, err = add_company_alias(from_name, to_name)
    if err:
        return err, 409
    affected = [r["job_id"] for r in db.execute(
        "SELECT job_id FROM jobs WHERE lower(trim(company)) = lower(trim(?))", (from_name,)
    ).fetchall()]
    db.execute(
        "UPDATE jobs SET company = ? WHERE lower(trim(company)) = lower(trim(?))",
        (to_name, from_name),
    )
    # Company is a shared fact; dirty every affected job's per-lens state rows for rescoring.
    if affected:
        ph = ",".join("?" * len(affected))
        db.execute(f"UPDATE job_search_state SET needs_rescored = 1 WHERE job_id IN ({ph})", affected)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sid = _current_search_id()
    for jid in affected:
        append_history(db, jid, {"ts": ts, "event": "company_renamed",
                                 "from": from_name, "to": to_name, "via": "web"}, sid)
    db.commit()
    return {"renamed": len(affected), "alias_added": added}, 200


@app.route("/company/hotlist", methods=["POST"])
def toggle_hotlist():
    """Add/remove an employer from the hotlist. `company` is the effective employer name;
    `on` = 1/0. Keyed case-insensitively so any spelling of the same employer matches.
    Highlighting is a display concern, so this is workspace-wide, not per-job."""
    company = request.form.get("company", "").strip()
    if not company:
        return "Company required.", 400
    on = request.form.get("on") in ("1", "true", "on")
    key = company.lower()
    db = get_db()
    if on:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "INSERT INTO company_hotlist (name_key, display_name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name_key) DO UPDATE SET display_name = excluded.display_name",
            (key, company, ts),
        )
    else:
        db.execute("DELETE FROM company_hotlist WHERE name_key = ?", (key,))
    db.commit()
    return {"company": company, "hot": on}, 200


def _parse_salary_field(raw: str) -> int | None:
    """Parse a salary input into an annual integer, or None if blank.

    Accepts plain numbers with optional $, commas, and a trailing 'k' shorthand
    (e.g. "120k" → 120000). Raises ValueError on anything else.
    """
    s = (raw or "").strip().lower().replace("$", "").replace(",", "")
    if not s:
        return None
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    return int(round(float(s) * mult))


def _score_one_job(db: sqlite3.Connection, job_id: str) -> tuple[bool, str]:
    """Score one job's viability immediately, mirroring rescore_viability.py's per-job
    write (viability/reason/hash + a 'viability' history event). Fail-soft: returns
    (False, reason) when AI is disabled/unconfigured or the call fails, so the caller can
    report it without erroring — the job keeps viability NULL and the next scheduled
    rescore picks it up.

    Deliberately does NOT take the shared run lock: this is one short call, and
    last-write-wins with a concurrent batch rescore is harmless, whereas blocking on the
    lock could hang the web request behind a long-running rescore.
    """
    # Read config fresh so a prompt/key edit is honored without restarting the app.
    sid = _current_search_id()
    try:
        cfg = load_config(_config_path).get_search(sid).config
    except (ConfigError, AttributeError):
        return False, "could not read config.toml"
    vcfg = cfg.get("viability", {})
    if not vcfg.get("enabled", False):
        return False, "viability scoring is disabled in config"
    prompt = vcfg.get("prompt", "").strip()
    if not prompt:
        return False, "no viability prompt configured"
    # Optional geographic-preferences prompt (see rescore_viability.py). When set, run the
    # focused location sub-call and feed its verdict to the scorer in place of raw locations.
    location_prompt = vcfg.get("location_prompt", "").strip()
    geo_uses_description = bool(vcfg.get("location_use_description", True))
    from ai_config import resolve_ai_settings, resolve_effort, resolve_geo_effort, resolve_geo_model
    api_key, model = resolve_ai_settings(cfg, "viability")
    if not api_key:
        return False, "no Anthropic API key configured"
    # When the sub-call reads the description this escalates to the viability model — haiku
    # false-POORs remote jobs on noisy descriptions (see resolve_geo_model).
    geo_model = resolve_geo_model(cfg, geo_uses_description)
    # Thinking effort for the scorer and geo sub-call (applied only on reasoning models; see
    # viability._thinking_call_config). Resolved the same way rescore_viability does.
    effort     = resolve_effort(cfg, "viability")[0]
    geo_effort = resolve_geo_effort(cfg, geo_uses_description)[0]
    # Fetch with the per-lens state joined (jss columns first, so status/viability/salary/geo
    # come from THIS search's row, shadowing the dormant jobs.* columns build_score_message reads).
    row = db.execute(
        f"SELECT {_JSS_COLS}, jobs.* FROM jobs {_jss_join(sid)} WHERE jobs.job_id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return False, "job not found"
    # Company reject-list: a listed employer in a pre-decision (or already-autoskipped) status is
    # forced to low/autoskipped WITHOUT an AI call, mirroring the batch rescorer's deny check so
    # the "rescore this job" button honors the same guardrail. Statuses you've acted on aren't in
    # REJECT_DENYABLE_STATUSES, so those still get a real score.
    from viability import build_reject_set, is_rejected_company, reject_reason, REJECT_DENYABLE_STATUSES
    reject_set = build_reject_set(cfg)
    if (reject_set and row["status"] in REJECT_DENYABLE_STATUSES
            and is_rejected_company(row["company"], reject_set)):
        rating = "low"
        reason = reject_reason(row["company"])
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # No AI call on the reject path → no factor breakdown; NULL it so a job newly caught by the
        # reject-list doesn't keep a stale breakdown from an earlier real score (mirrors rescore).
        db.execute(
            "UPDATE job_search_state SET viability = ?, viability_reason = ?, "
            "viability_factors = NULL, viability_prompt_hash = ?, needs_rescored = 0 "
            "WHERE job_id = ? AND search_id = ?",
            (rating, reason, scoring_hash_for_config(cfg), job_id, sid),
        )
        old_rating = row["viability"]
        if old_rating is None:
            append_history(db, job_id, {"ts": ts, "event": "viability", "rating": rating, "reason": reason}, sid)
        elif old_rating != rating:
            append_history(db, job_id, {"ts": ts, "event": "rescore", "from": old_rating, "to": rating, "reason": reason}, sid)
        if row["status"] != "autoskipped":
            db.execute("UPDATE job_search_state SET status = 'autoskipped' "
                       "WHERE job_id = ? AND search_id = ?", (job_id, sid))
            append_history(db, job_id, {"ts": ts, "event": "status", "from": row["status"],
                                        "to": "autoskipped", "note": "company on reject-list"}, sid)
        db.commit()
        return True, f"{rating} (reject-listed)"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        # A manual geographic verdict (ACCEPTABLE override, or the remote-in-unsupported-
        # location POOR flag) short-circuits the billed sub-call; fit is None only when
        # neither applies, which is the signal to run the AI location call. See the helper.
        fit, gnote, manual_geo_poor = manual_geo_verdict(dict(row))
        if fit is None and location_prompt:
            fit, match, _gu = assess_location_fit(
                client, location_prompt, dict(row), model=geo_model,
                include_description=geo_uses_description, effort=geo_effort)
            gnote = geo_note(fit, match)
        rating, reason, factors, _usage = score_job(client, prompt, dict(row), model=model,
                                                     geo_note=gnote, effort=effort)
        # A POOR geographic fit is disqualifying — clamp to low (the main scorer discounts it).
        # The clamp rewrites only rating/reason; factors stay as the model reported them.
        rating, reason = clamp_viability_for_geo(fit, rating, reason, manual=manual_geo_poor)
    except Exception as e:  # network/SDK/config errors — stay fail-soft
        return False, f"scoring call failed: {e}"
    if rating is None:
        return False, "model returned no valid rating"
    db.execute(
        "UPDATE job_search_state SET viability = ?, viability_reason = ?, viability_factors = ?, "
        "viability_prompt_hash = ?, needs_rescored = 0 WHERE job_id = ? AND search_id = ?",
        (rating, reason, json.dumps(factors) if factors else None,
         scoring_hash_for_config(cfg), job_id, sid),
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "viability", "rating": rating, "reason": reason}, sid)
    db.commit()
    return True, rating


@app.route("/jobs/manual", methods=["POST"])
def add_manual_job():
    """Create a job by hand — for applications that didn't originate in an Apify feed.

    Requires title + company; everything else is optional. source is 'manual' and a
    unique job_id is minted with a 'manual_' prefix (mirroring careersite's 'cs_'). If
    the 'rescore' checkbox is set, viability is scored inline; otherwise it starts NULL
    and the next scheduled rescore evaluates it.
    """
    db = get_db()
    f  = request.form
    title       = f.get("title", "").strip()
    company     = f.get("company", "").strip()
    job_url     = f.get("job_url", "").strip()
    company_url = f.get("company_url", "").strip()
    # Job URL and company URL are required: nearly every feed-sourced job has both (the
    # posting link and the employer site — ~99.7% coverage), so manual entries should too.
    if not title or not company or not job_url or not company_url:
        return "Title, company, job URL, and company URL are required.", 400
    status = (f.get("status", "new").strip() or "new")
    if status not in STATUSES:
        return "Invalid status.", 400
    try:
        sal_min = _parse_salary_field(f.get("salary_min", ""))
        sal_max = _parse_salary_field(f.get("salary_max", ""))
    except ValueError:
        return "Salary must be a number (optionally with $, commas, or a trailing 'k').", 400
    if sal_min is not None and sal_max is not None and sal_min > sal_max:
        return "Minimum salary exceeds maximum.", 400
    # Keep only configured labels; silently drop anything unrecognized.
    labels = [lbl for lbl in f.getlist("labels") if lbl in LABEL_NAMES]

    # Manual geographic-fit override ('acceptable' or None). A manual entry often exists only
    # because the candidate would work this location, so let them assert it at entry time; it
    # feeds the inline score below just like it feeds a later rescore. See set_geo_fit_actual.
    geo_fit_actual = f.get("geo_fit_actual", "").strip().lower() or None
    if geo_fit_actual is not None and geo_fit_actual not in MANUAL_GEO_FIT_CHOICES:
        return "Invalid geographic-fit override.", 400

    # Applied timestamp: honor an explicit value, else stamp now when the initial status
    # is an applied-family one so applied_at (and the weekly report) has a time to show.
    applied_at = f.get("applied_at", "").strip() or None
    if applied_at is None and status in _APPLIED_FAMILY:
        applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    job_id = f"manual_{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO jobs
             (job_id, title, company, location, posted_date, job_url, apply_url, company_url,
              easy_apply, salary_min, salary_max, salary_currency, job_description, notes,
              status, applied_at, labels, geo_fit_actual, source, raw)
           VALUES
             (:job_id, :title, :company, :location, :posted_date, :job_url, :apply_url, :company_url,
              0, :salary_min, :salary_max, :salary_currency, :job_description, :notes,
              :status, :applied_at, :labels, :geo_fit_actual, 'manual', :raw)""",
        {
            "job_id": job_id, "title": title, "company": company,
            "location":        f.get("location", "").strip() or None,
            "posted_date":     f.get("posted_date", "").strip() or None,
            "job_url":         job_url,
            "apply_url":       f.get("apply_url", "").strip() or None,
            "company_url":     company_url,
            "salary_min":      sal_min,
            "salary_max":      sal_max,
            "salary_currency": f.get("salary_currency", "").strip() or None,
            "job_description": f.get("job_description", "").strip() or None,
            "notes":           f.get("notes", "").strip() or None,
            "status":          status,
            "applied_at":      applied_at,
            "labels":          json.dumps(labels),
            "geo_fit_actual":  geo_fit_actual,
            # raw is NOT NULL; store the submitted form as the provenance record.
            "raw":             json.dumps(dict(f)),
        },
    )
    # Per-lens state row (membership + status/applied_at/geo-fit) — created before the history
    # appends below, which target this search's history.
    sid = _current_search_id()
    db.execute(
        "INSERT OR IGNORE INTO job_search_state "
        "(job_id, search_id, status, applied_at, geo_fit_actual) VALUES (?, ?, ?, ?, ?)",
        (job_id, sid, status, applied_at, geo_fit_actual),
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "created", "source": "manual"}, sid)
    if status != "new":
        append_history(db, job_id, {"ts": ts, "event": "status", "from": "new", "to": status}, sid)
    db.commit()

    scored, score_msg = False, ""
    if f.get("rescore") in ("1", "true", "on"):
        scored, score_msg = _score_one_job(db, job_id)
    return {"job_id": job_id, "scored": scored, "score_message": score_msg}, 201


@app.route("/job/<job_id>/salary_actual", methods=["POST"])
def set_salary_actual(job_id: str):
    try:
        sal_min = _parse_salary_field(request.form.get("salary_min", ""))
        sal_max = _parse_salary_field(request.form.get("salary_max", ""))
    except ValueError:
        return "Salary must be a number", 400
    if sal_min is not None and sal_max is not None and sal_min > sal_max:
        return "Minimum salary exceeds maximum", 400
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone():
        return "Not found", 404
    # Salary override is a per-lens assessment (comp can differ by the search's location).
    # Fan out across the matched group's members IN THIS SEARCH; each keeps its own copy.
    sid = _current_search_id()
    members = group_member_ids(db, job_id)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for mid in members:
        prev = db.execute(
            "SELECT salary_min_actual, salary_max_actual FROM job_search_state "
            "WHERE job_id = ? AND search_id = ?", (mid, sid)
        ).fetchone()
        if prev is None:      # not a member of this search — skip
            continue
        old_min, old_max = prev["salary_min_actual"], prev["salary_max_actual"]
        if old_min == sal_min and old_max == sal_max:
            continue
        db.execute(
            "UPDATE job_search_state SET salary_min_actual = ?, salary_max_actual = ?, "
            "needs_rescored = 1 WHERE job_id = ? AND search_id = ?",
            (sal_min, sal_max, mid, sid),
        )
        append_history(db, mid, {
            "ts": ts, "event": "salary_actual",
            "from": [old_min, old_max], "to": [sal_min, sal_max],
            "origin": job_id,
        }, sid)
    db.commit()
    return "", 204


@app.route("/job/<job_id>/notes", methods=["POST"])
def set_notes(job_id: str):
    """Save free-text notes for a job, fanned out to every matched-group member (each
    keeps its own copy if the group is later split). Empty input clears the note."""
    value = request.form.get("notes", "").strip() or None
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone():
        return "Not found", 404
    # Propagate to every current group member so each keeps its own copy if the
    # group is later split.
    members = group_member_ids(db, job_id)
    placeholders = ",".join("?" * len(members))
    db.execute(
        f"UPDATE jobs SET notes = ? WHERE job_id IN ({placeholders})",
        [value, *members],
    )
    # Log on every member so the paper trail survives a later de-group. `origin`
    # marks which posting the edit was actually made on. (Notes are shared; the event lands
    # in the current lens's history — see the history-storage note in the plan.)
    sid = _current_search_id()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for mid in members:
        append_history(db, mid, {"ts": ts, "event": "notes", "origin": job_id}, sid)
    db.commit()
    return "", 204


@app.route("/job/<job_id>/attachment", methods=["POST"])
def upload_attachment(job_id: str):
    """Store an uploaded file under a UUID name on disk and link it to every current
    matched-group member (one physical file, shared metadata; refcounted on delete).
    The client filename is never used on disk — only a UUID + sanitized extension."""
    db = get_db()
    if not db.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone():
        return "Not found", 404
    file = request.files.get("file")
    if not file or not file.filename:
        return "No file", 400
    original = file.filename[:255]
    # On-disk name is a UUID (+ a sanitized extension); never the client filename.
    ext = os.path.splitext(secure_filename(file.filename))[1]
    attachment_id = uuid.uuid4().hex
    stored_name = f"{attachment_id}{ext}"
    file.save(os.path.join(UPLOADS_DIR, stored_name))
    size = os.path.getsize(os.path.join(UPLOADS_DIR, stored_name))
    ctype = file.mimetype or None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Link the one physical file to every current group member (shared metadata).
    members = group_member_ids(db, job_id)
    db.executemany(
        "INSERT INTO job_attachments (job_id, attachment_id, stored_name, original_name, "
        "content_type, size, uploaded_at) VALUES (?,?,?,?,?,?,?)",
        [(mid, attachment_id, stored_name, original, ctype, size, ts) for mid in members],
    )
    sid = _current_search_id()
    for mid in members:
        append_history(db, mid, {"ts": ts, "event": "attachment_added", "name": original, "origin": job_id}, sid)
    db.commit()
    return {"attachment_id": attachment_id, "original_name": original, "size": size,
            "content_type": ctype, "uploaded_at": ts}, 201


@app.route("/job/<job_id>/attachment/<attachment_id>")
def download_attachment(job_id: str, attachment_id: str):
    """Stream an attachment back under its original filename (404 if the link or the
    on-disk file is missing). basename() guards against path traversal via stored_name."""
    db = get_db()
    row = db.execute(
        "SELECT stored_name, original_name, content_type FROM job_attachments "
        "WHERE job_id = ? AND attachment_id = ?", (job_id, attachment_id),
    ).fetchone()
    if not row:
        abort(404)
    path = os.path.join(UPLOADS_DIR, os.path.basename(row["stored_name"]))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=row["original_name"],
                     mimetype=row["content_type"] or None)


@app.route("/job/<job_id>/attachment/<attachment_id>/delete", methods=["POST"])
def delete_attachment(job_id: str, attachment_id: str):
    """Unlink an attachment from this job; delete the physical file only once no posting
    references it anymore (reference-counted, so de-grouping doesn't orphan others)."""
    db = get_db()
    row = db.execute(
        "SELECT stored_name, original_name FROM job_attachments "
        "WHERE job_id = ? AND attachment_id = ?", (job_id, attachment_id),
    ).fetchone()
    if not row:
        return "Not found", 404
    db.execute("DELETE FROM job_attachments WHERE job_id = ? AND attachment_id = ?",
               (job_id, attachment_id))
    # Reference count: drop the physical file only once no posting references it.
    remaining = db.execute(
        "SELECT COUNT(*) FROM job_attachments WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0]
    if remaining == 0:
        try:
            os.remove(os.path.join(UPLOADS_DIR, os.path.basename(row["stored_name"])))
        except OSError:
            pass
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_history(db, job_id, {"ts": ts, "event": "attachment_removed", "name": row["original_name"]},
                   _current_search_id())
    db.commit()
    return "", 204


@app.route("/jobs/autocomplete")
def jobs_autocomplete():
    """Group-level candidates for the manual link/merge picker: one row per canonical group
    whose title/company matches the query, carrying the group's size, earliest first_seen,
    and source(s) so identically-titled groups (e.g. two 24-posting reposts of the same req)
    are distinguishable. The in-progress job's OWN group is excluded — you can't merge a
    group with itself. Quoted phrases stay whole; each token must match title OR company.

    Grouping matters: a search matches individual postings, but the size/first_seen are
    aggregated over the *whole* group (all members), not just the ones that matched — so the
    count shown is the true group size."""
    q       = request.args.get("q", "").strip()
    exclude = request.args.get("exclude", "")
    if not q:
        return []
    db = get_db()
    token_clauses, token_params = search_token_where(
        q, ["j.title", "j.title_actual", "j.company", "COALESCE(j.company_actual, j.company)"])
    if not token_clauses:
        return []
    # The source job's group root, so we can drop it from the merge candidates.
    source_root = ""
    if exclude:
        r = db.execute(
            "SELECT COALESCE(canonical_id, jobs.job_id) AS root FROM jobs WHERE job_id = ?",
            (exclude,)).fetchone()
        source_root = r["root"] if r else exclude
    rows = db.execute(
        f"""
        SELECT r.job_id AS root_id, COALESCE(r.title_actual, r.title) AS title,
               COALESCE(r.company_actual, r.company) AS company,
               grp.size AS size, grp.first_seen AS first_seen, grp.sources AS sources
        FROM (
            SELECT COALESCE(m.canonical_id, m.job_id) AS root_id,
                   COUNT(*) AS size, MIN(m.first_seen) AS first_seen,
                   GROUP_CONCAT(DISTINCT m.source) AS sources
            FROM jobs m
            WHERE COALESCE(m.canonical_id, m.job_id) IN (
                SELECT DISTINCT COALESCE(j.canonical_id, j.job_id)
                FROM jobs j
                WHERE {token_clauses} AND COALESCE(j.canonical_id, j.job_id) != ?
            )
            GROUP BY COALESCE(m.canonical_id, m.job_id)
        ) grp
        JOIN jobs r ON r.job_id = grp.root_id
        ORDER BY grp.first_seen DESC
        LIMIT 10
        """,
        token_params + [source_root],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = ", ".join(
            SOURCE_NAMES.get(s, s) for s in (d.pop("sources") or "").split(",") if s)
        out.append(d)
    return out


def _merge_group_into(db: sqlite3.Connection, job_id: str, target_root: str, ts: str,
                      search_id: str = DEFAULT_SEARCH_ID) -> int:
    """Merge job_id's ENTIRE current group into the group rooted at target_root.

    Re-points the source group's root and every member at target_root (preserving the
    one-hop / no-chain invariant), and inherits the target's status + applied date for any
    moved member still in a new/reviewing state — per lens (status is per-(job, search)), so
    only members that belong to ``search_id`` inherit. Manual merges may cross searches, so the
    canonical_id re-point (shared grouping) is unrestricted. Pure DB logic (no request/response)
    so it's unit-testable. Returns the number of postings moved.
    """
    row = db.execute("SELECT canonical_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    source_root = (row["canonical_id"] if row else None) or job_id
    source_ids = [r["job_id"] for r in db.execute(
        "SELECT job_id FROM jobs WHERE job_id = ? OR canonical_id = ?", (source_root, source_root)
    ).fetchall() if r["job_id"] != target_root]  # never re-point the target onto itself
    if not source_ids:
        return 0
    placeholders = ",".join("?" * len(source_ids))
    db.execute(f"UPDATE jobs SET canonical_id = ? WHERE job_id IN ({placeholders})",
               [target_root, *source_ids])
    append_history(db, source_root, {
        "ts": ts, "event": "linked", "canonical_id": target_root,
        "note": f"merged group of {len(source_ids)}",
    }, search_id)
    root = db.execute("SELECT status, applied_at FROM job_search_state "
                      "WHERE job_id = ? AND search_id = ?", (target_root, search_id)).fetchone()
    if root and root["status"] not in ("new", "reviewing"):
        for mid in source_ids:
            st = db.execute("SELECT status FROM job_search_state WHERE job_id = ? AND search_id = ?",
                            (mid, search_id)).fetchone()
            if st and st["status"] in ("new", "reviewing"):
                db.execute("UPDATE job_search_state SET status = ?, applied_at = ? "
                           "WHERE job_id = ? AND search_id = ?",
                           (root["status"], root["applied_at"], mid, search_id))
                append_history(db, mid, {
                    "ts": ts, "event": "status", "from": st["status"], "to": root["status"],
                    "note": "inherited from canonical on link",
                }, search_id)
    return len(source_ids)


@app.route("/job/<job_id>/link", methods=["POST"])
def link_job(job_id: str):
    """Merge this posting's whole group into the group whose root is `canonical_id` (from the
    picker — always a group root). Empty `canonical_id` unlinks this posting only."""
    db             = get_db()
    target_raw     = request.form.get("canonical_id", "").strip()
    ts             = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Unlink (this posting only) ────────────────────────────────────────────
    if not target_raw:
        db.execute("UPDATE jobs SET canonical_id = NULL WHERE job_id = ?", (job_id,))
        append_history(db, job_id, {"ts": ts, "event": "unlinked"}, _current_search_id())
        db.commit()
        return "", 204

    # ── Link / merge group ────────────────────────────────────────────────────
    if target_raw == job_id:
        return {"error": "A job cannot be linked to itself."}, 400
    target_row = db.execute(
        "SELECT job_id, canonical_id FROM jobs WHERE job_id = ?", (target_raw,)).fetchone()
    if not target_row:
        return {"error": f"Job {target_raw!r} not found."}, 404
    resolved_root = target_row["canonical_id"] or target_row["job_id"]

    src = db.execute("SELECT canonical_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    source_root = (src["canonical_id"] if src else None) or job_id
    if resolved_root == source_root:
        return {"error": "Those postings are already in the same group."}, 400

    merged = _merge_group_into(db, job_id, resolved_root, ts, _current_search_id())
    db.commit()
    return {"canonical_id": resolved_root, "merged": merged}, 200


def promote_to_canonical(db: sqlite3.Connection, job_id: str, ts: str,
                         search_id: str = DEFAULT_SEARCH_ID) -> tuple[bool, str]:
    """Make `job_id` the canonical (root) of its fuzzy-match group.

    Re-points the former root AND every other member at this job, then clears this job's
    own canonical_id so it becomes the root — preserving the one-hop / no-chain invariant
    (roots have canonical_id IS NULL; members point straight at a root). Pure DB logic (no
    request/response) so it's unit-testable; the route wraps it and commits.

    Returns (True, old_root) on success, or (False, error_message) if the job doesn't exist
    or is already a root/standalone (canonical_id IS NULL) and thus has nothing to promote.
    """
    row = db.execute("SELECT job_id, canonical_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return False, "Job not found."
    old_root = row["canonical_id"]
    if not old_root:
        return False, "This job is already the canonical of its group."
    # Point the old root and all its other members at us. The `AND job_id != ?` guard keeps
    # us from setting our own canonical_id to ourselves in this sweep; we NULL it next.
    db.execute(
        "UPDATE jobs SET canonical_id = ? WHERE (job_id = ? OR canonical_id = ?) AND job_id != ?",
        (job_id, old_root, old_root, job_id),
    )
    db.execute("UPDATE jobs SET canonical_id = NULL WHERE job_id = ?", (job_id,))
    append_history(db, job_id, {"ts": ts, "event": "promoted_canonical", "from_canonical": old_root}, search_id)
    append_history(db, old_root, {"ts": ts, "event": "canonical_demoted", "to_canonical": job_id}, search_id)
    return True, old_root


@app.route("/job/<job_id>/promote_canonical", methods=["POST"])
def promote_canonical(job_id: str):
    """Promote this posting to be its group's canonical/root. The canonical is the group's
    representative everywhere (jobs list, dedup resolution, rescore canonical-promotion), so
    this changes which posting fronts the group. Only valid for a grouped member."""
    db = get_db()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok, result = promote_to_canonical(db, job_id, ts, _current_search_id())
    if not ok:
        return {"error": result}, (404 if result == "Job not found." else 400)
    db.commit()
    return {"canonical_id": None, "old_canonical": result}, 200


if __name__ == "__main__":
    app.run(debug=True)
