from __future__ import annotations

from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from common.media.language import normalize_language
from common.metadata import MetadataValues

if TYPE_CHECKING:
    from common.media.probe import ProbeData

_SUBTITLE_FORMATS = frozenset({"srt", "vtt"})


class ActionOutcome(BaseModel):
    kind: str
    ok: bool
    error: str | None = None
    artifacts: list[str] = []


class PluginAction(BaseModel):
    plugin_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    config_version: int | None = Field(default=None, ge=1, strict=True, exclude_if=lambda value: value is None)


class ExtractSubtitles(PluginAction):
    kind: Literal["extract_subtitles"] = "extract_subtitles"
    formats: list[str]
    languages: list[str]

    @field_validator("languages")
    @classmethod
    def check_languages(cls, value: list[str]) -> list[str]:
        return [normalize_language(code) for code in value]

    @field_validator("formats")
    @classmethod
    def check_formats(cls, value: list[str]) -> list[str]:
        for fmt in value:
            if fmt not in _SUBTITLE_FORMATS:
                msg = f"subtitle formats must be one of {sorted(_SUBTITLE_FORMATS)}, got {fmt!r}"
                raise ValueError(msg)
        return value


class PreserveTimestamps(PluginAction):
    kind: Literal["preserve_timestamps"] = "preserve_timestamps"


class Notify(PluginAction):
    kind: Literal["notify"] = "notify"
    targets: list[str] = Field(min_length=1)
    on: list[Literal["job.succeeded", "job.failed", "job.cancelled"]] = Field(min_length=1)


class PathMapping(BaseModel):
    source: str = Field(min_length=1, description="Path used by Millwright.")
    destination: str = Field(min_length=1, description="Path used by the application.")

    @model_validator(mode="after")
    def check_paths(self) -> PathMapping:
        source = PurePath(self.source)
        destination = (
            PureWindowsPath(self.destination)
            if PureWindowsPath(self.destination).is_absolute()
            else PurePosixPath(self.destination)
        )
        if not source.is_absolute() or not destination.is_absolute():
            raise ValueError("Use absolute paths in mappings.")
        if ".." in source.parts or ".." in destination.parts:
            raise ValueError("Mapping paths can't contain '..'.")
        return self


class ExternalNotificationConfig(BaseModel):
    application: Literal["radarr", "sonarr", "plex", "jellyfin", "emby"]
    url: HttpUrl = Field(description="Application URL, including any URL base.")
    api_key: str = Field(
        min_length=1, repr=False, description="Application API key or token.", json_schema_extra={"ui": "password"}
    )
    unmonitor_after_refresh: bool = Field(
        default=False,
        description="Disable future automatic upgrades for the movie or associated episodes after refresh succeeds.",
    )
    path_mappings: list[PathMapping] = Field(
        default=[],
        description="Add mappings if an application uses different paths for your files.",
        json_schema_extra={"ui": "path_mappings"},
    )

    @field_validator("url")
    @classmethod
    def check_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Use an application URL without credentials, a query, or a fragment.")
        return value

    @model_validator(mode="after")
    def check_options(self) -> ExternalNotificationConfig:
        if self.application not in {"radarr", "sonarr"} and self.unmonitor_after_refresh:
            raise ValueError("Unmonitor after refresh is only supported for Radarr and Sonarr.")
        sources = [PurePath(mapping.source) for mapping in self.path_mappings]
        if len(sources) != len(set(sources)):
            raise ValueError("Each Millwright path can appear only once.")
        return self


class NotifyExternal(ExternalNotificationConfig, PluginAction):
    kind: Literal["notify_external"] = "notify_external"


class EmbedMetadata(PluginAction):
    kind: Literal["embed_metadata"] = "embed_metadata"
    values: MetadataValues
    existing_tags: Literal["fill_missing", "overwrite"] = "fill_missing"
    when_no_media_changes: Literal["skip", "remux"] = "skip"


Action = Annotated[
    ExtractSubtitles | PreserveTimestamps | Notify | NotifyExternal | EmbedMetadata, Field(discriminator="kind")
]


def extraction_error(actions: list[Action], probe: ProbeData, outcomes: list[ActionOutcome]) -> str | None:
    expected = sum(
        len(action.formats)
        * sum(not action.languages or stream.language in action.languages for stream in probe.subtitle)
        for action in actions
        if isinstance(action, ExtractSubtitles)
    )
    extracted = [outcome for outcome in outcomes if outcome.kind == "extract_subtitles"]
    if any(not outcome.ok for outcome in extracted):
        return "Subtitle extraction failed. Source preserved."
    if len(extracted) < expected or any(len(outcome.artifacts) != 1 for outcome in extracted):
        return "Subtitle extraction report incomplete. Source preserved."
    return None
