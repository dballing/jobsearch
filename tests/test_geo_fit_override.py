"""Manual location-viability override (geo_fit_actual): the candidate asserting "I'd work
this location" forces the geographic verdict to ACCEPTABLE, which skips the billed location
sub-call and — being ACCEPTABLE, not POOR — spares the job the POOR→low clamp. Covered here
as pure precedence logic (manual_geo_verdict) plus the two write paths (the override endpoint
and the manual-add form); the score itself needs a live AI call, so it isn't exercised."""
import sqlite3

import app
import viability


# ── manual_geo_fit(): the override value, validated ───────────────────────────
def test_manual_geo_fit_returns_acceptable_when_set():
    assert viability.manual_geo_fit({"geo_fit_actual": "acceptable"}) == "acceptable"


def test_manual_geo_fit_is_case_insensitive_and_trims():
    assert viability.manual_geo_fit({"geo_fit_actual": "  ACCEPTABLE "}) == "acceptable"


def test_manual_geo_fit_none_for_unset_blank_or_bogus():
    assert viability.manual_geo_fit({}) is None
    assert viability.manual_geo_fit({"geo_fit_actual": ""}) is None
    assert viability.manual_geo_fit({"geo_fit_actual": "sorta"}) is None  # not a GEO_FITS tier


# ── manual_geo_verdict(): precedence before the AI call ───────────────────────
def test_verdict_none_when_no_manual_signal():
    """No override and no POOR flag → (None, None, False): the caller runs the AI sub-call."""
    assert viability.manual_geo_verdict({}) == (None, None, False)


def test_verdict_acceptable_override_skips_ai_and_wont_clamp():
    """An ACCEPTABLE override yields a non-None fit (so the AI call is skipped) with
    manual_poor False, and the clamp then leaves the rating untouched."""
    fit, gnote, manual_poor = viability.manual_geo_verdict({"geo_fit_actual": "acceptable"})
    assert fit == "acceptable"
    assert gnote == viability.geo_note("acceptable", "")
    assert manual_poor is False
    # ACCEPTABLE is a non-POOR tier, so the disqualifying low-clamp never fires.
    assert viability.clamp_viability_for_geo(fit, "medium", "Good scope.") == ("medium", "Good scope.")


def test_verdict_override_wins_over_manual_poor_flag():
    """When a job carries BOTH the ACCEPTABLE override and the remote-in-unsupported-location
    POOR flag, the explicit "I'd work here" override wins and suppresses the clamp."""
    job = {"geo_fit_actual": "acceptable",
           "work_arrangement_actual": viability.GEO_UNSUPPORTED_ARRANGEMENT}
    fit, _gnote, manual_poor = viability.manual_geo_verdict(job)
    assert fit == "acceptable"
    assert manual_poor is False


def test_verdict_poor_flag_without_override_forces_low():
    """No override but the POOR flag set → ('poor', poor-note, True); the clamp then forces
    the rating to low with the manual-flag reason suffix."""
    job = {"work_arrangement_actual": viability.GEO_UNSUPPORTED_ARRANGEMENT}
    fit, gnote, manual_poor = viability.manual_geo_verdict(job)
    assert fit == "poor"
    assert manual_poor is True
    rating, reason = viability.clamp_viability_for_geo(fit, "medium", "Great role.", manual=manual_poor)
    assert rating == "low"
    assert "manually flagged" in reason  # the _GEO_MANUAL_POOR_SUFFIX, not the AI-verdict one


# ── /job/<id>/geo_fit_actual endpoint ─────────────────────────────────────────
def _post_geo(job_id: str, value: str):
    return app.app.test_client().post(f"/job/{job_id}/geo_fit_actual",
                                      data={"geo_fit_actual": value})


def test_route_sets_override_and_marks_rescore(sample_app_db):
    resp = _post_geo("cs_review", "acceptable")
    assert resp.status_code == 204
    con = sqlite3.connect(app.DB_PATH)
    row = con.execute("SELECT geo_fit_actual, needs_rescored FROM job_search_state "
                      "WHERE job_id = ? AND search_id = '__default__'",
                      ("cs_review",)).fetchone()
    con.close()
    assert row[0] == "acceptable"
    assert row[1] == 1               # geo verdict feeds the scorer → rescore


def test_route_blank_clears_override(sample_app_db):
    client = app.app.test_client()
    client.post("/job/cs_review/geo_fit_actual", data={"geo_fit_actual": "acceptable"})
    resp = client.post("/job/cs_review/geo_fit_actual", data={"geo_fit_actual": "  "})
    assert resp.status_code == 204
    con = sqlite3.connect(app.DB_PATH)
    val = con.execute("SELECT geo_fit_actual FROM job_search_state WHERE job_id = ? AND search_id = '__default__'", ("cs_review",)).fetchone()[0]
    con.close()
    assert val is None


def test_route_rejects_unknown_value(sample_app_db):
    """Only the offered tier ('acceptable') is accepted — free text can't inject a bogus
    verdict (e.g. forcing 'preferred') into scoring."""
    resp = _post_geo("cs_review", "preferred")
    assert resp.status_code == 400


def test_route_unknown_job_is_404(sample_app_db):
    resp = _post_geo("nope", "acceptable")
    assert resp.status_code == 404


# ── manual-add form carries the override ──────────────────────────────────────
def _add_job(**overrides):
    data = {"title": "Staff SRE", "company": "Acme",
            "job_url": "https://example.com/j", "company_url": "https://acme.example"}
    data.update(overrides)
    return app.app.test_client().post("/jobs/manual", data=data)


def test_manual_add_persists_geo_override(sample_app_db):
    resp = _add_job(geo_fit_actual="acceptable")
    assert resp.status_code == 201
    job_id = resp.get_json()["job_id"]
    con = sqlite3.connect(app.DB_PATH)
    val = con.execute("SELECT geo_fit_actual FROM job_search_state WHERE job_id = ? AND search_id = '__default__'", (job_id,)).fetchone()[0]
    con.close()
    assert val == "acceptable"


def test_manual_add_without_override_leaves_it_null(sample_app_db):
    resp = _add_job()
    assert resp.status_code == 201
    job_id = resp.get_json()["job_id"]
    con = sqlite3.connect(app.DB_PATH)
    val = con.execute("SELECT geo_fit_actual FROM job_search_state WHERE job_id = ? AND search_id = '__default__'", (job_id,)).fetchone()[0]
    con.close()
    assert val is None


def test_manual_add_rejects_bogus_override(sample_app_db):
    resp = _add_job(geo_fit_actual="wherever")
    assert resp.status_code == 400
