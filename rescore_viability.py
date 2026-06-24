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
    python3 rescore_viability.py [--config PATH] [--dry-run] [--force] [--all]

Flags:
    --config PATH  Path to TOML config (default: config.toml).
    --dry-run      Print how many jobs would be scored without scoring them.
    --force        Rescore even jobs whose prompt hash already matches current.
    --all          Score all jobs regardless of status (default: active only).
    --early-stage  Score only new/reviewing jobs (narrower than default).
"""

import argparse
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from ai_config import format_token_summary, resolve_ai_settings
from ingest import append_history
from runlock import acquire_run_lock
from viability import prompt_hash, score_job

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
    parser.add_argument(
        "--all", action="store_true",
        help="Score all jobs regardless of status (default: active jobs only)",
    )
    parser.add_argument(
        "--early-stage", action="store_true",
        help="Score only new/reviewing jobs (narrower than the default active filter)",
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

    current_hash = prompt_hash(viability_prompt)
    conn = open_db(db_path)

    # Build the selection WHERE clause: which jobs need (re)scoring this run. Two
    # independent filters are AND-ed — a staleness/force filter (unless --force) and a
    # status filter (unless --all / --early-stage) — each with escapes so never-scored
    # and needs_rescored jobs are always included regardless of status.
    conditions: list[str] = []
    params: list = []

    if not args.force:
        # Always score jobs that have never been scored (viability IS NULL), regardless
        # of status — they may have inherited a status from a canonical without ever
        # getting their own evaluation.  Jobs flagged needs_rescored (a viability-
        # relevant field changed, e.g. a manual salary/company correction) are always
        # included too.  Everything else is filtered by prompt hash as usual.
        conditions.append(
            "(viability IS NULL OR viability_prompt_hash IS NULL "
            "OR viability_prompt_hash != ? OR needs_rescored = 1)"
        )
        params.append(current_hash)

    if args.all:
        pass  # No status filter.
    elif args.early_stage:
        conditions.append("status IN ('new', 'reviewing', 'deferred')")
    else:
        # Default: active jobs only (matches the UI "Active" filter).
        # NULL-viability and needs_rescored jobs are escaped here so a corrected
        # skipped/closed job gets re-evaluated (and can be un-skipped if it improves).
        conditions.append(
            "(status NOT IN ('skipped', 'autoskipped', 'rejected', 'withdrawn', 'ghosted', 'closed')"
            " OR viability IS NULL OR needs_rescored = 1)"
        )

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

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
    tally: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    tok_input   = 0
    tok_output  = 0
    tok_write   = 0
    tok_read    = 0
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

        rating, reason, usage = score_job(client, viability_prompt, dict(row), model=model)

        if rating is None:
            if args.verbose:
                print("FAILED")
            failed += 1
        else:
            tally[rating] = tally.get(rating, 0) + 1
            did_autoskip = False
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

            # Auto-skip: if enabled and job is new/reviewing and score is at or below
            # the configured threshold, move it to autoskipped.
            if (auto_skip
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
                print(f"{rating} → autoskipped" if did_autoskip else rating)
            conn.commit()
            scored += 1

    if interactive:
        print()  # move past the progress line
    conn.close()
    elapsed        = (datetime.now(timezone.utc) - start_time).total_seconds()
    breakdown      = ", ".join(f"{r}: {tally[r]}" for r in ("high", "medium", "low") if tally.get(r))
    fail_note      = f", {failed} failed" if failed else ""
    autoskip_note  = f", {auto_skipped} auto-skipped" if auto_skipped else ""
    # Lead with walltime (like ingest) so a tailed log surfaces slow runs at a glance.
    print(f"Done in {elapsed:.1f}s. {scored} job(s) scored{fail_note}{autoskip_note}." + (f" ({breakdown})" if breakdown else ""))
    summary = format_token_summary(
        model, input=tok_input, output=tok_output,
        cache_write=tok_write, cache_read=tok_read,
    )
    if summary:
        print("  " + summary)
    print()


if __name__ == "__main__":
    main()
