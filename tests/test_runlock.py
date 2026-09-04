"""The single-writer lock keys on the *canonical* (symlink-resolved) DB path. This is the
regression guard for a race that crashed ingest mid-run: this repo is reachable both directly
and via a ~/src symlink into a Dropbox folder, and the lock used to key on os.path.abspath —
which does NOT resolve symlinks — so a run launched via the symlink and one via the real path
took different locks, failed to serialize, and both entered ingest's "new job" branch for the
same posting, one crashing on the jobs.job_id UNIQUE constraint. realpath collapses them."""
import os

import runlock


def test_symlink_and_target_share_one_lock(tmp_path):
    real = tmp_path / "real" / "jobs.db"
    real.parent.mkdir()
    real.write_text("")                       # realpath requires the target to exist to resolve
    link_dir = tmp_path / "link"
    os.symlink(real.parent, link_dir)         # ~/src-style symlink to the containing dir
    via_link = link_dir / "jobs.db"

    assert os.path.abspath(via_link) != os.path.abspath(real)      # the bug's precondition
    assert runlock.lock_path_for(str(via_link)) == runlock.lock_path_for(str(real))


def test_distinct_databases_get_distinct_locks(tmp_path):
    a = tmp_path / "a.db"; a.write_text("")
    b = tmp_path / "b.db"; b.write_text("")
    assert runlock.lock_path_for(str(a)) != runlock.lock_path_for(str(b))


def test_lock_path_is_in_tempdir_and_stable(tmp_path):
    import tempfile
    p = tmp_path / "jobs.db"; p.write_text("")
    lp = runlock.lock_path_for(str(p))
    assert lp.startswith(tempfile.gettempdir())
    assert lp == runlock.lock_path_for(str(p))     # deterministic
