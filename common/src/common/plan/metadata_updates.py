from __future__ import annotations

from typing import TYPE_CHECKING

from common.metadata import MetadataValues
from common.plan.metadata import field_tags, global_tags, metadata_updates
from common.plan.renderer import changes_media

if TYPE_CHECKING:
    from common.ctx import PluginContext
    from common.plan.models import FFmpegPlan


def apply_metadata(plan: FFmpegPlan, ctx: PluginContext) -> FFmpegPlan:
    if not ctx.metadata_requests:
        return plan
    media_changes = changes_media(plan.model_copy(update={"metadata": None}), ctx.probe)
    values = plan.metadata.model_dump(exclude_none=True) if plan.metadata is not None else {}
    tags = global_tags(ctx.probe)
    for request in ctx.metadata_requests:
        if not media_changes and request.when_no_media_changes == "skip":
            continue
        for field, value in request.values.model_dump(exclude_none=True).items():
            if request.existing_tags == "overwrite" or not (
                values.get(field) or any(current.strip() for _, current in field_tags(field, tags))
            ):
                values[field] = value
    if not values:
        return plan
    metadata = MetadataValues.model_validate(values)
    container = ctx.probe.container if plan.container == "original" else plan.container
    updates = metadata_updates(metadata, ctx.probe, container)
    return plan.model_copy(update={"metadata": metadata}) if updates else plan
