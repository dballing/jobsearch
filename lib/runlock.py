#!/usr/bin/env python3
# requires Python 3.11+
"""Single-writer advisory lock shared by the DB-writing cron jobs (ingest, rescore).

Why this exists: the writers must not collide. ingest commits per item (releasing
SQLite's write lock between rows, so the web UI isn't starved during the per-job AI
reformat calls), which means the SQLite lock alone no longer serializes a whole run
against a concurrent writer — this process-level lock is the *only* thing that does.
Two overlapping ingests would both take the "new job" branch for the same posting and
race to INSERT it, one crashing on the jobs.job_id UNIQUE constraint; two overlapping
rescores would burn duplicate Anthropic tokens re-scoring the same needs_rescored
jobs. WAL keeps the web UI's *reads* unaffected either way.

We serialize them with a non-blocking fcntl.flock on one lock file keyed to the
database file. The lock is advisory and process-scoped: it releases automatically
when the holding process exits (or its fd closes), so a crashed run can't wedge the
lock. macOS ships fcntl.flock but NOT the `flock(1)` shell tool, so doing this in
Python keeps the cron lines portable.

Usage — keep the returned handle alive for the whole process:

    from runlock import acquire_run_lock
    _lock = acquire_run_lock(db_path, label="ingest")   # exits(0) if another run holds it
"""

import fcntl
import hashlib
import os
import sys
import tempfile


def lock_path_for(db_path: str) -> str:
    """The lock file that guards writes to ``db_path``.

    Keyed on the *canonical* (symlink-resolved) path so two spellings of the same
    physical DB share one lock, while genuinely different DB files get their own.
    This uses os.path.realpath, NOT abspath: this repo is reachable both directly and
    via a ~/src symlink into the Dropbox folder, and abspath does not resolve symlinks
    — so a run launched via the symlink and one via the real path would compute
    DIFFERENT keys, take DIFFERENT locks, and fail to serialize (the exact race that
    produced a jobs.job_id UNIQUE-constraint crash mid-ingest). realpath collapses both
    to the same key. Hashed to keep the filename short and free of path separators;
    lives in the system temp dir (deliberately NOT next to the DB, which sits in a
    Dropbox-synced folder we don't want a churning lock file syncing around)."""
    key = hashlib.sha256(os.path.realpath(db_path).encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"jobsearch-{key}.lock")


def acquire_run_lock(db_path: str, *, label: str = "run"):
    """Take the exclusive ingest/rescore lock, or exit(0) if another run holds it.

    The lock is non-blocking: a second runner does NOT queue behind the first — it
    prints a notice and exits cleanly. That's the right behavior for a cron cadence,
    since the skipped work is harmless (needs_rescored / new-job state persists and is
    picked up by the next run) and skipping avoids a pile-up of waiting processes.

    Args:
        db_path: the SQLite path; the lock file is derived from its canonical
            (symlink-resolved) path so ingest and rescore against the *same* physical
            DB share one lock (mutually exclusive), while a different DB gets its own
            lock. See lock_path_for.
        label:   what to call this run in the skip message (e.g. "ingest", "rescore").

    Returns:
        The open file object holding the lock. The caller MUST keep a reference for
        the process lifetime — the lock is released when this fd is closed/GC'd or the
        process exits. (We never explicitly release it; process exit is the signal.)
    """
    lock_path = lock_path_for(db_path)

    fh = open(lock_path, "w")
    try:
        # LOCK_NB → fail immediately instead of blocking if another run holds it.
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another ingest/rescore is in progress. Skip this run rather than crash or
        # queue. exit(0) (not nonzero) so cron / logs don't treat a deliberate skip as
        # a failure.
        print(f"Another ingest/rescore run is in progress; skipping this {label}.")
        fh.close()
        sys.exit(0)
    return fh
