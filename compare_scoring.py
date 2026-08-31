#!/usr/bin/env python3
# requires Python 3.11+
"""Before/after viability-scoring stability check — a read-only validation harness.

When the scoring prompt changes (a `_SYSTEM_BOILERPLATE` edit, a new output contract, etc.) we
want to know whether ratings actually drift before adopting it, on the premise that the *current*
prompt is scoring correctly. This tool samples recent jobs and scores each one TWICE on the
identical inputs — once with the committed prompt (from `git show HEAD:viability.py`) and once with
the working-tree prompt — so the only variable is the boilerplate under edit. It then reports how
often the two agree, a confusion matrix, and the stored-vs-old-fresh disagreement as a
model-nondeterminism baseline (drift that isn't attributable to the prompt).

It writes NOTHING to the database — it only reads jobs and makes AI calls — so it's safe to run
against the live DB mid-development. Run it BEFORE committing a prompt change (while HEAD still
holds the old prompt); iterate on the prompt until drift is acceptable.

Because the harness writes nothing, the reasons and factor breakdowns it computes are the ONLY
place to judge whether a rating *move* is an improvement (they never reach the DB). It therefore
prints the old/new reason and the new factor breakdown for every CHANGED job by default (or for
all jobs with --reasons), so the qualitative check can happen without persisting scores.

Usage:
    python3 compare_scoring.py [--config PATH] [--n N] [--tier low|medium|high]
        [--previous-days N | --since YYYY-MM-DD] [--job-id ID] [--reasons]

Flags:
    --config PATH       Path to TOML config (default: config.toml).
    --n N               Jobs to sample per current stored tier (high/medium/low). Default 10.
    --tier TIER         Only compare jobs currently stored at this tier (default: all three) —
                        handy when the churn is concentrated in one band (e.g. --tier medium).
    --previous-days N   Sample only jobs first ingested within the trailing N days (default 30).
    --since YYYY-MM-DD  Sample only jobs first ingested on/after this date (overrides --previous-days).
    --job-id ID         Compare exactly this one job, bypassing tier/date sampling. Each printed
                        line shows the job's id, so you can copy one and re-probe it later.
    --reasons           Print the old/new reason + new factor breakdown for EVERY job, not just
                        the ones whose rating changed.
"""

import argparse
import importlib.util
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

from config import ConfigError, load_config
from ai_config import (format_token_summary, resolve_ai_settings, resolve_effort,
                       resolve_geo_effort, resolve_geo_model)
import viability as viability_new
from viability import (assess_location_fit, clamp_viability_for_geo, geo_note,
                       manual_geo_verdict)

# The tiers we sample and compare across, ordered worst→best so a signed rank delta reads naturally.
TIERS = ("low", "medium", "high")
_RANK = {"low": 0, "medium": 1, "high": 2}


def tiers_to_compare(tier: str | None) -> tuple[str, ...]:
    """Which stored tiers to sample this run: just ``tier`` when --tier is given, else all of them.
    Pure, so it's unit-testable; keeps the 'one tier vs all' choice out of main()."""
    return (tier,) if tier else TIERS


def valid_since_date(value: str) -> str:
    """argparse type for --since: a YYYY-MM-DD calendar date (compared to date(first_seen))."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since expects YYYY-MM-DD, got {value!r}")
    return value


def positive_int(value: str) -> int:
    """argparse type for a whole number >= 1."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def build_sample_query(tier: str, n: int, *, since: str | None = None,
                       previous_days: int | None = None) -> tuple[str, list]:
    """Build the (SQL, params) selecting up to ``n`` recent jobs whose CURRENT stored score is
    ``tier``, in random order. Pure (no DB) so the selection is unit-testable.

    ``since`` (a YYYY-MM-DD lower bound on date(first_seen)) takes precedence over ``previous_days``
    (a trailing N*24h window); with neither, there's no date bound. ORDER BY RANDOM() gives a fresh
    sample each run — fine for a one-off validation tool (it's never snapshot-tested)."""
    conditions = ["viability = ?"]
    params: list = [tier]
    if since is not None:
        conditions.append("date(first_seen) >= ?")
        params.append(since)
    elif previous_days is not None:
        conditions.append("first_seen >= datetime('now', ?)")
        params.append(f"-{int(previous_days)} days")
    sql = "SELECT * FROM jobs WHERE " + " AND ".join(conditions) + " ORDER BY RANDOM() LIMIT ?"
    params.append(int(n))
    return sql, params


def summarize_pairs(pairs: "list[tuple[str, str]]") -> dict:
    """Tabulate (old_rating, new_rating) pairs into a comparison summary. Pure, so the confusion
    matrix / agreement math is unit-testable without any AI call.

    Returns {total, same, up, down, agreement_rate, matrix} where ``up``/``down`` count ratings the
    new prompt moved to a higher/lower tier, ``matrix[old][new]`` is the confusion count, and
    ``agreement_rate`` is same/total (0.0 when nothing comparable). Pairs with an unrecognized
    rating on either side (e.g. a failed score) are skipped, not counted."""
    matrix = {o: {n: 0 for n in TIERS} for o in TIERS}
    same = up = down = total = 0
    for old, new in pairs:
        if old not in _RANK or new not in _RANK:
            continue
        total += 1
        matrix[old][new] += 1
        delta = _RANK[new] - _RANK[old]
        if delta == 0:
            same += 1
        elif delta > 0:
            up += 1
        else:
            down += 1
    return {
        "total": total, "same": same, "up": up, "down": down,
        "agreement_rate": (same / total) if total else 0.0, "matrix": matrix,
    }


def load_old_viability(repo_root: Path):
    """Import the committed (HEAD) viability.py as a separate module so we can score with the OLD
    prompt while the working tree holds the NEW one. Returns the module, or exits if HEAD's copy
    can't be read (e.g. not a git repo, or viability.py isn't committed yet).

    The old module still does `from ai_config import …`; those resolve to the working-tree ai_config
    on sys.path, which is intended — only viability.py's own prompt/logic differs between the two."""
    try:
        src = subprocess.run(
            ["git", "show", "HEAD:viability.py"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.exit(f"Could not read HEAD:viability.py (need a git repo with viability.py committed): {e}")
    # Write to a temp file and import under a distinct name so it doesn't shadow the working-tree
    # module already imported as `viability` / `viability_new`.
    tmp = tempfile.NamedTemporaryFile("w", suffix="_viability_old.py", delete=False)
    tmp.write(src)
    tmp.close()
    spec = importlib.util.spec_from_file_location("viability_old", tmp.name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _score_with(module, prompt: str, job: dict, *, model: str, gnote: str | None,
                effort: str, fit: str | None, manual_poor: bool
                ) -> "tuple[str | None, str, list[dict] | None, object]":
    """Score one job with one viability module, applying the (unchanged) geo clamp identically, and
    return (rating, reason, factors, usage). Handles either return shape — the old HEAD module's
    3-tuple (rating, reason, usage) has no factors, the new 4-tuple (rating, reason, factors, usage)
    does. Uses the working-tree clamp for both modules — the clamp isn't what's under test, and
    keeping it identical isolates the difference to the main-scorer prompt. The clamped reason is
    returned so the printed detail matches what the scorer would actually store."""
    result = module.score_job(_SCORE_CLIENT, prompt, job, model=model,
                              geo_note=gnote, effort=effort)
    rating, reason = result[0], result[1]
    factors = result[2] if len(result) == 4 else None   # old HEAD (3-tuple) reports no factors
    usage = result[-1]
    rating, reason = clamp_viability_for_geo(fit, rating, reason, manual=manual_poor)
    return rating, reason, factors, usage


def _factor_detail_lines(factors: "list[dict] | None") -> list[str]:
    """Human-readable factor lines for the printed detail, fixed dimensions first (in canonical
    order) then extras — mirrors the preview panel's ordering. Pure, so it's unit-testable."""
    if not factors:
        return []
    order = {dim: i for i, dim in enumerate(viability_new.FACTOR_DIMENSIONS)}
    lines = []
    for f in sorted(factors, key=lambda f: order.get(f["dimension"], len(order))):
        score = f["score"]
        shown = int(score) if float(score).is_integer() else score
        sign = f"{'+' if score > 0 else ''}{shown}"
        note = f" — {f['note']}" if f.get("note") else ""
        lines.append(f"          {sign:>3}  {f['dimension']}{note}")
    return lines


def _factor_sum(factors: "list[dict] | None") -> "float | None":
    """Deterministic sum of the factor scores — COMPUTED HERE, not emitted by the model — as a QA
    aid for eyeballing whether the rating tracks the breakdown. It is deliberately NOT fed back to
    the scorer or used to derive the rating: the rating stays a holistic judgment (some negatives
    are near-vetoes a naive sum can't model), and asking the model to hit a numeric target would
    invite it to fudge the factor scores. Returns None when there are no factors. Pure."""
    if not factors:
        return None
    return sum(f["score"] for f in factors)


# Module-level client handle set in main() so _score_with can share one Anthropic client.
_SCORE_CLIENT: anthropic.Anthropic | None = None


def _tok(usage, name: str) -> int:
    return getattr(usage, name, 0) or 0 if usage is not None else 0


def main() -> None:
    global _SCORE_CLIENT
    parser = argparse.ArgumentParser(
        description="Compare working-tree vs committed (HEAD) viability scoring on a recent sample.")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config (default: config.toml)")
    parser.add_argument("--n", type=positive_int, default=10,
                        help="Jobs to sample per stored tier high/medium/low (default 10)")
    parser.add_argument("--tier", choices=TIERS,
                        help="Only compare jobs whose current stored score is this tier "
                             "(default: all three). Handy when the churn is concentrated in one band.")
    parser.add_argument("--job-id", dest="job_id", metavar="ID",
                        help="Compare exactly this one job (bypasses tier/date sampling). Use the "
                             "id printed on each line to re-probe a specific posting later.")
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument("--previous-days", dest="previous_days", type=positive_int, default=30,
                            metavar="N", help="Only jobs first ingested within the trailing N days (default 30)")
    date_group.add_argument("--since", type=valid_since_date, metavar="YYYY-MM-DD",
                            help="Only jobs first ingested on/after this date (overrides --previous-days)")
    parser.add_argument("--reasons", action="store_true",
                        help="Print the old/new reason and the new factor breakdown for EVERY job "
                             "(by default this detail is shown only for jobs whose rating changed)")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    config_path = Path(args.config)
    try:
        config = load_config(config_path).default_search().config
    except ConfigError as exc:
        sys.exit(str(exc))

    vcfg = config.get("viability", {}) or {}
    if not vcfg.get("enabled", False):
        sys.exit("Viability scoring is disabled ([viability] enabled = true) — nothing to compare.")
    viability_prompt = vcfg.get("prompt", "").strip()
    if not viability_prompt:
        sys.exit("No viability prompt configured — nothing to compare.")
    location_prompt = vcfg.get("location_prompt", "").strip()
    geo_uses_description = bool(vcfg.get("location_use_description", True))
    geo_model = resolve_geo_model(config, geo_uses_description)
    api_key, model = resolve_ai_settings(config, "viability")
    if not api_key:
        sys.exit("No Anthropic API key configured.")
    effort     = resolve_effort(config, "viability")[0]
    geo_effort = resolve_geo_effort(config, geo_uses_description)[0]
    db_path = config.get("db_path", "jobs.db")

    repo_root = Path(__file__).resolve().parent
    old_module = load_old_viability(repo_root)
    _SCORE_CLIENT = anthropic.Anthropic(api_key=api_key)

    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"Comparing working-tree vs HEAD viability scoring (model {model}).")
    if args.job_id:
        print(f"Comparing a single job: {args.job_id}\n")
    else:
        window = f"since {args.since}" if args.since else f"last {args.previous_days} days"
        print(f"Sampling up to {args.n} jobs per tier from the {window}.\n")

    pairs_new_vs_old: list[tuple[str, str]] = []   # (old-fresh, new-fresh) — the prompt effect
    pairs_stored_vs_old: list[tuple[str, str]] = []  # (stored, old-fresh) — nondeterminism baseline
    # Token tallies: main scorer (both old+new calls priced on `model`), geo sub-call on `geo_model`.
    main_tok = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    geo_tok  = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    # Build the batches to compare: one explicit job (--job-id), else a random sample per tier.
    if args.job_id:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (args.job_id,)).fetchone()
        if row is None:
            conn.close()
            sys.exit(f"No job found with job_id {args.job_id!r}.")
        batches = [(f"job {args.job_id}", [row])]
    else:
        batches = [
            (f"[{tier}]",
             conn.execute(*build_sample_query(
                 tier, args.n, since=args.since, previous_days=args.previous_days)).fetchall())
            for tier in tiers_to_compare(args.tier)
        ]

    for batch_label, rows in batches:
        if not rows:
            print(f"{batch_label} no jobs in the window.")
            continue
        print(f"{batch_label} {len(rows)} job(s):")
        for row in rows:
            job = dict(row)
            job_id = job.get("job_id")
            label = f"{(job.get('title') or '(no title)').strip()} @ {(job.get('company') or '?').strip()}"

            # Resolve the geographic verdict ONCE and feed the same gnote to both scorers, so the
            # only variable is the main-scorer prompt (and we pay for one geo call, not two).
            fit, gnote, manual_poor = manual_geo_verdict(job)
            if fit is None and location_prompt:
                fit, match, gusage = assess_location_fit(
                    _SCORE_CLIENT, location_prompt, job, model=geo_model,
                    include_description=geo_uses_description, effort=geo_effort)
                gnote = geo_note(fit, match)
                for k, attr in (("input", "input_tokens"), ("output", "output_tokens"),
                                ("cache_write", "cache_creation_input_tokens"),
                                ("cache_read", "cache_read_input_tokens")):
                    geo_tok[k] += _tok(gusage, attr)

            old_rating, old_reason, _old_factors, old_usage = _score_with(
                old_module, viability_prompt, job, model=model, gnote=gnote,
                effort=effort, fit=fit, manual_poor=manual_poor)
            new_rating, new_reason, new_factors, new_usage = _score_with(
                viability_new, viability_prompt, job, model=model, gnote=gnote,
                effort=effort, fit=fit, manual_poor=manual_poor)
            for usage in (old_usage, new_usage):
                for k, attr in (("input", "input_tokens"), ("output", "output_tokens"),
                                ("cache_write", "cache_creation_input_tokens"),
                                ("cache_read", "cache_read_input_tokens")):
                    main_tok[k] += _tok(usage, attr)

            stored = job.get("viability")
            if old_rating and new_rating:
                pairs_new_vs_old.append((old_rating, new_rating))
            if stored and old_rating:
                pairs_stored_vs_old.append((stored, old_rating))

            changed = old_rating != new_rating
            marker = "  <-- CHANGED" if changed else ""
            # The code-computed (not model-emitted) sum of the NEW factor scores, shown on every
            # line so the sum-vs-rating relationship is eyeballable across a whole run without
            # --reasons. It is purely observational — never fed to the scorer or used to derive the
            # rating (see _factor_sum). None when the new scoring produced no factors.
            total = _factor_sum(new_factors)
            sum_str = f"{'+' if total > 0 else ''}{total:g}" if total is not None else "-"
            # Print the job_id on every line so it can be copied and re-probed later with --job-id.
            print(f"    {label[:50]:<50} stored={stored or '-':<6} "
                  f"old={old_rating or 'FAIL':<6} new={new_rating or 'FAIL':<6} sum={sum_str:<4} "
                  f"id={job_id}{marker}")
            # The harness writes nothing, so the reasons/factors it computed are the ONLY place to
            # judge whether a move is an improvement. Print them for changed jobs (always) or all
            # jobs (--reasons), so the qualitative check doesn't need the DB.
            if changed or args.reasons:
                if old_reason:
                    print(f"        old reason: {old_reason}")
                if new_reason:
                    print(f"        new reason: {new_reason}")
                for line in _factor_detail_lines(new_factors):
                    print(line)
        print()

    conn.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    summary = summarize_pairs(pairs_new_vs_old)
    baseline = summarize_pairs(pairs_stored_vs_old)
    print("=" * 72)
    print("NEW vs OLD prompt (both scored fresh on the same jobs — this is the prompt's effect):")
    if summary["total"]:
        print(f"  {summary['same']}/{summary['total']} unchanged "
              f"({summary['agreement_rate']*100:.0f}% agreement); "
              f"{summary['up']} moved up, {summary['down']} moved down.")
        print("  confusion matrix (rows = old, cols = new):")
        header = "            " + "".join(f"{t:>9}" for t in TIERS)
        print(header)
        for o in TIERS:
            print(f"    old {o:<7}" + "".join(f"{summary['matrix'][o][n]:>9}" for n in TIERS))
    else:
        print("  no comparable pairs (both scorings failed on every sampled job).")
    print()
    print("STORED vs OLD-fresh (same prompt, re-scored) — model-nondeterminism baseline:")
    if baseline["total"]:
        print(f"  {baseline['same']}/{baseline['total']} unchanged "
              f"({baseline['agreement_rate']*100:.0f}% agreement). Drift above this in the block "
              f"above is what the prompt change is actually responsible for.")
    else:
        print("  no comparable pairs.")
    print()

    main_summary = format_token_summary(model, **main_tok)
    if main_summary:
        print(f"  Main scorer ({model}, old+new calls): {main_summary}")
    geo_summary = format_token_summary(geo_model, **geo_tok)
    if geo_summary:
        print(f"  Location pre-assessment ({geo_model}): {geo_summary}")
    print("\n(No changes were written to the database.)")


if __name__ == "__main__":
    main()
