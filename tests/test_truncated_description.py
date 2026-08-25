"""Tests for the truncated-feed-description flag: detection, extraction, backfill, the
ingest round-trip, and the auto-skip exemption it drives.

Background: some careersite/ATS feeds (Oracle HCM, Workable, ADP, …) deliver only a short
teaser in `description_text` while the full body is rendered client-side. We flag such rows
(`description_truncated`) so viability scoring won't silently auto-skip a possibly-good role
judged on partial text, and the UI can badge it. See ingest.feed_description_truncated and
rescore_viability.should_autoskip.
"""
import json

import ingest
from rescore_viability import should_autoskip

# A description comfortably over the teaser cap, for the "not truncated" cases.
_LONG = "word " * 600  # ~3000 chars, well above _TRUNCATED_DESC_MAXLEN


def _careersite_item(description, *, reqs="Must have 5 years of X.", **extra):
    """Minimal fantastic-jobs careersite actor item with the fields our extractor reads."""
    item = {
        "id": 12345,
        "title": "Senior Data Program Manager",
        "organization": "Honeywell",
        "locations_derived": ["Charlotte, North Carolina, United States"],
        "date_posted": "2026-08-20",
        "url": "https://example.oraclecloud.com/job/1",
        "description_text": description,
        "ai_requirements_summary": reqs,
    }
    item.update(extra)
    return item


# ── feed_description_truncated ───────────────────────────────────────────────
def test_truncated_when_short_and_has_requirements_summary():
    item = _careersite_item("Short teaser.", reqs="Needs SQL and Power BI.")
    assert ingest.feed_description_truncated(item, item["description_text"], "careersite") is True


def test_not_truncated_when_description_is_full_length():
    item = _careersite_item(_LONG, reqs="Needs SQL and Power BI.")
    assert ingest.feed_description_truncated(item, item["description_text"], "careersite") is False


def test_not_truncated_without_a_requirements_summary():
    # A genuinely short posting the actor extracted nothing from isn't our signal.
    item = _careersite_item("Short teaser.", reqs=None)
    assert ingest.feed_description_truncated(item, item["description_text"], "careersite") is False


def test_empty_requirements_summary_is_not_a_signal():
    item = _careersite_item("Short teaser.", reqs="")
    assert ingest.feed_description_truncated(item, item["description_text"], "careersite") is False


def test_linkedin_is_never_flagged_even_when_short():
    # LinkedIn's description_text carries the full body, so the heuristic must not apply.
    item = _careersite_item("Short teaser.", reqs="Needs SQL.")
    assert ingest.feed_description_truncated(item, item["description_text"], "linkedin") is False


def test_none_description_counts_as_short():
    item = _careersite_item(None, reqs="Needs SQL.")
    assert ingest.feed_description_truncated(item, None, "careersite") is True


# ── extractor wiring ─────────────────────────────────────────────────────────
def test_extract_careersite_sets_flag_on_truncated():
    fields = ingest.extract_fields_careersite(_careersite_item("Short teaser."))
    assert fields["description_truncated"] == 1


def test_extract_careersite_clears_flag_on_full():
    fields = ingest.extract_fields_careersite(_careersite_item(_LONG))
    assert fields["description_truncated"] == 0


def test_extract_linkedin_always_zero():
    # extract_fields_linkedin reads its own field names; a short description must still be 0.
    fields = ingest.extract_fields_linkedin(
        {"linkedin_id": 999, "title": "T", "organization": "O", "description_text": "Short.",
         "ai_requirements_summary": "Needs SQL."}
    )
    assert fields["description_truncated"] == 0


# ── backfill over pre-existing rows ──────────────────────────────────────────
def test_backfill_flags_only_truncated_careersite_rows(jobs_db):
    rows = [
        ("cs_trunc", "careersite", "Short teaser.", _careersite_item("Short teaser.")),
        ("cs_full",  "careersite", _LONG,          _careersite_item(_LONG)),
        ("cs_noreq", "careersite", "Short.",        _careersite_item("Short.", reqs=None)),
        ("ln_short", "linkedin",   "Short.",        {"description_text": "Short."}),
    ]
    for job_id, source, desc, item in rows:
        jobs_db.execute(
            "INSERT INTO jobs (job_id, source, job_description, raw, description_truncated) "
            "VALUES (?, ?, ?, ?, 0)",
            (job_id, source, desc, json.dumps(item)),
        )
    # A careersite row whose raw JSON is corrupt must be left at its default, not crash.
    jobs_db.execute(
        "INSERT INTO jobs (job_id, source, job_description, raw, description_truncated) "
        "VALUES ('cs_badjson', 'careersite', 'Short.', 'not json', 0)"
    )
    jobs_db.commit()

    flagged = ingest.backfill_description_truncated(jobs_db)
    assert flagged == 1

    got = {r["job_id"]: r["description_truncated"]
           for r in jobs_db.execute("SELECT job_id, description_truncated FROM jobs")}
    assert got == {"cs_trunc": 1, "cs_full": 0, "cs_noreq": 0,
                   "ln_short": 0, "cs_badjson": 0}


# ── ingest round-trip: flag persists on insert and clears on a fuller re-ingest ──
def test_ingest_persists_and_clears_truncated_flag(jobs_db):
    item = _careersite_item("Short teaser.")
    ingest.ingest(jobs_db, [item], "lbl", actor_type="careersite", formatter=None)
    stored = jobs_db.execute(
        "SELECT description_truncated, status FROM jobs WHERE job_id = 'cs_12345'"
    ).fetchone()
    assert stored["description_truncated"] == 1
    assert stored["status"] == "new"  # ingest never auto-skips; scoring does

    # The same posting later comes back with the full body — the flag must clear.
    full = _careersite_item(_LONG)
    ingest.ingest(jobs_db, [full], "lbl", actor_type="careersite", formatter=None)
    assert jobs_db.execute(
        "SELECT description_truncated FROM jobs WHERE job_id = 'cs_12345'"
    ).fetchone()["description_truncated"] == 0


# ── should_autoskip: the truncation exemption ────────────────────────────────
# auto_skip_threshold=0 means "low" (rank 0) is at/below the bar (the common config).
def test_autoskip_fires_for_low_untruncated_new_job():
    assert should_autoskip(auto_skip=True, status="new", rating="low",
                           auto_skip_threshold=0, truncated=False) is True


def test_autoskip_exempts_truncated_job():
    # Same low score, but a partial description must NOT be auto-skipped.
    assert should_autoskip(auto_skip=True, status="new", rating="low",
                           auto_skip_threshold=0, truncated=True) is False


def test_autoskip_off_never_fires():
    assert should_autoskip(auto_skip=False, status="new", rating="low",
                           auto_skip_threshold=0, truncated=False) is False


def test_autoskip_only_for_early_statuses():
    assert should_autoskip(auto_skip=True, status="applied", rating="low",
                           auto_skip_threshold=0, truncated=False) is False


def test_autoskip_not_for_above_threshold_score():
    assert should_autoskip(auto_skip=True, status="new", rating="high",
                           auto_skip_threshold=0, truncated=False) is False
