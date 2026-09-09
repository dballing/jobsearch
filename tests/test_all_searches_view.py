"""Combined "all searches" view (the ALL_SEARCHES lens): the unfiltered per-lens join that yields
one row per (job, search) tuple, the read-only view resolver, and the guard that keeps the sentinel
from ever resolving as a write target. Hermetic — in-memory DB + request contexts, no real app
config (the test app is single-search, so the multi-search paths are exercised via monkeypatch)."""
import app


def _job_two_searches(conn):
    """One physical posting that belongs to two lenses with divergent per-lens status/viability."""
    conn.execute("INSERT INTO jobs (job_id, title, company, job_description, raw) "
                 "VALUES ('p', 'Platform Eng', 'Acme', 'desc', '{}')")
    conn.execute("INSERT INTO job_search_state (job_id, search_id, status, viability) "
                 "VALUES ('p', 'tpm', 'applied', 'high')")
    conn.execute("INSERT INTO job_search_state (job_id, search_id, status, viability) "
                 "VALUES ('p', 'director', 'skipped', 'low')")


# ── _jss_join: concrete filters, ALL_SEARCHES doesn't ────────────────────────

def test_jss_join_concrete_filters_by_search():
    # A concrete lens scopes the join to its own state rows → one row per job.
    assert "jss.search_id = 'tpm'" in app._jss_join("tpm")


def test_jss_join_all_searches_is_unfiltered():
    # The combined view drops the per-lens predicate, so a job in N searches yields N rows.
    j = app._jss_join(app.ALL_SEARCHES)
    assert "JOIN job_search_state jss ON jss.job_id = jobs.job_id" in j
    assert "search_id =" not in j   # no lens filter at all


# ── the combined SELECT yields the (job, search) tuples ──────────────────────

def test_all_searches_select_yields_one_row_per_lens(jobs_db):
    # The real listing SQL over the unfiltered join returns both lenses' rows for one posting, each
    # carrying its own per-lens status — the tuple identity the combined view depends on.
    _job_two_searches(jobs_db)
    sql = app.FLAT_SELECT.format(join=app._jss_join(app.ALL_SEARCHES), where="",
                                 order="ORDER BY jss.search_id")
    processed = [app.process_job_row(r) for r in jobs_db.execute(sql, [-1, 0]).fetchall()]
    assert len(processed) == 2
    assert {p["job_id"] for p in processed} == {"p"}          # one physical posting…
    assert {p["search_id"]: p["status"] for p in processed} == {   # …two independent lenses
        "tpm": "applied", "director": "skipped"}


def test_concrete_lens_select_stays_scoped(jobs_db):
    # The same posting under a concrete lens returns exactly that lens's single row/status.
    _job_two_searches(jobs_db)
    sql = app.FLAT_SELECT.format(join=app._jss_join("tpm"), where="", order="")
    rows = jobs_db.execute(sql, [-1, 0]).fetchall()
    assert len(rows) == 1
    assert app.process_job_row(rows[0])["status"] == "applied"


# ── _current_view_id: read-only combined view resolver ───────────────────────

def test_view_id_single_search_never_reaches_all():
    # The test app is single-search; the combined view is unreachable even when explicitly asked
    # for (nothing to combine), so it collapses to the concrete lens.
    with app.app.test_request_context("/?search=__all__"):
        assert app._current_view_id() != app.ALL_SEARCHES
        assert app._current_view_id() == app._current_search_id()


def test_view_id_all_only_when_multi_search(monkeypatch):
    # Multi-search + an explicit ?search=__all__ selects the combined view.
    monkeypatch.setattr(type(app.APP_CONFIG), "is_multi_search", property(lambda self: True))
    with app.app.test_request_context("/?search=__all__"):
        assert app._current_view_id() == app.ALL_SEARCHES


def test_view_id_concrete_lens_in_multi_search(monkeypatch):
    # A concrete ?search= in a multi-search setup resolves to that lens, not the combined view.
    monkeypatch.setattr(type(app.APP_CONFIG), "is_multi_search", property(lambda self: True))
    with app.app.test_request_context("/?search=__default__"):
        assert app._current_view_id() == "__default__"


# ── write-target guard: ALL_SEARCHES is never a mutation target ──────────────

def test_write_target_never_resolves_to_all_searches():
    # _current_search_id (what every write route uses) must reject the sentinel and fall back to a
    # concrete lens, so a stray search=__all__ on a POST can never mutate "every lens" / the wrong one.
    with app.app.test_request_context("/job/x/status", method="POST",
                                      data={"search": app.ALL_SEARCHES}):
        assert app._current_search_id() == app.APP_CONFIG.default_search().id
        assert app._current_search_id() != app.ALL_SEARCHES


def test_write_target_honors_explicit_valid_search():
    # A per-row control in the combined view carries its row's concrete lens; the write resolver
    # honours it (here trivially the only search, but it documents the contract the JS relies on).
    sid = app.APP_CONFIG.default_search().id
    with app.app.test_request_context("/job/x/status", method="POST", data={"search": sid}):
        assert app._current_search_id() == sid


# ── route smoke test: the combined view renders both lenses ──────────────────

def test_combined_view_route_renders_all_lenses(sample_app_db, monkeypatch):
    """GET /?search=__all__ in a (monkeypatched) multi-search setup renders a 200 with the Search
    column and rows from more than one lens. Membership is the point: a sample job additionally
    placed under a second lens shows up alongside every __default__ posting."""
    import os
    import sqlite3

    monkeypatch.setattr(type(app.APP_CONFIG), "is_multi_search", property(lambda self: True))
    con = sqlite3.connect(os.environ["JOBSEARCH_DB"])
    con.execute("INSERT INTO job_search_state (job_id, search_id, status, viability) "
                "VALUES ('cs_review', 'director', 'applied', 'medium')")
    con.commit(); con.close()

    body = app.app.test_client().get("/?search=__all__").get_data(as_text=True)
    assert ">Search</th>" in body                 # the lens-label column header is present
    assert 'class="col-search"' in body           # …and at least one labelled cell
    assert 'class="badge" style="background-color:' in body   # lens tags render as colored badges
    # The second lens's membership row is included (its id labels the cell; no configured name).
    assert "director" in body


# ── lens tag colors ──────────────────────────────────────────────────────────

def test_lens_colors_are_stable_distinct_and_cycle():
    # Each palette entry is a valid hex color, and the first len(palette) searches get distinct ones.
    assert all(c.startswith("#") and len(c) == 7 for c in app._LENS_PALETTE)
    assert len(set(app._LENS_PALETTE)) == len(app._LENS_PALETTE)   # no dup hues in the palette

    class _S:  # minimal search stand-in (only .id is read)
        def __init__(self, id): self.id = id

    n = len(app._LENS_PALETTE)
    searches = [_S(f"s{i}") for i in range(n + 2)]                  # two more than the palette
    colors = {s.id: app._LENS_PALETTE[i % n] for i, s in enumerate(searches)}
    assert len(set(list(colors.values())[:n])) == n                # first n are all distinct
    assert colors["s0"] == colors[f"s{n}"]                         # wraps around (cycles)
    # And the real map is keyed by the configured searches (here just the single default lens).
    assert set(app.LENS_COLORS) == {s.id for s in app.APP_CONFIG.searches}
