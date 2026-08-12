"""Live-pricing drift check for ai_config.MODEL_PRICING.

Anthropic doesn't expose per-token prices via the Models API — the only machine-readable
source is the public pricing page — so this fetches that page's markdown and compares its base
input/output per-MTok rates against our hard-coded MODEL_PRICING. A rate change (or a current
model we forgot to price) then surfaces within a day instead of silently skewing every cost
line.

Kept off the hot path so it doesn't undermine the otherwise-hermetic test suite: the markdown
is cached on disk for 24h (network hit only on a cold/stale cache), a fetch failure falls back
to a stale cache and finally to "skip", and the pytest wrapper fails *only* on a real price
mismatch. The parsing/compare logic is split into pure functions so it unit-tests without any
network; see tests/test_pricing_live.py.
"""
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import warnings

from ai_config import MODEL_PRICING

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
CACHE_TTL_SECONDS = 24 * 60 * 60
# System temp (not the repo) so the cache persists across runs on a dev machine without being
# committed or needing a .gitignore entry; overridable for tests.
_DEFAULT_CACHE = os.path.join(tempfile.gettempdir(), "jobsearch_pricing_cache.json")


def parse_price(cell: str) -> "float | None":
    """Pull the dollar figure out of a '$5 / MTok' pricing cell, or None if the cell has none."""
    m = re.search(r"\$([\d.]+)", cell)
    return float(m.group(1)) if m else None


def display_to_model_id(display: str) -> str:
    """Map a pricing-table display name to its API model id: 'Claude Opus 4.8' → 'claude-opus-4-8'.
    Trailing markdown links/parentheticals (e.g. a '([retired …])' note) are dropped first."""
    name = re.split(r"[(\[]", display)[0].strip()
    return name.lower().replace(" ", "-").replace(".", "-")


def parse_model_pricing_table(markdown: str) -> "dict[str, tuple[float, float]]":
    """Extract {model_id: (input_per_mtok, output_per_mtok)} from the page's 'Model pricing' table.

    Scoped to the one table whose header carries 'Base Input Tokens', so the half-price Batch
    table (and every other table on the page) is ignored — matching on those would produce
    spurious mismatches. Returns {} when that table can't be found, which the caller treats as
    'couldn't parse, skip' rather than a failure."""
    prices: "dict[str, tuple[float, float]]" = {}
    in_table = False
    for line in markdown.splitlines():
        s = line.strip()
        if not in_table:
            # The Model-pricing header is the anchor; the Batch table's header ('Batch input')
            # doesn't contain this string, so we never start capturing there.
            if s.startswith("|") and "Base Input Tokens" in s:
                in_table = True
            continue
        if not s.startswith("|"):
            break                        # first non-table line ends the section
        if set(s) <= set("|-: "):
            continue                     # header/body separator row (|---|---|)
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 6:               # Model | Base Input | 5m | 1h | Hits | Output
            continue
        inp, out = parse_price(cells[1]), parse_price(cells[-1])
        if inp is not None and out is not None:
            prices[display_to_model_id(cells[0])] = (inp, out)
    return prices


def compare_to_repo(live: "dict[str, tuple[float, float]]") -> "list[str]":
    """Return a mismatch line for every model priced in BOTH the repo and the live table whose
    base input/output rate disagrees. Models present on only one side are ignored: a repo-only
    model is a repo bug caught elsewhere (it wouldn't be on the live page to compare), and a
    live-only model is simply one we don't use. Rounded to 4dp to shrug off float noise."""
    problems = []
    for model, pricing in MODEL_PRICING.items():
        if model not in live:
            continue
        repo = (round(pricing["input"] * 1_000_000, 4), round(pricing["output"] * 1_000_000, 4))
        live_pair = (round(live[model][0], 4), round(live[model][1], 4))
        if repo != live_pair:
            problems.append(
                f"{model}: repo ${repo[0]}/${repo[1]} vs live ${live_pair[0]}/${live_pair[1]} per MTok")
    return problems


class _PermanentRedirectRecorder(urllib.request.HTTPRedirectHandler):
    """Follows redirects like the default handler (so a moved page still fetches) but records the
    target of any *permanent* (301/308) redirect — the signal that PRICING_URL should be repointed.
    Temporary redirects (302/303/307) are followed silently and not recorded."""

    def __init__(self):
        self.permanent_to = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code in (301, 308):
            self.permanent_to = newurl
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str, timeout: float = 15) -> "tuple[str, str | None]":
    """GET `url`, following redirects, returning (body_text, permanent_redirect_target_or_None).
    The second value is set only when a 301/308 was followed. Raises urllib.error.HTTPError on a
    4xx/5xx and OSError/URLError on a connection failure — same as urllib — so the caller branches
    on structural vs transient failures. Kept separate from the cache logic so both unit-test
    without a live server."""
    recorder = _PermanentRedirectRecorder()
    opener = urllib.request.build_opener(recorder)
    req = urllib.request.Request(url, headers={"User-Agent": "jobsearch-pricing-check"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8"), recorder.permanent_to


def load_pricing_markdown(cache_path: str = _DEFAULT_CACHE, ttl: int = CACHE_TTL_SECONDS,
                          url: str = PRICING_URL, now: "float | None" = None) -> "str | None":
    """Return the pricing-page markdown, cached on disk for `ttl` seconds — network hit only on
    a cold or stale cache. On a fetch failure, fall back to a stale cache if one exists (so the
    check still runs offline against last-known pricing); return None only when there's nothing
    cached AND the fetch failed, which signals the caller to skip. `now`/`url`/`cache_path` are
    injectable so the cache/fallback logic is unit-testable without the clock or the network."""
    now = time.time() if now is None else now
    try:
        with open(cache_path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        cached = None
    if cached and (now - cached.get("fetched_at", 0)) < ttl:
        return cached["markdown"]
    try:
        markdown, redirected_to = _http_get(url)
    except urllib.error.HTTPError as e:
        # The server answered with an error status (404 = page moved/renamed; 5xx = down). Unlike
        # being offline, this is a structural break in the check itself worth surfacing — warn so
        # PRICING_URL gets fixed — then fall back to a stale cache if we have one, else skip. The
        # warning shows in pytest's summary even when we then validate against the stale cache.
        warnings.warn(
            f"Pricing page fetch got HTTP {e.code} for {url} — the page may have moved/renamed; "
            "the drift check is running on cached data or skipping. Update pricing_check.PRICING_URL.",
            stacklevel=2)
        return cached["markdown"] if cached else None
    except Exception:
        # Offline / DNS / timeout / connection refused — transient and external; stay quiet and
        # fall back to a stale cache if present, else return None so the caller skips.
        return cached["markdown"] if cached else None
    if redirected_to:
        # A permanent (301/308) redirect was followed: the fetch succeeded, so the check still runs
        # correctly this time, but PRICING_URL now points at a stale location that will eventually
        # 404 — surface it so it gets repointed. Temporary redirects don't reach here.
        warnings.warn(
            f"Pricing page permanently redirected to {redirected_to} (from {url}) — the fetch "
            "still worked, but update pricing_check.PRICING_URL before the old URL is retired.",
            stacklevel=2)
    try:
        with open(cache_path, "w") as f:
            json.dump({"fetched_at": now, "markdown": markdown}, f)
    except OSError:
        pass                             # caching is best-effort; a write failure just re-fetches
    return markdown
