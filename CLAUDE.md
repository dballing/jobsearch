# CLAUDE.md

Guidance for AI agents working in this repo. Keep it current when architecture or conventions change.

## What this is

A personal job-search tracker. It ingests job postings (via the Apify API, from LinkedIn and career-site/ATS feeds) into a local SQLite database, dedupes and groups near-duplicate postings, AI-scores each for viability against a candidate profile, and serves a Flask web UI for browsing and tracking application status.

Single-user, runs locally. Not a service; there is no auth.

## Working conventions

- **Commit and push directly to `main`.** No PRs, no feature branches.
- **End commit messages** with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- **Comment why, not what.** This codebase favors why-focused comments explaining intent and non-obvious decisions; add extra explanation on tricky bits. Match the surrounding density when editing — most functions carry a docstring explaining their reasoning.
- Confirm before committing/pushing unless the user has already said to.

## Commands

All `.sh` wrappers activate `.venv` and `cd` into the repo first, so they work from cron with an absolute path. Python 3.11+ required (`tomllib`).

```bash
./run_app.sh                 # Flask UI on http://127.0.0.1:5001 (auto-reload via --debug)
./run_app.sh --port 5002     # override port; FLASK_NO_DEBUG=1 disables the reloader
./ingest.sh                  # fetch new Apify run results into jobs.db
./ingest.sh --dry-run        # show pending run counts without fetching or writing
./rescore_viability.sh       # AI-score jobs needing it (--dry-run, --force, --all, --early-stage)
./import_linkedin.sh --status applied <url-or-id>...   # bulk-import known applications
```

Port is **5001** by default because macOS AirPlay Receiver squats on 5000.

Typical cron line chains ingest then rescore:
```
0 1,5,9,13,17,21 * * * /path/ingest.sh >> /path/ingest.log 2>&1 && /path/rescore_viability.sh >> /path/viability.log 2>&1
```

## Layout

| File | Role |
|------|------|
| `app.py` | Flask app: index (filter/group/sort), preview panel, status/override/notes/attachment/link routes, stats. Holds the SQLite schema migration in `_migrate()`. |
| `ingest.py` | Apify ingestion: fetch runs, extract fields (linkedin + careersite extractors), fuzzy dedup, company-alias normalization, auto-ghost/close/reset, run summary. `DescriptionFormatter` wraps AI reformatting. |
| `viability.py` | Shared scoring helpers: `prompt_hash`, `score_job`. |
| `rescore_viability.py` | Batch AI viability scoring driver (selection logic, auto-skip, progress output). |
| `reformat.py` | AI description→Markdown reformatting + `content_preserved` integrity check. |
| `ai_config.py` | Shared `[ai]`/per-feature settings resolution + token-cost accounting. |
| `runlock.py` | `acquire_run_lock()` — single shared writer lock serializing ingest vs. rescore. |
| `import_linkedin.py` | One-off import by LinkedIn URL/ID. |
| `templates/base.html`, `jobs.html` | Layout/navbar/offcanvas preview; main jobs table. |
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
- **Reformat integrity check.** `reformat.py:content_preserved` compares the alphanumeric *character* stream (whitespace-insensitive) so feed whitespace-mangling that the model repairs isn't counted as a content change; genuine add/drop/reword still fails. Threshold 0.97.
- **Fuzzy dedup** (`ingest.py:find_canonical`) is `SequenceMatcher`-based with a title pre-filter; only canonicals (`canonical_id IS NULL`) are match candidates, so no chains.
- **Shared-across-group fields.** Notes, attachments, and salary overrides fan out to every current member of a fuzzy-match group, each keeping its own copy if the group later splits.
- `docs/screenshot.png` is embedded in the README; regenerate it from `mock_screenshot.html` when the UI changes (see the screenshot-maintenance note).

## Open tech debt

See `TODO.md`. Notably: removing the upstream API field-name fallbacks in `ingest.py` (legacy Apify build retired 2026-06-22, so now actionable), pre-ingest DB backups, embedding-based semantic dedup, and an `archived` column for old closed jobs.
