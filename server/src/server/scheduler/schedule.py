from __future__ import annotations

from datetime import UTC, tzinfo
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from datetime import datetime


class GridSchedule(BaseModel):
    """Seven day rows with 24 hourly on/off cells each. Monday is the first row."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["grid"]
    cells: list[list[bool]]

    @field_validator("cells")
    @classmethod
    def _week_grid_shape(cls, value: list[list[bool]]) -> list[list[bool]]:
        if len(value) != 7:
            msg = f"grid needs 7 day rows, got {len(value)}"
            raise ValueError(msg)
        for day, row in enumerate(value):
            if len(row) != 24:
                msg = f"day {day} needs 24 hour cells, got {len(row)}"
                raise ValueError(msg)
        return value


ScheduleSpec = GridSchedule


def parse_schedule(raw: object | None) -> ScheduleSpec | None:
    if raw is None:
        return None
    return GridSchedule.model_validate(raw)


def active(schedule: ScheduleSpec | None, now: datetime, timezone: tzinfo = UTC) -> bool:
    if now.tzinfo is None:
        msg = "schedule evaluation requires a tz-aware datetime"
        raise ValueError(msg)
    now = now.astimezone(timezone)
    if schedule is None:
        return True
    return schedule.cells[now.weekday()][now.hour]
