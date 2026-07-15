"""HTML snapshot regression tests.

Render the real app (Flask test client) against the deterministic sample dataset and compare
the jobs-table region — delimited by <!-- snapshot:jobs-table:… --> markers in jobs.html — to
a committed golden. A diff means the rendered output changed; decide whether that's a bug or
an intended change, and if intended regenerate with:

    UPDATE_SNAPSHOTS=1 ./run_tests.sh tests/test_snapshots.py

Only the table region is snapshotted (not the chrome/JS), so unrelated navbar/script edits
don't churn the goldens. The fixture's data and timestamps are all fixed, so the output is
byte-stable across machines and runs.
"""
import os
import pathlib
import re

import pytest

import app

_SNAP_DIR = pathlib.Path(__file__).parent / "snapshots"
_UPDATE   = os.environ.get("UPDATE_SNAPSHOTS") == "1"
# The view captured for the README screenshot: all statuses, flat, newest first — the most
# feature-dense single page (varied statuses/viability, salary override, hotlist tint).
_SCREENSHOT_URL = "/?status_filter=all&group_match=0&sort=first_seen&dir=desc&per_page=200"
_MARKERS  = re.compile(
    r"<!-- snapshot:jobs-table:start -->(.*)<!-- snapshot:jobs-table:end -->", re.DOTALL)


def _table_region(url: str) -> str:
    html = app.app.test_client().get(url).get_data(as_text=True)
    m = _MARKERS.search(html)
    assert m, f"snapshot markers not found in response for {url}"
    return m.group(1).strip()


def _assert_snapshot(name: str, content: str) -> None:
    _SNAP_DIR.mkdir(exist_ok=True)
    path = _SNAP_DIR / name
    if _UPDATE:
        path.write_text(content + "\n")
        return
    if not path.exists():
        path.write_text(content + "\n")
        pytest.fail(f"created missing snapshot {name}; review it, then commit")
    expected = path.read_text().rstrip("\n")
    assert content == expected, (
        f"{name} differs from the committed snapshot. If this change is intended, "
        f"regenerate with: UPDATE_SNAPSHOTS=1 ./run_tests.sh")


@pytest.mark.usefixtures("sample_app_db")
def test_snapshot_all_flat():
    # Every status, flat (group_match=0), newest first — the richest single view.
    _assert_snapshot(
        "jobs_all_flat.html",
        _table_region("/?status_filter=all&group_match=0&sort=first_seen&dir=desc&per_page=200"))


@pytest.mark.usefixtures("sample_app_db")
def test_snapshot_all_grouped():
    # Grouped (matched-jobs) view — exercises the fuzzy group's collapsed header + members.
    _assert_snapshot(
        "jobs_all_grouped.html",
        _table_region("/?status_filter=all&group_match=1&sort=first_seen&dir=desc&per_page=200"))


@pytest.mark.usefixtures("sample_app_db")
def test_snapshot_screenshot_mock():
    """The FULL page rendered against the fixture IS the committed snapshots/mock_screenshot.html
    — the standalone HTML the README screenshot (docs/screenshot.png) is captured from headless
    (see make_screenshot.sh). An unexpected diff is a regression to fix; an intended one means
    regenerating the mock with UPDATE_SNAPSHOTS=1 ./run_tests.sh AND re-running make_screenshot.sh
    to refresh the PNG. Unlike the table-region snapshots, this whole-page golden intentionally
    DOES track chrome/JS changes, since the screenshot must reflect the real current UI."""
    html = app.app.test_client().get(_SCREENSHOT_URL).get_data(as_text=True)
    path = _SNAP_DIR / "mock_screenshot.html"
    if _UPDATE:
        path.write_text(html)
        return
    if not path.exists():
        path.write_text(html)
        pytest.fail("created mock_screenshot.html; review it, regenerate the PNG, then commit")
    assert html == path.read_text(), (
        "mock_screenshot.html differs from the app's current render. If intended, regenerate "
        "with UPDATE_SNAPSHOTS=1 ./run_tests.sh then run ./make_screenshot.sh to refresh the PNG.")
