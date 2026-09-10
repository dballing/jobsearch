"""Tests for live (hot) config reload and the stale-config drift banner (app.py).

Two independent pieces:

  * ``_compute_drift`` — the pure predicate behind the banner: which *pinned* fields (the ones
    that can't hot-reload) have been edited since startup. Tested with fabricated configs so it
    needs no disk or import-time env capture.
  * ``current_config`` / ``_reload_if_stale`` — the accessor that reloads config on an mtime
    change, keeps the last-good config when a (half-)saved file won't parse, and self-heals once
    it parses again. Driven by monkeypatching the module's active-config globals at a throwaway
    config on tmp_path, exercised outside any request context (so the flask.g snapshot is skipped
    and every call re-checks staleness).

These run outside a request, so they never touch the real config.toml or jobs.db.
"""
import os
from pathlib import Path

from config import AppConfig, load_config

import app


# ── _compute_drift: the pure banner predicate ──────────────────────────────────

def _fake_cfg(db_path: str, uploads_dir: str) -> AppConfig:
    """A minimal AppConfig whose db_path/uploads_dir properties read from `shared`."""
    return AppConfig(searches=[], shared={"db_path": db_path, "uploads_dir": uploads_dir},
                     source_files=[], aliases_path=Path("."), label_names={})


def test_compute_drift_none_when_file_matches_startup():
    cfg = _fake_cfg("jobs.db", "uploads")
    pinned = {"db_path": ("jobs.db", False), "uploads_dir": ("uploads", False)}
    assert app._compute_drift(cfg, pinned) == []


def test_compute_drift_flags_an_edited_file_sourced_field():
    # db_path now differs from the value the process started with → needs a restart → banner.
    cfg = _fake_cfg("moved.db", "uploads")
    pinned = {"db_path": ("jobs.db", False), "uploads_dir": ("uploads", False)}
    assert app._compute_drift(cfg, pinned) == ["db_path"]


def test_compute_drift_skips_env_sourced_field():
    # The running db_path came from JOBSEARCH_DB, so the file value is permanently inert — editing
    # it isn't real drift and must NOT raise a (misleading) banner. This is the test-suite's own
    # situation: conftest sets JOBSEARCH_DB.
    cfg = _fake_cfg("moved.db", "uploads")
    pinned = {"db_path": ("env.db", True), "uploads_dir": ("uploads", False)}
    assert app._compute_drift(cfg, pinned) == []


# ── current_config / _reload_if_stale: live reload, fail-soft, self-heal ────────

def _max_mtime(cfg: AppConfig) -> float:
    return max(f.stat().st_mtime for f in cfg.source_files)


def _bump(path: Path, t: float) -> None:
    """Force a file's mtime to `t` so the reload trigger fires deterministically (a same-second
    rewrite might otherwise leave mtime unchanged)."""
    os.utime(path, (t, t))


def _write_pathb(tmp_path: Path, search_name: str, *, second: bool = False) -> Path:
    """Write a Path-B canonical config (+ per-search file(s)) and return the canonical path.

    One search "tpm" named `search_name`; `second=True` adds an "eng" search too."""
    extra = ('\n[[searches]]\nsearch_id = "eng"\nsearch_name = "Eng"\n'
             'search_config_file = "eng.toml"\n') if second else ""
    (tmp_path / "config.toml").write_text(
        '[basics]\ndb_path = "jobs.db"\nuploads_dir = "up"\n\n'
        '[[searches]]\nsearch_id = "tpm"\n'
        f'search_name = "{search_name}"\nsearch_config_file = "tpm.toml"\n' + extra,
        encoding="utf-8")
    (tmp_path / "tpm.toml").write_text('[viability]\nprompt = "hi"\n', encoding="utf-8")
    if second:
        (tmp_path / "eng.toml").write_text('[viability]\nprompt = "eng"\n', encoding="utf-8")
    return tmp_path / "config.toml"


def _arm(monkeypatch, tmp_path, search_name, *, second=False) -> AppConfig:
    """Point app's active-config globals at a fresh throwaway config; return the loaded config.
    monkeypatch restores the real startup globals on teardown even though _reload_if_stale
    reassigns them directly."""
    p = _write_pathb(tmp_path, search_name, second=second)
    cfg0 = load_config(p)
    monkeypatch.setattr(app, "_config_path", p)
    monkeypatch.setattr(app, "_active_config", cfg0)
    monkeypatch.setattr(app, "_active_mtime", _max_mtime(cfg0))
    return cfg0


def test_current_config_reloads_search_name_on_mtime_change(tmp_path, monkeypatch):
    # This is exactly the user-reported case: edit search_name and it shows up without a restart.
    _arm(monkeypatch, tmp_path, "Europe TPM")
    assert app.current_config().get_search("tpm").name == "Europe TPM"

    base = app._active_mtime
    _write_pathb(tmp_path, "Europe Platform TPM")
    _bump(tmp_path / "config.toml", base + 100)

    assert app.current_config().get_search("tpm").name == "Europe Platform TPM"
    assert app._active_mtime == base + 100  # advanced, so we don't re-parse every request


def test_reload_picks_up_a_newly_added_search(tmp_path, monkeypatch):
    _arm(monkeypatch, tmp_path, "TPM")
    assert app._valid_search_ids() == {"tpm"}

    base = app._active_mtime
    _write_pathb(tmp_path, "TPM", second=True)
    _bump(tmp_path / "config.toml", base + 100)

    assert app._valid_search_ids() == {"tpm", "eng"}
    assert app.current_config().is_multi_search is True


def test_malformed_file_keeps_last_good_then_self_heals(tmp_path, monkeypatch):
    _arm(monkeypatch, tmp_path, "Good")
    base = app._active_mtime

    # A half-saved / broken file must NOT crash the app: keep the last-good config, and crucially
    # do NOT advance _active_mtime so the next request re-attempts the parse.
    (tmp_path / "config.toml").write_text("this is ]=[ not valid toml", encoding="utf-8")
    _bump(tmp_path / "config.toml", base + 100)
    assert app.current_config().get_search("tpm").name == "Good"
    assert app._active_mtime == base  # unchanged ⇒ will retry

    # Fix the file: the change goes live on the next request with no extra nudge.
    _write_pathb(tmp_path, "Fixed")
    _bump(tmp_path / "config.toml", base + 200)
    assert app.current_config().get_search("tpm").name == "Fixed"
    assert app._active_mtime == base + 200
