"""Tests for the small pure helpers across ingest / app / reformat."""
import pytest

import app
import ingest
import reformat


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
