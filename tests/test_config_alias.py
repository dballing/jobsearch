"""Tests for the config.toml alias writer (app.add_company_alias) — the highest-blast-
radius helper, since it edits the file holding the API key."""
import re
import tomllib

import app


BASE = (
    '[ai]\n'
    'api_key = "sk-secret-xyz"\n\n'
    '[company_aliases]\n'
    '"Amazon Web Services (AWS)" = "Amazon"\n'
    '"Sirius XM"                 = "Sirius XM Radio"\n'
    '"Rithum LinkedIn Board"     = "Rithum"\n\n'
    '[[tasks]]\n'
    'name = "x"\n'
)


def _aliases(path):
    return tomllib.loads(path.read_text())["company_aliases"]


def test_add_new_alias(config_file):
    p = config_file(BASE)
    added, err = app.add_company_alias("X, LLC", "Xenon")
    assert (added, err) == (True, None)
    assert _aliases(p)["X, LLC"] == "Xenon"
    # EOL comment present and dated
    line = next(l for l in p.read_text().splitlines() if l.startswith('"X, LLC"'))
    assert re.search(r'#\s*Added \d{4}-\d{2}-\d{2} via web app\.$', line)


def test_idempotent_same_target(config_file):
    p = config_file(BASE)
    before = p.read_text()
    # case-insensitive match, same canonical -> no-op, no write
    assert app.add_company_alias("sirius xm", "Sirius XM Radio") == (False, None)
    assert p.read_text() == before


def test_conflict_different_target_refuses(config_file):
    p = config_file(BASE)
    before = p.read_text()
    added, err = app.add_company_alias("Sirius XM", "Something Else")
    assert added is False and err and "already maps" in err
    assert p.read_text() == before  # nothing written


def test_equals_and_comments_aligned(config_file):
    p = config_file(BASE)
    app.add_company_alias("Prime Video & Amazon MGM Studios", "Amazon")  # new widest key
    app.add_company_alias("SiriusXM", "Sirius XM Radio")
    body = [l for l in p.read_text().splitlines()
            if l.strip() and not l.startswith(("[", "api_key", "name"))]
    # every '=' lines up in one column; every '#' lines up in one column
    assert len({l.index(" = ") for l in body}) == 1
    assert len({l.index("#") for l in body if "#" in l}) == 1


def test_grouped_by_canonical_and_sorted(config_file):
    p = config_file(BASE)
    app.add_company_alias("Prime Video & Amazon MGM Studios", "Amazon")
    app.add_company_alias("SiriusXM", "Sirius XM Radio")
    body = [l for l in p.read_text().splitlines() if l.strip().startswith('"')]
    canon = [l.split(" = ")[1].split("  #")[0].strip() for l in body]
    assert canon == sorted(canon, key=str.lower)                    # canonicals A->Z
    assert canon == ['"Amazon"', '"Amazon"', '"Rithum"',            # variants grouped
                     '"Sirius XM Radio"', '"Sirius XM Radio"']


def test_other_sections_preserved(config_file):
    p = config_file(BASE)
    app.add_company_alias("X, LLC", "Xenon")
    doc = tomllib.loads(p.read_text())
    assert doc["ai"]["api_key"] == "sk-secret-xyz"  # secret untouched
    assert doc["tasks"][0]["name"] == "x"


def test_creates_section_when_missing(config_file):
    p = config_file('[ai]\napi_key = "k"\n')
    added, err = app.add_company_alias("Foo Inc", "Foo")
    assert added is True and err is None
    assert _aliases(p) == {"Foo Inc": "Foo"}


def test_toml_quoting_roundtrips(config_file):
    p = config_file(BASE)
    # keys/values with characters that need TOML basic-string quoting
    app.add_company_alias('Weird "Quoted" & Co.', "Weird\\Co")
    assert _aliases(p)['Weird "Quoted" & Co.'] == "Weird\\Co"


def test_never_writes_unparseable(config_file, monkeypatch):
    p = config_file(BASE)
    before = p.read_text()
    # Force the quoter to emit something that breaks TOML; the guard must refuse.
    monkeypatch.setattr(app, "_toml_basic_string", lambda s: f'"{s}')  # missing close quote
    added, err = app.add_company_alias("Bad", "Value")
    assert added is False and err and "parse" in err.lower()
    assert p.read_text() == before
