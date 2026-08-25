"""Manual job-description override (description_actual): the shared effective-description
helpers, the scoring message, the set/clear route, and how the override flows through
process_job_row and the preview JSON.

The override lets the user paste the full posting from the employer's site to replace a
wrong/partial feed description (typically a career-site teaser — see
ingest.feed_description_truncated). The scoring effect itself needs a live AI call, so here we
test the pure helpers, persistence, and the derived display/exemption state.
"""
import sqlite3

import app
import viability


# ── effective_description / has_description_override / description_is_truncated ──
def test_effective_description_prefers_override():
    job = {"job_description": "feed teaser", "description_actual": "the full pasted posting"}
    assert viability.effective_description(job) == "the full pasted posting"


def test_effective_description_falls_back_to_feed():
    job = {"job_description": "feed text", "description_actual": None}
    assert viability.effective_description(job) == "feed text"


def test_whitespace_only_override_is_ignored():
    # A blank override must never blank out a real feed description.
    job = {"job_description": "feed text", "description_actual": "   \n  "}
    assert viability.effective_description(job) == "feed text"
    assert viability.has_description_override(job) is False


def test_has_description_override_true_when_set():
    assert viability.has_description_override({"description_actual": "x"}) is True


def test_description_is_truncated_true_when_flagged_and_no_override():
    job = {"description_truncated": 1, "description_actual": None}
    assert viability.description_is_truncated(job) is True


def test_override_clears_effective_truncation():
    # A pasted full posting supersedes a partial feed, so the practical truncation is gone.
    job = {"description_truncated": 1, "description_actual": "full pasted text"}
    assert viability.description_is_truncated(job) is False


def test_not_truncated_without_flag():
    assert viability.description_is_truncated({"description_truncated": 0, "description_actual": None}) is False


# ── build_score_message uses the override ────────────────────────────────────
def test_score_message_uses_override_text():
    job = {"title": "T", "company": "C", "job_description": "SHORT FEED TEASER",
           "description_actual": "FULL PASTED RESPONSIBILITIES AND QUALIFICATIONS"}
    msg = viability.build_score_message(job)
    assert "FULL PASTED RESPONSIBILITIES" in msg
    assert "SHORT FEED TEASER" not in msg


# ── the set/clear route ──────────────────────────────────────────────────────
def _post_desc(job_id: str, value: str):
    return app.app.test_client().post(
        f"/job/{job_id}/description_actual", data={"description_actual": value})


def test_route_sets_override_and_marks_rescore(sample_app_db):
    resp = _post_desc("cs_review", "Full pasted job description from the employer site.")
    assert resp.status_code == 204

    con = sqlite3.connect(app.DB_PATH)
    row = con.execute(
        "SELECT description_actual, needs_rescored, history FROM jobs WHERE job_id = ?",
        ("cs_review",)).fetchone()
    con.close()
    assert row[0] == "Full pasted job description from the employer site."
    assert row[1] == 1
    # History is stored SQLite-normalized (compact JSON, no spaces).
    assert '"event":"description_actual"' in row[2]
    assert '"action":"set"' in row[2]


def test_route_clears_override(sample_app_db):
    _post_desc("cs_review", "Some pasted text.")
    resp = _post_desc("cs_review", "   ")   # blank clears
    assert resp.status_code == 204

    con = sqlite3.connect(app.DB_PATH)
    row = con.execute(
        "SELECT description_actual, needs_rescored FROM jobs WHERE job_id = ?",
        ("cs_review",)).fetchone()
    con.close()
    assert row[0] is None
    assert row[1] == 1


def test_route_404_for_unknown_job(sample_app_db):
    assert _post_desc("does_not_exist", "x").status_code == 404


# ── process_job_row: derived display/exemption state ─────────────────────────
def test_process_job_row_override_supersedes_feed_and_clears_badge():
    row = {"job_id": "j1", "job_description": "feed teaser", "description_actual": "full text",
           "description_truncated": 1, "source": "careersite", "status": "new"}
    out = app.process_job_row(row)
    assert out["has_description_override"] is True
    assert out["job_description"] == "full text"       # effective wins
    assert out["description_original"] == "feed teaser"  # feed kept for reference
    assert out["description_truncated"] == 0            # badge suppressed under an override


def test_process_job_row_truncated_without_override_keeps_badge():
    row = {"job_id": "j2", "job_description": "teaser", "description_actual": None,
           "description_truncated": 1, "source": "careersite", "status": "new"}
    out = app.process_job_row(row)
    assert out["description_truncated"] == 1
    assert out["has_description_override"] is False


# ── preview JSON reflects the override ───────────────────────────────────────
def test_preview_json_reflects_override(sample_app_db):
    app.app.test_client().post(
        "/job/cs_review/description_actual",
        data={"description_actual": "Pasted full description for scoring."})
    job = app.app.test_client().get("/job/cs_review").get_json()
    assert job["has_description_override"] is True
    assert job["job_description"] == "Pasted full description for scoring."
    # The AI-formatted HTML is suppressed under an override (it was rendered from the feed text).
    assert job["job_description_html"] is None
    # The feed's original is still available for revert/inspection.
    assert job["description_feed"] is not None
    # Effective truncation is cleared once the full text is pasted in.
    assert job["description_truncated"] is False
