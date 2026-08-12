"""Pricing-drift guard for ai_config.MODEL_PRICING (see pricing_check.py).

Most of this is hermetic: pure parser/compare/cache tests that never touch the network. The
single live test fetches Anthropic's pricing page (24h cached), and by design *skips* when the
page is unreachable or its format changed — it fails only when a price genuinely drifted.
"""
import json
import warnings

import pytest

import pricing_check as pc

# A trimmed stand-in for the real pricing page: a Model-pricing table (base rates) followed by
# a Batch table (half price). The parser must read the first and ignore the second.
SAMPLE = """# Pricing

## Model pricing

| Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes | Cache Hits & Refreshes | Output Tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Fable 5 | $10 / MTok | $12.50 / MTok | $20 / MTok | $1 / MTok | $50 / MTok |
| Claude Opus 5 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |
| Claude Sonnet 5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |
| Claude Opus 4.1 ([retired](/x)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |

## Batch processing

| Model | Batch input | Batch output |
| --- | --- | --- |
| Claude Opus 5 | $2.50 / MTok | $12.50 / MTok |
"""


# ── pure helpers (hermetic) ───────────────────────────────────────────────────
def test_parse_price():
    assert pc.parse_price("$5 / MTok") == 5.0
    assert pc.parse_price("$0.80 / MTok") == 0.80
    assert pc.parse_price("free") is None


def test_display_to_model_id_maps_names_and_strips_notes():
    assert pc.display_to_model_id("Claude Opus 4.8") == "claude-opus-4-8"
    assert pc.display_to_model_id("Claude Sonnet 5") == "claude-sonnet-5"
    assert pc.display_to_model_id("Claude Mythos 5 ([limited availability](/x))") == "claude-mythos-5"
    assert pc.display_to_model_id("Claude Opus 4.1 ([retired](/x))") == "claude-opus-4-1"


def test_parse_reads_base_table_not_batch_table():
    prices = pc.parse_model_pricing_table(SAMPLE)
    # Base rates from the Model-pricing table — NOT the $2.50/$12.50 batch row below it.
    assert prices["claude-opus-5"] == (5.0, 25.0)
    assert prices["claude-fable-5"] == (10.0, 50.0)
    assert prices["claude-sonnet-5"] == (2.0, 10.0)
    assert prices["claude-opus-4-1"] == (15.0, 75.0)


def test_parse_returns_empty_when_table_absent():
    assert pc.parse_model_pricing_table("no pricing table here") == {}


def test_format_change_yields_no_known_models():
    """If the page's Model-pricing table header changes, the anchor is lost and parsing yields
    nothing recognizable — this is how the live test distinguishes 'format changed' (warn) from
    a normal comparison. Verified here so the detection can't silently rot."""
    changed = SAMPLE.replace("Base Input Tokens", "Input price")   # anchor header gone
    live = pc.parse_model_pricing_table(changed)
    assert set(live) & set(pc.MODEL_PRICING) == set()


def test_compare_passes_on_agreement_and_flags_drift():
    agree = {m: (round(p["input"] * 1e6, 4), round(p["output"] * 1e6, 4))
             for m, p in pc.MODEL_PRICING.items()}
    assert pc.compare_to_repo(agree) == []
    drifted = dict(agree, **{"claude-opus-5": (6.0, 30.0)})
    problems = pc.compare_to_repo(drifted)
    assert len(problems) == 1 and "claude-opus-5" in problems[0]


def test_compare_ignores_models_not_in_both():
    # A live-only model we don't price must not trip the check.
    assert pc.compare_to_repo({"claude-future-9": (1.0, 2.0)}) == []


# ── redirect recorder (hermetic) ──────────────────────────────────────────────
def test_recorder_records_permanent_redirects_only(monkeypatch):
    """The recorder flags 301/308 (permanent — 'update your URL') but ignores 302/307 (temporary).
    The super().redirect_request call is stubbed so we don't exercise urllib's real redirect
    machinery — we're only testing the recording decision."""
    monkeypatch.setattr(pc.urllib.request.HTTPRedirectHandler, "redirect_request",
                        lambda self, req, fp, code, msg, headers, newurl: "delegated")
    rec = pc._PermanentRedirectRecorder()
    assert rec.redirect_request(None, None, 302, "Found", {}, "https://temp") == "delegated"
    assert rec.permanent_to is None                       # temporary → not recorded
    rec.redirect_request(None, None, 308, "Permanent Redirect", {}, "https://perm")
    assert rec.permanent_to == "https://perm"             # permanent → recorded


# ── cache / fallback logic (hermetic — _http_get monkeypatched, never real network) ──
def test_fresh_cache_is_used_without_any_fetch(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not fetch when the cache is fresh")
    monkeypatch.setattr(pc, "_http_get", boom)
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"fetched_at": 1000.0, "markdown": SAMPLE}))
    md = pc.load_pricing_markdown(cache_path=str(cache), ttl=100, now=1050.0)  # 50s < ttl
    assert "Model pricing" in md


def test_stale_cache_used_when_fetch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_http_get",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"fetched_at": 0.0, "markdown": SAMPLE}))
    md = pc.load_pricing_markdown(cache_path=str(cache), ttl=100, now=10_000.0)  # stale, but fetch fails
    assert "Model pricing" in md


def test_none_when_no_cache_and_fetch_fails(tmp_path, monkeypatch, recwarn):
    """A connection failure (offline / DNS / timeout) is transient and external — return None
    (so the live test skips) and stay quiet, no warning."""
    monkeypatch.setattr(pc, "_http_get",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    md = pc.load_pricing_markdown(cache_path=str(tmp_path / "missing.json"), ttl=100, now=0.0)
    assert md is None
    assert len(recwarn) == 0                     # connection errors don't warn


def test_http_404_warns_then_skips_when_no_cache(tmp_path, monkeypatch):
    """A 404 means the page moved — a structural break, not a transient outage — so it warns
    (visible in pytest) before returning None."""
    def raise_404(*a, **k):
        raise pc.urllib.error.HTTPError("https://x/pricing.md", 404, "Not Found", None, None)
    monkeypatch.setattr(pc, "_http_get", raise_404)
    with pytest.warns(UserWarning, match="HTTP 404"):
        md = pc.load_pricing_markdown(cache_path=str(tmp_path / "missing.json"), ttl=100, now=0.0)
    assert md is None


def test_http_error_warns_but_still_uses_stale_cache(tmp_path, monkeypatch):
    """On an HTTP error with a stale cache present, warn (so the URL gets fixed) yet still fall
    back to the cache so the check keeps validating against last-known pricing."""
    def raise_500(*a, **k):
        raise pc.urllib.error.HTTPError("https://x", 500, "Server Error", None, None)
    monkeypatch.setattr(pc, "_http_get", raise_500)
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"fetched_at": 0.0, "markdown": SAMPLE}))
    with pytest.warns(UserWarning, match="HTTP 500"):
        md = pc.load_pricing_markdown(cache_path=str(cache), ttl=100, now=10_000.0)  # stale
    assert "Model pricing" in md


def test_permanent_redirect_warns_but_succeeds(tmp_path, monkeypatch):
    """A followed 301/308 still yields the content (check runs), but warns to repoint PRICING_URL
    and still caches the fetched markdown."""
    monkeypatch.setattr(pc, "_http_get", lambda *a, **k: (SAMPLE, "https://new.example/pricing.md"))
    cache = tmp_path / "c.json"
    with pytest.warns(UserWarning, match="permanently redirected"):
        md = pc.load_pricing_markdown(cache_path=str(cache), ttl=100, now=42.0)
    assert "Model pricing" in md
    assert "Model pricing" in json.loads(cache.read_text())["markdown"]   # still cached


def test_fetch_writes_cache_and_no_redirect_is_quiet(tmp_path, monkeypatch, recwarn):
    monkeypatch.setattr(pc, "_http_get", lambda *a, **k: (SAMPLE, None))
    cache = tmp_path / "c.json"
    md = pc.load_pricing_markdown(cache_path=str(cache), ttl=100, now=42.0)
    assert "Model pricing" in md
    written = json.loads(cache.read_text())
    assert written["fetched_at"] == 42.0 and "Model pricing" in written["markdown"]
    assert len(recwarn) == 0                     # a clean 200 (no redirect) doesn't warn


# ── the live drift check (network; 24h cached; skip on failure, fail on mismatch) ──
def test_live_pricing_matches_repo():
    markdown = pc.load_pricing_markdown()
    if markdown is None:
        # Genuinely external/transient (offline, page down, nothing cached) — quiet skip.
        pytest.skip("pricing page unreachable and nothing cached — skipping live check")
    live = pc.parse_model_pricing_table(markdown)
    if not (set(live) & set(pc.MODEL_PRICING)):
        # The page WAS fetched but we recognized none of our models in it — the table format or
        # the model display names almost certainly changed, so the guard is validating nothing.
        # Warn loudly (pytest surfaces warnings by default, unlike skip reasons), then skip
        # rather than hard-fail the suite over an external page change unrelated to this repo.
        warnings.warn(
            "Pricing drift check parsed 0 known models from the Anthropic pricing page — its "
            "format likely changed and the guard is no longer validating anything. Update "
            "pricing_check.parse_model_pricing_table / display_to_model_id.",
            stacklevel=2)
        pytest.skip("pricing page format changed (no known models parsed) — see warning above")
    problems = pc.compare_to_repo(live)
    assert not problems, "MODEL_PRICING drifted from live Anthropic pricing:\n  " + "\n  ".join(problems)
