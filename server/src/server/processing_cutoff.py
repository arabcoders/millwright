from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_serializer
from sqlalchemy import BigInteger, DateTime, and_, cast, func, or_

from server.models import Library, MediaFile
from server.serialize import iso_utc

if TYPE_CHECKING:
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement

    from server.fs.source import MediaSnapshot


class DisabledCutoff(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessingCutoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: AwareDatetime
    fields: list[Literal["created_at", "first_seen", "mtime"]] = Field(default=["created_at"], min_length=1)
    match: Literal["all", "any"] = "all"

    @field_validator("at")
    @classmethod
    def utc_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Processing cutoff fields must be unique.")
        return value

    @model_serializer
    def serialize(self) -> dict:
        # PATCH excludes unset library settings, not defaults within a supplied policy.
        return {"at": iso_utc(self.at), "fields": self.fields, "match": self.match}


def epoch_ns(value: datetime) -> int:
    # Avoid floating-point timestamp rounding.
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def processing_allowed(library: Library, media: MediaFile | MediaSnapshot) -> bool:
    policy = library.processing_cutoff
    if not policy:
        return True
    cutoff = epoch_ns(datetime.fromisoformat(policy["at"]))
    dates = {
        "created_at": epoch_ns(media.created_at),
        "first_seen": epoch_ns(media.first_seen),
        "mtime": media.mtime_ns,
    }
    matches = (dates[field] >= cutoff for field in policy["fields"])
    return all(matches) if policy["match"] == "all" else any(matches)


def _epoch_us(column: InstrumentedAttribute[datetime] | ColumnElement[datetime]) -> ColumnElement[int]:
    return cast(func.extract("epoch", column) * 1_000_000, BigInteger)


def processing_clause() -> ColumnElement[bool]:
    # Microseconds cover the entire datetime range without bigint overflow.
    # Integer division truncates negative mtimes, so their remainder must also qualify.
    cutoff_at = Library.processing_cutoff["at"].as_string()
    cutoff = _epoch_us(cast(cutoff_at, DateTime(timezone=True)))
    match = Library.processing_cutoff["match"].as_string()
    modified = MediaFile.mtime_ns // 1000
    comparisons = {
        "created_at": _epoch_us(MediaFile.created_at) >= cutoff,
        "first_seen": _epoch_us(MediaFile.first_seen) >= cutoff,
        "mtime": or_(modified > cutoff, and_(modified == cutoff, MediaFile.mtime_ns % 1000 >= 0)),
    }
    all_matches = []
    any_matches = []
    for field, matches in comparisons.items():
        selected = Library.processing_cutoff["fields"].contains([field])
        all_matches.append(or_(~selected, matches))
        any_matches.append(and_(selected, matches))
    return or_(
        cutoff_at.is_(None),
        and_(match == "all", *all_matches),
        and_(match == "any", or_(*any_matches)),
    )
