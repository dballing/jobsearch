"""Tests for the small pure helpers across ingest / app / reformat."""
import pytest

import app
import ingest
import reformat

# Captured at import (before conftest's autouse _no_fx_network stubs app.get_fx_rates) so the
# get_fx_rates memo behaviour can be exercised for real.
_REAL_GET_FX_RATES = app.get_fx_rates


# ── ingest.extract_company_url ────────────────────────────────────────────────
def test_company_url_prefers_real_site():
    item = {"linkedin_org_url": "https://www.hdrinc.com",
            "domain_derived": "acme.net",
            "organization_url": "https://www.linkedin.com/company/hdr"}
    assert ingest.extract_company_url(item) == "https://www.hdrinc.com"


def test_company_url_falls_back_and_prefixes_bare_domain():
    assert ingest.extract_company_url({"domain_derived": "techop.net"}) == "https://techop.net"
    assert ingest.extract_company_url(
        {"organization_url": "https://jobs.lever.co/x"}) == "https://jobs.lever.co/x"


def test_company_url_ignores_none_and_empty():
    assert ingest.extract_company_url({"linkedin_org_url": "None",
                                       "domain_derived": "", "organization_url": None}) is None
    assert ingest.extract_company_url({}) is None


# ── app._toml_basic_string ────────────────────────────────────────────────────
def test_toml_basic_string_escapes():
    assert app._toml_basic_string("Acme") == '"Acme"'
    assert app._toml_basic_string('a"b\\c') == '"a\\"b\\\\c"'


# ── app._parse_salary_field ───────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("", None), ("  ", None),
    ("120000", 120000), ("$120,000", 120000), ("120k", 120000), ("150K", 150000),
    ("$1.5k", 1500),
])
def test_parse_salary_ok(raw, expected):
    assert app._parse_salary_field(raw) == expected


def test_parse_salary_invalid():
    with pytest.raises(ValueError):
        app._parse_salary_field("abc")


# ── currency: viability.currency_symbol + app.effective_currency/format_salary ──
import viability


@pytest.mark.parametrize("code,sym", [
    ("USD", "$"), ("usd", "$"), ("EUR", "€"), ("GBP", "£"),
    (None, "$"), ("", "$"), ("  ", "$"),   # missing/blank ⇒ dollars (backward-compatible default)
    ("CHF", "CHF "), ("zar", "ZAR "),      # unknown-but-present ⇒ spaced code prefix, never "$"
])
def test_currency_symbol(code, sym):
    assert viability.currency_symbol(code) == sym


def test_effective_currency_override_wins():
    # The per-lens override beats the feed currency; absent it, the feed value shows.
    assert app.effective_currency({"salary_currency": "USD", "salary_currency_actual": "EUR"}) == "EUR"
    assert app.effective_currency({"salary_currency": "GBP", "salary_currency_actual": None}) == "GBP"
    assert app.effective_currency({}) is None


def test_format_salary_uses_currency():
    assert app.format_salary({"salary_min": 120000, "salary_max": 150000, "salary_currency": "EUR"}) \
        == "€120k – €150k"
    assert app.format_salary({"salary_min": 175000, "salary_currency": "GBP"}) == "£175k+"
    assert app.format_salary({"salary_max": 150000, "salary_currency": "CHF"}) == "up to CHF 150k"
    # No currency ⇒ dollars, preserving prior output for the common US case.
    assert app.format_salary({"salary_min": 120000, "salary_max": 150000}) == "$120k – $150k"
    # Currency override wins over the feed currency.
    assert app.format_salary({"salary_min": 120000, "salary_max": 150000,
                              "salary_currency": "USD", "salary_currency_actual": "EUR"}) \
        == "€120k – €150k"


# ── app.format_salary_usd (USD-equivalent hover) ────────────────────────────────
# rates are USD-based: units of the currency per 1 USD, so USD = amount / rate.
_RATES = {"EUR": 0.90, "GBP": 0.80}


def test_format_salary_usd_range_and_open_ended():
    # €135k–€180k at 0.90 €/$ ⇒ $150k–$200k.
    assert app.format_salary_usd(
        {"salary_min": 135000, "salary_max": 180000, "salary_currency": "EUR"}, _RATES) \
        == "≈ $150k – $200k USD"
    # £160k+ at 0.80 £/$ ⇒ $200k+.
    assert app.format_salary_usd(
        {"salary_min": 160000, "salary_currency": "GBP"}, _RATES) == "≈ $200k+ USD"
    assert app.format_salary_usd(
        {"salary_max": 90000, "salary_currency": "EUR"}, _RATES) == "≈ up to $100k USD"


def test_format_salary_usd_none_cases():
    # USD / absent currency: already dollars, no tooltip.
    assert app.format_salary_usd({"salary_min": 120000, "salary_currency": "USD"}, _RATES) is None
    assert app.format_salary_usd({"salary_min": 120000}, _RATES) is None
    # No rates at all (offline / unavailable).
    assert app.format_salary_usd({"salary_min": 120000, "salary_currency": "EUR"}, None) is None
    # A currency we have no rate for → no tooltip rather than a wrong number.
    assert app.format_salary_usd({"salary_min": 120000, "salary_currency": "CHF"}, _RATES) is None
    # No salary band.
    assert app.format_salary_usd({"salary_currency": "EUR"}, _RATES) is None


def test_format_salary_usd_uses_currency_override():
    # The per-lens currency override drives the conversion, not the feed currency.
    row = {"salary_min": 135000, "salary_max": 180000,
           "salary_currency": "USD", "salary_currency_actual": "EUR"}
    assert app.format_salary_usd(row, _RATES) == "≈ $150k – $200k USD"


# ── app.get_fx_rates memo (asymmetric positive/negative TTL) ────────────────────
def test_get_fx_rates_negative_ttl_recovers(monkeypatch):
    """A failed (None) load is re-attempted after the short negative TTL, not held for the full
    hour — so a transient startup failure can't disable the tooltip while the disk cache is healthy.
    A successful load is then trusted for the positive TTL."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(app.time, "time", lambda: clock["t"])
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return None if calls["n"] == 1 else {"EUR": 0.9}   # first attempt fails, then succeeds

    monkeypatch.setattr(app.fx_rates, "load_rates", fake_load)
    monkeypatch.setattr(app, "_fx_memo", {"rates": None, "loaded_at": 0.0})

    assert _REAL_GET_FX_RATES() is None and calls["n"] == 1          # first attempt: None
    clock["t"] += app._FX_NEG_TTL - 1                                # still within negative TTL
    assert _REAL_GET_FX_RATES() is None and calls["n"] == 1          # memoized, no re-load
    clock["t"] += 2                                                  # negative TTL now elapsed
    assert _REAL_GET_FX_RATES() == {"EUR": 0.9} and calls["n"] == 2  # retried → success
    clock["t"] += app._FX_POS_TTL - 1                                # within positive TTL
    assert _REAL_GET_FX_RATES() == {"EUR": 0.9} and calls["n"] == 2  # trusted, no re-load


# ── build_grouped_job carries the USD hover onto single-job rows ─────────────────
def test_grouped_single_job_carries_usd_hover():
    """Regression: a single-posting group renders like a flat row via
    salary_text(job.salary_display, job.salary_usd_display), so build_grouped_job must copy
    salary_usd_display off the sub-row — otherwise non-USD single rows lost the tooltip in the
    (default) grouped view even though flat view had it."""
    sub = app.process_job_row(
        {"job_id": "j1", "salary_min": 120000, "salary_max": 150000, "salary_currency": "GBP"},
        frozenset(), {"GBP": 0.75})
    assert sub["salary_usd_display"]                       # sanity: the sub-row got it
    header = {"location_count": 1, "group_key": "j1",
              "salary_min": 120000, "salary_max": 150000}
    job = app.build_grouped_job(header, [sub])
    assert job["multi"] is False
    assert job["salary_display"] == sub["salary_display"]
    assert job["salary_usd_display"] == sub["salary_usd_display"]


def test_grouped_multi_row_carries_usd_hover():
    """The multi-location header path exposes the root's USD hover as group_salary_usd."""
    root = app.process_job_row(
        {"job_id": "r", "salary_min": 120000, "salary_max": 150000, "salary_currency": "EUR"},
        frozenset(), {"EUR": 0.9})
    dup = app.process_job_row(
        {"job_id": "d", "canonical_id": "r", "salary_min": 120000, "salary_max": 150000,
         "salary_currency": "EUR"}, frozenset(), {"EUR": 0.9})
    header = {"location_count": 2, "group_key": "r",
              "salary_min": 120000, "salary_max": 150000}
    job = app.build_grouped_job(header, [root, dup])
    assert job["multi"] is True
    assert job["group_salary_usd"] == root["salary_usd_display"]


# ── app._company_key ──────────────────────────────────────────────────────────
def test_company_key_actual_wins_and_normalizes():
    assert app._company_key("  Real Co ", "Feed Co") == "real co"
    assert app._company_key(None, " Feed Co ") == "feed co"
    assert app._company_key("", "") == ""


# ── app.process_job_row is_hot ────────────────────────────────────────────────
def _row(status, company, company_actual=None):
    return {"job_id": "j", "title": "T", "company": company, "company_actual": company_actual,
            "status": status, "labels": "[]", "source": "linkedin",
            "salary_min": None, "salary_max": None}


def test_is_hot_only_for_actionable_at_hotlisted():
    hot = {"acme corp"}
    assert app.process_job_row(_row("new", "Acme Corp"), hot)["is_hot"] is True
    assert app.process_job_row(_row("reviewing", "acme corp"), hot)["is_hot"] is True
    assert app.process_job_row(_row("applied", "Acme Corp"), hot)["is_hot"] is False
    assert app.process_job_row(_row("new", "Other Inc"), hot)["is_hot"] is False
    # company_actual (effective name) is what's matched
    assert app.process_job_row(_row("new", "Feed", "Acme Corp"), hot)["is_hot"] is True
    assert app.process_job_row(_row("new", "Acme Corp"), set())["is_hot"] is False


# ── reformat.content_preserved ────────────────────────────────────────────────
def test_content_preserved_identical():
    text = "We are hiring a Staff Engineer. You will build things. Apply now."
    assert reformat.content_preserved(text, "**We are hiring a Staff Engineer.**\n\n"
                                            "- You will build things.\n- Apply now.") is True


def test_content_preserved_repairs_whitespace_mangling():
    # Feed splits words; a faithful reformat repairs them — must NOT be flagged as changed.
    orig = "responsibilitie sproject managemen t and optimizatio n of pipelines"
    fixed = "responsibilities project management and optimization of pipelines"
    assert reformat.content_preserved(orig, fixed) is True


def test_content_preserved_rejects_dropped_content():
    orig = "Sentence one is here. " * 20
    dropped = "Sentence one is here. " * 10  # half the content gone
    assert reformat.content_preserved(orig, dropped) is False


# ── app._viability_factors (deterministic render order) ───────────────────────
# The scorer emits factors in whatever order it likes and may append extra axes beyond the six
# fixed FACTOR_DIMENSIONS; the preview panel must render them in a stable order so "the bottom
# line is always the bottom line" regardless of the model's whim.
def _dims(raw):
    """Just the dimension names, in render order, from a factors JSON string."""
    return [f["dimension"] for f in app._viability_factors(raw)]


def test_viability_factors_orders_fixed_dims_canonically_regardless_of_input():
    import json
    from viability import FACTOR_DIMENSIONS
    # Feed the six fixed dims reversed; they must come back in canonical order.
    shuffled = list(reversed(FACTOR_DIMENSIONS))
    raw = json.dumps([{"dimension": d, "score": 0} for d in shuffled])
    assert _dims(raw) == list(FACTOR_DIMENSIONS)


def test_viability_factors_pins_application_competitiveness_last():
    import json
    from viability import FACTOR_DIMENSIONS, RESUME_COMPETITIVENESS_DIMENSION
    # Model emits the bottom-line axis FIRST; it must be pushed to the very bottom.
    raw = json.dumps(
        [{"dimension": RESUME_COMPETITIVENESS_DIMENSION, "score": 1}]
        + [{"dimension": d, "score": 0} for d in FACTOR_DIMENSIONS]
    )
    assert _dims(raw) == list(FACTOR_DIMENSIONS) + [RESUME_COMPETITIVENESS_DIMENSION]


def test_viability_factors_sorts_unknown_extras_alphabetically_between():
    import json
    from viability import FACTOR_DIMENSIONS, RESUME_COMPETITIVENESS_DIMENSION
    # Two unknown model-chosen axes emitted out of alphabetical order, plus the pinned bottom line.
    raw = json.dumps([
        {"dimension": "work_life_balance", "score": 1},
        {"dimension": RESUME_COMPETITIVENESS_DIMENSION, "score": 2},
        {"dimension": "role_interest_fit", "score": 1},
        {"dimension": "growth_opportunity", "score": 0},
    ])
    # Fixed dims first (only role_interest_fit present), then unknowns alphabetically, then the
    # bottom-line axis last — fully deterministic no matter the emitted order.
    assert _dims(raw) == [
        "role_interest_fit", "growth_opportunity", "work_life_balance",
        RESUME_COMPETITIVENESS_DIMENSION,
    ]


def test_viability_factors_returns_none_for_empty_or_malformed():
    assert app._viability_factors(None) is None
    assert app._viability_factors("") is None
    assert app._viability_factors("not json") is None
    assert app._viability_factors("[]") is None
