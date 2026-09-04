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
  - **One deliberate exception:** `tests/test_pricing_live.py::test_live_pricing_matches_repo` fetches Anthropic's public pricing page (plain unauthenticated HTTP — no API key) and compares it against `ai_config.MODEL_PRICING` (via `pricing_check.py`) so a rate change surfaces within a day. It's network-tolerant by design — the markdown is cached on disk for 24h (system temp), a fetch failure falls back to a stale cache. Behavior: **fails only** on a genuine price mismatch (incl. a column shift that yields wrong numbers); **warns** (visible `UserWarning`, so the guard can't silently go dark) when the page was fetched but no known models parsed (probable table/format change), when the fetch returns an HTTP error like 404, or when a **permanent (301/308) redirect** was followed — all three mean "repoint `PRICING_URL`" and then it skips or uses the stale cache; **quietly** follows temporary redirects and **quietly skips** only on a transient connection failure (offline/DNS/timeout) with nothing cached. All the parsing/cache logic is unit-tested hermetically alongside it, so the suite stays green offline/CI. If this test fails, real pricing drifted — update `MODEL_PRICING`.
- **Shared sample data + HTML snapshots.** `tests/fixtures/sample_data.py` builds one deterministic, fully-fabricated dataset (every status, all sources/viability levels, a fuzzy group with overrides, a hotlisted employer, etc.) with *fixed* timestamps so rendered output is byte-stable. Use the `sample_db` (in-memory) or `sample_app_db` (app DB) fixtures for tests wanting realistic rows. `tests/test_snapshots.py` renders the real app against it and compares the jobs-table region (delimited by `<!-- snapshot:jobs-table:… -->` markers in `jobs.html`) to committed goldens in `tests/snapshots/`. A diff means the rendered output changed — decide if it's a bug or intended; if intended, **regenerate with `UPDATE_SNAPSHOTS=1 ./run_tests.sh`** and commit the updated goldens. Only the table region is snapshotted, so unrelated chrome/JS edits don't churn them.
- Confirm before committing/pushing unless the user has already said to.

## Commands

All `.sh` wrappers activate `.venv` and `cd` into the repo first, so they work from cron with an absolute path. Python 3.11+ required (`tomllib`).

```bash
./run_app.sh                 # Flask UI on http://127.0.0.1:5001 (auto-reload via --debug)
./run_app.sh --port 5002     # override port; FLASK_NO_DEBUG=1 disables the reloader
./ingest.sh                  # fetch new Apify run results into jobs.db
./ingest.sh --dry-run        # show pending run counts without fetching or writing
./ingest.sh --fixbasics      # migrate a config's bare top-level keys under [basics] and exit
./ingest.sh --seed <search>  # mark a search's current runs all-seen (no ingest) so it starts fresh from now — use when adding a schedule-scoped search to skip its backlog
./rescore_viability.sh       # AI-score jobs needing it (--dry-run, --force, --all, --early-stage, --autoskipped, --status, --current-viability, --since, --previous-days, --search <id>)
                             # scores EVERY configured search (each in its own child process); --search <id> narrows to one
./compare_scoring.sh         # read-only before/after check: score a recent sample with the HEAD prompt vs the working-tree prompt (--n, --previous-days, --since). Run before committing a prompt edit.
./import_linkedin.sh --status applied <url-or-id>...   # bulk-import known applications
./run_tests.sh               # pytest suite (hermetic except one cached live-pricing check; run before committing). Passes args through, e.g. -k config
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
| `config.py` | Central config loader (all entry points use it). Resolves the one canonical config into `AppConfig`/`Search`: Path A (no `[[searches]]`) = one implicit `__default__` search; Path B = shared globals + `[[searches]]` manifest, each search file merged with the globals. `[basics]` (bare-key fallback + `--fixbasics` migrator), `adopts_legacy`, per-search `config` dicts shape-identical to the old flat config. |
| `app.py` | Flask app: index (filter/group/sort), preview panel, status/override/notes/attachment/link routes, manual job add (`/jobs/manual`), stats, weekly contact report (`/report/weekly`). Holds the SQLite schema migration in `_migrate()`. The current search ("lens") comes from `_current_search_id()` (`?search=` / form / sticky cookie); every listing/stats query joins `job_search_state` via `_jss_join()`. |
| `ingest.py` | Apify ingestion: fetch runs, extract fields (linkedin + careersite extractors), fuzzy dedup, company-alias normalization, auto-ghost/close/reset, run summary. `DescriptionFormatter` wraps AI reformatting. |
| `viability.py` | Shared scoring helpers: `prompt_hash`, `score_job`. |
| `rescore_viability.py` | Batch AI viability scoring driver (selection logic, auto-skip, progress output). |
| `compare_scoring.py` | Read-only validation harness: scores a recent sample with the committed (`git HEAD`) prompt vs the working-tree prompt and reports rating drift (confusion matrix + nondeterminism baseline). Writes nothing. |
| `reformat.py` | AI description→Markdown reformatting + `content_preserved` integrity check. |
| `ai_config.py` | Shared `[ai]`/per-feature settings resolution + token-cost accounting (`MODEL_PRICING`). |
| `pricing_check.py` | Fetches Anthropic's public pricing page (24h disk cache) and diffs it against `MODEL_PRICING`; drives the network-tolerant drift test. |
| `runlock.py` | `acquire_run_lock()` — single shared writer lock serializing ingest vs. rescore. |
| `import_linkedin.py` | One-off import by LinkedIn URL/ID. |
| `templates/base.html`, `jobs.html` | Layout/navbar/offcanvas preview; main jobs table. |
| `templates/report_weekly.html` | Printable weekly job-hunt-contact report (Sun→Sat, local time), grouped by employer. |
| `docs/configuration.md`, `docs/features.md` | Full config and feature reference. Keep in sync with behavior changes. |

## Data & state

- `jobs.db` — the SQLite database (gitignored). `jobs.db-wal` / `jobs.db-shm` are transient WAL files; ignore them in status.
- `jobsbackup.db`, `jobbackup2.db` — manual backups (gitignored).
- `uploads/` — attachment files stored under UUID names; real filenames live in the DB. Back up separately from `jobs.db`.
- Schema changes happen in `app.py:_migrate()` (idempotent `ALTER TABLE` guards), run on app start. There are no migration files. A **shared helper `ingest.ensure_job_search_state()`** creates the `job_search_state` table + one-time backfill; it's called from all three migration paths (`ingest.open_db`, `app._migrate`, `rescore_viability.open_db`).
- **Per-lens state lives in `job_search_state(job_id, search_id, …)`** — status, viability (+reason/factors/hash/needs_rescored), the `salary_*_actual`/`geo_fit_actual` overrides, `applied_at`, and per-lens `history`. A row's existence IS the job's membership in that search. The matching columns on `jobs` are **dormant** (kept for rollback, migrated once to `__default__`); nothing reads them (a poison test guards this). Shared, objective posting facts stay on `jobs`: notes, attachments, `description_actual`, `company_actual`, `title_actual`, `work_arrangement_actual`.
- `config.toml` is gitignored; `config.toml.example` is the tracked template. `docs/configuration.md` documents every key.

## Multi-search ("lenses")

One app + one DB can run several distinct job searches, each with its own `[viability]` criteria and feeds, scored independently. **Path A** (no `[[searches]]`) is the single-search default — everything behaves exactly as before under the implicit `__default__` search. **Path B** adds a `[[searches]]` manifest in the canonical config (shared globals: `[basics]`, `[company_aliases]`, optionally `[ai]`/`[descriptions]`) pointing at per-search files (`[viability]`, `[[tasks]]`, `[labels]`); a search file may not redeclare a global stanza. See `config.py` and `docs/configuration.md`.

- **Everything per-lens is keyed by `search_id`.** `app.py` reads/writes the current lens (`_current_search_id()`); `ingest.ingest(search_id=…)` tags membership + status/history (one `./ingest.sh` loops all searches' tasks); `rescore_viability.py` scores **every** search by default — a Path-B run fans out one child process per search (the writer lock is process-scoped, so it can't loop in-process; `--search <id>` narrows to one and takes the single-search path directly). Automatic fuzzy dedup (`find_canonical(search_id=…)`) is restricted to the incoming search; **manual** merges (`_merge_group_into`/`promote_to_canonical`) may cross searches.
- **Single→multi transition:** mark one `[[searches]]` entry `adopts_legacy = true`; `ingest.adopt_legacy()` re-points the pre-split `__default__` state/history/`ingest_state` into it (gated, idempotent, run from the migration paths). At most one adopter; a loud warning if legacy rows go unadopted.
- **The `jss.*`-first SELECT trick:** listing queries select the per-lens columns FIRST (aliased to their canonical names) so they shadow the dormant `jobs.*` columns of the same name — `sqlite3.Row` and `dict(row)` both take the first match. `first_seen`/`applied_at` are on both tables, so they're qualified in joined queries.

## Things that bite

- **Serialized writers.** ingest and rescore both call `acquire_run_lock()` and hold it for the process lifetime. If one is running, the other skips rather than waiting/duplicating. Don't add a second writer path without taking this lock.
- **Line-buffer stdout in long-running scripts.** ingest and rescore call `sys.stdout.reconfigure(line_buffering=True)` early in `main()` so progress streams to a `tail -f`'d log (cron redirects make stdout block-buffered otherwise). New batch scripts should do the same.
- **AI features are optional and fail-soft.** No API key / disabled → reformatting falls back to the heuristic renderer, viability is skipped. Don't make AI a hard dependency.
- **Viability scoring version bump — order matters.** `viability.py:_SCORING_INPUT_VERSION` is folded into `prompt_hash`, so bumping it marks every existing score stale (they re-score on the next run). When you change what the model sees (the `_SYSTEM_BOILERPLATE`, `build_score_message`, the config `prompt`, or the location sub-call's inputs / `_GEO_SYSTEM`) *or how the stored rating is derived from it* (e.g. `clamp_viability_for_geo`, the caller-side override that forces a POOR-geography job to `low`) — all of which shape the stored score — **make the behavior change first, verify it, then increment the version last — and keep both in one commit.** Never let the higher version number go live ahead of the change: a score computed in that window gets stamped with the new hash but old behavior, so it looks current and never gets re-scored. This is a live risk because the app runs under `--debug` auto-reload, which can reload the module mid-edit.
- **Reformat integrity check.** `reformat.py:content_preserved` compares the alphanumeric *character* stream (whitespace-insensitive) so feed whitespace-mangling that the model repairs isn't counted as a content change; genuine add/drop/reword still fails. Threshold 0.97.
- **Fuzzy dedup** (`ingest.py:find_canonical`) is `SequenceMatcher`-based with a title pre-filter. It matches against *all* postings (roots and already-linked members), then resolves each hit to its canonical root — so an aggregator repost that rewrites the prose (near-0 overlap with the original ATS posting) still links via an identical sibling already in the group. Returning roots keeps the no-chain invariant; merging two matched roots also re-points the loser's members. Before the O(n·m) description compare it runs cheap pre-gates so it scales as the DB grows (~44× faster at 7.7k rows): a length-ratio bound, a word-shingle Jaccard floor (`_JACCARD_GATE`), and `quick_ratio` — all upper bounds or empirically well below the true-match floor, so they only prune non-matches. The autojunk-asymmetry reverse check runs only within `_REVERSE_MARGIN` of the threshold. These are tuned to be behavior-preserving (verified equal to the pre-gate version on real data); if you retune, re-validate rather than trusting the thresholds blindly.
- **Shared-across-group fields.** Notes, attachments, and salary overrides fan out to every current member of a fuzzy-match group, each keeping its own copy if the group later splits.
- **Per-schedule run scoping** (`schedule_name` on a `[[tasks]]` entry) lets two searches share one Apify task, split by which schedule triggered each run — for one scraper invoked by, say, a US and a Europe schedule with different location inputs. Two gotchas discovered against live Apify: (1) the run-**list** endpoint's `meta` carries only `origin`, **not** `scheduleId` — that field is on the per-run **detail** only, so `fetch_run_schedule_id` fetches it one run at a time, called *after* `runs_to_process` so only unseen runs are probed (never the whole backlog each cycle). (2) Apify stamps runs with the schedule's opaque **id**, but the config field is the human **name**; `fetch_schedules`/`resolve_schedule_id` translate name→id once per run. A run from another schedule is recorded via `mark_run_seen` (ingest_history only, not the `last_run` bookmark) so it isn't re-probed — safe because history is per-search, so the sibling search that owns that schedule still ingests it. **Backlog caveat:** the per-run detail fetch means a *brand-new* scoped search (no history) would probe the whole run backlog one-by-one — slow. `ingest.py --seed <search_id>` marks a search's current runs all-seen (list-only, no detail fetch) so it starts fresh from now; a scoped task also prints a `--seed` hint when it's about to probe more than `_SCHEDULE_PROBE_WARN` runs.
- `docs/screenshot.png` (in the README) is captured headless from `tests/snapshots/mock_screenshot.html`, which is **committed, app-generated, and a golden** (`tests/test_snapshots.py::test_snapshot_screenshot_mock`) — the full page rendered against the sample fixture. Unlike the table-region snapshots, this whole-page golden intentionally tracks chrome/JS too, since the screenshot must mirror the real UI. When the UI changes: `UPDATE_SNAPSHOTS=1 ./run_tests.sh` regenerates the mock, then `./make_screenshot.sh` re-captures the PNG (headless Chrome; `CHROME=` overridable). Commit both. Don't hand-edit the mock. `test_snapshot_screenshot_mock` / `test_screenshot_not_clipped` guard it: the latter decodes the PNG (pure stdlib) and fails if content reaches the bottom edge — i.e. the fixture outgrew the capture window and a row was clipped; fix by raising the `--window-size` height in `make_screenshot.sh`.

## Open tech debt

See `TODO.md`. Notably: pre-ingest DB backups, embedding-based semantic dedup, and an `archived` column for old closed jobs.
