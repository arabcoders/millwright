from datetime import UTC, date, datetime, timedelta

import pytest

from common.dates import Dates


@pytest.mark.parametrize(
    "timezone, value, expected",
    [
        ("Asia/Kuwait", "2026-01-01T23:30:00Z", "2026-01-02T02:30:00+03:00"),
        ("Asia/Kuwait", "2026-01-01T23:30:00.123456+00:00", "2026-01-02T02:30:00.123456+03:00"),
        ("Asia/Kuwait", datetime(2026, 1, 1, 23, 30), "2026-01-02T02:30:00+03:00"),
        ("Europe/Berlin", "2026-10-25T00:30:00Z", "2026-10-25T02:30:00+02:00"),
        ("Europe/Berlin", "2026-10-25T01:30:00Z", "2026-10-25T02:30:00+01:00"),
    ],
)
def test_local_format(timezone, value, expected):
    assert Dates(timezone).format(value) == expected


@pytest.mark.parametrize("day, hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_midnight_dst(day, hours):
    dates = Dates("Europe/Berlin")
    assert dates.midnight(day + timedelta(days=1)) - dates.midnight(day) == timedelta(hours=hours)
    assert dates.midnight(day).tzinfo is UTC


def test_clock_format():
    assert Dates("Asia/Kuwait").format("2026-01-01T23:00:00Z", clock_only=True) == "02:00:00"
    assert Dates("Asia/Kuwait").format(None) == "-"


@pytest.mark.parametrize(
    "timezone, value, expected",
    [
        ("Asia/Kuwait", "2026-01-01T12:00:00", datetime(2026, 1, 1, 9, tzinfo=UTC)),
        ("Asia/Kuwait", "2026-01-01T12:00:00+01:00", datetime(2026, 1, 1, 11, tzinfo=UTC)),
        ("Asia/Kuwait", "2026-01-01T12:00:00Z", datetime(2026, 1, 1, 12, tzinfo=UTC)),
        ("Europe/Berlin", "2026-10-25T02:30:00", datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
    ],
)
def test_parse_timezone(timezone, value, expected):
    assert Dates(timezone).parse(value) == expected


def test_parse_skipped_time():
    with pytest.raises(ValueError):
        Dates("Europe/Berlin").parse("2026-03-29T02:30:00")


def test_parse_requires_time():
    with pytest.raises(ValueError):
        Dates("Asia/Kuwait").parse("2026-01-01")
