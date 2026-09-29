#!/usr/bin/env python3
# requires Python 3.11+
"""Recompute historical spend-ledger costs from the current pricing timeline.

``cost_usd`` is a *cache*. Every AI row already stores the model and all four token counts, so
the cost is fully derivable — which means a price we had wrong (or learned about late) is a
repairable mistake rather than a permanent one, provided the pricing file says what the rate
was *at the time*. That's what the ``effective_start``/``effective_until`` periods in
model_pricing.json are for: this tool reprices each row at the rate effective at its own ``ts``.

The usual sequence after a price change you found out about late:

  1. add the new period to model_pricing.json (or model_pricing.local.json) with the real
     effective date from the announcement, leaving the old period ending on that date;
  2. run ``./reprice.sh --dry-run`` to see what would move;
  3. run ``./reprice.sh``.

Deliberately manual. Anthropic publishes *current* rates, not effective dates or a price
history, so the cutoff is a judgment call made from the announcement — not something this repo
can derive. Automating it would mean guessing the one fact we can't observe.

Apify rows ('apify' feature) are skipped: they carry a run charge that was never derived from a
token count, so there is nothing to recompute.

Usage:
    python3 reprice.py [--config PATH] [--dry-run] [--model MODEL] [--search SEARCH_ID]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from ai_config import (PricingError, describe_pricing_overrides,
                       override_coverage_warnings, pricing_for)
from config import ConfigError, load_config
from runlock import acquire_run_lock

# Rows whose cost never came from tokens, so there's nothing to recompute.
NON_TOKEN_FEATURES = ("apify",)


def rows_to_reprice(conn: sqlite3.Connection, *, model: str | None = None,
                    search_id: str | None = None) -> list[sqlite3.Row]:
    """Every token-derived ledger row, optionally narrowed to one model / one lens."""
    where = ["feature NOT IN (%s)" % ",".join("?" * len(NON_TOKEN_FEATURES))]
    params: list = list(NON_TOKEN_FEATURES)
    if model:
        where.append("model = ?")
        params.append(model)
    if search_id:
        where.append("search_id = ?")
        params.append(search_id)
    return conn.execute(
        "SELECT id, ts, search_id, model, input_tokens, output_tokens, cache_write_tokens, "
        "cache_read_tokens, cost_usd FROM spend_ledger WHERE " + " AND ".join(where)
        + " ORDER BY ts", params).fetchall()


def plan_reprice(rows) -> "tuple[list[tuple[int, float]], dict[tuple[str, str], int]]":
    """Split rows into (id, new_cost) updates and {(model, YYYY-MM): count} uncovered rows.

    A row whose model has no pricing period covering its timestamp is NOT silently left alone:
    "we don't know what this cost" is the same failure as not knowing today's price, which the
    startup gate already refuses to run on. Reporting it per model-month makes the missing
    window obvious at a glance, which is what you need to write the period that closes it.
    """
    updates: list[tuple[int, float]] = []
    uncovered: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        rates = pricing_for(row["model"], row["ts"])
        if rates is None:
            uncovered[(row["model"], row["ts"][:7])] += 1
            continue
        cost = (row["input_tokens"]       * rates["input"]
              + row["output_tokens"]      * rates["output"]
              + row["cache_write_tokens"] * rates["cache_write"]
              + row["cache_read_tokens"]  * rates["cache_read"])
        # Compare rounded to the cent-fraction the ledger meaningfully carries, so float noise
        # doesn't turn a no-op reprice into thousands of pointless writes.
        if row["cost_usd"] is None or round(cost, 10) != round(row["cost_usd"], 10):
            updates.append((row["id"], cost))
    return updates, dict(uncovered)


def summarize(rows, updates) -> "list[str]":
    """Per-(lens, model) before/after totals for the rows that would change."""
    changed = dict(updates)
    by_key: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    for row in rows:
        if row["id"] not in changed:
            continue
        key = (row["search_id"] or "—", row["model"])
        by_key[key][0] += row["cost_usd"] or 0.0
        by_key[key][1] += changed[row["id"]]
        by_key[key][2] += 1
    lines = []
    for (sid, model), (before, after, n) in sorted(by_key.items()):
        delta = after - before
        lines.append(f"  {sid:<28} {model:<22} {n:>6} rows  "
                     f"${before:>9.4f} → ${after:>9.4f}  ({delta:+.4f})")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change without writing")
    ap.add_argument("--model", help="only reprice rows billed to this model")
    ap.add_argument("--search", help="only reprice rows for this search id")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    try:
        app_cfg = load_config(Path(args.config))
    except ConfigError as exc:
        sys.exit(str(exc))
    try:
        for line in describe_pricing_overrides():
            print(f"NOTE: local pricing override — {line}")
        for line in override_coverage_warnings():
            print(f"WARNING: {line}", file=sys.stderr)
    except PricingError as exc:
        sys.exit(f"ERROR: {exc}")

    # Same writer lock as ingest/rescore: this is a bulk UPDATE against the same DB, and the
    # repo's rule is that no second writer path exists without it.
    acquire_run_lock(app_cfg.db_path, label="reprice")

    conn = sqlite3.connect(app_cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = rows_to_reprice(conn, model=args.model, search_id=args.search)
    except sqlite3.OperationalError:
        sys.exit("ERROR: no spend_ledger table in this database — nothing to reprice.")
    if not rows:
        print("No token-derived ledger rows matched; nothing to do.")
        return

    try:
        updates, uncovered = plan_reprice(rows)
    except PricingError as exc:
        sys.exit(f"ERROR: {exc}")

    if uncovered:
        # Refuse the whole run rather than reprice the covered rows and leave the rest stale:
        # a half-repriced ledger is harder to reason about than one that's uniformly old, and
        # the fix (add the missing period) is the same either way.
        print("ERROR: no pricing period covers these ledger rows, so they cannot be repriced:",
              file=sys.stderr)
        for (model, month), n in sorted(uncovered.items()):
            print(f"  {model} — {n} row(s) in {month}", file=sys.stderr)
        sys.exit(
            "\nAdd a pricing period covering those dates (consult the published pricing table "
            "for the historical rate). If the model is one you override locally, remember an "
            "override replaces the model's ENTIRE timeline — including the history the shipped "
            "table used to cover.")

    print(f"{len(rows)} token-derived row(s) examined; {len(updates)} would change.")
    if not updates:
        print("Every row already matches the current pricing timeline.")
        return
    print("\n  {:<28} {:<22} {:>6}  {:>10}   {:>10}".format("lens", "model", "rows", "before", "after"))
    for line in summarize(rows, updates):
        print(line)
    total_before = sum(r["cost_usd"] or 0.0 for r in rows if r["id"] in dict(updates))
    total_after  = sum(c for _, c in updates)
    print(f"\n  TOTAL  ${total_before:.4f} → ${total_after:.4f}  ({total_after - total_before:+.4f})")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return
    conn.executemany("UPDATE spend_ledger SET cost_usd = ? WHERE id = ?",
                     [(cost, rid) for rid, cost in updates])
    conn.commit()
    print(f"\nRepriced {len(updates)} row(s).")


if __name__ == "__main__":
    main()
