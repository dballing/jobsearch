"""Tests for resume.py — turning a resume file into plain text for the viability scorer.

Hermetic: text/markdown cases write a temp file; the PDF and DOCX cases build a real file with
pypdf / python-docx (installed via requirements.txt) so the extraction path is exercised end to
end, skipping only if the optional library is genuinely absent."""
import pytest

from resume import ResumeError, extract_resume_text, main, _MAX_RESUME_CHARS


def test_reads_plaintext(tmp_path):
    p = tmp_path / "resume.txt"
    p.write_text("Derek Balling\nSenior TPM\n", encoding="utf-8")
    assert extract_resume_text(p) == "Derek Balling\nSenior TPM"  # stripped


def test_reads_markdown(tmp_path):
    p = tmp_path / "resume.md"
    p.write_text("# Derek Balling\n\n- 30 years IT\n", encoding="utf-8")
    text = extract_resume_text(p)
    assert "# Derek Balling" in text and "30 years IT" in text


def test_extensionless_treated_as_text(tmp_path):
    p = tmp_path / "resume"
    p.write_text("plain resume body", encoding="utf-8")
    assert extract_resume_text(p) == "plain resume body"


def test_missing_file_errors(tmp_path):
    with pytest.raises(ResumeError, match="resume_file not found"):
        extract_resume_text(tmp_path / "nope.md")


def test_empty_file_errors(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("   \n\t\n", encoding="utf-8")  # whitespace-only → nothing to score against
    with pytest.raises(ResumeError, match="extracted to no text"):
        extract_resume_text(p)


def test_legacy_doc_errors(tmp_path):
    p = tmp_path / "resume.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE magic; content irrelevant — the suffix is refused
    with pytest.raises(ResumeError, match="legacy .doc isn't supported"):
        extract_resume_text(p)


def test_unsupported_suffix_errors(tmp_path):
    p = tmp_path / "resume.rtf"
    p.write_text("whatever", encoding="utf-8")
    with pytest.raises(ResumeError, match="unsupported resume format"):
        extract_resume_text(p)


def test_non_utf8_text_errors(tmp_path):
    p = tmp_path / "resume.txt"
    p.write_bytes(b"\xff\xfe\x00\x01\x80")  # invalid UTF-8
    with pytest.raises(ResumeError, match="not valid UTF-8"):
        extract_resume_text(p)


def test_text_is_capped(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("x" * (_MAX_RESUME_CHARS + 500), encoding="utf-8")
    assert len(extract_resume_text(p)) == _MAX_RESUME_CHARS


def test_reads_docx(tmp_path):
    docx = pytest.importorskip("docx")  # python-docx
    p = tmp_path / "resume.docx"
    document = docx.Document()
    document.add_paragraph("Derek Balling")
    document.add_paragraph("Senior Technical Program Manager")
    document.save(str(p))
    text = extract_resume_text(p)
    assert "Derek Balling" in text and "Technical Program Manager" in text


def test_reads_pdf():
    pytest.importorskip("pypdf")
    # A tiny committed one-page PDF with a real text layer (tests/fixtures/sample_resume.pdf).
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "sample_resume.pdf"
    text = extract_resume_text(fixture)
    assert "Derek Balling" in text and "TPM" in text


# ── Letter-spacing repair (PDF "P R O F E S S I O N A L" tracking artifact) ────

def test_despace_collapses_tracked_header():
    from resume import _collapse_letterspaced
    # Word boundaries are the wider (2-space) gaps; single spaces are inter-letter tracking.
    assert _collapse_letterspaced("D E R E K  J .  B A L L I N G") == "DEREK J. BALLING"
    assert _collapse_letterspaced("P R O F E S S I O N A L  S U M M A R Y") == "PROFESSIONAL SUMMARY"
    assert _collapse_letterspaced("E X P E R I E N C E") == "EXPERIENCE"


def test_despace_leaves_prose_untouched():
    from resume import _collapse_letterspaced
    prose = ("Technical Program Manager with 20+ years of experience designing and executing "
             "large-scale programs across infrastructure, security, and compliance domains.")
    assert _collapse_letterspaced(prose) == prose
    # A normal line with a couple of legitimate single-char tokens is NOT letter-spaced.
    assert _collapse_letterspaced("I am a TPM with deep infra experience") == \
        "I am a TPM with deep infra experience"
    # A contact line of symbols/digits (few real single LETTERS) is left alone.
    contact = "Alexandria, VA, USA  ·  +1 929 346 2855"
    assert _collapse_letterspaced(contact) == contact


def test_despace_only_matched_lines_in_multiline():
    from resume import _collapse_letterspaced
    text = ("E X P E R I E N C E\n"
            "Sr. Technical Program Manager at Google\n"
            "S K I L L S")
    assert _collapse_letterspaced(text) == (
        "EXPERIENCE\n"
        "Sr. Technical Program Manager at Google\n"
        "SKILLS")


def test_extract_applies_despacing(tmp_path):
    # End to end through extract_resume_text (any format runs the repair).
    p = tmp_path / "resume.txt"
    p.write_text("P R O F E S S I O N A L  S U M M A R Y\nSenior TPM, 20+ years.\n", encoding="utf-8")
    text = extract_resume_text(p)
    assert text.startswith("PROFESSIONAL SUMMARY")
    assert "Senior TPM, 20+ years." in text


# ── CLI preview (preview_resume.sh → resume.main): "how the parser sees this" ──

def test_cli_prints_extracted_text(tmp_path, capsys):
    p = tmp_path / "resume.md"
    p.write_text("# Derek\nSenior TPM, 30 years\n", encoding="utf-8")
    rc = main([str(p)])
    out = capsys.readouterr()
    assert rc == 0
    assert "Senior TPM, 30 years" in out.out          # the body goes to stdout (pipeable)
    assert "Parsed resume" in out.err                 # the summary goes to stderr
    assert "format: md" in out.err


def test_cli_error_returns_nonzero(tmp_path, capsys):
    rc = main([str(tmp_path / "missing.md")])
    out = capsys.readouterr()
    assert rc == 1
    assert "ERROR" in out.err and "not found" in out.err
    assert out.out == ""                              # nothing on stdout on failure


def test_cli_flags_truncation(tmp_path, capsys):
    p = tmp_path / "big.txt"
    p.write_text("y" * (_MAX_RESUME_CHARS + 100), encoding="utf-8")
    rc = main([str(p)])
    out = capsys.readouterr()
    assert rc == 0
    assert "truncated" in out.err                     # user is warned the scorer sees less
    assert len(out.out.rstrip("\n")) == _MAX_RESUME_CHARS


# ── compose_candidate_prompt: the pure prompt-folding step ─────────────────────

def test_compose_without_resume_is_identity():
    from viability import compose_candidate_prompt
    # No resume → byte-identical prompt, so a resume-less search never re-scores.
    assert compose_candidate_prompt("candidate profile", None) == "candidate profile"
    assert compose_candidate_prompt("candidate profile", "") == "candidate profile"
    assert compose_candidate_prompt("candidate profile", "   \n ") == "candidate profile"


def test_compose_with_resume_appends_and_mandates_factor():
    from viability import compose_candidate_prompt, RESUME_COMPETITIVENESS_DIMENSION
    out = compose_candidate_prompt("candidate profile", "RESUME BODY HERE")
    assert out.startswith("candidate profile")          # original prompt preserved up front
    assert "RESUME BODY HERE" in out                    # resume appended
    assert "CANDIDATE RESUME (verbatim)" in out         # under the delimited header
    # The employer's-eye competitiveness factor is mandated (never optional) when a resume exists.
    assert RESUME_COMPETITIVENESS_DIMENSION in out
    assert "REQUIRED" in out


# ── config integration: resume_file folds into the viability prompt + hash ─────

def _cfg(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_config_folds_resume_into_prompt_and_hash(tmp_path):
    from config import load_config
    from viability import scoring_hash_for_config
    _cfg(tmp_path / "resume.md", "# Derek\n30 years infrastructure/TPM experience\n")
    p = _cfg(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"

[ai]
model = "claude-haiku-4-5"

[viability]
enabled = true
prompt = "base candidate profile"
resume_file = "resume.md"
""")
    cfg = load_config(p)
    vprompt = cfg.default_search().config["viability"]["prompt"]
    assert vprompt.startswith("base candidate profile")
    assert "30 years infrastructure/TPM experience" in vprompt
    assert "application_competitiveness" in vprompt
    # The resume path is registered for mtime-based reload.
    assert (tmp_path / "resume.md") in cfg.source_files

    # A config with the resume must hash differently from the same config without it.
    p2 = _cfg(tmp_path / "noresume.toml", """
[basics]
db_path = "jobs.db"

[ai]
model = "claude-haiku-4-5"

[viability]
enabled = true
prompt = "base candidate profile"
""")
    cfg2 = load_config(p2)
    assert (scoring_hash_for_config(cfg.default_search().config)
            != scoring_hash_for_config(cfg2.default_search().config))


def test_editing_resume_content_changes_scoring_hash(tmp_path):
    """Editing the resume must mark existing scores stale. Because the resume TEXT is folded into
    the viability prompt (which scoring_hash_for_config hashes), a content change flows straight
    into the staleness hash — no separate file-hash needed, and it's content-based, so a rename or
    a byte-identical copy re-scores nothing while a real edit does."""
    from config import load_config
    from viability import scoring_hash_for_config
    resume = _cfg(tmp_path / "resume.md", "30 years infrastructure/TPM experience")
    p = _cfg(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"

[ai]
model = "claude-haiku-4-5"

[viability]
enabled = true
prompt = "profile"
resume_file = "resume.md"
""")
    h_before = scoring_hash_for_config(load_config(p).default_search().config)
    # Edit the resume body; the composed prompt (hence the hash) must change.
    resume.write_text("30 years infrastructure/TPM experience, now AWS-certified", encoding="utf-8")
    h_after = scoring_hash_for_config(load_config(p).default_search().config)
    assert h_before != h_after


def test_config_missing_resume_file_errors(tmp_path):
    from config import ConfigError, load_config
    p = _cfg(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"

[viability]
prompt = "profile"
resume_file = "does-not-exist.md"
""")
    with pytest.raises(ConfigError, match="resume_file not found"):
        load_config(p)


def test_config_resume_resolved_per_search_dir(tmp_path):
    """Path B: a search's resume_file resolves relative to that search FILE's directory, so it
    lives alongside the search config — not the top-level canonical config."""
    from config import load_config
    (tmp_path / "searches").mkdir()
    _cfg(tmp_path / "searches" / "resume_tpm.md", "TPM resume: 30 yrs infra")
    _cfg(tmp_path / "searches" / "tpm.toml", """
[viability]
enabled = true
prompt = "tpm profile"
resume_file = "resume_tpm.md"
""")
    p = _cfg(tmp_path / "config.toml", """
[basics]
db_path = "jobs.db"

[[searches]]
search_id = "tpm"
search_config_file = "searches/tpm.toml"
""")
    cfg = load_config(p)
    vprompt = cfg.get_search("tpm").config["viability"]["prompt"]
    assert "TPM resume: 30 yrs infra" in vprompt
    assert (tmp_path / "searches" / "resume_tpm.md") in cfg.source_files
