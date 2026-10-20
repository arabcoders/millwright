from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo
from zoneinfo import ZoneInfo


class Dates:
    def __init__(self, timezone: str | tzinfo) -> None:
        self.timezone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone

    def local(self, value: datetime | str) -> datetime:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        # Offset-free diagnostic timestamps represent UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self.timezone)

    def midnight(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=self.timezone).astimezone(UTC)

    def parse(self, value: str) -> datetime:
        if len(value) <= 10:
            raise ValueError("Enter an ISO datetime with a date and time.")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
        # fold=0 selects the earlier occurrence when the wall clock repeats.
        utc = parsed.replace(tzinfo=self.timezone, fold=0).astimezone(UTC)
        if self.local(utc).replace(tzinfo=None) != parsed:
            msg = f"Local time {value} does not exist in timezone {self.timezone}."
            raise ValueError(msg)
        return utc

    def format(self, value: datetime | str | None, *, clock_only: bool = False) -> str:
        if value is None:
            return "-"
        local = self.local(value)
        return local.time().isoformat(timespec="seconds") if clock_only else local.isoformat()
