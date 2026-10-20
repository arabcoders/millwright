from __future__ import annotations

from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from common.enums import WebhookType
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _source_path(value: str) -> PurePath:
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        message = f"Expected an absolute path without parent traversal: {value}"
        raise ValueError(message)
    return path


class PathReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    destination: str

    @field_validator("source")
    @classmethod
    def source_path(cls, value: str) -> str:
        return str(_source_path(value))

    @field_validator("destination")
    @classmethod
    def destination_path(cls, value: str) -> str:
        path = _source_path(value)
        if not isinstance(path, PurePosixPath):
            raise ValueError("Destination must be an absolute server path")
        return str(path)


class WebhookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: WebhookType
    path_replacements: list[PathReplacement] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_sources(self) -> WebhookBody:
        sources = [_source_path(item.source) for item in self.path_replacements]
        if len(set(sources)) != len(sources):
            raise ValueError("Path replacement sources must be unique")
        return self


def convert_path(value: str, replacements: list[PathReplacement]) -> Path:
    path = _source_path(value)
    for replacement in sorted(replacements, key=lambda item: len(_source_path(item.source).parts), reverse=True):
        source = _source_path(replacement.source)
        if path.is_relative_to(source):
            return Path(replacement.destination).joinpath(*path.relative_to(source).parts)
    if not isinstance(path, PurePosixPath):
        message = f"No server path replacement matches: {value}"
        raise ValueError(message)
    return Path(path)


def event_paths(kind: WebhookType, payload: dict) -> set[str] | None:
    """Return None for connection tests and unrelated native notification events."""
    event = payload.get("eventType")
    if event == "Test":
        return None
    if kind == WebhookType.GENERIC:
        path = payload.get("path")
        if isinstance(path, str) and path:
            return {path}
        if isinstance(path, list) and path and all(isinstance(item, str) and item for item in path):
            return set(path)
        raise ValueError("Generic Connect requires a nonempty path string or an array of nonempty path strings")

    sonarr = kind == WebhookType.SONARR
    file_key = "episodeFile" if sonarr else "movieFile"
    folder_key = "series" if sonarr else "movie"
    folder_path_key = "path" if sonarr else "folderPath"
    file_delete = "EpisodeFileDelete" if sonarr else "MovieFileDelete"
    folder_events = {"SeriesAdd", "SeriesDelete"} if sonarr else {"MovieAdded", "MovieDelete"}
    if not isinstance(event, str) or not event:
        raise ValueError("Native Connect requires eventType")
    if event not in {"Download", "Rename", file_delete, *folder_events}:
        return None

    paths: set[str] = set()

    def add_file(item: object, *, previous: bool = False) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            message = f"{event} requires file paths"
            raise ValueError(message)
        paths.add(item["path"])
        if previous:
            old = item.get("previousPath")
            if not isinstance(old, str) or not old:
                raise ValueError("Rename requires previousPath")
            paths.add(old)

    if event in folder_events:
        folder = payload.get(folder_key)
        path = folder.get(folder_path_key) if isinstance(folder, dict) else None
        if not isinstance(path, str) or not path:
            message = f"{event} requires {folder_key}.{folder_path_key}"
            raise ValueError(message)
        paths.add(path)
    elif event == "Rename":
        files = payload.get("renamedEpisodeFiles" if sonarr else "renamedMovieFiles")
        if not isinstance(files, list) or not files:
            raise ValueError("Rename requires renamed files")
        for item in files:
            add_file(item, previous=True)
    else:
        files = payload.get("episodeFiles") if sonarr and event == "Download" else None
        if files is not None:
            if not isinstance(files, list) or not files:
                raise ValueError("Download requires episodeFiles")
            for item in files:
                add_file(item)
        else:
            add_file(payload.get(file_key))
        if event == "Download":
            deleted = payload.get("deletedFiles", [])
            if not isinstance(deleted, list):
                raise ValueError("Download deletedFiles must be a list")
            for item in deleted:
                add_file(item)
    return paths
