"""Hermetic tests for fx_rates: the pure parse/convert helpers and the disk-cache/fallback logic
in load_rates. The network fetch (_http_get_json) is monkeypatched or injected so nothing here
touches a live endpoint — mirroring test_pricing_live's split of pure logic from I/O."""
import json

import pytest

import fx_rates


# ── parse_rates ─────────────────────────────────────────────────────────────────
def test_parse_rates_happy_path_drops_usd():
    payload = {"result": "success", "base_code": "USD",
               "rates": {"USD": 1, "EUR": 0.9, "GBP": 0.8}}
    assert fx_rates.parse_rates(payload) == {"EUR": 0.9, "GBP": 0.8}


def test_parse_rates_normalizes_and_filters_bad_values():
    payload = {"base_code": "USD",
               "rates": {"eur": 0.9, "ZWL": 0, "AAA": -1, "BBB": "x", "CCC": True}}
    # lower-cased key upper-cased; zero/negative/non-numeric/bool all dropped.
    assert fx_rates.parse_rates(payload) == {"EUR": 0.9}


@pytest.mark.parametrize("payload", [
    None, [], "nope", 42,
    {"result": "error", "base_code": "USD", "rates": {"EUR": 0.9}},   # error body
    {"base_code": "EUR", "rates": {"USD": 1.1}},                      # non-USD base
    {"result": "success", "base_code": "USD"},                        # no rates object
    {"result": "success", "base_code": "USD", "rates": "not-a-dict"},
])
def test_parse_rates_rejects_malformed(payload):
    assert fx_rates.parse_rates(payload) == {}


# ── to_usd ──────────────────────────────────────────────────────────────────────
def test_to_usd_converts():
    # 180000 EUR at 0.9 €/$ ⇒ 200000 USD.
    assert fx_rates.to_usd(180000, "EUR", {"EUR": 0.9}) == pytest.approx(200000)
    assert fx_rates.to_usd(160000, "gbp", {"GBP": 0.8}) == pytest.approx(200000)


@pytest.mark.parametrize("amount,code,rates", [
    (0, "EUR", {"EUR": 0.9}),            # no amount
    (None, "EUR", {"EUR": 0.9}),
    (100000, "EUR", None),               # no rates
    (100000, "EUR", {}),
    (100000, "USD", {"EUR": 0.9}),       # already dollars
    (100000, "", {"EUR": 0.9}),          # blank code
    (100000, None, {"EUR": 0.9}),
    (100000, "CHF", {"EUR": 0.9}),       # unknown currency
])
def test_to_usd_none_cases(amount, code, rates):
    assert fx_rates.to_usd(amount, code, rates) is None


# ── load_rates: cache / fallback / write ────────────────────────────────────────
_GOOD = {"result": "success", "base_code": "USD", "rates": {"EUR": 0.9}}


def _cache_file(tmp_path):
    return str(tmp_path / "fx_cache.json")


def test_load_rates_fetches_and_writes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fx_rates, "_http_get_json", lambda url, timeout=8: _GOOD)
    cache = _cache_file(tmp_path)
    assert fx_rates.load_rates(cache_path=cache, url="http://x", now=1000) == {"EUR": 0.9}
    # Persisted for the next process/run.
    with open(cache) as f:
        stored = json.load(f)
    assert stored["rates"] == {"EUR": 0.9} and stored["fetched_at"] == 1000


def test_load_rates_uses_fresh_cache_without_fetching(tmp_path, monkeypatch):
    cache = _cache_file(tmp_path)
    with open(cache, "w") as f:
        json.dump({"fetched_at": 1000, "rates": {"EUR": 0.85}}, f)

    def _boom(url, timeout=8):
        raise AssertionError("should not fetch when cache is fresh")

    monkeypatch.setattr(fx_rates, "_http_get_json", _boom)
    # 50s < 100s ttl ⇒ served from cache, no network.
    assert fx_rates.load_rates(cache_path=cache, ttl=100, url="http://x", now=1050) == {"EUR": 0.85}


def test_load_rates_falls_back_to_stale_cache_on_failure(tmp_path, monkeypatch):
    cache = _cache_file(tmp_path)
    with open(cache, "w") as f:
        json.dump({"fetched_at": 0, "rates": {"EUR": 0.7}}, f)   # very old

    def _fail(url, timeout=8):
        raise OSError("offline")

    monkeypatch.setattr(fx_rates, "_http_get_json", _fail)
    # Stale (0 + ttl < now) → tries to fetch → fails → returns the stale rates rather than None.
    assert fx_rates.load_rates(cache_path=cache, ttl=100, url="http://x", now=10_000) == {"EUR": 0.7}


def test_load_rates_none_when_cold_and_offline(tmp_path, monkeypatch):
    def _fail(url, timeout=8):
        raise OSError("offline")

    monkeypatch.setattr(fx_rates, "_http_get_json", _fail)
    assert fx_rates.load_rates(cache_path=_cache_file(tmp_path), url="http://x", now=1) is None


def test_load_rates_none_when_response_unusable_and_no_cache(tmp_path, monkeypatch):
    # Fetch works but parses to nothing (schema change / error body) and no cache to fall back on.
    monkeypatch.setattr(fx_rates, "_http_get_json",
                        lambda url, timeout=8: {"result": "error"})
    assert fx_rates.load_rates(cache_path=_cache_file(tmp_path), url="http://x", now=1) is None
