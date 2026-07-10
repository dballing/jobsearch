#!/usr/bin/env python3
# requires Python 3.11+
"""Re-score job postings for viability using the Anthropic API.

Typically run on a schedule right after ingest (see README's cron line). Engine
settings come from the shared [ai] stanza (api_key/model), overridable per-feature
under [viability]; the candidate profile and on/off toggle live under [viability].
Falls back to the ANTHROPIC_API_KEY env var if no key is configured.

A score is "current" while its stored prompt_hash matches the current prompt — a hash
covering BOTH the config candidate prompt AND the fixed system boilerplate in
viability.py (see viability.prompt_hash), so editing either marks existing scores stale.

Which jobs actually get re-scored is narrower than "everything stale", though. By
DEFAULT only ACTIVE jobs (the UI's Active set) are eligible, and among those only the
ones that are stale, never scored, or flagged needs_rescored — so editing the prompt
does NOT re-score the whole table; a stale score on a skipped/closed/rejected job is
left as-is unless you pass --all. Two cases always override the status filter,
regardless of flags: jobs never scored (viability IS NULL) and jobs flagged
needs_rescored (a viability-relevant field — salary/company override — changed since
the last score). --all scores every status, --early-stage narrows to new/reviewing,
and --force re-scores eligible jobs even when their hash is already current.

Usage:
    python3 rescore_viability.py [--config PATH] [--dry-run] [--force]
        [--all | --early-stage | --autoskipped] [--since YYYY-MM-DD | --previous_days N]

Flags:
    --config PATH      Path to TOML config (default: config.toml).
    --dry-run          Print how many jobs would be scored without scoring them.
    --force            Rescore even jobs whose prompt hash already matches current.
    --all              Score all jobs regardless of status (default: active only).
    --early-stage      Score only new/reviewing/deferred jobs (narrower than default).
    --autoskipped      Score only autoskipped jobs (not plain 'skipped'); promote any that
                       no longer score at/below the auto-skip threshold back to 'new'. Meant
                       for after a prompt change — pair with --force if the prompt is
                       unchanged, since otherwise only stale autoskipped jobs are selected.
    --since DATE       Only jobs first ingested on DATE (UTC, YYYY-MM-DD) or later.
    --previous_days N  Only jobs first ingested within the trailing N days.

(--all/--early-stage/--autoskipped are mutually exclusive, as are --since/--previous_days.)
"""

import argparse
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from ai_config import DEFAULT_MODEL, format_token_summary, resolve_ai_settings
from ingest import append_history
from runlock import acquire_run_lock
from viability import (
    _job_locations, _work_arrangement, assess_location_fit, geo_note,
    prompt_hash, score_job,
)

# Numeric ranking of ratings. Used two ways: to compare a score against the auto-skip
# threshold, and to decide whether a re-evaluated duplicate scored *strictly better*
# than both its prior score and its canonical (the promotion logic below).
VIABILITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def check_model_currency(client: anthropic.Anthropic, configured_model: str) -> None:
    """Warn if the configured model is unavailable or a newer sibling exists.

    Non-fatal: any failure in the check is silently ignored so scoring can proceed.
    """
    try:
        all_models = list(client.models.list())
        model_ids  = {m.id for m in all_models}

        # The models API returns dated IDs (e.g. claude-haiku-4-5-20251001) but
        # not undated aliases (e.g. claude-haiku-4-5).  Treat a configured model
        # as available if it matches exactly OR is a prefix of an available ID.
        def matches_available(name: str) -> bool:
            return any(
                mid == name or mid.startswith(name + "-")
                for mid in model_ids
            )

        if not matches_available(configured_model):
            print(
                f"WARNING: configured model '{configured_model}' is not available "
                f"(it may have been retired). Update [viability] model in config.toml.",
                file=sys.stderr,
            )
            return

        # Derive the family prefix — everything before the first digit-led segment.
        # e.g. "claude-haiku-4-5" → "claude-haiku-"
        parts = configured_model.split("-")
        family_parts: list[str] = []
        for part in parts:
            if part and part[0].isdigit():
                break
            family_parts.append(part)
        family_prefix = "-".join(family_parts) + "-"

        family_models = sorted(
            [m for m in all_models if m.id.startswith(family_prefix)],
            key=lambda m: m.created_at,
            reverse=True,
        )

        if family_models:
            newest = family_models[0].id
            # Not newer if configured model IS the newest or is an alias for it.
            if not (newest == configured_model or newest.startswith(configured_model + "-")):
                print(
                    f"Note: a newer model is available in this family: "
                    f"'{newest}' (you are using '{configured_model}'). "
                    f"Consider updating [viability] model in config.toml."
                )
    except Exception:
        pass  # Non-fatal — don't interrupt scoring if the check fails.


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL mode lets the Flask app read/write concurrently without blocking.
    # busy_timeout retries on lock contention instead of raising immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # Defensive idempotent migrations: ingest.py owns the schema, but this script can be
    # run standalone against a DB ingest hasn't touched yet, so ensure every column we
    # read/write exists. Each ALTER is a no-op once the column is present.
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
    return conn


def rescore_change_note(label: str, old: str | None, new: str) -> str | None:
    """One-line 'Rescored' note for the log when a job's score *changed* value, else None.

    Returns None on a first-time score (old is None) or an unchanged re-score, so a tailed
    viability.log surfaces only the records that actually moved.
    """
    if old is None or old == new:
        return None
    return f"  Rescored: {label} : {old} → {new}"


def valid_since_date(value: str) -> str:
    """argparse type for --since: accept a YYYY-MM-DD calendar date, reject anything else.

    Returns the string unchanged (it's compared directly against ``date(first_seen)`` in
    SQL, both plain YYYY-MM-DD) so we validate the shape but don't reformat it.
    """
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since expects YYYY-MM-DD, got {value!r}")
    return value


def positive_int(value: str) -> int:
    """argparse type for --previous_days: a whole number of days >= 1."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def should_unskip(rating: str, auto_skip_threshold: int) -> bool:
    """True when a rescored autoskipped job now scores strictly ABOVE the auto-skip
    threshold and should be surfaced back to 'new'. Drives --autoskipped re-evaluation:
    after a prompt change, a job the old prompt auto-skipped may now clear the bar."""
    return VIABILITY_RANK.get(rating, -1) > auto_skip_threshold


def build_selection(
    *,
    current_hash: str,
    force: bool = False,
    all_statuses: bool = False,
    early_stage: bool = False,
    autoskipped: bool = False,
    since: str | None = None,
    previous_days: int | None = None,
) -> tuple[str, list]:
    """Build the (WHERE clause, params) selecting which jobs to (re)score this run.

    Pure and free of DB/argparse so the whole filter matrix is unit-testable. Conditions
    are AND-ed across independent axes:

      * staleness (skipped when ``force``): only stale / never-scored / needs_rescored jobs.
      * status: the default is the active set (with NULL-viability / needs_rescored escapes
        so a corrected inactive job still gets looked at); ``all_statuses`` drops the status
        filter entirely; ``early_stage`` limits to new/reviewing/deferred; ``autoskipped``
        limits to the autoskipped set (for post-prompt-change re-evaluation). These four are
        mutually exclusive (enforced at the argparse layer).
      * ingest-date window (optional): ``since`` is a 'YYYY-MM-DD' lower bound compared to
        ``date(first_seen)``; ``previous_days`` is a rolling N*24h window ending now. Also
        mutually exclusive with each other.
    """
    conditions: list[str] = []
    params: list = []

    if not force:
        conditions.append(
            "(viability IS NULL OR viability_prompt_hash IS NULL "
            "OR viability_prompt_hash != ? OR needs_rescored = 1)"
        )
        params.append(current_hash)

    if all_statuses:
        pass  # No status filter.
    elif early_stage:
        conditions.append("status IN ('new', 'reviewing', 'deferred')")
    elif autoskipped:
        # Only the autoskipped set — never plain 'skipped'. No NULL/needs_rescored escape
        # here: --autoskipped is a deliberate, narrow re-evaluation of exactly that status.
        conditions.append("status = 'autoskipped'")
    else:
        conditions.append(
            "(status NOT IN ('skipped', 'autoskipped', 'rejected', 'withdrawn', 'ghosted', 'closed')"
            " OR viability IS NULL OR needs_rescored = 1)"
        )

    if since is not None:
        conditions.append("date(first_seen) >= ?")
        params.append(since)
    elif previous_days is not None:
        conditions.append("first_seen >= datetime('now', ?)")
        params.append(f"-{int(previous_days)} days")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-score job viability against the current candidate description."
    )
    parser.add_argument(
        "--config", default="config.toml",
        help="Path to TOML config (default: config.toml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print count without scoring",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Rescore all matching jobs regardless of prompt hash",
    )
    # Status-mode filters are mutually exclusive: each replaces the default active-only set.
    status_group = parser.add_mutually_exclusive_group()
    status_group.add_argument(
        "--all", action="store_true",
        help="Score all jobs regardless of status (default: active jobs only)",
    )
    status_group.add_argument(
        "--early-stage", action="store_true",
        help="Score only new/reviewing/deferred jobs (narrower than the default active filter)",
    )
    status_group.add_argument(
        "--autoskipped", action="store_true",
        help="Score only autoskipped jobs (NOT plain 'skipped'); promote any that no longer "
             "score at/below the auto-skip threshold back to 'new'. Use after a prompt change "
             "to recover jobs the old prompt auto-skipped.",
    )
    # Ingest-date window (mutually exclusive): limit to jobs first_seen on/after a date, or
    # within a trailing number of days.
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument(
        "--since", type=valid_since_date, metavar="YYYY-MM-DD",
        help="Only jobs first ingested on this date (UTC) or later.",
    )
    date_group.add_argument(
        "--previous-days", "--previous_days", dest="previous_days",
        type=positive_int, metavar="N",
        help="Only jobs first ingested within the trailing N days.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print one line per job regardless of whether stdout is a TTY",
    )
    args = parser.parse_args()

    # Line-buffer stdout so each line is flushed on its newline. When output is
    # redirected to a file (e.g. cron `>> viability.log`), Python block-buffers
    # stdout, which hides the startup banner and per-job progress from a `tail -f`
    # until the buffer fills or the run ends. Mirrors the same fix in ingest.py.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}")

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    viability_cfg = config.get("viability", {})
    if not viability_cfg.get("enabled", False):
        print("Viability scoring is disabled (set [viability] enabled = true to enable).")
        sys.exit(0)

    viability_prompt = viability_cfg.get("prompt", "").strip()
    if not viability_prompt:
        sys.exit(
            "No viability prompt configured. "
            'Add a [viability] prompt = """...""" section to config.toml.'
        )

    # Optional geographic-preferences prompt. When set, each job gets a focused location
    # sub-call (assess_location_fit) whose verdict is fed to the main scorer in place of the
    # raw location list. Empty → geography stays inline in the main message (legacy path).
    location_prompt = viability_cfg.get("location_prompt", "").strip()
    # The location sub-call is a trivial classification, so default it to the cheap [ai]
    # model (haiku) even when the main scorer runs on a pricier model; override with
    # [viability] location_model if desired.
    geo_model = viability_cfg.get("location_model") or config.get("ai", {}).get("model") or DEFAULT_MODEL

    # api_key/model resolve from [viability] -> [ai] -> ANTHROPIC_API_KEY env.
    api_key, model = resolve_ai_settings(config, "viability")
    if not api_key:
        sys.exit(
            "No Anthropic API key found. Set api_key under [ai] (or [viability]) in "
            "config.toml, or the ANTHROPIC_API_KEY environment variable."
        )

    auto_skip          = viability_cfg.get("auto_skip", False)
    auto_skip_conf_raw = viability_cfg.get("auto_skip_confidence", "low").lower().strip()
    if auto_skip_conf_raw not in VIABILITY_RANK:
        sys.exit(
            f"Invalid auto_skip_confidence {auto_skip_conf_raw!r}. "
            "Must be 'low' or 'medium'."
        )
    auto_skip_threshold = VIABILITY_RANK[auto_skip_conf_raw]
    db_path = config.get("db_path", "jobs.db")

    # Fold location_prompt into the hash too: the geographic verdict the scorer sees depends
    # on it, so editing geography prefs must mark scores stale even when `prompt` is unchanged.
    current_hash = prompt_hash(viability_prompt, location_prompt)
    conn = open_db(db_path)

    # Build the selection WHERE clause (see build_selection for the full filter matrix).
    where, params = build_selection(
        current_hash=current_hash,
        force=args.force,
        all_statuses=args.all,
        early_stage=args.early_stage,
        autoskipped=args.autoskipped,
        since=args.since,
        previous_days=args.previous_days,
    )

    count = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
    start_time = datetime.now(timezone.utc)
    print(f"Starting viability scoring at {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    if args.dry_run:
        print(f"Would score {count} job(s) (run without --dry-run to proceed).")
        conn.close()
        return

    if count == 0:
        print("No jobs need scoring.")
        conn.close()
        return

    # We have real scoring work to do, so claim the shared writer lock now (after the
    # dry-run and count==0 early returns, which write nothing). This makes us mutually
    # exclusive with ingest and with any other rescore: if one is already running we
    # skip rather than (a) crash on the write lock waiting out an ingest's long
    # transaction, or (b) duplicate-score the same needs_rescored jobs as a sibling
    # rescore and waste tokens. Held until the process exits.
    _run_lock = acquire_run_lock(db_path, label="rescore")  # noqa: F841 (held for lifetime)

    print(f"Scoring {count} job(s) with model {model}...")

    client      = anthropic.Anthropic(api_key=api_key)
    check_model_currency(client, model)
    rows        = conn.execute(f"SELECT * FROM jobs {where}", params).fetchall()
    scored       = 0
    failed       = 0
    auto_skipped = 0
    promoted     = 0   # autoskipped jobs surfaced back to 'new' by --autoskipped re-evaluation
    tally: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    tok_input   = 0
    tok_output  = 0
    tok_write   = 0
    tok_read    = 0
    # Location sub-call tokens are tracked separately: it may run on a different (cheaper)
    # model than the main scorer, so its cost must be priced against geo_model, not model.
    geo_input   = 0
    geo_output  = 0
    geo_write   = 0
    geo_read    = 0
    # Cache geo verdicts within a run keyed on (locations, work arrangement): many jobs
    # share a location set (single-city employers, fully-remote roles), so this avoids
    # paying for an identical sub-call repeatedly. location_prompt is constant per run.
    geo_cache: dict[tuple, tuple[str | None, str | None]] = {}
    interactive = not args.verbose and sys.stdout.isatty()

    # Score each selected job, then for each success: persist the rating, record a
    # history entry, apply auto-skip / canonical-promotion side-effects, and tally
    # tokens. One commit per job so a mid-run interruption keeps completed work.
    for i, row in enumerate(rows, 1):
        title   = (row["title"]   or "(no title)").strip()
        company = (row["company"] or "(unknown company)").strip()
        label   = f"{title} at {company}"

        if args.verbose:
            print(f"  [{i}/{count}] {label}", end=" ", flush=True)
        elif interactive:
            # \r returns to start of line; \033[K erases to end of line.
            print(f"\r\033[K  [{i}/{count}] Scoring: {label}", end="", flush=True)

        job = dict(row)
        # Focused geographic pre-assessment (only when a location_prompt is configured).
        # Its verdict replaces the raw location list in the scorer message; on any failure
        # geo_note is None and the scorer falls back to the raw list. Cache by location set.
        gnote = None
        if location_prompt:
            geo_key = (tuple(_job_locations(job)), _work_arrangement(job))
            if geo_key in geo_cache:
                fit, match = geo_cache[geo_key]
            else:
                fit, match, gusage = assess_location_fit(client, location_prompt, job, model=geo_model)
                geo_cache[geo_key] = (fit, match)
                if gusage is not None:
                    geo_input  += getattr(gusage, "input_tokens",                0) or 0
                    geo_output += getattr(gusage, "output_tokens",               0) or 0
                    geo_write  += getattr(gusage, "cache_creation_input_tokens", 0) or 0
                    geo_read   += getattr(gusage, "cache_read_input_tokens",     0) or 0
            gnote = geo_note(fit, match)

        rating, reason, usage = score_job(client, viability_prompt, job, model=model, geo_note=gnote)

        if rating is None:
            if args.verbose:
                print("FAILED")
            failed += 1
        else:
            tally[rating] = tally.get(rating, 0) + 1
            did_autoskip = False
            did_promote  = False
            if usage is not None:
                tok_input  += getattr(usage, "input_tokens",                0) or 0
                tok_output += getattr(usage, "output_tokens",               0) or 0
                tok_write  += getattr(usage, "cache_creation_input_tokens", 0) or 0
                tok_read   += getattr(usage, "cache_read_input_tokens",     0) or 0
            conn.execute(
                "UPDATE jobs SET viability = ?, viability_reason = ?, "
                "viability_prompt_hash = ?, needs_rescored = 0 WHERE job_id = ?",
                (rating, reason, current_hash, row["job_id"]),
            )
            old_rating   = row["viability"]
            current_status = row["status"]
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if old_rating is None:
                append_history(conn, row["job_id"], {
                    "ts": ts, "event": "viability", "rating": rating, "reason": reason,
                })
            elif old_rating != rating:
                append_history(conn, row["job_id"], {
                    "ts": ts, "event": "rescore", "from": old_rating, "to": rating, "reason": reason,
                })
            # Note score changes in the log so a tail can spot records that moved. In
            # verbose mode the transition is shown on the per-job line below instead; in
            # interactive mode, clear the transient progress line first so it persists.
            change_note = rescore_change_note(label, old_rating, rating)
            if change_note and not args.verbose:
                print(f"\r\033[K{change_note}" if interactive else change_note, flush=True)

            # --autoskipped re-evaluation: an autoskipped job that now scores strictly
            # above the auto-skip threshold is surfaced back to 'new' for a fresh look;
            # the rest stay autoskipped. This is the whole point of the mode — after a
            # prompt change, recover jobs the old prompt had auto-skipped that would now
            # pass. First in the chain so it, not the canonical-promotion branch below,
            # governs autoskipped jobs in this mode.
            if args.autoskipped:
                if should_unskip(rating, auto_skip_threshold):
                    conn.execute(
                        "UPDATE jobs SET status = 'new' WHERE job_id = ?",
                        (row["job_id"],),
                    )
                    append_history(conn, row["job_id"], {
                        "ts": ts, "event": "status",
                        "from": current_status, "to": "new",
                        "note": f"re-evaluated above auto-skip threshold (viability: {rating})",
                    })
                    promoted   += 1
                    did_promote = True
                    # Log the promotion for a tailed viability.log (mirrors change_note).
                    note = f"  Promoted: {label} : {current_status} → new (viability: {rating})"
                    if not args.verbose:
                        print(f"\r\033[K{note}" if interactive else note, flush=True)

            # Auto-skip: if enabled and job is new/reviewing and score is at or below
            # the configured threshold, move it to autoskipped.
            elif (auto_skip
                    and current_status in ("new", "reviewing")
                    and VIABILITY_RANK.get(rating, -1) <= auto_skip_threshold):
                conn.execute(
                    "UPDATE jobs SET status = 'autoskipped' WHERE job_id = ?",
                    (row["job_id"],),
                )
                append_history(conn, row["job_id"], {
                    "ts": ts, "event": "status",
                    "from": current_status, "to": "autoskipped",
                    "note": f"auto-skipped by rescore (viability: {rating})",
                })
                auto_skipped += 1
                did_autoskip  = True

            # Canonical promotion. A skipped/autoskipped duplicate normally stays
            # hidden, but if a rescore makes it score strictly better than BOTH its
            # canonical and its own prior score, it may be the better representative of
            # the group and worth a fresh look. Surface it (→ new) unless auto-skip is
            # on and it's still at/below threshold (then just re-record it). The
            # `elif` means this never runs for a job already auto-skipped above.
            elif row["canonical_id"] and current_status in ("skipped", "autoskipped"):
                canonical = conn.execute(
                    "SELECT viability FROM jobs WHERE job_id = ?",
                    (row["canonical_id"],),
                ).fetchone()
                canon_viability = canonical["viability"] if canonical else None
                new_rank   = VIABILITY_RANK.get(rating, -1)
                prev_rank  = VIABILITY_RANK.get(old_rating or "", -1)
                canon_rank = VIABILITY_RANK.get(canon_viability or "", -1)
                if new_rank > canon_rank and new_rank > prev_rank:
                    if auto_skip and new_rank <= auto_skip_threshold:
                        # Score improved but still at/below threshold — update to
                        # autoskipped to record the re-evaluation.
                        conn.execute(
                            "UPDATE jobs SET status = 'autoskipped' WHERE job_id = ?",
                            (row["job_id"],),
                        )
                        append_history(conn, row["job_id"], {
                            "ts": ts, "event": "status",
                            "from": current_status, "to": "autoskipped",
                            "note": f"re-evaluated; still at/below auto-skip threshold (viability: {rating})",
                        })
                        if args.verbose:
                            print(f"    → autoskipped (re-evaluated, still {rating})")
                    else:
                        # Score exceeds threshold (or auto_skip is off) — surface for review.
                        conn.execute(
                            "UPDATE jobs SET status = 'new' WHERE job_id = ?",
                            (row["job_id"],),
                        )
                        append_history(conn, row["job_id"], {
                            "ts": ts, "event": "status",
                            "from": current_status, "to": "new",
                            "note": f"viability {rating!r} exceeds canonical {canon_viability!r}",
                        })
                        if args.verbose:
                            print(f"    → reset to new (scores higher than canonical {row['canonical_id']})")
            if args.verbose:
                # Show the transition when the score changed (e.g. "medium → high"),
                # else just the (unchanged / first-time) rating, plus any status side-effect.
                shown = f"{old_rating} → {rating}" if old_rating and old_rating != rating else rating
                if did_autoskip:
                    print(f"{shown} → autoskipped")
                elif did_promote:
                    print(f"{shown} → promoted to new")
                else:
                    print(shown)
            conn.commit()
            scored += 1

    if interactive:
        print()  # move past the progress line
    conn.close()
    elapsed        = (datetime.now(timezone.utc) - start_time).total_seconds()
    breakdown      = ", ".join(f"{r}: {tally[r]}" for r in ("high", "medium", "low") if tally.get(r))
    fail_note      = f", {failed} failed" if failed else ""
    autoskip_note  = f", {auto_skipped} auto-skipped" if auto_skipped else ""
    promoted_note  = f", {promoted} promoted to new" if promoted else ""
    # Per-job average over the whole selection (count = jobs processed), for spotting a
    # slow model/API at a glance in a tailed log.
    avg_note       = f" (avg {elapsed / count:.2f}s/job)" if count else ""
    # Lead with walltime (like ingest) so a tailed log surfaces slow runs at a glance.
    print(f"Done in {elapsed:.1f}s{avg_note}. {scored} job(s) scored{fail_note}{autoskip_note}{promoted_note}." + (f" ({breakdown})" if breakdown else ""))
    summary = format_token_summary(
        model, input=tok_input, output=tok_output,
        cache_write=tok_write, cache_read=tok_read,
    )
    if summary:
        print("  " + summary)
    # Separate line for the location sub-call: it may run on a different model, so its cost
    # is priced independently. Only shown when the sub-call actually ran (tokens spent).
    geo_summary = format_token_summary(
        geo_model, input=geo_input, output=geo_output,
        cache_write=geo_write, cache_read=geo_read,
    )
    if geo_summary:
        print(f"  Location pre-assessment ({geo_model}): {geo_summary}")
    print()


if __name__ == "__main__":
    main()
