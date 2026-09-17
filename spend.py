"""Persistent spend ledger — every dollar this tracker costs, attributed to a search lens
(and, where it can be, to a job).

Before this, spend was only visible as aggregate "N tokens … estimated cost" lines printed to
ingest.log / viability.log, so it couldn't be broken down by lens, by day, or against the real
value measure — what it cost to find a job worth applying to. Now each billed Anthropic call
writes a ``spend_ledger`` row via ``record_usage``, and each Apify run's charge is pro-rated over
the postings it returned via ``record_apify_run``; the query helpers below turn the ledger into
the stats-modal "All lenses" tab.

Accounting rules (see docs/features.md → "Cost"):
  * Spend is ADDITIVE: every call is its own row, rescores included, dated the day it ran. A
    prompt edit that re-scores everything is real money and shows up as such.
  * Reformat spend is charged to the lens whose ingest triggered the call (descriptions are
    shared across lenses, but the call happens once, caused by that lens's feed).
  * Apify spend is split evenly across the items a run returned — including re-sightings of
    postings already known, since monitoring for changes is what the run was paying for. A run
    that returned nothing attributable becomes an unattributed (job_id NULL) overhead row, which
    keeps lens totals exact while leaving per-job figures honest.
  * ``cost_usd`` is priced/charged at the time of the call, so a later MODEL_PRICING change
    doesn't rewrite history.

Only depends on ai_config, so ingest/app/rescore can all import it without a cycle.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta

from ai_config import estimate_cost

# Ledger row kinds. The first three are Anthropic calls (token-priced); 'apify' is a pro-rated
# share of one scraper run's charge.
FEATURES = ("viability", "location", "reformat", "apify")
VIABILITY_BUCKETS = ("high", "medium", "low", "other")

# Kept as a standalone DDL string so ingest.SCHEMA can include it (fresh/in-memory DBs get the
# table from the base schema) and ensure_spend_ledger can apply it to existing DBs.
SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_ledger (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    search_id          TEXT,
    job_id             TEXT,
    feature            TEXT NOT NULL,
    -- What was billed: the Anthropic model for AI rows, the Apify task name for 'apify' rows.
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL
);
CREATE INDEX IF NOT EXISTS idx_spend_search_ts ON spend_ledger(search_id, ts);
CREATE INDEX IF NOT EXISTS idx_spend_job ON spend_ledger(job_id, search_id);
"""


def ensure_spend_ledger(conn: sqlite3.Connection) -> None:
    """Create the ledger table + indexes (idempotent). Called from every migration path
    (ingest.open_db, app._migrate, rescore_viability.open_db) so whichever entry point touches
    a DB first creates it — the same pattern as ingest.ensure_job_search_state."""
    conn.executescript(SCHEMA)
    # The table was born as `ai_usage` and outgrew the name once Apify runs joined it. Carry its
    # rows over and drop it. Deliberately a copy-then-drop rather than an ALTER … RENAME: a
    # running (auto-reloading) app may already have created the new empty table beside the old
    # one, in which case a rename would silently do nothing and strand every historical row.
    # Ids are re-issued (nothing references them); the migration is one-shot — once `ai_usage` is
    # gone this is a cheap sqlite_master lookup.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_usage'").fetchone():
        cols = ("ts, search_id, job_id, feature, model, input_tokens, output_tokens, "
                "cache_write_tokens, cache_read_tokens, cost_usd")
        conn.execute(f"INSERT INTO spend_ledger ({cols}) SELECT {cols} FROM ai_usage")
        conn.execute("DROP TABLE ai_usage")
        conn.commit()


def usage_counts(usage) -> dict[str, int] | None:
    """The four billed token counts from an Anthropic ``usage`` object, or None when there's
    nothing to record (no usage object, or every count zero — a call that never reached the
    API). Tolerant of missing/None attributes, matching how the log tallies read usage."""
    if usage is None:
        return None
    counts = {
        "input":       getattr(usage, "input_tokens",                0) or 0,
        "output":      getattr(usage, "output_tokens",               0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read":  getattr(usage, "cache_read_input_tokens",     0) or 0,
    }
    return counts if any(counts.values()) else None


def record_usage(conn: sqlite3.Connection, *, feature: str, model: str, usage,
                 search_id: str | None, job_id: str | None) -> bool:
    """Append one ledger row for a billed call. Returns True if a row was written.

    Deliberately does NOT commit: callers already commit per job (rescore) / per run (ingest) /
    per request (app), and folding the insert into that transaction keeps the ledger consistent
    with the work it paid for. Never raises on a missing table either — accounting must not be
    able to break scoring or ingest, so a DB that somehow lacks the table just skips the row."""
    counts = usage_counts(usage)
    if counts is None:
        return False
    cost = estimate_cost(model, **counts)
    try:
        conn.execute(
            "INSERT INTO spend_ledger (search_id, job_id, feature, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (search_id, job_id, feature, model, counts["input"], counts["output"],
             counts["cache_write"], counts["cache_read"], cost),
        )
    except sqlite3.OperationalError:
        return False
    return True


def record_apify_run(conn: sqlite3.Connection, *, search_id: str, task_name: str,
                     cost_usd: float | None, item_job_ids: "list[str | None]",
                     divisor: int = 1) -> int:
    """Pro-rate one Apify run's charge over the items it returned. Returns rows written.

    ``item_job_ids`` is one entry per item in the run's dataset — the job it maps to, or None for
    an item that produced no job row (an ATS duplicate we skipped). Each item carries an equal
    share of ``cost_usd``: a posting seen 30 times a day accrues 30 shares, because re-scraping a
    known posting is exactly what the run was paying for. Shares for the same job within one run
    are summed into a single row, so this writes at most one row per (run, job).

    Unattributable share — items with no job, or a run that returned nothing at all (the common
    case on a frequent schedule) — lands in ONE unattributed row per lens per day (job_id NULL),
    accumulated in place so a high-cadence task can't flood the table. That keeps the lens total
    exact for cost-per-application while leaving the by-viability split to attributable spend.

    ``divisor`` splits the charge when several lenses ingest the same run (a task shared without
    schedule scoping, where Apify billed once but each lens processes it) so the total isn't
    double-counted; it's 1 for the normal one-lens-per-run case.
    """
    if not cost_usd:
        return 0
    # A run that returned nothing still cost money — treat it as one unattributable item so the
    # whole charge lands in the lens's overhead row rather than being dropped.
    items = item_job_ids or [None]
    share = (cost_usd / divisor) / len(items)
    per_job: Counter = Counter(j for j in items if j)
    unattributed = share * sum(1 for j in items if not j)
    written = 0
    try:
        for job_id, n in per_job.items():
            conn.execute(
                "INSERT INTO spend_ledger (search_id, job_id, feature, model, cost_usd) "
                "VALUES (?, ?, 'apify', ?, ?)", (search_id, job_id, task_name, share * n))
            written += 1
        if unattributed:
            # One overhead row per lens per UTC day, topped up in place.
            row = conn.execute(
                "SELECT id, cost_usd FROM spend_ledger WHERE feature = 'apify' AND job_id IS NULL "
                "AND search_id = ? AND DATE(ts) = DATE('now')", (search_id,)).fetchone()
            if row:
                conn.execute("UPDATE spend_ledger SET cost_usd = ? WHERE id = ?",
                             ((row[1] or 0) + unattributed, row[0]))
            else:
                conn.execute(
                    "INSERT INTO spend_ledger (search_id, job_id, feature, model, cost_usd) "
                    "VALUES (?, NULL, 'apify', ?, ?)", (search_id, task_name, unattributed))
            written += 1
    except sqlite3.OperationalError:
        return written
    return written


# ── Query helpers ────────────────────────────────────────────────────────────────────────
# All take a `now` SQLite time-value (default the literal 'now') so tests can pin the clock.
# Days are UTC, matching the existing stats charts' DATE(...) bucketing.

def _today(conn: sqlite3.Connection, now: str) -> date:
    return date.fromisoformat(conn.execute("SELECT DATE(?)", (now,)).fetchone()[0])


def _day_range(end: date, n: int) -> list[str]:
    """The n consecutive ISO dates ending on (and including) `end`, oldest first."""
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def daily_spend_by_lens(conn: sqlite3.Connection, *, days: int = 30,
                        now: str = "now") -> dict:
    """Per-day spend per lens over the last `days` UTC days (today included), for the stacked
    daily-spend chart — AI calls and Apify runs together, since it answers "what did each lens
    cost me that day?". Every day in the span is present, zero-filled, so all series
    index-align with `days`; only lenses with spend in the span get a series. Unpriced rows
    (cost_usd NULL) count as 0 here — the summary's unpriced_calls flags that gap."""
    day_list = _day_range(_today(conn, now), days)
    rows = conn.execute(
        "SELECT DATE(ts) AS day, search_id, SUM(COALESCE(cost_usd, 0)) AS usd FROM spend_ledger "
        "WHERE search_id IS NOT NULL AND DATE(ts) BETWEEN ? AND ? GROUP BY DATE(ts), search_id",
        (day_list[0], day_list[-1]),
    ).fetchall()
    index = {d: i for i, d in enumerate(day_list)}
    series: dict[str, list[float]] = {}
    for day, sid, usd in rows:
        series.setdefault(sid, [0.0] * days)[index[day]] = usd
    return {"days": day_list, "series": series}


def lens_cost_summary(conn: sqlite3.Connection, *, applied_statuses, window_days: int | None = None,
                      now: str = "now") -> dict[str, dict]:
    """Per-lens cost breakdown, all-time (window_days=None) or over a trailing window.

    Keyed by search_id; includes every lens that has ANY ledger rows (so a lens with no spend in
    a short window still appears, with zero totals and None ratios). Each entry:

      total_usd (AI + Apify), ai_usd, apify_usd, calls, unpriced_calls, by_feature{feature: usd}
      initial_usd / rescore_usd — of the AI spend only, the earliest row per (lens, job, feature)
          is the initial call and later rows are rescores. Decided over ALL time, so a rescore
          inside the window of a job first scored before it is still rescore spend. Apify is
          excluded: re-scraping a known posting is monitoring, not re-deciding it, so calling it
          "tuning" would be wrong (the two sum to ai_usd, not total_usd).
      by_viability{high, medium, low, other} — spend grouped by the job's CURRENT rating in that
          lens ("what did I spend on jobs that ended up Low?"); other = unscored / no state row.
      applied_jobs, cost_per_applied, high_jobs, cost_per_high, first_ts

    Denominators only count TRACKED jobs (any ledger row in the lens) so pre-ledger jobs, whose
    cost was never recorded, can't make the ratio look falsely cheap:
      * all-time applied: current status in `applied_statuses` (the applied family — "ever applied";
        a job moved back to an early status drops out).
      * windowed applied: additionally applied_at inside the window. A spend-rate ÷ application-
        rate ratio, so it isn't inflated by recent jobs you haven't reviewed yet.
      * high: currently rated High and has a viability-scoring row; windowed, that FIRST scoring
        row must fall in the window (the rating arrives with the score, so there's no review lag).
    `applied_statuses` is a parameter (not imported from app) to avoid an import cycle.
    """
    applied_statuses = tuple(applied_statuses)
    windowed = window_days is not None
    # Stored timestamps are 'YYYY-MM-DD HH:MM:SS' (CURRENT_TIMESTAMP format), which compare
    # correctly as strings against datetime() output. The same (start, end] bound is reused for
    # spend ts, applied_at, and first-scoring ts, so each is formatted from one column name.
    win_params = (now, f"-{int(window_days)} days", now) if windowed else ()

    def window_clause(col: str) -> str:
        return f"AND {col} > datetime(?, ?) AND {col} <= datetime(?)" if windowed else ""

    out: dict[str, dict] = {}
    for sid, first_ts in conn.execute(
        "SELECT search_id, MIN(ts) FROM spend_ledger WHERE search_id IS NOT NULL GROUP BY search_id"
    ):
        out[sid] = {
            "total_usd": 0.0, "ai_usd": 0.0, "apify_usd": 0.0, "calls": 0, "unpriced_calls": 0,
            "by_feature": {f: 0.0 for f in FEATURES},
            "initial_usd": 0.0, "rescore_usd": 0.0,
            "by_viability": {b: 0.0 for b in VIABILITY_BUCKETS},
            "applied_jobs": 0, "cost_per_applied": None,
            "high_jobs": 0, "cost_per_high": None,
            "first_ts": first_ts,
        }
    if not out:
        return out

    # Spend, split every way at once. The ROW_NUMBER is computed over ALL rows (before the window
    # filter) so initial-vs-rescore reflects the job's whole history, not just the window.
    rows = conn.execute(
        f"""WITH ranked AS (
                SELECT u.*,
                       CASE WHEN job_id IS NULL THEN 1
                            ELSE ROW_NUMBER() OVER (PARTITION BY search_id, job_id, feature
                                                    ORDER BY ts, id) END AS rn
                FROM spend_ledger u WHERE search_id IS NOT NULL
            )
            SELECT r.search_id, r.feature, r.rn = 1 AS initial,
                   jss.viability AS viability,
                   SUM(COALESCE(r.cost_usd, 0)) AS usd, COUNT(*) AS calls,
                   SUM(r.cost_usd IS NULL) AS unpriced
            FROM ranked r
            LEFT JOIN job_search_state jss
                   ON jss.job_id = r.job_id AND jss.search_id = r.search_id
            WHERE 1 {window_clause('r.ts')}
            GROUP BY r.search_id, r.feature, initial, jss.viability""",
        win_params,
    ).fetchall()
    for sid, feature, initial, viability, usd, calls, unpriced in rows:
        e = out[sid]
        e["total_usd"] += usd
        e["calls"] += calls
        e["unpriced_calls"] += unpriced
        e["by_feature"][feature] = e["by_feature"].get(feature, 0.0) + usd
        if feature == "apify":
            e["apify_usd"] += usd
        else:
            # Only AI spend splits into first-score vs re-score (see the docstring).
            e["ai_usd"] += usd
            e["initial_usd" if initial else "rescore_usd"] += usd
        bucket = viability if viability in ("high", "medium", "low") else "other"
        e["by_viability"][bucket] += usd

    placeholders = ", ".join("?" for _ in applied_statuses) or "NULL"
    for sid, n in conn.execute(
        f"""SELECT t.search_id, COUNT(*) FROM
                (SELECT DISTINCT search_id, job_id FROM spend_ledger
                 WHERE search_id IS NOT NULL AND job_id IS NOT NULL) t
            JOIN job_search_state jss ON jss.job_id = t.job_id AND jss.search_id = t.search_id
            WHERE jss.status IN ({placeholders}) {window_clause('jss.applied_at')}
            GROUP BY t.search_id""",
        applied_statuses + win_params,
    ):
        out[sid]["applied_jobs"] = n

    for sid, n in conn.execute(
        f"""SELECT f.search_id, COUNT(*) FROM
                (SELECT search_id, job_id, MIN(ts) AS first_ts FROM spend_ledger
                 WHERE feature = 'viability' AND search_id IS NOT NULL AND job_id IS NOT NULL
                 GROUP BY search_id, job_id) f
            JOIN job_search_state jss ON jss.job_id = f.job_id AND jss.search_id = f.search_id
            WHERE jss.viability = 'high' {window_clause('f.first_ts')}
            GROUP BY f.search_id""",
        win_params,
    ):
        out[sid]["high_jobs"] = n

    for e in out.values():
        if e["applied_jobs"]:
            e["cost_per_applied"] = e["total_usd"] / e["applied_jobs"]
        if e["high_jobs"]:
            e["cost_per_high"] = e["total_usd"] / e["high_jobs"]
    return out


def rolling_ratio(spend: list[float], apps: list[int], window: int,
                  first_index: int = 0) -> list[float | None]:
    """Trailing-`window` spend ÷ applications, one point per full window. Pure/testable.

    `spend` and `apps` are index-aligned per-day arrays; the result has one entry per window
    END position, i.e. len(spend) - window + 1 points (the first ends at index window-1). A point
    is None — a gap in the trend line, not a zero — when:
      * the window holds no applications (the ratio is undefined), or
      * the window STARTS before `first_index` (the lens's first tracked day). A partial window
        would understate spend and make a new lens look falsely cheap, so the line only starts
        once the lens has a full window of tracking.
    """
    out: list[float | None] = []
    for end in range(window - 1, len(spend)):
        start = end - window + 1
        n = sum(apps[start:end + 1])
        if start < first_index or n == 0:
            out.append(None)
        else:
            out.append(sum(spend[start:end + 1]) / n)
    return out


def trailing_cost_per_applied_series(conn: sqlite3.Connection, *, applied_statuses,
                                     window_days: int = 30, span_days: int = 90,
                                     now: str = "now") -> dict:
    """Per-lens trend of trailing-`window_days` cost per application, one point per UTC day over
    the last `span_days` — lets you watch a lens's running cost settle after up-front tuning.

    Uses the same windowed definitions as lens_cost_summary (spend in the window ÷ tracked
    applications whose applied_at falls in it); see rolling_ratio for when a point is None.
    Returns {days, window_days, series: {search_id: [usd | None, ...]}}; lenses whose series is
    entirely None are omitted, so an empty `series` means "not enough tracking yet"."""
    applied_statuses = tuple(applied_statuses)
    today = _today(conn, now)
    ext_n = span_days + window_days - 1          # extra leading days so day 1 has a full window
    ext_days = _day_range(today, ext_n)
    index = {d: i for i, d in enumerate(ext_days)}

    spend: dict[str, list[float]] = {}
    firsts: dict[str, str] = {}
    for sid, first_day in conn.execute(
        "SELECT search_id, DATE(MIN(ts)) FROM spend_ledger WHERE search_id IS NOT NULL GROUP BY search_id"
    ):
        firsts[sid] = first_day
        spend[sid] = [0.0] * ext_n
    for day, sid, usd in conn.execute(
        "SELECT DATE(ts), search_id, SUM(COALESCE(cost_usd, 0)) FROM spend_ledger "
        "WHERE search_id IS NOT NULL AND DATE(ts) BETWEEN ? AND ? GROUP BY DATE(ts), search_id",
        (ext_days[0], ext_days[-1]),
    ):
        spend[sid][index[day]] = usd

    apps: dict[str, list[int]] = {sid: [0] * ext_n for sid in spend}
    placeholders = ", ".join("?" for _ in applied_statuses) or "NULL"
    for day, sid, n in conn.execute(
        f"""SELECT DATE(jss.applied_at), t.search_id, COUNT(*) FROM
                (SELECT DISTINCT search_id, job_id FROM spend_ledger
                 WHERE search_id IS NOT NULL AND job_id IS NOT NULL) t
            JOIN job_search_state jss ON jss.job_id = t.job_id AND jss.search_id = t.search_id
            WHERE jss.status IN ({placeholders}) AND DATE(jss.applied_at) BETWEEN ? AND ?
            GROUP BY DATE(jss.applied_at), t.search_id""",
        applied_statuses + (ext_days[0], ext_days[-1]),
    ):
        apps[sid][index[day]] = n

    series: dict[str, list[float | None]] = {}
    for sid in spend:
        # A first tracked day before the extended range means every window is fully tracked.
        first_index = index.get(firsts[sid], 0 if firsts[sid] < ext_days[0] else ext_n)
        points = rolling_ratio(spend[sid], apps[sid], window_days, first_index)
        if any(p is not None for p in points):
            series[sid] = points
    return {"days": ext_days[window_days - 1:], "window_days": window_days, "series": series}
