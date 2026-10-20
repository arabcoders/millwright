from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    source: Literal["video", "container", "average"] = Field(
        default="video", description="Bitrate source: video, container, or their average when both are available"
    )
    avc_ratio: float = Field(
        default=0.75, ge=0.05, le=1.0, description="AVC bitrate estimate as a source bitrate ratio."
    )
    hevc_ratio: float = Field(
        default=0.5, ge=0.05, le=1.0, description="HEVC bitrate estimate as a source bitrate ratio."
    )
    av1_ratio: float = Field(
        default=0.45, ge=0.05, le=1.0, description="AV1 bitrate estimate as a source bitrate ratio."
    )


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    video = next(iter(ctx.probe.real_video), None)
    if video is None:
        return {"type": "skip", "reason": "no video stream"}
    if ctx.probe.target_bitrate_kbps(inputs.source, 1) is None:
        return {"type": "skip", "reason": "no derivable video bitrate"}
    ratios = {"avc": inputs.avc_ratio, "hevc": inputs.hevc_ratio, "av1": inputs.av1_ratio}
    targets = ["avc", "hevc", "av1"]
    facets: dict[str, int] = {}
    for target in targets:
        ratio = ratios[target]
        projected = ctx.probe.target_bitrate_kbps(inputs.source, ratio)
        assert projected is not None
        facets[f"{target}_kbps"] = projected
        facets[f"{target}_size_bytes"] = round(projected * 1000 * ctx.probe.duration_s / 8)
    return {
        "type": "match",
        "facets": facets,
    }
