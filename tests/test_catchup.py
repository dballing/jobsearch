""""Catch up": bulk-bury the unreviewed backlog into the 'bulkskipped' status.

The feature's whole correctness claim is that the number on the button and the rows the POST
writes are the SAME SET — so these tests pin the shared selection helper, the filter composition,
and the staleness guard that refuses when a cron ingest has moved the count. They also lock
'bulkskipped' into the places a new status has to reach to behave: the early-status set, the
Active exclusion, the default rescore selection (an unread pile must never be paid to re-score),
and the two mirrored status vocabularies.
"""
import sqlite3

import pytest

import app
import ingest
import import_linkedin as il
import rescore_viability as rv


# ── parse_comp: hoisted out of index() so both sides scope identically ───────

@pytest.mark.parametrize("raw, expected", [
    ("100000-200000", ("100000-200000", 100000, 200000, True)),
    ("100000-",       ("100000-", 100000, None, True)),      # open upper bound
    ("-200000",       ("-200000", None, 200000, True)),      # open lower bound
    ("",              ("", None, None, False)),
    ("150000",        ("", None, None, False)),              # no separator → not a range
    ("abc-def",       ("", None, None, False)),              # unparseable degrades to "off"
    ("-",             ("", None, None, False)),              # both bounds empty → nothing to match
])
def test_parse_comp(raw, expected):
    assert app.parse_comp(raw) == expected


# ── catchup_where: the status is pinned, the rest composes ───────────────────

def test_catchup_where_pins_new_regardless_of_the_active_view_filter():
    """The selection is anchored to 'new', not to whatever status filter the page is showing —
    otherwise catching up from the default Active view would bury applied/interviewing rows."""
    where, params = app.catchup_where("")
    assert where == "WHERE jss.status = 'new'"
    assert params == []


def test_catchup_where_composes_with_the_other_filters():
    where, params = app.catchup_where("remote", q="", source="linkedin", viability="medium")
    assert "jss.status = 'new'" in where
    assert "labels LIKE ?" in where and "source = ?" in where and "jss.viability = ?" in where
    assert params == ['%"remote"%', "linkedin", "medium"]


def test_catchup_where_is_the_new_status_filter_clause():
    """Pinned via STATUS_FILTERS['new'], so the two can't drift apart."""
    assert app.STATUS_FILTERS[app.CATCHUP_FROM_STATUS][1] in app.catchup_where("")[0]


# ── the route ────────────────────────────────────────────────────────────────

def _status(job_id, search_id="__default__"):
    con = sqlite3.connect(app.DB_PATH)
    row = con.execute("SELECT status FROM job_search_state WHERE job_id = ? AND search_id = ?",
                      (job_id, search_id)).fetchone()
    con.close()
    return row[0] if row else None


def _history(job_id):
    con = sqlite3.connect(app.DB_PATH)
    row = con.execute("SELECT history FROM job_search_state WHERE job_id = ? "
                      "AND search_id = '__default__'", (job_id,)).fetchone()
    con.close()
    import json
    return json.loads(row[0] or "[]")


def _catchup(**form):
    return app.app.test_client().post("/jobs/catchup", data=form)


# The sample fixture's three 'new' postings, and what they score.
_NEW_HIGH   = ["ln_new_hot", "manual_recruiter"]
_NEW_MEDIUM = ["cs_stale"]
_ALL_NEW    = _NEW_HIGH + _NEW_MEDIUM


def test_catchup_buries_every_new_posting_and_nothing_else(sample_app_db):
    resp = _catchup()
    assert resp.status_code == 200
    assert resp.get_json() == {"updated": len(_ALL_NEW)}
    for jid in _ALL_NEW:
        assert _status(jid) == "bulkskipped"
    # Everything that isn't 'new' is left exactly as it was — including the pre-decision statuses
    # the user deliberately set, and the acted-on ones a filter-scoped write could otherwise eat.
    for jid, was in [("cs_review", "reviewing"), ("ln_deferred", "deferred"),
                     ("cs_skipped", "skipped"), ("ln_autoskip", "autoskipped"),
                     ("cs_applied_ovr", "applied"), ("ln_root", "interviewing"),
                     ("ln_offered", "offered"), ("cs_rejected", "rejected"),
                     ("ln_ghosted", "ghosted"), ("cs_withdrawn", "withdrawn"),
                     ("ln_closed", "closed")]:
        assert _status(jid) == was


def test_catchup_logs_a_distinguishable_history_entry(sample_app_db):
    """The note is the point: nothing else in the status machinery records that this transition
    was neither the user's judgment on this job nor the scorer's."""
    _catchup()
    ev = [e for e in _history("cs_stale") if e.get("event") == "status"][-1]
    assert ev["from"] == "new" and ev["to"] == "bulkskipped"
    assert ev["note"] == app.CATCHUP_HISTORY_NOTE == "caught up"


def test_catchup_respects_the_viability_filter(sample_app_db):
    """The real workflow: bury the mediums, keep the highs for a human look."""
    assert _catchup(viability="medium").get_json() == {"updated": len(_NEW_MEDIUM)}
    for jid in _NEW_MEDIUM:
        assert _status(jid) == "bulkskipped"
    for jid in _NEW_HIGH:
        assert _status(jid) == "new"


def test_catchup_respects_the_source_filter(sample_app_db):
    """cs_stale is the only careersite posting among the three 'new' rows."""
    assert _catchup(source="careersite").get_json() == {"updated": 1}
    assert _status("cs_stale") == "bulkskipped"
    for jid in _NEW_HIGH:
        assert _status(jid) == "new"


def test_catchup_ignores_an_unrecognized_filter_value_like_index_does(sample_app_db):
    """A junk viability is dropped (not matched, not injected), exactly as the listing route drops
    it — so the set acted on is always one some reachable view would have shown."""
    assert _catchup(viability="bogus").get_json() == {"updated": len(_ALL_NEW)}


def test_catchup_with_no_matches_writes_nothing(sample_app_db):
    _catchup()                                   # drains the backlog
    assert _catchup().get_json() == {"updated": 0}


# ── the staleness guard ──────────────────────────────────────────────────────

def test_catchup_refuses_when_the_count_moved_and_writes_nothing(sample_app_db):
    """"Everything currently new" is a point-in-time claim. Ingest runs on cron, so a tab left
    open overnight can be stale — refuse with the real count rather than bury a different set."""
    resp = _catchup(expect=99)
    assert resp.status_code == 409
    assert resp.get_json() == {"error": "stale", "count": len(_ALL_NEW)}
    for jid in _ALL_NEW:
        assert _status(jid) == "new"             # nothing moved


def test_catchup_proceeds_when_the_count_still_matches(sample_app_db):
    assert _catchup(expect=len(_ALL_NEW)).status_code == 200
    assert _status("cs_stale") == "bulkskipped"


def test_expect_is_checked_against_the_filtered_count_not_the_whole_backlog(sample_app_db):
    """The button's count is per-filter, so the guard has to compare against the same filter —
    the unfiltered total would spuriously refuse every filtered catch-up."""
    assert _catchup(viability="medium", expect=len(_NEW_MEDIUM)).status_code == 200


# ── the count on the button and the rows written are one set ─────────────────

def test_the_counted_set_is_exactly_the_written_set(sample_app_db):
    """Computed the way index() computes the button's number, then compared to what the POST
    reports updating. This is the property the whole filter-scoped design rests on."""
    con = sqlite3.connect(app.DB_PATH)
    where, params = app.catchup_where("", viability="high")
    counted = con.execute(
        app.FLAT_COUNT.format(join=app._jss_join("__default__"), where=where),
        params).fetchone()[0]
    con.close()
    assert _catchup(viability="high").get_json() == {"updated": counted}


def test_index_still_renders_with_the_button_present(sample_app_db):
    """Smoke only (no markup assertions): the extra count query runs on the real listing path."""
    assert app.app.test_client().get("/").status_code == 200


# ── lens scoping ─────────────────────────────────────────────────────────────

def test_catchup_is_scoped_to_one_lens(sample_app_db, monkeypatch):
    """A posting that's 'new' in two lenses is buried only in the one being caught up."""
    monkeypatch.setattr(app, "_valid_search_ids", lambda: {"__default__", "other"})
    con = sqlite3.connect(app.DB_PATH)
    con.execute("INSERT INTO job_search_state (job_id, search_id, status, viability) "
                "VALUES ('cs_stale', 'other', 'new', 'medium')")
    con.commit(); con.close()

    assert _catchup(search="other").get_json() == {"updated": 1}
    assert _status("cs_stale", "other") == "bulkskipped"
    assert _status("cs_stale", "__default__") == "new"       # the sibling lens is untouched


def test_catchup_refuses_the_combined_all_searches_view(sample_app_db, monkeypatch):
    """A filter there can span lenses, so there's no single write target. Guarded explicitly
    because _current_search_id would otherwise silently fall back to the DEFAULT lens and catch
    up the wrong one."""
    from config import AppConfig
    monkeypatch.setattr(AppConfig, "is_multi_search", property(lambda self: True))
    resp = _catchup(search=app.ALL_SEARCHES)
    assert resp.status_code == 400
    for jid in _ALL_NEW:
        assert _status(jid) == "new"


# ── 'bulkskipped' is wired into the status machinery ─────────────────────────

def test_bulkskipped_is_a_known_status_with_a_colour():
    assert "bulkskipped" in app.STATUSES
    assert app.STATUS_COLORS["bulkskipped"] == app.STATUS_COLORS["autoskipped"] == "dark"


def test_bulkskipped_is_an_early_status():
    """No application is outstanding, so a transition into it clears applied_at."""
    assert "bulkskipped" in app._EARLY_STATUSES


def test_transition_into_bulkskipped_clears_applied_at(sample_app_db):
    con = sqlite3.connect(app.DB_PATH)
    con.execute("UPDATE job_search_state SET status = 'applied', applied_at = '2026-07-01 09:00:00' "
                "WHERE job_id = 'cs_stale' AND search_id = '__default__'")
    con.commit(); con.close()
    app.app.test_client().post("/job/cs_stale/status", data={"status": "bulkskipped"})
    con = sqlite3.connect(app.DB_PATH)
    assert con.execute("SELECT applied_at FROM job_search_state WHERE job_id = 'cs_stale' "
                       "AND search_id = '__default__'").fetchone()[0] is None
    con.close()


def test_bulkskipped_is_excluded_from_active():
    _, active = app.STATUS_FILTERS["active"]
    assert "'bulkskipped'" in active


def test_bulkskipped_has_its_own_filter_separate_from_skipped():
    """Offered so the pile stays mineable (pair with High viability), and kept OUT of 'Skipped'
    so that filter keeps meaning "skips I decided on by hand"."""
    label, condition = app.STATUS_FILTERS["bulkskipped"]
    assert label == "Caught up"
    assert condition == "jss.status = 'bulkskipped'"
    assert "bulkskipped" not in app.STATUS_FILTERS["skipped"][1]


def test_bulkskipped_filter_builds_the_expected_clause():
    where, params = app.build_where("", "bulkskipped")
    assert where == "WHERE jss.status = 'bulkskipped'"
    assert params == []


# ── the pile must not be re-scored by a scheduled run ───────────────────────

def test_default_rescore_run_excludes_bulkskipped():
    """The economic point of the feature: a scheduled run must not pay to score a few hundred
    postings the user explicitly buried unread."""
    where, _ = rv.build_selection(current_hash="h")
    assert "'bulkskipped'" in where
    assert "status NOT IN" in where


def test_bulkskipped_is_still_addressable_explicitly():
    """--status bulkskipped remains available for deliberately mining the pile."""
    assert "bulkskipped" in rv.VALID_STATUSES
    where, params = rv.build_selection(current_hash="h", status="bulkskipped")
    assert "jss.status = ?" in where and "bulkskipped" in params


def test_autoskipped_promotion_cannot_reach_the_pile():
    """Why this is a third status and not a reuse of 'autoskipped': that status's rescore
    promotion fires on rating alone, so a caught-up medium/high pile would resurrect itself."""
    assert rv.should_unskip("medium", rv.VIABILITY_RANK["low"]) is True   # would have promoted
    where, _ = rv.build_selection(current_hash="h", autoskipped=True)
    assert "jss.status = 'autoskipped'" in where
    assert "bulkskipped" not in where                                      # out of reach


# ── a rewritten posting is the one automatic way out ────────────────────────

def test_reset_on_change_covers_bulkskipped():
    """Never read, so a rewritten description is genuinely new information about it."""
    assert "bulkskipped" in ingest.RESET_ON_CHANGE_STATUSES


def test_reset_on_change_leaves_parked_and_acted_on_statuses_alone():
    for keep in ("deferred", "new", "reviewing", "applied", "interviewing", "closed"):
        assert keep not in ingest.RESET_ON_CHANGE_STATUSES


# ── mirrored vocabularies ───────────────────────────────────────────────────

def test_import_linkedin_statuses_match_app():
    """import_linkedin duplicates the vocabulary to stay standalone (importing app would spin up
    a Flask app), so it needs a drift lock like rescore_viability's — without one a real status is
    silently rejected by --status, which is how 'deferred' went missing here."""
    assert set(il.VALID_STATUSES) == set(app.STATUSES)


def test_rescore_statuses_match_app():
    assert set(rv.VALID_STATUSES) == set(app.STATUSES)
