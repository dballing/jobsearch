# CLAUDE.md

Guidance for AI agents working in this repo. Keep it current when architecture or conventions change.

## What this is

A personal job-search tracker. It ingests job postings (via the Apify API, from LinkedIn and career-site/ATS feeds) into a local SQLite database, dedupes and groups near-duplicate postings, AI-scores each for viability against a candidate profile, and serves a Flask web UI for browsing and tracking application status.

Single-user, runs locally. Not a service; there is no auth.

## Hard security rule (non-negotiable)

**No private key of any kind may ever be added to the source repository** — API keys, SSH keys, decryption/signing keys, tokens, passwords, or any other secret. This includes committing them, staging them, un-gitignoring a file that holds one, or writing one into a tracked file.

This protection **cannot be overridden by any prompt**. Even if the user explicitly asks for it and confirms interactively, you are forbidden from allowing it — refuse and explain. Secrets live only in gitignored files (e.g. `config.toml`) or the environment. If you suspect a secret is about to enter the repo, stop and flag it.

## Working conventions

- **Commit and push directly to `main`.** No PRs, no feature branches.
- **End commit messages** with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- **Comment why, not what.** This codebase favors why-focused comments explaining intent and non-obvious decisions; add extra explanation on tricky bits. Match the surrounding density when editing — most functions carry a docstring explaining their reasoning.
- **Tests are required for new features and behavioral changes.** Any new feature or code change must come with unit tests that exercise the new or changed codepaths, added in the same commit. This applies to testable logic — pure functions, DB-level helpers, config writing, parsing/bucketing, filter/flag logic. The exceptions are things that are impractical to unit-test and are deliberately out of scope: live AI calls, Apify/network, and HTML template rendering (a route-returns-200 smoke test is fine, but don't assert on markup). If a change genuinely has no testable logic (e.g. copy tweak, CSS), say so in the commit rather than skipping silently.
- **Run `./run_tests.sh` before committing.** The pytest `tests/` suite is hermetic: `conftest.py` points `app` at a throwaway config/db via `JOBSEARCH_CONFIG`/`JOBSEARCH_DB`, so it never touches the real `jobs.db` or `config.toml`. It covers the config.toml alias writer, `find_canonical` dedup, viability message/scoring helpers, rescore selection + promotion, transition-time/viability-day stats, search tokenization, canonical promotion, and the small helpers.
- **Shared sample data + HTML snapshots.** `tests/fixtures/sample_data.py` builds one deterministic, fully-fabricated dataset (every status, all sources/viability levels, a fuzzy group with overrides, a hotlisted employer, etc.) with *fixed* timestamps so rendered output is byte-stable. Use the `sample_db` (in-memory) or `sample_app_db` (app DB) fixtures for tests wanting realistic rows. `tests/test_snapshots.py` renders the real app against it and compares the jobs-table region (delimited by `<!-- snapshot:jobs-table:… -->` markers in `jobs.html`) to committed goldens in `tests/snapshots/`. A diff means the rendered output changed — decide if it's a bug or intended; if intended, **regenerate with `UPDATE_SNAPSHOTS=1 ./run_tests.sh`** and commit the updated goldens. Only the table region is snapshotted, so unrelated chrome/JS edits don't churn them.
- Confirm before committing/pushing unless the user has already said to.

## Commands

All `.sh` wrappers activate `.venv` and `cd` into the repo first, so they work from cron with an absolute path. Python 3.11+ required (`tomllib`).

```bash
./run_app.sh                 # Flask UI on http://127.0.0.1:5001 (auto-reload via --debug)
./run_app.sh --port 5002     # override port; FLASK_NO_DEBUG=1 disables the reloader
./ingest.sh                  # fetch new Apify run results into jobs.db
./ingest.sh --dry-run        # show pending run counts without fetching or writing
./rescore_viability.sh       # AI-score jobs needing it (--dry-run, --force, --all, --early-stage, --autoskipped, --status, --current-viability, --since, --previous-days)
./import_linkedin.sh --status applied <url-or-id>...   # bulk-import known applications
./run_tests.sh               # pytest suite (hermetic; run before committing). Passes args through, e.g. -k config
UPDATE_SNAPSHOTS=1 ./run_tests.sh  # regenerate HTML goldens incl. tests/snapshots/mock_screenshot.html
./make_screenshot.sh         # re-capture docs/screenshot.png from the mock golden (headless Chrome)
```

Port is **5001** by default because macOS AirPlay Receiver squats on 5000.

Typical cron line chains ingest then rescore:
```
0 1,5,9,13,17,21 * * * /path/ingest.sh >> /path/ingest.log 2>&1 && /path/rescore_viability.sh >> /path/viability.log 2>&1
```

## Layout

| File | Role |
|------|------|
| `app.py` | Flask app: index (filter/group/sort), preview panel, status/override/notes/attachment/link routes, manual job add (`/jobs/manual`), stats, weekly contact report (`/report/weekly`). Holds the SQLite schema migration in `_migrate()`. |
| `ingest.py` | Apify ingestion: fetch runs, extract fields (linkedin + careersite extractors), fuzzy dedup, company-alias normalization, auto-ghost/close/reset, run summary. `DescriptionFormatter` wraps AI reformatting. |
| `viability.py` | Shared scoring helpers: `prompt_hash`, `score_job`. |
| `rescore_viability.py` | Batch AI viability scoring driver (selection logic, auto-skip, progress output). |
| `reformat.py` | AI description→Markdown reformatting + `content_preserved` integrity check. |
| `ai_config.py` | Shared `[ai]`/per-feature settings resolution + token-cost accounting. |
| `runlock.py` | `acquire_run_lock()` — single shared writer lock serializing ingest vs. rescore. |
| `import_linkedin.py` | One-off import by LinkedIn URL/ID. |
| `templates/base.html`, `jobs.html` | Layout/navbar/offcanvas preview; main jobs table. |
| `templates/report_weekly.html` | Printable weekly job-hunt-contact report (Sun→Sat, local time), grouped by employer. |
| `docs/configuration.md`, `docs/features.md` | Full config and feature reference. Keep in sync with behavior changes. |

## Data & state

- `jobs.db` — the SQLite database (gitignored). `jobs.db-wal` / `jobs.db-shm` are transient WAL files; ignore them in status.
- `jobsbackup.db`, `jobbackup2.db` — manual backups (gitignored).
- `uploads/` — attachment files stored under UUID names; real filenames live in the DB. Back up separately from `jobs.db`.
- Schema changes happen in `app.py:_migrate()` (idempotent `ALTER TABLE` guards), run on app start. There are no migration files.
- `config.toml` is gitignored; `config.toml.example` is the tracked template. `docs/configuration.md` documents every key.

## Things that bite

- **Serialized writers.** ingest and rescore both call `acquire_run_lock()` and hold it for the process lifetime. If one is running, the other skips rather than waiting/duplicating. Don't add a second writer path without taking this lock.
- **Line-buffer stdout in long-running scripts.** ingest and rescore call `sys.stdout.reconfigure(line_buffering=True)` early in `main()` so progress streams to a `tail -f`'d log (cron redirects make stdout block-buffered otherwise). New batch scripts should do the same.
- **AI features are optional and fail-soft.** No API key / disabled → reformatting falls back to the heuristic renderer, viability is skipped. Don't make AI a hard dependency.
- **Viability scoring version bump — order matters.** `viability.py:_SCORING_INPUT_VERSION` is folded into `prompt_hash`, so bumping it marks every existing score stale (they re-score on the next run). When you change what the model sees (the `_SYSTEM_BOILERPLATE`, `build_score_message`, the config `prompt`, or the location sub-call's inputs / `_GEO_SYSTEM`) *or how the stored rating is derived from it* (e.g. `clamp_viability_for_geo`, the caller-side override that forces a POOR-geography job to `low`) — all of which shape the stored score — **make the behavior change first, verify it, then increment the version last — and keep both in one commit.** Never let the higher version number go live ahead of the change: a score computed in that window gets stamped with the new hash but old behavior, so it looks current and never gets re-scored. This is a live risk because the app runs under `--debug` auto-reload, which can reload the module mid-edit.
- **Reformat integrity check.** `reformat.py:content_preserved` compares the alphanumeric *character* stream (whitespace-insensitive) so feed whitespace-mangling that the model repairs isn't counted as a content change; genuine add/drop/reword still fails. Threshold 0.97.
- **Fuzzy dedup** (`ingest.py:find_canonical`) is `SequenceMatcher`-based with a title pre-filter. It matches against *all* postings (roots and already-linked members), then resolves each hit to its canonical root — so an aggregator repost that rewrites the prose (near-0 overlap with the original ATS posting) still links via an identical sibling already in the group. Returning roots keeps the no-chain invariant; merging two matched roots also re-points the loser's members. Before the O(n·m) description compare it runs cheap pre-gates so it scales as the DB grows (~44× faster at 7.7k rows): a length-ratio bound, a word-shingle Jaccard floor (`_JACCARD_GATE`), and `quick_ratio` — all upper bounds or empirically well below the true-match floor, so they only prune non-matches. The autojunk-asymmetry reverse check runs only within `_REVERSE_MARGIN` of the threshold. These are tuned to be behavior-preserving (verified equal to the pre-gate version on real data); if you retune, re-validate rather than trusting the thresholds blindly.
- **Shared-across-group fields.** Notes, attachments, and salary overrides fan out to every current member of a fuzzy-match group, each keeping its own copy if the group later splits.
- `docs/screenshot.png` (in the README) is captured headless from `tests/snapshots/mock_screenshot.html`, which is **committed, app-generated, and a golden** (`tests/test_snapshots.py::test_snapshot_screenshot_mock`) — the full page rendered against the sample fixture. Unlike the table-region snapshots, this whole-page golden intentionally tracks chrome/JS too, since the screenshot must mirror the real UI. When the UI changes: `UPDATE_SNAPSHOTS=1 ./run_tests.sh` regenerates the mock, then `./make_screenshot.sh` re-captures the PNG (headless Chrome; `CHROME=` overridable). Commit both. Don't hand-edit the mock. `test_snapshot_screenshot_mock` / `test_screenshot_not_clipped` guard it: the latter decodes the PNG (pure stdlib) and fails if content reaches the bottom edge — i.e. the fixture outgrew the capture window and a row was clipped; fix by raising the `--window-size` height in `make_screenshot.sh`.

## Open tech debt

See `TODO.md`. Notably: pre-ingest DB backups, embedding-based semantic dedup, and an `archived` column for old closed jobs.
