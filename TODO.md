# To-Do

## Pre-ingest database backup

Before any ingest run that will actually do work (i.e. at least one task has pending runs),
back up the database using SQLite's `conn.backup()` API. Store dated backups in a `backups/`
subdirectory next to the DB. At most one backup per calendar day; configurable retention
count (default: 7 days). Discussed: 2026-05-28.

## Archival of old closed jobs

Add an `archived` boolean column rather than a separate database. Archiving sets
`archived = true`; all normal queries default to `WHERE archived IS NOT TRUE`. A UI toggle
removes that filter to show everything. No cross-DB canonical_id complications, no ATTACH
overhead, and SQLite handles the current scale (and years more of it) without any
performance concern. Discussed: 2026-05-28.
