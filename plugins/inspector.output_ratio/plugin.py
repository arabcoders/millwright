from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext
    from common.media.probe import ProbeData


class Inputs(BaseModel):
    metric: Literal["size", "bitrate"] = Field(
        default="size",
        description=(
            "Compare output and source by file size or bitrate. Bitrate uses the first video stream's reported value, "
            "or whole-file average bitrate when unavailable."
        ),
    )
    min_ratio: float = Field(
        default=0.5,
        ge=0.01,
        le=100.0,
        description="Minimum output-to-source ratio. 0.5 permits output values down to half the source.",
    )


INPUTS = Inputs


def metric_value(metric: str, probe: ProbeData) -> float | None:
    if metric == "size":
        return float(probe.size_bytes)
    stream = next(iter(probe.real_video), None)
    if stream is not None and stream.bit_rate:
        return float(stream.bit_rate)
    if probe.duration_s > 0:
        return round(probe.size_bytes * 8 / probe.duration_s)
    return None


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    if ctx.source_probe is None:
        return {"type": "skip", "reason": "no source probe"}
    output = metric_value(inputs.metric, ctx.probe)
    source = metric_value(inputs.metric, ctx.source_probe)
    if output is None or source is None or source == 0:
        return {"type": "veto", "reason": f"cannot determine {inputs.metric} for guard comparison"}
    ratio = output / source
    if ratio < inputs.min_ratio:
        return {
            "type": "veto",
            "reason": f"output {inputs.metric} is {ratio:.2f}x of source, minimum is {inputs.min_ratio:.2f}x",
        }
    return {"type": "match"}
