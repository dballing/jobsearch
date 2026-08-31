"""Tests for the central config loader (config.py).

Pure, hermetic: every case writes throwaway TOML under pytest's tmp_path and loads it.
Covers Path A (single-search back-compat), Path B (multi-search composition), the
[basics]/bare-key fallback + --fixbasics migrator, and all the validation errors.
"""
import pytest

import config
from config import ConfigError, load_config, migrate_config_to_basics


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ── Path A: single-search back-compat ─────────────────────────────────────────

def test_path_a_single_default_search(tmp_path):
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
uploads_dir = "up"

[viability]
prompt = "hi"

[[tasks]]
name = "t1"
label = "dc"
""")
    cfg = load_config(p)
    assert [s.id for s in cfg.searches] == [config.DEFAULT_SEARCH_ID]
    only = cfg.default_search()
    # The single search's config is the flattened canonical: [basics] merged to top level.
    assert only.config["db_path"] == "jobs.db"
    assert only.config["viability"]["prompt"] == "hi"
    assert only.tasks == [{"name": "t1", "label": "dc"}]
    assert cfg.db_path == "jobs.db"
    assert cfg.uploads_dir == "up"
    assert cfg.aliases_path == p
    assert cfg.source_files == [p]


def test_missing_basics_bare_key_fallback_warns(tmp_path, capsys):
    # No [basics] table — bare top-level scalars must still load, with a deprecation nudge.
    p = _write(tmp_path / "config.toml", 'db_path = "jobs.db"\nuploads_dir = "up"\n')
    cfg = load_config(p)
    assert cfg.db_path == "jobs.db"
    assert cfg.uploads_dir == "up"
    assert "DEPRECATION" in capsys.readouterr().err


def test_basics_and_bare_duplicate_errors(tmp_path):
    p = _write(tmp_path / "config.toml", '[basics]\ndb_path = "a.db"\n\n[misc]\n')  # sanity table
    # A key present both bare and under [basics] is ambiguous.
    p = _write(tmp_path / "config.toml", 'db_path = "bare.db"\n[basics]\ndb_path = "b.db"\n')
    with pytest.raises(ConfigError, match="both under \\[basics\\] and bare"):
        load_config(p)


# ── Path B: multi-search composition ──────────────────────────────────────────

def _multi(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "tpm.toml", """
[viability]
prompt = "tpm candidate"

[labels]
dc = "DC/DMV"

[[tasks]]
name = "tpm-task"
label = "dc"
""")
    _write(tmp_path / "searches" / "dir.toml", """
[viability]
prompt = "director candidate"

[labels]
nc = "NC"

[[tasks]]
name = "dir-task"
label = "nc"
""")
    return _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
api_token = "tok"

[company_aliases]
"Sirius XM" = "SiriusXM"

[[searches]]
search_id = "tpm"
search_name = "TPM"
search_config_file = "searches/tpm.toml"
adopts_legacy = true

[[searches]]
search_id = "director"
search_name = "Director"
search_config_file = "searches/dir.toml"
""")


def test_path_b_composition(tmp_path):
    cfg = load_config(_multi(tmp_path))
    assert [s.id for s in cfg.searches] == ["tpm", "director"]
    tpm = cfg.get_search("tpm")
    # Effective config = canonical globals + this search's own stanzas.
    assert tpm.config["db_path"] == "jobs.db"                      # global
    assert tpm.config["company_aliases"] == {"Sirius XM": "SiriusXM"}  # global
    assert tpm.config["viability"]["prompt"] == "tpm candidate"    # per-search
    assert tpm.tasks == [{"name": "tpm-task", "label": "dc"}]
    # Globals are identical across searches; per-search stanzas differ.
    assert cfg.get_search("director").config["viability"]["prompt"] == "director candidate"


def test_path_b_label_union(tmp_path):
    cfg = load_config(_multi(tmp_path))
    assert cfg.label_names == {"dc": "DC/DMV", "nc": "NC"}


def test_path_b_source_files_and_adopter(tmp_path):
    cfg = load_config(_multi(tmp_path))
    assert cfg.source_files == [
        tmp_path / "config.toml",
        tmp_path / "searches" / "tpm.toml",
        tmp_path / "searches" / "dir.toml",
    ]
    assert cfg.adopter.id == "tpm"
    assert cfg.aliases_path == tmp_path / "config.toml"


def test_search_file_redeclaring_global_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "s.toml", '[company_aliases]\n"X" = "Y"\n[viability]\nprompt="p"\n')
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[company_aliases]
"Sirius XM" = "SiriusXM"
[[searches]]
search_id = "s"
search_config_file = "searches/s.toml"
""")
    with pytest.raises(ConfigError, match="redeclares global stanza"):
        load_config(p)


def test_search_file_with_basics_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "s.toml", '[basics]\ndb_path="nope.db"\n')
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[[searches]]
search_id = "s"
search_config_file = "searches/s.toml"
""")
    with pytest.raises(ConfigError, match="must not contain"):
        load_config(p)


def test_duplicate_search_id_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "s.toml", '[viability]\nprompt="p"\n')
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[[searches]]
search_id = "dup"
search_config_file = "searches/s.toml"
[[searches]]
search_id = "dup"
search_config_file = "searches/s.toml"
""")
    with pytest.raises(ConfigError, match="duplicate search_id"):
        load_config(p)


def test_empty_search_id_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "s.toml", '[viability]\nprompt="p"\n')
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[[searches]]
search_id = ""
search_config_file = "searches/s.toml"
""")
    with pytest.raises(ConfigError, match="missing a non-empty search_id"):
        load_config(p)


def test_reserved_default_search_id_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "s.toml", '[viability]\nprompt="p"\n')
    p = _write(tmp_path / "config.toml", f"""
[basics]
db_path = "jobs.db"
[[searches]]
search_id = "{config.DEFAULT_SEARCH_ID}"
search_config_file = "searches/s.toml"
""")
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


def test_missing_search_config_file_errors(tmp_path):
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[[searches]]
search_id = "s"
search_config_file = "searches/nope.toml"
""")
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(p)


def test_two_adopters_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    for n in ("a", "b"):
        _write(tmp_path / "searches" / f"{n}.toml", '[viability]\nprompt="p"\n')
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[[searches]]
search_id = "a"
search_config_file = "searches/a.toml"
adopts_legacy = true
[[searches]]
search_id = "b"
search_config_file = "searches/b.toml"
adopts_legacy = true
""")
    with pytest.raises(ConfigError, match="at most one search may set adopts_legacy"):
        load_config(p)


def test_conflicting_label_display_errors(tmp_path):
    (tmp_path / "searches").mkdir()
    _write(tmp_path / "searches" / "a.toml", '[viability]\nprompt="p"\n[labels]\ndc = "DMV"\n')
    _write(tmp_path / "searches" / "b.toml", '[viability]\nprompt="p"\n[labels]\ndc = "DC Metro"\n')
    p = _write(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"
[[searches]]
search_id = "a"
search_config_file = "searches/a.toml"
[[searches]]
search_id = "b"
search_config_file = "searches/b.toml"
""")
    with pytest.raises(ConfigError, match="maps to both"):
        load_config(p)


def test_missing_file_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.toml")


# ── Integration with the scoring hash (proves per-search dicts are shape-correct) ──

def test_per_search_scoring_hash_differs(tmp_path):
    from viability import scoring_hash_for_config
    cfg = load_config(_multi(tmp_path))
    h_tpm = scoring_hash_for_config(cfg.get_search("tpm").config)
    h_dir = scoring_hash_for_config(cfg.get_search("director").config)
    assert h_tpm and h_dir and h_tpm != h_dir


# ── --fixbasics migrator ──────────────────────────────────────────────────────

def test_fixbasics_moves_bare_keys_preserving_comments(tmp_path):
    p = _write(tmp_path / "config.toml", """# my config
api_token = "tok"   # apify token
db_path = "jobs.db"

[labels]
dc = "DC/DMV"
""")
    changed, msg = migrate_config_to_basics(p)
    assert changed and "under [basics]" in msg
    text = p.read_text()
    assert "[basics]" in text
    assert "# apify token" in text        # inline comments preserved
    assert text.startswith("# my config") # file-level comment stays on top
    # Re-loads with the same effective globals and no deprecation warning path.
    cfg = load_config(p)
    assert cfg.api_token == "tok"
    assert cfg.db_path == "jobs.db"
    assert cfg.label_names == {"dc": "DC/DMV"}


def test_fixbasics_idempotent(tmp_path):
    p = _write(tmp_path / "config.toml", 'db_path = "jobs.db"\n')
    assert migrate_config_to_basics(p)[0] is True
    changed, msg = migrate_config_to_basics(p)   # second run
    assert changed is False and "already present" in msg


def test_fixbasics_noop_when_no_bare_keys(tmp_path):
    p = _write(tmp_path / "config.toml", "[labels]\ndc = \"DC\"\n")
    changed, msg = migrate_config_to_basics(p)
    assert changed is False and "no bare top-level settings" in msg
