"""Persistent per-call AI token/cost ledger, attributed to a search lens and a job.

Before this, spend was only visible as aggregate "N tokens … estimated cost" lines printed to
ingest.log / viability.log, so it couldn't be broken down by lens, by day, or against the real
value measure — what it cost to find a job worth applying to. Every billed Anthropic call now
writes one ``ai_usage`` row via ``record_usage``; the query helpers below turn the ledger into the
stats-modal "AI cost" section.

Accounting rules (see docs/features.md → "AI cost"):
  * Spend is ADDITIVE: every call is its own row, rescores included, dated the day it ran. A
    prompt edit that re-scores everything is real money and shows up as such.
  * Reformat spend is charged to the lens whose ingest triggered the call (descriptions are
    shared across lenses, but the call happens once, caused by that lens's feed).
  * ``cost_usd`` is priced at call time, so a later MODEL_PRICING change doesn't rewrite history.

Only depends on ai_config, so ingest/app/rescore can all import it without a cycle.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from ai_config import estimate_cost

FEATURES = ("viability", "location", "reformat")
VIABILITY_BUCKETS = ("high", "medium", "low", "other")

# Kept as a standalone DDL string so ingest.SCHEMA can include it (fresh/in-memory DBs get the
# table from the base schema) and ensure_ai_usage_table can apply it to existing DBs.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    search_id          TEXT,
    job_id             TEXT,
    feature            TEXT NOT NULL,
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_search_ts ON ai_usage(search_id, ts);
CREATE INDEX IF NOT EXISTS idx_ai_usage_job ON ai_usage(job_id, search_id);
"""


def ensure_ai_usage_table(conn: sqlite3.Connection) -> None:
    """Create the ledger table + indexes (idempotent). Called from every migration path
    (ingest.open_db, app._migrate, rescore_viability.open_db) so whichever entry point touches
    a DB first creates it — the same pattern as ingest.ensure_job_search_state."""
    conn.executescript(SCHEMA)


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
            "INSERT INTO ai_usage (search_id, job_id, feature, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (search_id, job_id, feature, model, counts["input"], counts["output"],
             counts["cache_write"], counts["cache_read"], cost),
        )
    except sqlite3.OperationalError:
        return False
    return True


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
    "Daily AI spend by lens" chart. Every day in the span is present, zero-filled, so all series
    index-align with `days`; only lenses with spend in the span get a series. Unpriced rows
    (cost_usd NULL) count as 0 here — the summary's unpriced_calls flags that gap."""
    day_list = _day_range(_today(conn, now), days)
    rows = conn.execute(
        "SELECT DATE(ts) AS day, search_id, SUM(COALESCE(cost_usd, 0)) AS usd FROM ai_usage "
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

      total_usd, calls, unpriced_calls, by_feature{feature: usd}
      initial_usd / rescore_usd — the earliest row per (lens, job, feature) is the initial call,
          later rows are rescores. Decided over ALL time, so a rescore inside the window of a job
          first scored before it is still rescore spend.
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
        "SELECT search_id, MIN(ts) FROM ai_usage WHERE search_id IS NOT NULL GROUP BY search_id"
    ):
        out[sid] = {
            "total_usd": 0.0, "calls": 0, "unpriced_calls": 0,
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
                FROM ai_usage u WHERE search_id IS NOT NULL
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
        e["initial_usd" if initial else "rescore_usd"] += usd
        bucket = viability if viability in ("high", "medium", "low") else "other"
        e["by_viability"][bucket] += usd

    placeholders = ", ".join("?" for _ in applied_statuses) or "NULL"
    for sid, n in conn.execute(
        f"""SELECT t.search_id, COUNT(*) FROM
                (SELECT DISTINCT search_id, job_id FROM ai_usage
                 WHERE search_id IS NOT NULL AND job_id IS NOT NULL) t
            JOIN job_search_state jss ON jss.job_id = t.job_id AND jss.search_id = t.search_id
            WHERE jss.status IN ({placeholders}) {window_clause('jss.applied_at')}
            GROUP BY t.search_id""",
        applied_statuses + win_params,
    ):
        out[sid]["applied_jobs"] = n

    for sid, n in conn.execute(
        f"""SELECT f.search_id, COUNT(*) FROM
                (SELECT search_id, job_id, MIN(ts) AS first_ts FROM ai_usage
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
        "SELECT search_id, DATE(MIN(ts)) FROM ai_usage WHERE search_id IS NOT NULL GROUP BY search_id"
    ):
        firsts[sid] = first_day
        spend[sid] = [0.0] * ext_n
    for day, sid, usd in conn.execute(
        "SELECT DATE(ts), search_id, SUM(COALESCE(cost_usd, 0)) FROM ai_usage "
        "WHERE search_id IS NOT NULL AND DATE(ts) BETWEEN ? AND ? GROUP BY DATE(ts), search_id",
        (ext_days[0], ext_days[-1]),
    ):
        spend[sid][index[day]] = usd

    apps: dict[str, list[int]] = {sid: [0] * ext_n for sid in spend}
    placeholders = ", ".join("?" for _ in applied_statuses) or "NULL"
    for day, sid, n in conn.execute(
        f"""SELECT DATE(jss.applied_at), t.search_id, COUNT(*) FROM
                (SELECT DISTINCT search_id, job_id FROM ai_usage
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
