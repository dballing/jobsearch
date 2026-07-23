# To-Do

## Pre-ingest database backup

Before any ingest run that will actually do work (i.e. at least one task has pending runs),
back up the database using SQLite's `conn.backup()` API. Store dated backups in a `backups/`
subdirectory next to the DB. At most one backup per calendar day; configurable retention
count (default: 7 days). Discussed: 2026-05-28.

## Embedding-based semantic deduplication

Replace (or augment) the current `SequenceMatcher` description comparison in `find_canonical`
with dense vector embeddings and cosine similarity. Embeddings handle reworded/reformatted
descriptions and title variations that character-level similarity misses.

Preferred approach: local embeddings via `fastembed` or `sentence-transformers`
(e.g. `all-MiniLM-L6-v2`) to avoid API cost/dependency. Store as `embedding BLOB` in the
jobs table. Title pre-filter stays as a cheap first pass. Needs a one-time backfill pass for
existing rows (ties into the `--backfill` flag idea). Revisit once the current SequenceMatcher
approach has run long enough to reveal real-world misses. Discussed: 2026-05-28.

## Archival of old closed jobs

Add an `archived` boolean column rather than a separate database. Archiving sets
`archived = true`; all normal queries default to `WHERE archived IS NOT TRUE`. A UI toggle
removes that filter to show everything. No cross-DB canonical_id complications, no ATTACH
overhead, and SQLite handles the current scale (and years more of it) without any
performance concern. Discussed: 2026-05-28.

## Salary fallback: parse the description when the feed's AI misses it

*Only if this starts happening often — first observed 2026-07-23 (all four Intrado Life &
Safety postings), handled by manual override for now, so low priority.*

Apify's AI salary extraction sometimes returns null `ai_salary_*_value` fields (and a zeroed
`salary` object) even when the range is plainly stated in the description text
(e.g. "$67,000-70,000"). `extract_salary` (`ingest.py`) reads only those AI fields, so salary
ends up NULL. The designed remedy today is the manual [Salary override](docs/features.md#salary-override).

If it recurs enough to be worth automating: add a `salary_from_description` fallback that runs
*only* when the AI fields are empty (never overriding a real feed value). Regex the prose for a
clear annual range and feed it through the existing `_normalize_salary` path. Must be
conservative — free-text salary parsing is error-prone: distinguish annual from hourly, a real
comp band from an unrelated dollar amount (revenue, budget, "$1M in savings"), and handle
`$67,000-70,000` / `$67K–$70K` / "up to $70,000" forms. Well-tested against those cases before
trusting it. Revisit once there's evidence it's a pattern, not a one-off.
