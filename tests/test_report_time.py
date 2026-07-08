"""Tests for the weekly-report time helpers — the UTC-vs-local week bucketing that's
easy to get subtly wrong."""
import time
from datetime import datetime, timezone

import pytest

import app


def test_parse_utc_handles_both_stored_formats():
    # CURRENT_TIMESTAMP / first_seen form (space, no zone) is treated as UTC...
    a = app._parse_utc("2026-06-26 03:13:19")
    assert a == datetime(2026, 6, 26, 3, 13, 19, tzinfo=timezone.utc)
    # ...and the ISO 'Z' history form.
    b = app._parse_utc("2026-06-22T12:16:20Z")
    assert b == datetime(2026, 6, 22, 12, 16, 20, tzinfo=timezone.utc)


def test_parse_utc_bad_input():
    assert app._parse_utc("") is None
    assert app._parse_utc(None) is None
    assert app._parse_utc("not a date") is None


@pytest.mark.parametrize("anchor,exp_start,exp_end", [
    (datetime(2026, 6, 24), datetime(2026, 6, 21), datetime(2026, 6, 28)),  # Wed
    (datetime(2026, 6, 21), datetime(2026, 6, 21), datetime(2026, 6, 28)),  # the Sunday itself
    (datetime(2026, 6, 27), datetime(2026, 6, 21), datetime(2026, 6, 28)),  # the Saturday
])
def test_week_bounds_sun_to_sat(anchor, exp_start, exp_end):
    start, end = app._week_bounds(anchor)
    assert start == exp_start and end == exp_end


@pytest.fixture
def eastern_tz():
    """Pin the process to America/New_York so local-time bucketing is deterministic."""
    if not hasattr(time, "tzset"):
        pytest.skip("tzset() unavailable on this platform")
    import os
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if prev is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prev
    time.tzset()


def test_local_bucketing_across_utc_midnight(eastern_tz):
    # A rejection stamped 2026-06-24T03:16:59Z is 11:16 PM EDT on the 23rd — it must
    # bucket into the Jun 21–27 (local) week, not the following one.
    dt = app._parse_utc("2026-06-24T03:16:59Z")
    local = app._local_naive(dt)
    assert local == datetime(2026, 6, 23, 23, 16, 59)
    start, end = app._week_bounds(datetime(2026, 6, 24))
    assert start <= local < end
