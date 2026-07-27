"""Grouped-header title display: a fuzzy group whose members carry slightly different
titles should show one concrete, identifiable title (the canonical root's) with a
title_varies flag — never the opaque '(varied)' that hid *what* the group was."""
import app


def _header(group_key, n):
    # Minimal grouped-header stand-in (build_grouped_job reads only these keys for a
    # multi-row group). location_count > 1 makes it a real fuzzy group.
    return {"group_key": group_key, "location_count": n,
            "source": "linkedin", "source_max": "linkedin",
            "salary_min": None, "salary_max": None}


def _sub(job_id, title, **extra):
    return {"job_id": job_id, "title": title, "status": "new", **extra}


def test_varying_titles_show_root_title_and_flag():
    """Different member titles → representative is the ROOT's title (job_id == group_key),
    not '(varied)', and title_varies is True."""
    subs = [
        _sub("m2", "Senior Director, Technical Program Management - AI  (Remote Eligible)"),
        _sub("root", "Senior Director, Technical Program Management -Ai/ML (Remote Eligible)"),
    ]
    job = app.build_grouped_job(_header("root", 2), subs)
    assert job["title"] == "Senior Director, Technical Program Management -Ai/ML (Remote Eligible)"
    assert job["title_varies"] is True
    assert job["title"] != app.GROUP_VARIED


def test_uniform_titles_have_no_varies_flag():
    subs = [_sub("root", "Staff Engineer"), _sub("m2", "Staff Engineer")]
    job = app.build_grouped_job(_header("root", 2), subs)
    assert job["title"] == "Staff Engineer"
    assert job["title_varies"] is False


def test_whitespace_only_difference_is_not_a_variation():
    """A cosmetic double-space (feed noise) must not count as a real title variation."""
    subs = [_sub("root", "Program  Manager"), _sub("m2", "Program Manager")]
    job = app.build_grouped_job(_header("root", 2), subs)
    assert job["title_varies"] is False


def test_representative_prefers_root_even_when_not_first():
    """Root preference holds regardless of sub-row order."""
    subs = [_sub("m2", "Bravo Title"), _sub("m3", "Charlie Title"), _sub("root", "Alpha Title")]
    job = app.build_grouped_job(_header("root", 3), subs)
    assert job["title"] == "Alpha Title"
    assert job["title_varies"] is True


def test_grouped_salary_shows_root_band_and_flags_varies():
    """Differing member salaries → show the ROOT's band, not '(varied)' or an envelope,
    with salary_varies set so the template appends '(varies)'."""
    subs = [_sub("root", "T", salary_display="$200k – $250k"),
            _sub("m2", "T", salary_display="$180k – $210k")]
    job = app.build_grouped_job(_header("root", 2), subs)
    assert job["group_salary"] == "$200k – $250k"
    assert job["salary_varies"] is True


def test_grouped_salary_uniform_has_no_varies_flag():
    subs = [_sub("root", "T", salary_display="$200k – $250k"),
            _sub("m2", "T", salary_display="$200k – $250k")]
    job = app.build_grouped_job(_header("root", 2), subs)
    assert job["group_salary"] == "$200k – $250k"
    assert job["salary_varies"] is False
