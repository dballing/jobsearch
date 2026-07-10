"""Tests for viability.build_score_message — the per-job prompt payload. The live AI call
in score_job is out of scope, but the message *content* (which fields get sent) is pure
and worth locking down: location was silently missing, so the model dinged jobs for it."""
import viability


def test_location_from_column_when_no_raw_list():
    msg = viability.build_score_message(
        {"title": "Sr. TPM, Wallet Services", "company": "Apple", "location": "Cary, NC"})
    assert "Location: Cary, NC" in msg


def test_all_locations_sent_from_raw():
    import json
    raw = json.dumps({"locations_derived": ["Shelton, Connecticut, United States",
                                            "Aiken, South Carolina, United States",
                                            "Solon, Ohio, United States"]})
    # The `location` column only kept the first — the message must include every site.
    msg = viability.build_score_message(
        {"title": "Manager, IT PM", "company": "Hubbell",
         "location": "Shelton, Connecticut, United States", "raw": raw})
    line = next(l for l in msg.splitlines() if l.startswith("Location:"))
    assert "South Carolina" in line and "Connecticut" in line and "Ohio" in line
    assert line.count(";") == 2  # three locations, semicolon-joined


def test_locations_capped_with_more_marker():
    import json
    raw = json.dumps({"locations_derived": [f"City {i}, ST, US" for i in range(50)]})
    msg = viability.build_score_message({"title": "T", "company": "C", "raw": raw})
    line = next(l for l in msg.splitlines() if l.startswith("Location:"))
    assert "(+10 more)" in line  # 50 total, capped at 40


def test_location_omitted_when_absent():
    import json
    for job in ({"title": "T", "company": "C"},
                {"title": "T", "company": "C", "location": ""},
                {"title": "T", "company": "C", "location": None},
                {"title": "T", "company": "C", "raw": json.dumps({"locations_derived": []})}):
        assert "Location:" not in viability.build_score_message(job)


import json as _json


def _wa(arrangement, days=None):
    raw = {"ai_work_arrangement": arrangement}
    if days is not None:
        raw["ai_work_arrangement_office_days"] = days
    msg = viability.build_score_message({"title": "T", "company": "C", "raw": _json.dumps(raw)})
    return next((l for l in msg.splitlines() if l.startswith("Work arrangement:")), None)


def test_remote_ok_is_glossed_not_raw_enum():
    # The Transform9 case: "Remote OK" + office days must read as remote-with-near-office
    # caveat, not the bare (ambiguous) enum.
    line = _wa("Remote OK", 3)
    assert "Remote OK (3" not in line  # not the raw enum text
    assert "Remote-friendly" in line and "near the office" in line and "3 days/week" in line


def test_remote_solely_is_fully_remote():
    assert _wa("Remote Solely", 0) == "Work arrangement: Fully remote"


def test_hybrid_and_onsite_glosses():
    assert _wa("Hybrid", 3) == "Work arrangement: Hybrid — 3 days/week in office"
    assert _wa("Hybrid") == "Work arrangement: Hybrid"
    assert _wa("On-site", 5) == "Work arrangement: On-site"


def test_unknown_arrangement_passes_through():
    assert _wa("Flexible", 2) == "Work arrangement: Flexible (2 days/week in office)"


def test_work_arrangement_omitted_when_absent():
    for job in ({"title": "T", "company": "C"},
                {"title": "T", "company": "C", "raw": _json.dumps({"ai_work_arrangement": "None"})},
                {"title": "T", "company": "C", "raw": _json.dumps({})}):
        assert "Work arrangement:" not in viability.build_score_message(job)


def test_has_title_and_company_lines():
    msg = viability.build_score_message({"title": "Staff PM", "company": "Acme"})
    assert "Job title: Staff PM" in msg and "Company: Acme" in msg


def test_company_override_shows_both():
    msg = viability.build_score_message(
        {"title": "T", "company": "Ladders", "company_actual": "Capital One"})
    assert "Company: Capital One (posted via Ladders)" in msg


def test_salary_variants():
    both = viability.build_score_message({"title": "T", "company": "C",
                                          "salary_min": 150000, "salary_max": 200000})
    assert "Salary: $150,000 – $200,000" in both
    lo = viability.build_score_message({"title": "T", "company": "C", "salary_min": 150000})
    assert "Salary: $150,000+" in lo
    hi = viability.build_score_message({"title": "T", "company": "C", "salary_max": 200000})
    assert "Salary: up to $200,000" in hi
    none = viability.build_score_message({"title": "T", "company": "C"})
    assert "Salary:" not in none


def test_salary_override_wins():
    msg = viability.build_score_message({"title": "T", "company": "C",
                                         "salary_min": 100000, "salary_max": 120000,
                                         "salary_min_actual": 150000, "salary_max_actual": 200000})
    assert "Salary: $150,000 – $200,000" in msg  # override, not the feed pair


def test_description_capped():
    msg = viability.build_score_message(
        {"title": "T", "company": "C", "job_description": "x" * 9000})
    assert msg.count("x") == 4000


def test_field_order():
    msg = viability.build_score_message(
        {"title": "T", "company": "C", "location": "Cary, NC",
         "salary_min": 150000, "salary_max": 200000, "job_description": "desc"})
    assert msg.index("Job title:") < msg.index("Company:") < msg.index("Location:") \
        < msg.index("Salary:") < msg.index("Description:")


# ── prompt_hash: staleness change-detector ────────────────────────────────────
def test_prompt_hash_deterministic_and_prompt_sensitive():
    assert viability.prompt_hash("candidate A") == viability.prompt_hash("candidate A")
    assert viability.prompt_hash("candidate A") != viability.prompt_hash("candidate B")
    assert len(viability.prompt_hash("x")) == 32


def test_prompt_hash_folds_in_message_schema_version(monkeypatch):
    # Bumping the per-job message schema must change the hash (so a change to which fields
    # we send — e.g. adding Location — flags existing scores stale), even for one prompt.
    before = viability.prompt_hash("same prompt")
    monkeypatch.setattr(viability, "_SCORING_INPUT_VERSION", "999")
    assert viability.prompt_hash("same prompt") != before
