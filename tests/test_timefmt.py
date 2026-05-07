"""Tests for the CDT timestamp formatter."""

from datetime import datetime, timezone

import pytest

from radar.timefmt import fmt_cdt, to_cdt


def test_to_cdt_from_unix_timestamp():
    # 2026-05-04T02:00:00Z = 2026-05-03 21:00:00 CDT (DST is active in May)
    ts = int(datetime(2026, 5, 4, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    dt = to_cdt(ts)
    assert dt.year == 2026 and dt.month == 5 and dt.day == 3
    assert dt.hour == 21 and dt.minute == 0


def test_to_cdt_from_iso_string():
    dt = to_cdt("2026-05-04T02:00:00Z")
    assert dt.day == 3 and dt.hour == 21


def test_to_cdt_from_naive_datetime_treated_as_utc():
    naive = datetime(2026, 5, 4, 2, 0, 0)  # no tz
    dt = to_cdt(naive)
    assert dt.day == 3 and dt.hour == 21


def test_to_cdt_winter_uses_cst():
    # 2026-01-15T14:00:00Z = 2026-01-15 08:00:00 CST (UTC-6 in winter)
    ts = int(datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    dt = to_cdt(ts)
    assert dt.day == 15 and dt.hour == 8


def test_fmt_cdt_default_format():
    ts = int(datetime(2026, 5, 4, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    s = fmt_cdt(ts)
    assert "2026-05-03 21:00" in s
    assert "CDT" in s


def test_fmt_cdt_custom_format():
    ts = int(datetime(2026, 5, 4, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    assert fmt_cdt(ts, "%Y-%m-%d %H:%M") == "2026-05-03 21:00"


def test_to_cdt_rejects_unsupported_type():
    with pytest.raises(TypeError):
        to_cdt(object())
