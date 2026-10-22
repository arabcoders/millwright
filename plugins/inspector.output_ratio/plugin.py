from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext
    from common.media.probe import ProbeData


class Inputs(BaseModel):
    metric: Literal["size", "bitrate"] = Field(
        default="size",
        description="Compare the output and input by file size or bitrate.",
    )
    min_ratio: float = Field(
        default=0.5,
        ge=0.01,
        le=100.0,
        description="Require output to be at least this fraction of the input. For example, 0.5 means half.",
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
        return {"type": "skip", "reason": "Input file information is unavailable for this check."}
    output = metric_value(inputs.metric, ctx.probe)
    source = metric_value(inputs.metric, ctx.source_probe)
    if output is None or source is None or source == 0:
        return {"type": "veto", "reason": f"Cannot compare input and output {inputs.metric}."}
    ratio = output / source
    if ratio < inputs.min_ratio:
        return {
            "type": "veto",
            "reason": f"Output {inputs.metric} is {ratio:.2f} times the input, below the minimum of {inputs.min_ratio:.2f}.",
        }
    return {"type": "match"}
