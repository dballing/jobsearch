"""Phase 1 multi-search DB layer: the per-(job, search) state table, membership, the
search-restricted automatic dedup, legacy adoption, run-tracking keys, and the dormant-column
poison guard. Hermetic — in-memory DBs and the app test client against the sample fixture."""
import json
import sqlite3
import sys

import pytest

import ingest
from ingest import DEFAULT_SEARCH_ID, adopt_legacy, ensure_job_search_state, find_canonical, state_key


# A description long/identical enough to clear find_canonical's shingle + ratio gates.
_DESC = ("We are seeking a senior platform engineer to design, build, and operate our cloud "
         "infrastructure across multiple regions, mentoring a small team and owning reliability "
         "end to end for a fast growing product used by millions of people worldwide today.")


def _job(conn, job_id, search_id, *, title="Senior Platform Engineer", company="Acme",
         description=_DESC, canonical_id=None, status="new", viability=None):
    """Insert a shared jobs row + one per-lens state row under `search_id`."""
    conn.execute(
        "INSERT OR IGNORE INTO jobs (job_id, title, company, job_description, canonical_id, raw) "
        "VALUES (?, ?, ?, ?, ?, '{}')",
        (job_id, title, company, description, canonical_id),
    )
    conn.execute(
        "INSERT INTO job_search_state (job_id, search_id, status, viability) VALUES (?, ?, ?, ?)",
        (job_id, search_id, status, viability),
    )


# ── state_key ────────────────────────────────────────────────────────────────

def test_state_key_bare_for_default_namespaced_otherwise():
    assert state_key(DEFAULT_SEARCH_ID, "my-task") == "my-task"
    assert state_key("tpm", "my-task") == "tpm:my-task"


# ── runs_to_process: rename-proof, per-search run-selection ──────────────────

def _record_run(conn, search_id, task_name, run_id):
    """Record that `search_id` ingested `run_id` under `task_name` — mirrors record_state's
    ingest_history write, using the same namespaced key runs_to_process reads back."""
    conn.execute(
        "INSERT INTO ingest_history (task_name, run_id, run_at, inserted, updated, unchanged) "
        "VALUES (?, ?, '2026-01-01T00:00:00Z', 0, 0, 1)",
        (state_key(search_id, task_name), run_id),
    )


def _runs(*ids):
    return [{"id": i, "startedAt": "2026-01-01T00:00:00Z"} for i in ids]


def test_runs_to_process_new_search_takes_full_backlog(jobs_db):
    # No history for this search → nothing filtered, whole backlog returned.
    pending = ingest.runs_to_process(jobs_db, "tpm", _runs("r1", "r2", "r3"))
    assert [r["id"] for r in pending] == ["r1", "r2", "r3"]


def test_runs_to_process_skips_already_ingested_runs(jobs_db):
    _record_run(jobs_db, "tpm", "task-a", "r1")
    _record_run(jobs_db, "tpm", "task-a", "r2")
    pending = ingest.runs_to_process(jobs_db, "tpm", _runs("r1", "r2", "r3"))
    assert [r["id"] for r in pending] == ["r3"]


def test_runs_to_process_is_rename_proof(jobs_db):
    # Runs first ingested under the OLD task name are recognized when the same run IDs are
    # later fetched under the NEW name — selection keys on run_id, not task name.
    _record_run(jobs_db, "tpm", "old-name", "r1")
    _record_run(jobs_db, "tpm", "old-name", "r2")
    # Apify task renamed old-name → new-name; same runs come back, plus one new run.
    pending = ingest.runs_to_process(jobs_db, "tpm", _runs("r1", "r2", "r3"))
    assert [r["id"] for r in pending] == ["r3"]


def test_runs_to_process_new_task_reusing_old_name_processes_its_fresh_runs(jobs_db):
    # A→B rename, then a brand-new Apify task reuses name A. The new task mints fresh run IDs,
    # so they're unseen and get ingested; the stale old rows don't false-match (unique IDs).
    _record_run(jobs_db, "tpm", "shared-name", "old-run")
    pending = ingest.runs_to_process(jobs_db, "tpm", _runs("new-run-1", "new-run-2"))
    assert [r["id"] for r in pending] == ["new-run-1", "new-run-2"]


def test_runs_to_process_is_per_search_preserving_task_reuse(jobs_db):
    # Two searches sharing one Apify task track it independently: a run consumed by search A
    # is still pending for search B.
    _record_run(jobs_db, "tpm", "shared-task", "r1")
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "tpm", _runs("r1"))] == []
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "director", _runs("r1"))] == ["r1"]


def test_runs_to_process_default_search_scope_excludes_namespaced_rows(jobs_db):
    # The default search's bare keys must not pick up a named search's namespaced runs.
    _record_run(jobs_db, "tpm", "task", "r1")                 # -> "tpm:task"
    _record_run(jobs_db, DEFAULT_SEARCH_ID, "task", "r2")     # -> bare "task"
    pending = ingest.runs_to_process(jobs_db, DEFAULT_SEARCH_ID, _runs("r1", "r2"))
    assert [r["id"] for r in pending] == ["r1"]               # r2 seen, r1 belongs to tpm


def test_runs_to_process_underscore_search_id_does_not_overmatch(jobs_db):
    # '_' is a LIKE single-char wildcard; an unescaped prefix for "mid_atlantic" would match
    # a sibling like "midXatlantic:...". Escaping keeps the sibling's run out of scope.
    _record_run(jobs_db, "midXatlantic", "task", "r1")
    pending = ingest.runs_to_process(jobs_db, "mid_atlantic", _runs("r1"))
    assert [r["id"] for r in pending] == ["r1"]               # not falsely marked seen


# ── filter_runs_by_schedule: one Apify task, split by triggering schedule ────

def _sched_runs(*pairs):
    """Build run dicts carrying a meta.scheduleId. Each pair is (run_id, schedule_id);
    a schedule_id of None models a manual/API run whose meta omits the key entirely."""
    out = []
    for run_id, sched in pairs:
        meta = {"origin": "SCHEDULER", "scheduleId": sched} if sched is not None else {"origin": "API"}
        out.append({"id": run_id, "startedAt": "2026-01-01T00:00:00Z", "meta": meta})
    return out


def test_run_schedule_id_reads_meta():
    us, api = _sched_runs(("r1", "sched-us"), ("r2", None))
    assert ingest.run_schedule_id(us) == "sched-us"
    assert ingest.run_schedule_id(api) is None            # meta present but no scheduleId key
    assert ingest.run_schedule_id({"id": "r3"}) is None   # meta absent entirely (defensive)


def test_filter_runs_by_schedule_none_is_passthrough():
    # A task with no schedule_id configured (the single-schedule default) ingests every run.
    runs = _sched_runs(("r1", "sched-us"), ("r2", "sched-eu"), ("r3", None))
    assert ingest.filter_runs_by_schedule(runs, None) == runs


def test_filter_runs_by_schedule_keeps_only_matching_schedule():
    # One shared Apify task, two schedules (US + Europe) mixed in one run list: each search
    # scopes to its own schedule and sees only its own runs.
    runs = _sched_runs(("us1", "sched-us"), ("eu1", "sched-eu"), ("us2", "sched-us"))
    assert [r["id"] for r in ingest.filter_runs_by_schedule(runs, "sched-us")] == ["us1", "us2"]
    assert [r["id"] for r in ingest.filter_runs_by_schedule(runs, "sched-eu")] == ["eu1"]


def test_filter_runs_by_schedule_drops_non_schedule_runs():
    # A manual/API run (no scheduleId) never matches a configured schedule and is dropped —
    # a schedule-scoped task deliberately ignores ad-hoc runs.
    runs = _sched_runs(("us1", "sched-us"), ("adhoc", None))
    assert [r["id"] for r in ingest.filter_runs_by_schedule(runs, "sched-us")] == ["us1"]


# ── resolve_schedule_id: config's schedule_name (or id) → opaque id ──────────

_SCHEDULES = [
    {"id": "SCHEDusOPAQUE", "name": "job-search-usa"},
    {"id": "SCHEDeuOPAQUE", "name": "job-search-europe"},
]


def test_resolve_schedule_id_by_name():
    # The config field holds the console NAME; resolution returns the opaque id runs carry.
    assert ingest.resolve_schedule_id("job-search-usa", _SCHEDULES) == "SCHEDusOPAQUE"
    assert ingest.resolve_schedule_id("job-search-europe", _SCHEDULES) == "SCHEDeuOPAQUE"


def test_resolve_schedule_id_passes_through_an_opaque_id():
    # An id written directly in config is accepted as-is (matches meta.scheduleId already).
    assert ingest.resolve_schedule_id("SCHEDusOPAQUE", _SCHEDULES) == "SCHEDusOPAQUE"


def test_resolve_schedule_id_unknown_returns_none():
    # A typo'd name matches neither id nor name → None (caller warns + lists known schedules).
    assert ingest.resolve_schedule_id("job-search-uk", _SCHEDULES) is None


# ── schedule_id_counts: the "0 matched" discovery diagnostic ─────────────────

def test_schedule_id_counts_tallies_by_schedule_with_dash_for_none():
    runs = _sched_runs(("a", "s1"), ("b", "s1"), ("c", "s2"), ("d", None))
    counts = ingest.schedule_id_counts(runs)
    assert counts == {"s1": 2, "s2": 1, "-": 1}


# ── mark_run_seen: bound per-cycle cost of a scoped task, without starving the sibling ──

def test_mark_run_seen_skips_run_for_this_search_only(jobs_db):
    # A run from the OTHER schedule, marked seen for "tpm", must not be re-probed by "tpm" — but
    # must still be pending for the sibling "director" search that actually owns that schedule.
    run = _runs("foreign-run")[0]
    ingest.mark_run_seen(jobs_db, state_key("tpm", "shared-task"), run)
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "tpm", [run])] == []
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "director", [run])] == ["foreign-run"]


def test_mark_run_seen_is_idempotent(jobs_db):
    # Re-marking the same run (e.g. two overlapping cycles) must not raise on the UNIQUE index.
    run = _runs("r1")[0]
    key = state_key("tpm", "shared-task")
    ingest.mark_run_seen(jobs_db, key, run)
    ingest.mark_run_seen(jobs_db, key, run)
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "tpm", [run])] == []


# ── seed_search: start a new scoped search from "now" without backfilling ─────

class _Search:  # minimal stand-in for config.Search (id + tasks are all seed_search reads)
    def __init__(self, id, tasks):
        self.id, self.tasks = id, tasks


def test_seed_search_marks_all_current_runs_seen(jobs_db, monkeypatch):
    # Seeding records every current run as seen (no detail/dataset fetch), so afterwards the
    # search treats the whole existing backlog as done and only picks up NEW runs.
    backlog = _runs("r1", "r2", "r3")
    monkeypatch.setattr(ingest, "fetch_task_runs", lambda user, task, token: list(backlog))
    search = _Search("europe_tpm", [{"name": "shared-task"}])

    n = ingest.seed_search(jobs_db, search, "user", "token")
    assert n == 3
    # The whole backlog is now seen; only a run minted after seeding is still pending.
    assert ingest.runs_to_process(jobs_db, "europe_tpm", backlog) == []
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "europe_tpm", _runs("r4"))] == ["r4"]


def test_seed_search_does_not_affect_a_sibling_search(jobs_db, monkeypatch):
    # Seeding is per-lens (state_key), so a sibling sharing the Apify task still sees the backlog.
    backlog = _runs("r1", "r2")
    monkeypatch.setattr(ingest, "fetch_task_runs", lambda user, task, token: list(backlog))
    ingest.seed_search(jobs_db, _Search("europe_tpm", [{"name": "shared-task"}]), "u", "t")
    assert [r["id"] for r in ingest.runs_to_process(jobs_db, "midatl_tpm", backlog)] == ["r1", "r2"]


# ── history_scope: per-search filter for the activity chart ──────────────────

def test_history_scope_fragments():
    clause, params = ingest.history_scope(DEFAULT_SEARCH_ID)
    assert "NOT LIKE" in clause and params == []
    clause, params = ingest.history_scope("mid_atlantic")
    assert "ESCAPE" in clause and params == ["mid\\_atlantic:%"]   # '_' escaped


def test_history_scope_aggregation_is_per_search(jobs_db):
    # The scoped SUM query (as the /stats/history chart runs it) sees only its own search's
    # ingest_history rows — the bug was that a fresh search charted every search's activity.
    _record_run(jobs_db, "tpm", "t", "r1")
    _record_run(jobs_db, "tpm", "t", "r2")
    _record_run(jobs_db, "director", "t", "r3")
    for sid, expected in (("tpm", 2), ("director", 1)):
        clause, params = ingest.history_scope(sid)
        total = jobs_db.execute(
            f"SELECT SUM(unchanged) FROM ingest_history WHERE {clause}", params
        ).fetchone()[0]
        assert total == expected


def test_history_scope_default_excludes_namespaced(jobs_db):
    _record_run(jobs_db, DEFAULT_SEARCH_ID, "t", "r1")   # bare key
    _record_run(jobs_db, "tpm", "t", "r2")               # "tpm:t"
    clause, params = ingest.history_scope(DEFAULT_SEARCH_ID)
    count = jobs_db.execute(
        f"SELECT COUNT(*) FROM ingest_history WHERE {clause}", params
    ).fetchone()[0]
    assert count == 1                                    # only the default search's row


# ── ensure_job_search_state backfill ─────────────────────────────────────────

def test_backfill_seeds_default_rows_from_dormant_columns(jobs_db):
    jobs_db.execute(
        "INSERT INTO jobs (job_id, title, status, viability, applied_at, history, raw) "
        "VALUES ('j1', 'T', 'applied', 'high', '2026-01-02', '[{\"e\":1}]', '{}')")
    jobs_db.execute("DELETE FROM job_search_state")   # simulate a pre-migration DB
    ensure_job_search_state(jobs_db)
    row = jobs_db.execute(
        "SELECT search_id, status, viability, applied_at, history FROM job_search_state "
        "WHERE job_id='j1'").fetchone()
    assert row["search_id"] == DEFAULT_SEARCH_ID
    assert (row["status"], row["viability"], row["applied_at"]) == ("applied", "high", "2026-01-02")
    assert row["history"] == '[{"e":1}]'


def test_backfill_is_gated_and_idempotent(jobs_db):
    jobs_db.execute("INSERT INTO jobs (job_id, title, status, raw) VALUES ('j1','T','new','{}')")
    jobs_db.execute("DELETE FROM job_search_state")
    ensure_job_search_state(jobs_db)
    # A later status change lives only on the state row; a second ensure() must NOT re-seed
    # from the (dormant) jobs.status and clobber it.
    jobs_db.execute("UPDATE job_search_state SET status='applied' WHERE job_id='j1'")
    ensure_job_search_state(jobs_db)
    assert jobs_db.execute(
        "SELECT status FROM job_search_state WHERE job_id='j1'").fetchone()["status"] == "applied"


# ── per-lens divergence: one posting, two searches, independent state ─────────

def test_same_posting_two_searches_independent_status(jobs_db):
    _job(jobs_db, "p", "tpm", status="applied", viability="high")
    # Same physical posting also a member of a second search, with its own state row.
    jobs_db.execute(
        "INSERT INTO job_search_state (job_id, search_id, status, viability) "
        "VALUES ('p', 'director', 'skipped', 'low')")
    rows = {r["search_id"]: (r["status"], r["viability"]) for r in jobs_db.execute(
        "SELECT search_id, status, viability FROM job_search_state WHERE job_id='p'")}
    assert rows == {"tpm": ("applied", "high"), "director": ("skipped", "low")}
    # Exactly one physical jobs row backs both lenses.
    assert jobs_db.execute("SELECT COUNT(*) FROM jobs WHERE job_id='p'").fetchone()[0] == 1


# ── automatic dedup is restricted to the incoming search ─────────────────────

def test_find_canonical_restricted_to_search_members(jobs_db):
    _job(jobs_db, "x", "tpm")           # near-dup member of search tpm
    _job(jobs_db, "y", "director")      # byte-identical, but member of a different search
    # Searching within tpm sees only x; within director only y.
    m_tpm = find_canonical(jobs_db, "new", "Senior Platform Engineer", "Acme", _DESC, 0.85,
                           search_id="tpm")
    m_dir = find_canonical(jobs_db, "new", "Senior Platform Engineer", "Acme", _DESC, 0.85,
                           search_id="director")
    assert [r["job_id"] for r in m_tpm] == ["x"]
    assert [r["job_id"] for r in m_dir] == ["y"]


def test_find_canonical_unrestricted_when_search_id_none(jobs_db):
    _job(jobs_db, "x", "tpm")
    _job(jobs_db, "y", "director")
    matches = find_canonical(jobs_db, "new", "Senior Platform Engineer", "Acme", _DESC, 0.85)
    assert {r["job_id"] for r in matches} == {"x", "y"}   # legacy: no search restriction


# ── legacy adoption ──────────────────────────────────────────────────────────

def test_adopt_legacy_repoints_default_to_adopter(jobs_db):
    _job(jobs_db, "a", DEFAULT_SEARCH_ID, status="applied")
    _job(jobs_db, "b", DEFAULT_SEARCH_ID, status="new")
    jobs_db.execute("INSERT INTO ingest_state (task_name, last_run_id, last_run_at) "
                    "VALUES ('my-task', 'r1', 't')")
    moved = adopt_legacy(jobs_db, "tpm")
    assert moved == 2
    assert jobs_db.execute(
        "SELECT COUNT(*) FROM job_search_state WHERE search_id=?", (DEFAULT_SEARCH_ID,)
    ).fetchone()[0] == 0
    assert {r["job_id"]: r["status"] for r in jobs_db.execute(
        "SELECT job_id, status FROM job_search_state WHERE search_id='tpm'")} == {
        "a": "applied", "b": "new"}
    # The run-tracking key is re-namespaced under the adopter.
    assert jobs_db.execute("SELECT task_name FROM ingest_state").fetchone()["task_name"] == "tpm:my-task"
    # Idempotent: nothing left to adopt.
    assert adopt_legacy(jobs_db, "tpm") == 0


def test_adopt_legacy_errors_on_adopter_collision(jobs_db):
    _job(jobs_db, "a", DEFAULT_SEARCH_ID)
    jobs_db.execute("INSERT INTO job_search_state (job_id, search_id, status) "
                    "VALUES ('a', 'tpm', 'new')")   # adopter already has a row for this job
    try:
        adopt_legacy(jobs_db, "tpm")
    except RuntimeError as e:
        assert "cannot adopt" in str(e)
    else:
        raise AssertionError("expected a RuntimeError on adopter collision")


def test_adopt_legacy_noop_without_legacy_rows(jobs_db):
    _job(jobs_db, "a", "tpm")   # no __default__ rows at all
    assert adopt_legacy(jobs_db, "tpm") == 0


# ── dormant-column poison guard (reads must come from job_search_state) ───────

def test_dormant_jobs_columns_are_not_read(sample_app_db):
    """After migration, jobs.status/viability are dormant. Scribble garbage into them and the
    app's stats (which read the per-lens state) must be unaffected."""
    import os
    import app

    con = sqlite3.connect(os.environ["JOBSEARCH_DB"])
    con.execute("UPDATE jobs SET status = 'POISON', viability = 'POISON'")
    con.commit(); con.close()

    client = app.app.test_client()
    # The jobs list and stats both read status/viability from job_search_state, so the poisoned
    # dormant jobs.* columns must not surface anywhere.
    assert client.get("/").status_code == 200
    stats_body = client.get("/stats").get_data(as_text=True)
    assert client.get("/stats").status_code == 200
    assert "POISON" not in stats_body
    assert "POISON" not in client.get("/").get_data(as_text=True)


def test_preview_surfaces_other_search_lenses(sample_app_db):
    """The preview JSON lists the same posting's (viability, status) under OTHER searches it's a
    member of — the cross-lens nudge. The current lens itself is excluded."""
    import os
    import app

    con = sqlite3.connect(os.environ["JOBSEARCH_DB"])
    con.execute("INSERT INTO job_search_state (job_id, search_id, status, viability) "
                "VALUES ('cs_review', 'director', 'applied', 'medium')")
    con.commit(); con.close()

    data = app.app.test_client().get("/job/cs_review").get_json()
    others = {o["search_id"]: (o["viability"], o["status"]) for o in data["other_searches"]}
    assert others == {"director": ("medium", "applied")}   # the __default__ lens is not listed


# ── rescore multi-search fan-out (one child process per search) ───────────────

def _write_two_search_config(tmp_path):
    (tmp_path / "searches").mkdir()
    (tmp_path / "searches" / "tpm.toml").write_text(
        '[viability]\nenabled = true\nprompt = "tpm"\n', encoding="utf-8")
    (tmp_path / "searches" / "dir.toml").write_text(
        '[viability]\nenabled = true\nprompt = "dir"\n', encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[basics]\ndb_path = "j.db"\n[ai]\napi_key = "sk-test"\n'
        '[[searches]]\nsearch_id = "tpm"\nsearch_name = "TPM"\nsearch_config_file = "searches/tpm.toml"\n'
        '[[searches]]\nsearch_id = "director"\nsearch_name = "Dir"\nsearch_config_file = "searches/dir.toml"\n',
        encoding="utf-8")
    return cfg


def test_rescore_fans_out_one_child_per_search(tmp_path, monkeypatch):
    import rescore_viability as rv
    cfg = _write_two_search_config(tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(rv.subprocess, "run", lambda argv, **k: (calls.append(argv), _Result())[1])
    monkeypatch.setattr(sys, "argv", ["rescore_viability.py", "--config", str(cfg), "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        rv.main()
    assert exc.value.code == 0
    # One child per configured search, each targeting its own --search and carrying --dry-run.
    targeted = sorted(a[a.index("--search") + 1] for a in calls)
    assert targeted == ["director", "tpm"]
    assert all("--dry-run" in a for a in calls)


def test_passthrough_argv_reconstructs_flags():
    import argparse
    import rescore_viability as rv
    ns = argparse.Namespace(
        config="c.toml", dry_run=True, force=True, all=False, early_stage=False,
        autoskipped=False, status="skipped", current_viability="high", since=None,
        previous_days=7, reconcile_autoskipped=False, verbose=True)
    out = rv._passthrough_argv(ns)
    assert out[:2] == ["--config", "c.toml"]
    assert "--dry-run" in out and "--force" in out and "--verbose" in out
    assert out[out.index("--status") + 1] == "skipped"
    assert out[out.index("--current-viability") + 1] == "high"
    assert out[out.index("--previous-days") + 1] == "7"
    assert "--all" not in out and "--since" not in out
