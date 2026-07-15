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
import struct
import zlib

import pytest

import app

_SNAP_DIR = pathlib.Path(__file__).parent / "snapshots"
_ROOT     = pathlib.Path(__file__).resolve().parent.parent   # repo root
_UPDATE   = os.environ.get("UPDATE_SNAPSHOTS") == "1"


def _decode_png(path: pathlib.Path):
    """Minimal pure-stdlib PNG decoder → (width, height, bytes_per_pixel, [row bytearrays]).

    Enough to inspect pixels without a third-party image library (no Pillow/numpy dependency).
    Handles 8-bit RGB (color type 2) and RGBA (6), which is what Chrome's --screenshot emits;
    other formats assert out. Undoes the five PNG scanline filters (they cascade, so all rows
    are decoded top-to-bottom). ~0.4s for the ~1400×1120 screenshot."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    pos, idat = 8, bytearray()
    width = height = color_type = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[pos + 8:pos + 18])
            assert bit_depth == 8, f"unsupported bit depth {bit_depth}"
        elif ctype == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        elif ctype == b"IEND":
            break
        pos += 12 + length
    bpp = {2: 3, 6: 4}.get(color_type)
    assert bpp, f"unsupported PNG color type {color_type}"
    raw = zlib.decompress(bytes(idat))
    stride = width * bpp

    def _paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)

    prev = bytearray(stride)
    rows, i = [], 0
    for _ in range(height):
        ft = raw[i]; i += 1
        cur = bytearray(raw[i:i + stride]); i += stride
        if ft == 1:      # Sub
            for x in range(bpp, stride): cur[x] = (cur[x] + cur[x - bpp]) & 255
        elif ft == 2:    # Up
            for x in range(stride): cur[x] = (cur[x] + prev[x]) & 255
        elif ft == 3:    # Average
            for x in range(stride):
                a = cur[x - bpp] if x >= bpp else 0
                cur[x] = (cur[x] + ((a + prev[x]) >> 1)) & 255
        elif ft == 4:    # Paeth
            for x in range(stride):
                a = cur[x - bpp] if x >= bpp else 0
                c = prev[x - bpp] if x >= bpp else 0
                cur[x] = (cur[x] + _paeth(a, prev[x], c)) & 255
        elif ft != 0:
            raise ValueError(f"unknown PNG filter {ft}")
        rows.append(cur); prev = cur
    return width, height, bpp, rows
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


def test_screenshot_not_clipped():
    """docs/screenshot.png must have a uniform background strip along its bottom edge — proof
    the whole fixture table fit inside the capture window and nothing was silently clipped.

    make_screenshot.sh captures a fixed window height; if the sample fixture grows past it,
    the last rows (including the hotlisted one) drop off the bottom without any error. This
    guards that: it fails whenever content reaches the bottom edge. Fix by raising the
    --window-size height in make_screenshot.sh and re-running it."""
    w, h, bpp, rows = _decode_png(_ROOT / "docs" / "screenshot.png")
    strip = 8  # px of required uniform bottom margin — a clipped row leaves content here
    bg = bytes(rows[h - 1][:bpp])  # background sampled from the bottom-left pixel
    for y in range(h - strip, h):
        row = rows[y]
        for x in range(0, w * bpp, bpp):
            assert bytes(row[x:x + bpp]) == bg, (
                f"docs/screenshot.png has non-background content within {strip}px of the "
                f"bottom edge (row {y}, col {x // bpp}) — the fixture table is clipped and a "
                f"job row was lost. Raise the --window-size height in make_screenshot.sh and "
                f"re-run it to re-capture the full table.")
