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


def test_work_arrangement_override_wins_over_feed():
    # Feed says On-site; a recruiter-confirmed Hybrid override must win, sent verbatim.
    raw = _json.dumps({"ai_work_arrangement": "On-site"})
    job = {"title": "T", "company": "Apple", "raw": raw, "work_arrangement_actual": "Hybrid"}
    assert "Work arrangement: Hybrid" in viability.build_score_message(job)


def test_work_arrangement_override_used_when_no_feed():
    job = {"title": "T", "company": "C", "work_arrangement_actual": "Fully remote"}
    assert "Work arrangement: Fully remote" in viability.build_score_message(job)


def test_blank_override_falls_back_to_feed():
    raw = _json.dumps({"ai_work_arrangement": "Hybrid", "ai_work_arrangement_office_days": 2})
    job = {"title": "T", "company": "C", "raw": raw, "work_arrangement_actual": ""}
    assert "Work arrangement: Hybrid — 2 days/week in office" in viability.build_score_message(job)


def test_has_title_and_company_lines():
    msg = viability.build_score_message({"title": "Staff PM", "company": "Acme"})
    assert "Job title: Staff PM" in msg and "Company: Acme" in msg


def test_company_override_drops_via_note_for_a_known_job_board():
    """A job board (Ladders) is pure distribution — surfacing 'posted via Ladders' made the model
    infer a staffing/contract role, so with the override we show just the real employer."""
    msg = viability.build_score_message(
        {"title": "T", "company": "Ladders", "company_actual": "Capital One"})
    assert "Company: Capital One\n" in msg
    assert "posted via" not in msg


def test_company_override_keeps_via_note_for_a_possible_recruiter():
    """An unknown source could be a recruiter/staffing firm — keep the 'posted via' context so the
    model can weigh a genuine contract arrangement."""
    msg = viability.build_score_message(
        {"title": "T", "company": "BigStaffing LLC", "company_actual": "Capital One"})
    assert "Company: Capital One (posted via BigStaffing LLC)" in msg


def test_is_job_board_normalizes_names():
    for name in ("Ladders", "The Ladders", "ladders.com", "LINKEDIN", "We Work Remotely",
                 "RemoteHunter", "Built In"):
        assert viability.is_job_board(name), name
    for name in ("Capital One", "Jobot", "Robert Half", "Netflix", "", "Talent Solutions Inc"):
        assert not viability.is_job_board(name), name


def test_boilerplate_defers_contract_judgment_to_the_via_note():
    """The employment-relationship guidance must stay in the system boilerplate: judge a
    contract/staffing arrangement from the '(posted via …)' note or explicit terms, not from
    generic 'client' body text. Wording can evolve; these stable anchors guard the intent."""
    bp = viability._SYSTEM_BOILERPLATE.lower()
    assert "posted via" in bp                    # the curated third-party signal
    assert "client" in bp and "neutral" in bp    # generic client boilerplate = neutral


def test_boilerplate_demands_a_single_plain_reason():
    """The one-sentence rule that keeps a reasoning model from dumping its scratchpad into
    `reason` must stay in the boilerplate."""
    bp = viability._SYSTEM_BOILERPLATE.lower()
    assert "single plain sentence" in bp and "scratchpad" in bp


def test_boilerplate_keeps_named_company_exclusions_from_generalizing():
    """A named-company preference must not bleed onto similar/adjacent companies (the guardrail
    that stopped a Google-only 'avoid' from penalizing Microsoft)."""
    bp = viability._SYSTEM_BOILERPLATE.lower()
    assert "company-preference note" in bp and "adjacent" in bp


# ── _scorer_thinking: adaptive for reasoning models, disabled for Haiku/older ──
def test_scorer_thinking_adaptive_for_reasoning_models():
    for m in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5",
              "claude-sonnet-4-6"):
        assert viability._scorer_thinking(m) == {"type": "adaptive"}, m


def test_scorer_thinking_disabled_for_haiku_and_older():
    for m in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-5", "", None):
        assert viability._scorer_thinking(m) == {"type": "disabled"}, m


# ── score_job: wiring (parsed rating/reason + per-model thinking) via the fake client ──
def test_score_job_uses_adaptive_thinking_and_medium_effort_for_sonnet_5():
    client = _FakeClient('{"rating": "high", "reason": "Strong scope and comp match."}')
    rating, reason, usage = viability.score_job(
        client, "candidate prompt", {"title": "Staff PM", "company": "Acme"},
        model="claude-sonnet-5")
    assert (rating, reason) == ("high", "Strong scope and comp match.")
    assert client.last_kwargs["thinking"] == {"type": "adaptive"}
    assert client.last_kwargs["output_config"] == {"effort": "medium"}
    assert client.last_kwargs["max_tokens"] >= 2048     # headroom for the thinking tokens
    assert usage is not None


def test_score_job_keeps_thinking_disabled_and_tiny_budget_for_haiku():
    client = _FakeClient('{"rating": "medium", "reason": "Decent but comp is light."}')
    rating, reason, _ = viability.score_job(
        client, "p", {"title": "T", "company": "C"}, model="claude-haiku-4-5")
    assert (rating, reason) == ("medium", "Decent but comp is light.")
    assert client.last_kwargs["thinking"] == {"type": "disabled"}
    assert "output_config" not in client.last_kwargs
    assert client.last_kwargs["max_tokens"] == 256


def test_score_job_reads_the_text_block_past_a_leading_thinking_block():
    """With thinking on, content[0] is a thinking block — the parser must find the text block."""
    class _ThinkThenText:
        def __init__(self):
            self.messages = self
        def create(self, **kwargs):
            think = type("T", (), {"type": "thinking", "thinking": "…hidden…"})()
            text = type("X", (), {"type": "text",
                                  "text": '{"rating": "low", "reason": "Poor scope fit."}'})()
            m = type("M", (), {})()
            m.content = [think, text]
            m.usage = _FakeUsage()
            return m
    rating, reason, _ = viability.score_job(
        _ThinkThenText(), "p", {"title": "T", "company": "C"}, model="claude-opus-5")
    assert (rating, reason) == ("low", "Poor scope fit.")


def test_title_override_wins_in_score_message():
    """A title override replaces the scraped title the model scores against."""
    msg = viability.build_score_message(
        {"title": "Generic Req 12345", "title_actual": "Senior TPM", "company": "Acme"})
    assert msg.startswith("Job title: Senior TPM\n")
    assert "Generic Req 12345" not in msg


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


# ── Geographic pre-assessment: the verdict replaces the raw Location line ──────
def test_geo_note_replaces_location_line():
    # With a pre-assessed verdict, the scorer must see the authoritative 'Geographic fit'
    # line and NOT the raw location list (which it read unreliably).
    import json
    raw = json.dumps({"locations_derived": ["Phoenix, AZ, US",
                                             "Washington, District of Columbia, US"]})
    msg = viability.build_score_message(
        {"title": "T", "company": "C", "raw": raw},
        geo_note="PREFERRED (best match: Washington, District of Columbia, US)")
    assert "Geographic fit" in msg
    assert "PREFERRED (best match: Washington, District of Columbia, US)" in msg
    assert "Location:" not in msg          # raw list suppressed
    assert "Phoenix" not in msg            # the buried non-preferred city isn't shown


def test_no_geo_note_keeps_raw_location_line():
    # Legacy path (no location_prompt configured / sub-call failed): fall back to raw list.
    msg = viability.build_score_message({"title": "T", "company": "C", "location": "Cary, NC"})
    assert "Location: Cary, NC" in msg
    assert "Geographic fit" not in msg


def test_geo_note_precedes_work_arrangement_and_salary():
    import json
    raw = json.dumps({"ai_work_arrangement": "Hybrid", "ai_work_arrangement_office_days": 2})
    msg = viability.build_score_message(
        {"title": "T", "company": "C", "raw": raw, "salary_min": 150000},
        geo_note="ACCEPTABLE (best match: Raleigh, NC)")
    assert msg.index("Geographic fit") < msg.index("Work arrangement:") < msg.index("Salary:")


# ── geo_note(): compose the verdict phrase from a (fit, match) pair ────────────
def test_geo_note_tiers():
    assert viability.geo_note("preferred", "Washington, DC") == "PREFERRED (best match: Washington, DC)"
    # The 'good' rung distinguishes a second-preference region (e.g. NC) from merely-acceptable.
    assert viability.geo_note("good", "Raleigh, NC") == "GOOD (best match: Raleigh, NC)"
    assert viability.geo_note("acceptable", "Aiken, SC") == "ACCEPTABLE (best match: Aiken, SC)"
    assert viability.geo_note("preferred", "") == "PREFERRED"   # no match string → tier only
    assert "POOR" in viability.geo_note("poor", "")
    # Anything not a recognized tier → None, so the caller falls back to the raw list.
    assert viability.geo_note(None, "x") is None
    assert viability.geo_note("great", "x") is None


# ── clamp_viability_for_geo(): POOR geography forces the final rating to low ────
def test_clamp_forces_medium_to_low_and_appends_note_on_poor():
    """A POOR fit + a medium/high rating → low, with the model's reason kept and the override
    noted. This is the WFAA case: the scorer said 'excludes candidate' yet returned medium."""
    rating, reason = viability.clamp_viability_for_geo(
        "poor", "medium", "Strong TPM fit but remote-state restriction excludes candidate.")
    assert rating == "low"
    assert reason.startswith("Strong TPM fit")          # model's own reasoning preserved
    assert "Forced to LOW" in reason and "POOR" in reason


def test_clamp_forces_high_to_low_on_poor():
    rating, reason = viability.clamp_viability_for_geo("poor", "high", "Great role.")
    assert rating == "low"
    assert "Forced to LOW" in reason


def test_clamp_leaves_already_low_untouched_no_redundant_suffix():
    """Already low → verbatim: the rating is right and the suffix would be noise."""
    rating, reason = viability.clamp_viability_for_geo("poor", "low", "Wrong location entirely.")
    assert rating == "low"
    assert reason == "Wrong location entirely."
    assert "Forced to LOW" not in reason


def test_clamp_passes_through_non_poor_tiers():
    """acceptable/good/preferred keep the model's verdict — only the bottom tier is disqualifying."""
    for fit in ("acceptable", "good", "preferred", None):
        assert viability.clamp_viability_for_geo(fit, "high", "r") == ("high", "r")
        assert viability.clamp_viability_for_geo(fit, "medium", "r") == ("medium", "r")


def test_clamp_leaves_none_rating_for_caller_to_skip():
    """A failed score (None) isn't turned into a low — the caller skips it as before."""
    assert viability.clamp_viability_for_geo("poor", None, "") == (None, "")


# ── manual "remote in an unsupported location" flag ────────────────────────────
def test_is_manual_geo_poor_matches_only_the_sentinel():
    """Detected only for the exact GEO_UNSUPPORTED_ARRANGEMENT override; other work
    arrangements (and none at all) are not the manual POOR flag."""
    assert viability.is_manual_geo_poor(
        {"work_arrangement_actual": viability.GEO_UNSUPPORTED_ARRANGEMENT})
    assert viability.is_manual_geo_poor(
        {"work_arrangement_actual": f"  {viability.GEO_UNSUPPORTED_ARRANGEMENT}  "})  # whitespace-tolerant
    for other in ("On-site", "Fully remote", "", None):
        assert not viability.is_manual_geo_poor({"work_arrangement_actual": other})
    assert not viability.is_manual_geo_poor({})  # key absent (e.g. manual job)


def test_clamp_manual_uses_distinct_suffix():
    """manual=True clamps identically but attributes the override to a manual flag, not the
    AI location verdict — so the two POOR paths are distinguishable in the stored reason."""
    rating, reason = viability.clamp_viability_for_geo(
        "poor", "high", "Strong TPM fit, great comp.", manual=True)
    assert rating == "low"
    assert reason.startswith("Strong TPM fit")            # model's own reasoning preserved
    assert "Forced to LOW" in reason and "manually flagged" in reason
    assert "geographic fit is POOR" not in reason         # not the AI-verdict wording


def test_clamp_manual_flag_ignored_when_not_poor():
    """The manual flag only matters on a POOR fit — a non-POOR/None fit passes through
    untouched, and an already-low rating gets no redundant suffix."""
    assert viability.clamp_viability_for_geo("good", "high", "r", manual=True) == ("high", "r")
    assert viability.clamp_viability_for_geo("poor", "low", "r", manual=True) == ("low", "r")


# ── assess_location_fit(): the focused sub-call (fake client, no network) ──────
class _FakeUsage:
    input_tokens = output_tokens = cache_creation_input_tokens = cache_read_input_tokens = 0


class _FakeClient:
    """Minimal stand-in: records the create() kwargs and returns a canned JSON reply."""
    def __init__(self, reply_text):
        self._reply = reply_text
        self.last_kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        msg = type("M", (), {})()
        msg.content = [type("C", (), {"type": "text", "text": self._reply})()]
        msg.usage = _FakeUsage()
        return msg


def test_assess_location_fit_parses_verdict_and_sends_prefs_locations_and_description():
    raw = _json.dumps({"locations_derived": ["Phoenix, AZ, US", "Washington, District of Columbia, US"],
                       "ai_work_arrangement": "On-site"})
    client = _FakeClient('{"fit": "preferred", "match": "Washington, District of Columbia, US"}')
    fit, match, usage = viability.assess_location_fit(
        client, "DC strongly preferred; AZ poor.",
        {"title": "T", "company": "C", "raw": raw,
         "job_description": "Remote candidates must reside in Arizona."})
    assert fit == "preferred" and match == "Washington, District of Columbia, US"
    assert usage is not None
    # The candidate's location_prompt rides in the (cached) system block…
    assert "DC strongly preferred" in client.last_kwargs["system"][0]["text"]
    # …and every job location + work arrangement + the description (where eligibility
    # conditions live) is in the user message.
    user = client.last_kwargs["messages"][0]["content"]
    assert "Phoenix, AZ, US" in user and "Washington, District of Columbia, US" in user
    assert "On-site" in user
    assert "Remote candidates must reside in Arizona." in user


def test_assess_location_fit_caps_description():
    client = _FakeClient('{"fit": "good", "match": "x"}')
    viability.assess_location_fit(
        client, "prefs", {"location": "Durham, NC", "job_description": "z" * 9000})
    user = client.last_kwargs["messages"][0]["content"]
    assert user.count("z") == viability._MAX_GEO_DESC_CHARS   # description truncated (no 'z' elsewhere)


def test_assess_location_fit_omits_description_when_disabled():
    # location_use_description=false: neither the description nor its handling clause is sent.
    client = _FakeClient('{"fit": "good", "match": "x"}')
    viability.assess_location_fit(
        client, "prefs",
        {"location": "Durham, NC", "job_description": "Remote only for Texas residents."},
        include_description=False)
    user = client.last_kwargs["messages"][0]["content"]
    system = client.last_kwargs["system"][0]["text"]
    assert "Remote only for Texas residents." not in user and "Job description" not in user
    assert "eligibility condition" not in system   # the description-handling clause is dropped


def test_assess_location_fit_rejects_invalid_fit():
    client = _FakeClient('{"fit": "maybe", "match": "somewhere"}')
    assert viability.assess_location_fit(client, "prefs", {"location": "Cary, NC"}) == (None, None, None)


def test_assess_location_fit_unparseable_reply():
    client = _FakeClient("I could not determine the fit.")
    assert viability.assess_location_fit(client, "prefs", {"location": "Cary, NC"}) == (None, None, None)


def test_assess_location_fit_skips_when_nothing_to_judge():
    # No locations and no work arrangement → don't spend a call; return the fallback sentinel.
    client = _FakeClient('{"fit": "preferred", "match": "x"}')
    assert viability.assess_location_fit(client, "prefs", {"title": "T", "company": "C"}) == (None, None, None)
    assert client.last_kwargs is None  # never called the API


# ── prompt_hash: staleness change-detector ────────────────────────────────────
def test_prompt_hash_deterministic_and_prompt_sensitive():
    assert viability.prompt_hash("candidate A") == viability.prompt_hash("candidate A")
    assert viability.prompt_hash("candidate A") != viability.prompt_hash("candidate B")
    assert len(viability.prompt_hash("x")) == 32


def test_prompt_hash_folds_in_location_prompt():
    # Editing geography prefs must flag scores stale even when the candidate prompt is
    # unchanged, since the geographic verdict the scorer sees depends on location_prompt.
    base = viability.prompt_hash("same prompt")
    assert viability.prompt_hash("same prompt", "DC preferred") != base
    assert viability.prompt_hash("same prompt", "DC preferred") \
        != viability.prompt_hash("same prompt", "NYC preferred")


def test_prompt_hash_folds_in_geo_uses_description():
    # Flipping the location_use_description toggle must flag scores stale (it changes the geo
    # verdict), and the default must equal geo_uses_description=True.
    base = viability.prompt_hash("p", "lp", True)
    assert viability.prompt_hash("p", "lp", False) != base
    assert viability.prompt_hash("p", "lp") == base


def test_prompt_hash_folds_in_message_schema_version(monkeypatch):
    # Bumping the per-job message schema must change the hash (so a change to which fields
    # we send — e.g. adding Location — flags existing scores stale), even for one prompt.
    before = viability.prompt_hash("same prompt")
    monkeypatch.setattr(viability, "_SCORING_INPUT_VERSION", "999")
    assert viability.prompt_hash("same prompt") != before
