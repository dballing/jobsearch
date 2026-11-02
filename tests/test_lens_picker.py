"""The navbar lens picker: a row of badged buttons at or below LENS_BUTTON_MAX searches, the
dropdown above it, and the per-lens `new` counts both shapes carry.

The counts are the testable part (the markup is covered by the whole-page snapshot golden), and
the thing worth pinning is what they're counted over: only `new`, only configured lenses, and
zero-filled so a quiet lens still renders a badge instead of vanishing from the row.
"""
import types

import pytest

import app


def _lenses(monkeypatch, *ids):
    """Pretend the config declares exactly these searches, in this order."""
    cfg = types.SimpleNamespace(searches=[types.SimpleNamespace(id=i, name=i.title()) for i in ids])
    monkeypatch.setattr(app, "current_config", lambda: cfg)


def _state(conn, job_id, search_id, status):
    conn.execute("INSERT OR IGNORE INTO jobs (job_id, title, company, job_description, raw) "
                 "VALUES (?, 't', 'c', 'd', '{}')", (job_id,))
    conn.execute("INSERT INTO job_search_state (job_id, search_id, status) VALUES (?, ?, ?)",
                 (job_id, search_id, status))


def test_counts_only_new_postings(jobs_db, monkeypatch):
    _lenses(monkeypatch, "tpm")
    for i, status in enumerate(["new", "new", "reviewing", "applied", "bulkskipped", "skipped"]):
        _state(jobs_db, f"j{i}", "tpm", status)
    assert app.new_counts_by_lens(jobs_db) == {"tpm": 2}


def test_counts_are_per_lens(jobs_db, monkeypatch):
    _lenses(monkeypatch, "tpm", "sysadmin")
    _state(jobs_db, "a", "tpm", "new")
    _state(jobs_db, "b", "tpm", "new")
    _state(jobs_db, "c", "sysadmin", "new")
    assert app.new_counts_by_lens(jobs_db) == {"tpm": 2, "sysadmin": 1}


def test_a_lens_with_nothing_new_is_zero_filled_not_missing(jobs_db, monkeypatch):
    """The button still renders, badged (0) — a lens that drops out of the row because it's
    quiet is worse than one that says so."""
    _lenses(monkeypatch, "tpm", "quiet")
    _state(jobs_db, "a", "tpm", "new")
    assert app.new_counts_by_lens(jobs_db) == {"tpm": 1, "quiet": 0}


def test_orphan_rows_from_an_unconfigured_lens_are_ignored(jobs_db, monkeypatch):
    """Renaming or dropping a search leaves its job_search_state rows behind. A count for a lens
    with no button to click is noise, and it would also break the invariant that the All total
    equals the sum of the buttons beside it."""
    _lenses(monkeypatch, "tpm")
    _state(jobs_db, "a", "tpm", "new")
    _state(jobs_db, "b", "retired_lens", "new")
    counts = app.new_counts_by_lens(jobs_db)
    assert counts == {"tpm": 1}
    assert "retired_lens" not in counts


def test_all_total_is_the_sum_of_the_buttons(jobs_db, monkeypatch):
    """What the template shows on the "All" button, and why it's summed in Python rather than
    over the raw query: the orphan row above must not inflate it."""
    _lenses(monkeypatch, "tpm", "sysadmin")
    _state(jobs_db, "a", "tpm", "new")
    _state(jobs_db, "b", "sysadmin", "new")
    _state(jobs_db, "c", "retired_lens", "new")
    counts = app.new_counts_by_lens(jobs_db)
    assert sum(counts.values()) == 2


# ── the buttons/dropdown threshold ──────────────────────────────────────────

def test_lens_button_threshold_is_five():
    """A layout limit (the row wraps the navbar), not a data one — pinned so the template's
    comparison and the documented behaviour can't drift apart."""
    assert app.LENS_BUTTON_MAX == 5


@pytest.mark.parametrize("n, buttons", [(2, True), (5, True), (6, False)])
def test_threshold_boundaries(n, buttons):
    """At or below the max is the button row; above it falls back to the dropdown."""
    assert (n <= app.LENS_BUTTON_MAX) is buttons


def test_threshold_is_a_jinja_global_not_a_per_render_value():
    """base.html is the shared layout, so a page rendering the picker must not be able to omit
    the threshold and silently get the wrong shape."""
    assert app.app.jinja_env.globals["lens_button_max"] == app.LENS_BUTTON_MAX
    assert app.app.jinja_env.from_string("{{ 6 <= lens_button_max }}").render() == "False"


def test_missing_counts_degrade_instead_of_raising():
    """The counts ARE per-render (they cost a query). A future page that renders the picker
    without them should lose the badges, not 500 — Jinja's Undefined.get() would raise."""
    tpl = app.app.jinja_env.from_string(
        "{% set counts = lens_new_counts | default({}, true) %}"
        "{% set total = lens_new_total | default(0, true) %}{{ counts.get('x', 0) }}/{{ total }}")
    assert tpl.render() == "0/0"


def test_index_renders_with_the_picker_context(sample_app_db):
    """Smoke: the extra per-lens count query runs on the real listing path. (The single-search
    test config hides the picker itself — the markup is covered by the snapshot golden.)"""
    assert app.app.test_client().get("/").status_code == 200
