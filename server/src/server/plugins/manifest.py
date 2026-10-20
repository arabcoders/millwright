from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from server.plugins.migrations import ConfigMigration, RenamePlugin

_ID_PATTERN = r"^(filter|transcode|action|inspector)\.[a-z0-9_]+$"
_TAG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
_RESERVED_NAMESPACES = frozenset({"media"})

PluginId = Annotated[str, Field(pattern=_ID_PATTERN)]
CapabilityTag = Annotated[str, Field(pattern=_TAG_PATTERN)]


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: PluginId
    kind: Literal["filter", "transcode", "action", "inspector"]
    name: str
    description: Annotated[str, Field(pattern=r"\S(?:.*\S)?")]
    help: str | None = None
    author: Annotated[str, Field(pattern=r"\S(?:.*\S)?")]
    url: HttpUrl
    version: str
    config_version: int = Field(default=1, ge=1, strict=True)
    migrations: list[ConfigMigration] = Field(default_factory=list)
    facets: dict[str, str] = Field(default_factory=dict)
    requires: list[CapabilityTag] = Field(default_factory=list)
    # Subset semantics: the node must have every listed encoder, unlike the
    # ANY-of class tags in `requires`.
    requires_encoders: list[str] = Field(default_factory=list)
    require_plugins: list[PluginId] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_kind_matches_id(self) -> Self:
        if not self.id.startswith(f"{self.kind}."):
            msg = f"id {self.id!r} must start with its kind prefix {self.kind!r}"
            raise ValueError(msg)
        if self.namespace in _RESERVED_NAMESPACES:
            msg = f"namespace {self.namespace!r} is reserved for built-in media facts"
            raise ValueError(msg)
        versions: set[int] = set()
        for migration in self.migrations:
            if migration.from_version in versions:
                msg = f"Multiple migrations start at configuration version {migration.from_version}."
                raise ValueError(msg)
            versions.add(migration.from_version)
            if migration.to_version > self.config_version:
                msg = "A migration cannot exceed the plugin's configuration version."
                raise ValueError(msg)
            for change in migration.changes:
                if isinstance(change, RenamePlugin) and any(
                    re.fullmatch(_ID_PATTERN, value) is None or not value.startswith(f"{self.kind}.")
                    for value in (change.source, change.target)
                ):
                    msg = "Renamed plugin IDs must be valid IDs of the same plugin kind."
                    raise ValueError(msg)
        return self

    @property
    def namespace(self) -> str:
        return self.id.split(".", 1)[1]


def load_manifest(plugin_dir: Path) -> PluginManifest:
    path = Path(plugin_dir) / "plugin.toml"
    with path.open("rb") as file_handle:
        data = tomllib.load(file_handle)
    return PluginManifest.model_validate(data)
