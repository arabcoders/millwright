from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from common.actions import EmbedMetadata
from common.metadata import MetadataSource, MetadataValues
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    existing_tags: Literal["fill_missing", "overwrite"] = Field(
        default="fill_missing", description="Fill absent tags or replace only supplied fields"
    )
    source_priority: list[MetadataSource] = Field(
        default=["nfo", "info_json"], description="Source order. The first nonempty value wins for each field"
    )
    when_no_media_changes: Literal["skip", "remux"] = Field(
        default="skip", description="Skip metadata-only work or permit changed tags alone to trigger processing"
    )

    @field_validator("source_priority")
    @classmethod
    def check_priority(cls, value: list[MetadataSource]) -> list[MetadataSource]:
        if not value or len(value) != len(set(value)):
            raise ValueError("source_priority must be nonempty and unique")
        return value


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    values: dict[str, str] = {}
    for source in inputs.source_priority:
        if error := ctx.sidecar_metadata.errors.get(source):
            return {"type": "error", "error": error}
        metadata = ctx.sidecar_metadata.sources.get(source)
        if metadata is not None:
            for key, value in metadata.model_dump(exclude_none=True).items():
                values.setdefault(key, value)
    actions = (
        [
            EmbedMetadata(
                values=MetadataValues.model_validate(values),
                existing_tags=inputs.existing_tags,
                when_no_media_changes=inputs.when_no_media_changes,
            )
        ]
        if values
        else []
    )
    return {"type": "actions", "actions": actions}
