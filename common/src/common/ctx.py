from __future__ import annotations

from pydantic import BaseModel, Field

from common.actions import EmbedMetadata
from common.media.probe import ProbeData
from common.metadata import SidecarMetadata

LibraryValue = str | int | float | bool
FacetValue = str | int | float | bool | list[str] | list[int]


class PluginContext(BaseModel):
    probe: ProbeData
    original_filename: str
    sidecar_subtitles: list[str] = Field(default_factory=list)
    sidecar_metadata: SidecarMetadata = Field(default_factory=SidecarMetadata)
    metadata_requests: list[EmbedMetadata] = Field(default_factory=list)
    # Later stages compare the completed stage's input with its output.
    source_probe: ProbeData | None = None
    library: dict[str, LibraryValue] = Field(default_factory=dict)
    facets: dict[str, FacetValue] = Field(default_factory=dict)
