from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from server.serialize import iso_utc


def test_iso_utc_values() -> None:
    assert iso_utc(datetime(2026, 1, 1, 12, 0)) == "2026-01-01T12:00:00Z"
    assert iso_utc(datetime(2026, 1, 1, 15, tzinfo=timezone(timedelta(hours=3)))) == "2026-01-01T12:00:00Z"
    assert iso_utc(datetime(2026, 1, 1, 12, tzinfo=UTC)) == "2026-01-01T12:00:00Z"
    assert iso_utc(None) is None
