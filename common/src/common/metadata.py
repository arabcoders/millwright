from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MetadataSource = Literal["nfo", "info_json"]


class MetadataValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    date: str | None = None
    genre: str | None = None
    artist: str | None = None
    show: str | None = None
    season_number: str | None = None
    episode_sort: str | None = None

    @field_validator("*", mode="after")
    @classmethod
    def check_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("metadata values must not contain NUL characters")
        return value.strip() or None


class SidecarMetadata(BaseModel):
    sources: dict[MetadataSource, MetadataValues] = Field(default_factory=dict)
    errors: dict[MetadataSource, str] = Field(default_factory=dict)
