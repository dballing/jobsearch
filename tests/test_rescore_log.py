"""Tests for rescore_viability.rescore_change_note — the log line emitted when a
re-score changes a job's viability value."""
import rescore_viability as rv


def test_note_on_change():
    assert rv.rescore_change_note("Foo at Acme", "medium", "high") == \
        "  Rescored: Foo at Acme : medium → high"


def test_no_note_on_first_score():
    assert rv.rescore_change_note("Foo at Acme", None, "high") is None


def test_no_note_when_unchanged():
    assert rv.rescore_change_note("Foo at Acme", "high", "high") is None
