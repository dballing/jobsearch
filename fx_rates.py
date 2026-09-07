"""Foreign-exchange rates for showing a USD-equivalent of non-USD salaries.

Salaries are stored and displayed in their real currency (see viability.currency_symbol /
app.effective_currency) so a €200k role isn't mis-shown or mis-scored as dollars. But for a
US-based candidate a foreign band is easier to sanity-check with a rough dollar figure alongside
it, so the UI shows an approximate USD conversion on hover. This module supplies the rates.

Modelled on pricing_check.py: a free, unauthenticated JSON endpoint (no API key — keys never
enter this repo), cached on disk for 24h so the network is hit at most once a day, and fail-soft
throughout — any fetch/parse failure with nothing cached returns None, and the caller simply
omits the tooltip rather than erroring. The parse/convert/cache logic is split into pure
functions so it unit-tests without a live network; see tests/test_fx_rates.py.

Rates are expressed the way the endpoint returns them: base USD, so rates[code] is "units of
`code` per 1 USD" (rates["EUR"] == 0.92 ⇒ €0.92 to the dollar). Converting an amount in `code`
back to USD therefore divides by that rate (see to_usd).
"""
import json
import os
import tempfile
import time
import urllib.request

# open.er-api.com is the free tier of exchangerate-api.com: no key, USD-based daily rates,
# stable JSON shape ({"result": "success", "base_code": "USD", "rates": {...}}). If it ever
# moves, repoint this — load_rates falls back to a stale cache meanwhile.
FX_URL = "https://open.er-api.com/v6/latest/USD"
CACHE_TTL_SECONDS = 24 * 60 * 60
# System temp (not the repo) so the cache persists across runs without being committed or needing
# a .gitignore entry; overridable for tests.
_DEFAULT_CACHE = os.path.join(tempfile.gettempdir(), "jobsearch_fx_cache.json")


def parse_rates(payload: object) -> "dict[str, float]":
    """Extract {currency_code: units_per_USD} from a decoded open.er-api.com response.

    Defensive: returns {} unless the payload is a USD-based success response carrying a rates
    object, and keeps only entries whose value is a real positive number (a zero or negative rate
    can't be inverted, and a non-numeric one is corrupt). {} signals the caller to skip rather
    than raise — a schema change degrades to "no tooltip", not a crash. USD is dropped: converting
    dollars to dollars needs no rate, and its presence (==1) would just be noise."""
    if not isinstance(payload, dict):
        return {}
    # A "result": "error" body (rate-limit / bad base) still parses as JSON; only trust success.
    if payload.get("result") not in (None, "success"):
        return {}
    if str(payload.get("base_code") or "USD").upper() != "USD":
        return {}
    raw = payload.get("rates")
    if not isinstance(raw, dict):
        return {}
    rates: "dict[str, float]" = {}
    for code, value in raw.items():
        c = str(code).strip().upper()
        if c == "USD":
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            rates[c] = float(value)
    return rates


def to_usd(amount: "int | float | None", code: object, rates: "dict[str, float] | None") -> "float | None":
    """Convert `amount` in currency `code` to USD using a USD-based `rates` map, or None when it
    can't/needn't be done: no amount, no rates, an absent/blank code, an explicit USD amount
    (already dollars — no tooltip wanted), or a currency we have no rate for. rates[code] is units
    of `code` per USD, so USD = amount / rate."""
    if not amount or not rates:
        return None
    c = str(code or "").strip().upper()
    if not c or c == "USD":
        return None
    rate = rates.get(c)
    if not rate:                     # missing or non-positive (parse_rates already drops <= 0)
        return None
    return amount / rate


def _http_get_json(url: str, timeout: float = 8) -> object:
    """GET `url` and decode the body as JSON. Raises (urllib/OSError on a connection failure,
    ValueError on non-JSON) so load_rates can branch on failure. Kept separate from the cache
    logic so both unit-test without a live server. Short timeout: this runs inline on a page
    request, and a slow/hung endpoint must degrade to 'no tooltip' quickly, not stall the page."""
    req = urllib.request.Request(url, headers={"User-Agent": "jobsearch-fx-rates"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_rates(cache_path: str = _DEFAULT_CACHE, ttl: int = CACHE_TTL_SECONDS,
               url: str = FX_URL, now: "float | None" = None) -> "dict[str, float] | None":
    """Return {currency_code: units_per_USD}, cached on disk for `ttl` seconds — network hit only
    on a cold or stale cache. On a fetch/parse failure, fall back to a stale cache if one exists
    (so conversions still work offline against last-known rates); return None only when there's
    nothing cached AND the fetch failed, or the response parsed to no usable rates. `now`/`url`/
    `cache_path` are injectable so the cache/fallback logic is unit-testable without clock or
    network. Fail-soft by design: the caller treats None as 'omit the USD tooltip'."""
    now = time.time() if now is None else now
    try:
        with open(cache_path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        cached = None
    if cached and (now - cached.get("fetched_at", 0)) < ttl and cached.get("rates"):
        return cached["rates"]
    try:
        rates = parse_rates(_http_get_json(url))
    except Exception:
        # Offline / DNS / timeout / non-JSON — transient or external; fall back to a stale cache
        # if we have one, else None so the caller omits the tooltip.
        return cached["rates"] if cached else None
    if not rates:
        # Fetch worked but yielded nothing usable (schema change / error body). Prefer stale
        # known-good rates over showing nothing; only None when we also have no cache.
        return cached["rates"] if cached else None
    try:
        with open(cache_path, "w") as f:
            json.dump({"fetched_at": now, "rates": rates}, f)
    except OSError:
        pass                             # caching is best-effort; a write failure just re-fetches
    return rates
