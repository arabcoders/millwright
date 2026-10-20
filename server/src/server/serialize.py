from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def orm_columns(value: object) -> dict:
    result = {}
    inspected = inspect(value)
    assert inspected is not None
    assert inspected.mapper is not None
    mapper = inspected.mapper
    for column in mapper.column_attrs:
        item = getattr(value, column.key)
        if isinstance(item, datetime):
            item = iso_utc(item)
        elif column.key == "log_data" and item is not None:
            item = item.decode(errors="replace")
        result[column.key] = item
    return result
