"""Deterministic sample dataset for tests and snapshots.

A committed, fabricated set of postings exercising the permutations the UI and query
paths care about: every status, the three sources, all viability levels (+ unscored), a
fuzzy-match group with a company override and varied fields, salary variants and an
override, a work-arrangement override, several labels, a hotlisted employer (row tint), a
needs-rescored (stale) row, and notes. All timestamps are FIXED (no CURRENT_TIMESTAMP) so
rendered output is byte-stable — that's what lets the HTML snapshot test compare against a
committed golden. Reused wherever a test wants a realistic populated DB.
"""
import json

import ingest

# Fixed reference instants (UTC) — everything is dated relative to these literals so the
# data never shifts with the wall clock.
_SEEN = "2026-06-{:02d} {:02d}:00:00"   # first_seen builder: (day, hour)


def _hist(*events):
    return json.dumps(list(events))


# Each entry is column overrides on top of _DEFAULTS. Ordered oldest→newest first_seen; the
# default view sorts first_seen desc, so the last rows here render at the top.
_JOBS = [
    # ── Fuzzy-match group: one Acme role reposted across sources, varied viability, with a
    #    company override on the aggregator repost. Exercises grouped view + "(varied)". ──
    dict(job_id="ln_root", title="Rocket Science (Personal Rocket Division)", company="Acme Corp",
         company_url="https://acme.example/careers", location="Washington, DC",
         source="linkedin", status="interviewing", viability="high",
         viability_reason="Strong infra/compliance TPM fit at target comp.",
         salary_min=190000, salary_max=230000, labels='["dc"]',
         posted_date="2026-06-08", applied_at="2026-06-12 09:00:00",
         first_seen=_SEEN.format(10, 8),
         history=_hist({"ts": "2026-06-10T08:00:00Z", "event": "ingested"},
                       {"ts": "2026-06-12T09:00:00Z", "event": "status", "from": "new", "to": "applied"},
                       {"ts": "2026-06-20T14:00:00Z", "event": "status", "from": "applied", "to": "interviewing"})),
    dict(job_id="cs_mem1", title="Blacksmithing and Anvil-Casting", company="Ladders",
         company_actual="Acme Corp", location="Washington, DC",
         source="careersite", status="interviewing", viability="high",
         canonical_id="ln_root", salary_min=190000, salary_max=230000, labels='["dc"]',
         posted_date="2026-06-09", first_seen=_SEEN.format(11, 9)),
    dict(job_id="ln_mem2", title="Explosives Ordnance Expert", company="Acme Corp",
         location="Remote", source="linkedin", status="interviewing", viability="medium",
         viability_reason="Good fit; scope a touch narrow.",
         canonical_id="ln_root", labels='["dc","remote"]',
         posted_date="2026-06-09", first_seen=_SEEN.format(12, 10)),

    # ── Standalone rows across statuses / viability / labels / salary shapes ──
    dict(job_id="ln_new_hot", title="Sr. Problem-Solver (Contract)", company="Pacific Trident Global",
         location="Arlington, VA", status="new", viability="high",
         viability_reason="Excellent match.", salary_min=170000, salary_max=210000,
         labels='["dc"]', posted_date="2026-06-13", first_seen=_SEEN.format(13, 11)),
    dict(job_id="cs_review", title="Photocopier Repair Technician", company="Initech",
         location="Raleigh, NC", source="careersite", status="reviewing", viability="medium",
         salary_max=180000, labels='["nc"]', posted_date="2026-06-12",
         first_seen=_SEEN.format(13, 12)),
    dict(job_id="ln_deferred", title="Sr. Virologist", company="Umbrella",
         location="Columbia, SC", status="deferred", viability="low",
         viability_reason="Below target scope and comp.", salary_min=120000,
         labels='["sc"]', posted_date="2026-06-11", first_seen=_SEEN.format(13, 13)),
    dict(job_id="cs_applied_ovr", title="Corporate Drone (Posting # AJ49124120Q)", company="Hooli",
         company_url="https://hooli.example", location="Remote", source="careersite",
         status="applied", viability="medium", salary_min=140000, salary_max=175000,
         salary_min_actual=160000, salary_max_actual=195000,   # salary override
         labels='["remote"]', posted_date="2026-06-10", applied_at="2026-06-14 10:00:00",
         first_seen=_SEEN.format(14, 8)),
    dict(job_id="ln_offered", title="Sr. Experimental Research Fellow", company="Stark Industries",
         location="Remote", status="offered", viability="high", salary_min=210000, salary_max=260000,
         labels='["remote"]', posted_date="2026-06-05", applied_at="2026-06-09 09:00:00",
         first_seen=_SEEN.format(14, 9)),
    dict(job_id="cs_rejected", title="Food-Science Experimentation Engineer", company="Soylent",
         location="Durham, NC", source="careersite", status="rejected", viability="low",
         salary_min=100000, salary_max=130000, labels='["nc"]', posted_date="2026-06-02",
         applied_at="2026-06-06 09:00:00", first_seen=_SEEN.format(14, 10),
         history=_hist({"ts": "2026-06-06T09:00:00Z", "event": "status", "from": "new", "to": "applied"},
                       {"ts": "2026-06-13T09:00:00Z", "event": "status", "from": "applied", "to": "rejected"})),
    dict(job_id="ln_ghosted", title="Latex Salesperson", company="Vandelay",
         location="Remote", status="ghosted", viability="medium", salary_min=150000, salary_max=185000,
         labels='["remote"]', posted_date="2026-05-28", applied_at="2026-06-01 09:00:00",
         first_seen=_SEEN.format(14, 11)),
    dict(job_id="cs_withdrawn", title="Senior Software Engineer - Compression", company="Pied Piper",
         location="Alexandria, VA", source="careersite", status="withdrawn", viability="low",
         labels='["dc"]', posted_date="2026-05-30", first_seen=_SEEN.format(14, 12)),
    dict(job_id="ln_closed", title="Oompa-Loompa", company="Wonka Industries",
         location="Remote", status="closed", labels='["remote"]', posted_date="2026-05-20",
         first_seen=_SEEN.format(14, 13)),   # viability NULL (unscored)
    dict(job_id="cs_skipped", title="Principal Robotics Research Scientist", company="Cyberdyne",
         location="Charleston, SC", source="careersite", status="skipped", viability="low",
         viability_reason="Not a technical PM role.", labels='["sc"]',
         posted_date="2026-06-01", first_seen=_SEEN.format(14, 14)),
    dict(job_id="ln_autoskip", title="Multiverse Science Engineer", company="Massive Dynamic",
         location="Remote", status="autoskipped", viability="low", labels='["remote"]',
         posted_date="2026-06-03", first_seen=_SEEN.format(14, 15)),

    # ── Manual entry (third source → the Source column appears) + work-arrangement override,
    #    plus a needs-rescored (stale) row with a note. ──
    dict(job_id="manual_recruiter", title="Human Likeness Sculpting", company="Tyrell Corp",
         company_url="https://tyrell.example", location="Washington, DC", source="manual",
         status="new", viability="high", salary_min=220000, salary_max=270000,
         work_arrangement_actual="Hybrid", labels='["dc"]', posted_date="2026-06-14",
         first_seen=_SEEN.format(15, 8), notes="Referred by a former colleague."),
    dict(job_id="cs_stale", title="Baker's Assistant - Cakes", company="Aperture Science",
         location="Remote", source="careersite", status="new", viability="medium",
         viability_reason="Solid fit; re-score pending.", salary_min=160000, salary_max=200000,
         needs_rescored=1, labels='["remote"]', posted_date="2026-06-13",
         first_seen=_SEEN.format(15, 9), notes="Recruiter says hybrid, not remote."),
]

_DEFAULTS = dict(
    title=None, company=None, location=None, posted_date=None, job_url="https://example/job",
    apply_url=None, company_url=None, easy_apply=0, salary_min=None, salary_max=None,
    salary_currency="USD", labels="[]", source="linkedin", status="new", notes=None,
    job_description="Sample job description for fixture data.", refreshed_at=None,
    canonical_id=None, viability=None, viability_reason=None, viability_prompt_hash="fixturehash",
    applied_at=None, history="[]", company_actual=None, salary_min_actual=None,
    salary_max_actual=None, work_arrangement_actual=None, needs_rescored=0,
    job_description_formatted=None, description_hash=None, first_seen=None, raw="{}",
)

_COLUMNS = list(_DEFAULTS) + ["job_id"]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def build_sample_db(conn) -> None:
    """Populate `conn` with the deterministic sample dataset (see module docstring).

    Ensures the base schema exists (ingest.SCHEMA is idempotent), inserts the jobs plus a
    fixed ingest_state (for a stable navbar timestamp) and a hotlisted employer. The hotlist
    insert is guarded on the table existing, since that table is created by app._migrate
    rather than ingest.SCHEMA — so this builder works against either a bare ingest DB or the
    fully-migrated app DB.
    """
    conn.executescript(ingest.SCHEMA)
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    for over in _JOBS:
        row = {**_DEFAULTS, **over}
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", row)
    # Fixed ingest timestamps so the navbar renders deterministically.
    conn.execute(
        "INSERT INTO ingest_state (task_name, last_run_id, last_run_at, last_synced_at) "
        "VALUES (?, ?, ?, ?)",
        ("sample", "run_sample", "2026-06-15T12:00:00Z", "2026-06-15T12:05:00Z"),
    )
    # Hotlist Pacific Trident Global → its 'new' job (ln_new_hot) renders tinted. name_key is
    # the lower-cased effective company name (see app._company_key).
    if _table_exists(conn, "company_hotlist"):
        conn.execute(
            "INSERT INTO company_hotlist (name_key, display_name, added_at) VALUES (?, ?, ?)",
            ("pacific trident global", "Pacific Trident Global", "2026-06-13T00:00:00Z"),
        )
    conn.commit()
