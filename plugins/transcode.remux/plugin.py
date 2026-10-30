from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from common.plan import AudioPlanInputs, FFmpegPlan, VerificationSpec
from common.plan.metadata_updates import apply_metadata
from common.plan.renderer import change_reasons, changes_media
from pydantic import Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(AudioPlanInputs):
    container: Literal["mkv", "mp4", "original"] = Field(default="mkv", description="Choose the output container.")
    size_ceiling_ratio: float = Field(
        default=1.05, gt=0, description="Reject output larger than this multiple of the input file size."
    )


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    audio = inputs.audio_targets()
    plan = FFmpegPlan(
        container=inputs.container,
        streams=inputs.stream_policy(),
        audio_encode=audio,
        expected=VerificationSpec(size_ceiling_ratio=inputs.size_ceiling_ratio),
    )
    media_changes = changes_media(plan, ctx.probe)
    plan = apply_metadata(plan, ctx)
    if not changes_media(plan, ctx.probe):
        return {"type": "skip", "reason": "The file already matches the requested settings."}
    reasons = change_reasons(plan, ctx.probe)
    # Generate missing packet timestamps during remuxing, not as a reason to remux again.
    plan.generate_timestamps = media_changes
    return {
        "type": "plan",
        "plan": plan,
        "reason": " ".join(reasons),
    }
