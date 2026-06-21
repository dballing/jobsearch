#!/usr/bin/env python3
# requires Python 3.11+
"""Single-writer advisory lock shared by the DB-writing cron jobs (ingest, rescore).

Why this exists: ingest.py commits its whole run in one transaction (ingest.py's
final conn.commit()), so it holds SQLite's write lock continuously for the entire
run — including the per-job AI reformat calls, which can stretch a run well past the
5s busy_timeout. If a rescore (or a second rescore) tries to write during that
window it waits out busy_timeout and then raises an *uncaught*
sqlite3.OperationalError ("database is locked"), crashing the run mid-way; two
overlapping rescores instead burn duplicate Anthropic tokens re-scoring the same
needs_rescored jobs. WAL keeps the web UI's *reads* unaffected either way, but the
writers must not collide.

We serialize them with a non-blocking fcntl.flock on one lock file keyed to the
database path. The lock is advisory and process-scoped: it releases automatically
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


def acquire_run_lock(db_path: str, *, label: str = "run"):
    """Take the exclusive ingest/rescore lock, or exit(0) if another run holds it.

    The lock is non-blocking: a second runner does NOT queue behind the first — it
    prints a notice and exits cleanly. That's the right behavior for a cron cadence,
    since the skipped work is harmless (needs_rescored / new-job state persists and is
    picked up by the next run) and skipping avoids a pile-up of waiting processes.

    Args:
        db_path: the SQLite path; the lock file is derived from its absolute path so
            ingest and rescore against the *same* DB share one lock (mutually
            exclusive), while a different DB gets its own lock.
        label:   what to call this run in the skip message (e.g. "ingest", "rescore").

    Returns:
        The open file object holding the lock. The caller MUST keep a reference for
        the process lifetime — the lock is released when this fd is closed/GC'd or the
        process exits. (We never explicitly release it; process exit is the signal.)
    """
    # Key the lock on the absolute DB path so multiple checkouts / databases don't
    # share a lock, but ingest and rescore of one DB always do. Hashed to keep the
    # filename short and free of path separators.
    key = hashlib.sha256(os.path.abspath(db_path).encode()).hexdigest()[:16]
    # Live in the system temp dir, deliberately NOT next to the DB: the DB here sits in
    # a Dropbox-synced folder, and we don't want a churning lock file syncing around.
    lock_path = os.path.join(tempfile.gettempdir(), f"jobsearch-{key}.lock")

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
