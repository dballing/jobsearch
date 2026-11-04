"""Domain reject-list: posting CHANNELS (an ATS/board host) whose jobs are forced to
low/autoskipped WITHOUT an AI call.

The company list's sibling, but it answers the question the company list can't: a fabricated
posting wears an employer name you've never heard of, so there's nothing to blocklist by name —
only the host it arrived through. Covered here: entry/URL normalization (they share one parser),
label-boundary suffix matching (so "join.com" never catches "notjoin.com"), the combined
reject_verdict both call sites use, build_selection's deliberately-loose SQL pre-filter against a
live DB, and the invariant that neither list touches the staleness hash. The AI-scoring loop isn't
unit-tested (it needs a live model); its deny branch is these helpers, locked down here.
"""
import sqlite3

import pytest

import rescore_viability as rv
from viability import (REJECT_DENYABLE_STATUSES, _host_from_url, build_reject_domain_set,
                       build_reject_set, matched_reject_domain, reject_domain_reason,
                       reject_verdict)

# The posting that motivated the feature: a real German ISP's name on a fabricated fully-remote
# US role, scored 'high' by the AI because every individual fact looked fine.
NETCOLOGNE = "https://join.com/companies/netcologneitsde/16903776-senior-system-administrator"


# ── _host_from_url: one parser for both sides of the match ────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("join.com",                      "join.com"),   # bare host (the normal config form)
    ("JOIN.COM",                      "join.com"),   # case-folded
    ("  join.com  ",                  "join.com"),   # trimmed
    (".join.com",                     "join.com"),   # cookie-style leading dot
    ("join.com.",                      "join.com"),  # trailing root dot
    ("join.com/companies/x",          "join.com"),   # entry with a path
    ("https://join.com/companies/x",  "join.com"),   # pasted URL
    ("http://www.join.com",           "www.join.com"),
    ("https://user:pw@join.com:8443/x", "join.com"),  # userinfo + port stripped
    (NETCOLOGNE,                      "join.com"),
])
def test_host_from_url_normalizes(raw, expected):
    assert _host_from_url(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", ".", "..", 0])
def test_host_from_url_no_host(raw):
    assert _host_from_url(raw) is None


def test_host_from_url_tolerates_malformed():
    # Broken IPv6 brackets make urlsplit *raise*; a typo'd config entry must not take down a whole
    # scoring run, so it degrades to "no host" instead of propagating.
    assert _host_from_url("http://[oops/x") is None
    # A junk or out-of-range port does NOT raise (.hostname never parses the port), so these still
    # resolve to the right host — which is the better outcome, and worth pinning so a future
    # "validate the port" refactor doesn't quietly start dropping these entries.
    assert _host_from_url("http://join.com:notaport/x") == "join.com"
    assert _host_from_url("join.com:99999/x") == "join.com"


# ── build_reject_domain_set: parsing ─────────────────────────────────────────
def test_build_domain_set_normalizes_and_drops_blanks():
    ds = build_reject_domain_set({"viability": {"reject_domains": [
        "  JOIN.com ", "https://foo.example/jobs", ".bar.test", "", "   ",
    ]}})
    assert ds == frozenset({"join.com", "foo.example", "bar.test"})


def test_build_domain_set_absent_or_off_is_empty():
    assert build_reject_domain_set({}) == frozenset()
    assert build_reject_domain_set({"viability": {}}) == frozenset()
    assert build_reject_domain_set({"viability": {"reject_domains": []}}) == frozenset()


def test_build_domain_set_tolerates_non_list():
    # A scalar (misconfigured) must not iterate characters into 26 bogus hosts.
    assert build_reject_domain_set({"viability": {"reject_domains": "join.com"}}) == frozenset()


# ── matched_reject_domain: label-boundary suffix matching ─────────────────────
def test_matches_exact_host_and_subdomains():
    ds = frozenset({"join.com"})
    assert matched_reject_domain("https://join.com/x", ds) == "join.com"
    assert matched_reject_domain("https://www.join.com/x", ds) == "join.com"
    assert matched_reject_domain("https://apply.eu.join.com/x", ds) == "join.com"
    assert matched_reject_domain(NETCOLOGNE, ds) == "join.com"


def test_does_not_match_across_a_label_boundary():
    # The whole point of the boundary rule: a reject entry must never bleed onto an unrelated host
    # that merely shares a tail of characters. A naive substring/endswith check fails all of these.
    ds = frozenset({"join.com"})
    assert matched_reject_domain("https://notjoin.com/x", ds) is None
    assert matched_reject_domain("https://myjoin.com/x", ds) is None
    assert matched_reject_domain("https://join.company.net/x", ds) is None
    # The host is what's matched — the entry appearing in the PATH is not a match.
    assert matched_reject_domain("https://example.com/join.com/x", ds) is None


def test_overlapping_entries_report_the_most_specific_deterministically():
    # With both listed, the reason must always name "apply.join.com" — iterating the set would pick
    # whichever the per-process hash order yielded, making a stored reason unreproducible.
    ds = frozenset({"join.com", "apply.join.com"})
    assert matched_reject_domain("https://apply.join.com/x", ds) == "apply.join.com"
    assert matched_reject_domain("https://other.join.com/x", ds) == "join.com"


def test_matched_domain_empty_inputs():
    assert matched_reject_domain(NETCOLOGNE, frozenset()) is None
    assert matched_reject_domain(None, frozenset({"join.com"})) is None
    assert matched_reject_domain("", frozenset({"join.com"})) is None
    assert matched_reject_domain("not a url at all", frozenset({"join.com"})) is None


def test_reject_domain_reason_names_the_host():
    assert reject_domain_reason("join.com") == (
        "Autoskipped: postings from join.com are on your domain reject-list.")
    assert "this domain" in reject_domain_reason("")


# ── reject_verdict: the single shared decision point ─────────────────────────
DS = frozenset({"join.com"})
RS = frozenset({"initech"})


def test_verdict_denies_on_job_url():
    reason, note = reject_verdict({"company": "NetCologne IT Services", "job_url": NETCOLOGNE}, frozenset(), DS)
    assert note == "domain on reject-list"
    assert "join.com" in reason


def test_verdict_denies_on_apply_url_alone():
    # A posting can arrive on a feed you trust while handing the application to one you don't.
    reason, note = reject_verdict(
        {"company": "Acme", "job_url": "https://www.linkedin.com/jobs/view/1",
         "apply_url": NETCOLOGNE}, frozenset(), DS)
    assert note == "domain on reject-list"
    assert "join.com" in reason


def test_verdict_company_wins_when_both_match():
    # "I will never work for this employer" is the more specific statement, and the one the user
    # expects to read in the reason.
    reason, note = reject_verdict({"company": "Initech", "job_url": NETCOLOGNE}, RS, DS)
    assert note == "company on reject-list"
    assert "Initech" in reason and "company reject-list" in reason


def test_verdict_none_when_nothing_matches():
    job = {"company": "Acme", "job_url": "https://boards.greenhouse.io/acme/jobs/1"}
    assert reject_verdict(job, RS, DS) is None
    assert reject_verdict(job, frozenset(), frozenset()) is None
    # Both lists off is the feature-disabled case and must never deny.
    assert reject_verdict({"company": "Initech", "job_url": NETCOLOGNE},
                          frozenset(), frozenset()) is None


def test_verdict_tolerates_missing_url_keys():
    # A manually-added job can have no URL at all; that's not a denial.
    assert reject_verdict({"company": "Acme"}, RS, DS) is None
    assert reject_verdict({"company": "Acme", "job_url": None, "apply_url": None}, RS, DS) is None


# ── build_selection's domain OR, executed against a live DB ──────────────────
def _db_with_jobs(rows):
    """(job_id, company, job_url, apply_url, status, viability, hash, needs_rescored)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE jobs (job_id TEXT, company TEXT, job_url TEXT, apply_url TEXT, "
                "first_seen TEXT)")
    con.execute("CREATE TABLE job_search_state (job_id TEXT, search_id TEXT, status TEXT, "
                "viability TEXT, viability_prompt_hash TEXT, needs_rescored INT)")
    for job_id, company, job_url, apply_url, status, viability, phash, nr in rows:
        con.execute("INSERT INTO jobs VALUES (?,?,?,?,'2026-08-01')",
                    (job_id, company, job_url, apply_url))
        con.execute("INSERT INTO job_search_state VALUES (?, '__default__', ?, ?, ?, ?)",
                    (job_id, status, viability, phash, nr))
    return con


_JSS_JOIN = ("JOIN job_search_state jss ON jss.job_id = jobs.job_id "
             "AND jss.search_id = '__default__'")


def _selected(con, **kwargs):
    where, params = rv.build_selection(**kwargs)
    return sorted(r["job_id"] for r in
                  con.execute(f"SELECT jobs.job_id AS job_id FROM jobs {_JSS_JOIN} {where}", params))


def test_default_run_pulls_in_nonstale_listed_predecision_jobs():
    # Already scored under the current hash, so the staleness gate alone would skip them — the
    # domain OR is what makes newly listing a host take effect on its existing jobs.
    con = _db_with_jobs([
        ("A", "NetCologne", NETCOLOGNE, NETCOLOGNE, "new",          "high",   "H", 0),  # → in
        ("B", "NetCologne", NETCOLOGNE, NETCOLOGNE, "applied",      "high",   "H", 0),  # acted on → out
        ("C", "NetCologne", NETCOLOGNE, NETCOLOGNE, "interviewing", "high",   "H", 0),  # acted on → out
        ("D", "Acme", "https://boards.greenhouse.io/a/1", None, "new", "medium", "H", 0),  # → out
        ("E", "Zetcom", "https://www.linkedin.com/jobs/view/9", NETCOLOGNE, "new", "low", "H", 0),  # apply_url → in
    ])
    ds = build_reject_domain_set({"viability": {"reject_domains": ["join.com"]}})
    assert _selected(con, current_hash="H", reject_domains=ds) == ["A", "E"]


def test_domain_or_does_not_widen_narrow_modes():
    # An explicit narrowing is a deliberate, bounded selection; the reject-OR must not widen it.
    con = _db_with_jobs([
        ("A", "NetCologne", NETCOLOGNE, None, "new",         "high", "H", 0),
        ("Z", "NetCologne", NETCOLOGNE, None, "autoskipped",  "low", "H", 1),
    ])
    ds = build_reject_domain_set({"viability": {"reject_domains": ["join.com"]}})
    assert _selected(con, current_hash="H", autoskipped=True, reject_domains=ds) == ["Z"]
    assert _selected(con, current_hash="H", status="skipped", reject_domains=ds) == []


def test_sql_prefilter_is_a_superset_that_the_verdict_prunes():
    # The documented contract: the SQL LIKE can't express a label boundary, so it over-matches and
    # reject_verdict is authoritative. Pin BOTH halves — SQL selects the near-miss host, and the
    # verdict then refuses to deny it — so nobody "tightens" the SQL believing it's the gate.
    con = _db_with_jobs([
        ("HIT",  "X", NETCOLOGNE,                  None, "new", "high", "H", 0),
        ("NEAR", "Y", "https://notjoin.com/jobs/1", None, "new", "high", "H", 0),
    ])
    ds = build_reject_domain_set({"viability": {"reject_domains": ["join.com"]}})
    assert _selected(con, current_hash="H", reject_domains=ds) == ["HIT", "NEAR"]
    assert reject_verdict({"company": "Y", "job_url": "https://notjoin.com/jobs/1"},
                          frozenset(), ds) is None


def test_both_lists_or_together_in_selection():
    con = _db_with_jobs([
        ("A", "Initech",    "https://boards.greenhouse.io/i/1", None, "new", "high", "H", 0),
        ("B", "NetCologne", NETCOLOGNE,                         None, "new", "high", "H", 0),
        ("C", "Acme",       "https://boards.greenhouse.io/a/1", None, "new", "high", "H", 0),
    ])
    rs = build_reject_set({"viability": {"reject_companies": ["Initech"]}})
    ds = build_reject_domain_set({"viability": {"reject_domains": ["join.com"]}})
    assert _selected(con, current_hash="H", reject_companies=rs, reject_domains=ds) == ["A", "B"]


def test_no_domain_list_never_references_the_url_columns():
    # Guards the backward-compatible shape: with reject_domains unset the generated SQL must not
    # mention job_url/apply_url at all (older callers and the company-only tests run against a
    # jobs table that has neither column).
    where, _ = rv.build_selection(current_hash="H",
                                  reject_companies=frozenset({"initech"}))
    assert "job_url" not in where and "apply_url" not in where


# ── invariants ───────────────────────────────────────────────────────────────
def test_autoskipped_is_denyable_but_acted_on_statuses_are_not():
    # The gate the callers apply around reject_verdict. autoskipped is in the set so --autoskipped
    # re-denies a still-listed match for free instead of paying to re-score and promote it back.
    assert "autoskipped" in REJECT_DENYABLE_STATUSES
    for s in ("applied", "interviewing", "offered", "rejected", "withdrawn"):
        assert s not in REJECT_DENYABLE_STATUSES


def test_domain_list_is_not_part_of_the_staleness_hash():
    # Folding it in would mark the whole DB stale and force a full-DB AI re-score just to add one
    # host — the same reasoning that keeps reject_companies out. If this ever fails, adding a
    # domain silently became a four-figure re-score.
    from viability import scoring_hash_for_config
    base = {"viability": {"prompt": "I am a candidate."}}
    with_domains = {"viability": {"prompt": "I am a candidate.",
                                  "reject_domains": ["join.com"],
                                  "reject_companies": ["Initech"]}}
    assert scoring_hash_for_config(base) == scoring_hash_for_config(with_domains)
