"""The 'skipped' status filter and its composition with the viability filter, so a
High + Skipped pairing surfaces manually-skipped strong roles for reconsideration.
build_where is pure, so these assert on the generated SQL/params directly — no DB."""
import app


def test_skipped_filter_is_registered_and_manual_only():
    """'skipped' is an offered filter labelled 'Skipped', and matches only the manual
    'skipped' status — never 'autoskipped' (the low-viability auto-decision)."""
    label, condition = app.STATUS_FILTERS["skipped"]
    assert label == "Skipped"
    assert condition == "status = 'skipped'"
    assert "autoskipped" not in condition


def test_skipped_filter_builds_expected_clause():
    where, params = app.build_where("", "skipped")
    assert where == "WHERE status = 'skipped'"
    assert params == []


def test_skipped_composes_with_high_viability():
    """The use case: status=skipped AND viability=high, AND-ed into one clause."""
    where, params = app.build_where("", "skipped", viability="high")
    assert "status = 'skipped'" in where
    assert "viability = ?" in where
    assert " AND " in where          # the two conditions are combined, not exclusive
    assert params == ["high"]
