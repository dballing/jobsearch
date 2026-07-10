"""Tests for viability.build_score_message — the per-job prompt payload. The live AI call
in score_job is out of scope, but the message *content* (which fields get sent) is pure
and worth locking down: location was silently missing, so the model dinged jobs for it."""
import viability


def test_location_included_when_present():
    msg = viability.build_score_message(
        {"title": "Sr. TPM, Wallet Services", "company": "Apple", "location": "Cary, NC"})
    assert "Location: Cary, NC" in msg


def test_location_omitted_when_absent():
    for job in ({"title": "T", "company": "C"},
                {"title": "T", "company": "C", "location": ""},
                {"title": "T", "company": "C", "location": None}):
        assert "Location:" not in viability.build_score_message(job)


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
