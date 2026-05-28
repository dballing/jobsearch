# To-Do

## Manual canonicalization from the UI

Sometimes fuzzy matching fails to group postings that a human can clearly
see are the same role (description just different enough to fall below the
threshold). Current workaround: skip the unmatched duplicates — they stay
out of the way and future duplicates inherit `skipped` via status
inheritance. This is acceptable in practice but means separate "pockets"
of skipped entries exist for what is logically one job.

Longer-term: a "Link to…" button in the job row that lets the user search
for and attach a posting to an existing canonical. Makes the correct action
(canonicalize) as cheap as the workaround (skip). Discussed: 2026-05-28.

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

## Auto-skip based on viability score

Once confidence in the viability scoring is high (i.e. the candidate prompt is
well-tuned and producing reliable results), add a config option to automatically
set low- or medium-viability jobs to `skipped` during a rescore run. Configurable
threshold (e.g. `auto_skip_at = "low"` or `"medium"`). Should be opt-in and
explicit — never on by default. Discussed: 2026-05-28.

## Archival of old closed jobs

Add an `archived` boolean column rather than a separate database. Archiving sets
`archived = true`; all normal queries default to `WHERE archived IS NOT TRUE`. A UI toggle
removes that filter to show everything. No cross-DB canonical_id complications, no ATTACH
overhead, and SQLite handles the current scale (and years more of it) without any
performance concern. Discussed: 2026-05-28.
