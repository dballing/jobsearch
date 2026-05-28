#!/usr/bin/env python3
# requires Python 3.11+
"""Re-score job postings for viability using the Anthropic API.

Reads the [viability] section from config.toml. Requires ANTHROPIC_API_KEY
to be set in the environment.

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
import os
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from viability import prompt_hash, score_job

# Approximate pricing per token (USD). Update if Anthropic changes rates.
# Source: https://docs.anthropic.com/en/docs/about-claude/models/overview
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input":       1.00 / 1_000_000,
        "output":      5.00 / 1_000_000,
        "cache_write": 1.25 / 1_000_000,
        "cache_read":  0.10 / 1_000_000,
    },
    "claude-sonnet-4-5": {
        "input":       3.00 / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
        "cache_read":  0.30 / 1_000_000,
    },
    "claude-sonnet-4-6": {
        "input":       3.00 / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
        "cache_read":  0.30 / 1_000_000,
    },
}


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
    # Ensure viability columns exist (ingest.py adds them too, but be safe here).
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

    api_key = viability_cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "No Anthropic API key found. Set [viability] api_key in config.toml "
            "or the ANTHROPIC_API_KEY environment variable."
        )

    model   = viability_cfg.get("model", "claude-haiku-4-5")
    db_path = config.get("db_path", "jobs.db")

    current_hash = prompt_hash(viability_prompt)
    conn = open_db(db_path)

    # Build WHERE clause.
    conditions: list[str] = []
    params: list = []

    if not args.force:
        conditions.append("(viability_prompt_hash IS NULL OR viability_prompt_hash != ?)")
        params.append(current_hash)

    if args.all:
        pass  # No status filter.
    elif args.early_stage:
        conditions.append("status IN ('new', 'reviewing')")
    else:
        # Default: active jobs only (matches the UI "Active" filter).
        conditions.append("status NOT IN ('skipped', 'rejected', 'withdrawn', 'closed')")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
    start_time = datetime.now(timezone.utc)

    if args.dry_run:
        print(f"Would score {count} job(s) (run without --dry-run to proceed).")
        conn.close()
        return

    if count == 0:
        print("No jobs need scoring.")
        conn.close()
        return

    print(f"Starting viability scoring at {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Scoring {count} job(s) with model {model}...")

    client      = anthropic.Anthropic(api_key=api_key)
    check_model_currency(client, model)
    rows        = conn.execute(f"SELECT * FROM jobs {where}", params).fetchall()
    scored      = 0
    failed      = 0
    tally: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    tok_input   = 0
    tok_output  = 0
    tok_write   = 0
    tok_read    = 0
    interactive = not args.verbose and sys.stdout.isatty()

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
            if args.verbose:
                print(rating)
            tally[rating] = tally.get(rating, 0) + 1
            if usage is not None:
                tok_input  += getattr(usage, "input_tokens",                0) or 0
                tok_output += getattr(usage, "output_tokens",               0) or 0
                tok_write  += getattr(usage, "cache_creation_input_tokens", 0) or 0
                tok_read   += getattr(usage, "cache_read_input_tokens",     0) or 0
            conn.execute(
                "UPDATE jobs SET viability = ?, viability_reason = ?, "
                "viability_prompt_hash = ? WHERE job_id = ?",
                (rating, reason, current_hash, row["job_id"]),
            )
            conn.commit()
            scored += 1

    if interactive:
        print()  # move past the progress line
    conn.close()
    breakdown = ", ".join(f"{r}: {tally[r]}" for r in ("high", "medium", "low") if tally.get(r))
    fail_note = f", {failed} failed" if failed else ""
    print(f"Done. {scored} job(s) scored{fail_note}." + (f" ({breakdown})" if breakdown else ""))
    if tok_input or tok_output:
        tok_total = tok_input + tok_output + tok_write + tok_read
        detail = (
            f"{tok_input:,} input, {tok_output:,} output"
            + (f", {tok_write:,} cache write, {tok_read:,} cache read" if tok_write or tok_read else "")
        )
        parts = [f"{tok_total:,} tokens total ({detail})"]
        pricing = MODEL_PRICING.get(model)
        if pricing:
            cost = (
                tok_input  * pricing["input"]
              + tok_output * pricing["output"]
              + tok_write  * pricing["cache_write"]
              + tok_read   * pricing["cache_read"]
            )
            parts.append(f"estimated cost: ${cost:.4f}")
        print("  " + ", ".join(parts))
    print()


if __name__ == "__main__":
    main()
