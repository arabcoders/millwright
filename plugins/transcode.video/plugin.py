from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from common.enums import RateControlMode
from common.plan import AudioPlanInputs, FFmpegPlan, RateControl, VerificationSpec, VideoTarget
from common.plan.metadata_updates import apply_metadata
from common.plan.renderer import change_reasons, changes_media
from pydantic import Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(AudioPlanInputs):
    target: Literal["avc", "hevc", "av1"] = Field(default="hevc", description="Choose the output video codec family.")
    rate_control: RateControlMode = Field(
        default=RateControlMode.QUALITY,
        description="Use encoder quality with variable output size, or target a source bitrate ratio.",
    )
    quality: int | None = Field(
        default=None, ge=0, le=63, description="Set the codec-specific quality value. Applies only in quality mode."
    )
    ratio: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Set video bitrate as a fraction of the source. Applies only in bitrate mode.",
    )
    encoder: str | None = Field(default=None, description="Override the FFmpeg video encoder.")
    encoder_priority: list[str] = Field(
        default=[],
        description=(
            "List encoders in priority order. The encoder override wins. Leave blank for the software default."
        ),
    )
    pixel_format: str | None = Field(default=None, description="Set the encoded video's pixel format.")
    ten_bit: bool = Field(default=False, description="Encode video with 10-bit precision.")
    preset: str | None = Field(default=None, description="Choose the encoder's speed and compression preset.")
    max_width: int | None = Field(default=None, ge=1, description="Limit output video width in pixels.")
    max_height: int | None = Field(default=None, ge=1, description="Limit output video height in pixels.")
    container: Literal["mkv", "mp4"] = Field(default="mkv", description="Choose the output container.")
    bitrate_source: Literal["video", "container", "average"] = Field(
        default="video",
        description="In bitrate mode, choose video bitrate, whole-file bitrate, or their average.",
    )
    size_ceiling_ratio: float = Field(
        default=1.05, gt=0, description="Reject output larger than this multiple of the input file size."
    )
    fix_timestamps: bool = Field(default=False, description="Generate missing timestamps for AVI and TS inputs.")
    extras: list[str] = Field(default=[], description="Add FFmpeg arguments, one argument per entry.")


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    video = ctx.probe.real_video
    if not video:
        return {"type": "skip", "reason": "No video stream found."}
    families = {"avc": {"avc", "h264", "avc1"}, "hevc": {"hevc", "h265"}, "av1": {"av1", "av01"}}
    defaults = {"avc": ("libx264", 0.75, 23), "hevc": ("libx265", 0.5, 21), "av1": ("libsvtav1", 0.45, 30)}
    encoder, ratio, quality = defaults[inputs.target]
    encoder = inputs.encoder or encoder
    priority = inputs.encoder_priority if inputs.encoder is None else []
    if priority:
        encoder = priority[0]
    option = (
        "cq" if "nvenc" in encoder else "global_quality" if "qsv" in encoder else "qp" if "vaapi" in encoder else "crf"
    )
    rate = RateControl(
        mode=inputs.rate_control,
        from_source_bitrate=inputs.ratio or ratio,
        bitrate_source=inputs.bitrate_source,
        quality_option=option,
    )
    if inputs.rate_control == RateControlMode.QUALITY:
        rate.quality = inputs.quality if priority or inputs.quality is not None else quality
    ten_bit = inputs.ten_bit or video[0].ten_bit is True
    pixel_format = inputs.pixel_format or (
        "p010le"
        if ten_bit and any(tag in encoder for tag in ("nvenc", "qsv", "vaapi", "amf"))
        else "yuv420p10le"
        if ten_bit
        else None
    )
    dimensions_change = any(
        (inputs.max_width is not None and stream.width is not None and stream.width > inputs.max_width)
        or (inputs.max_height is not None and stream.height is not None and stream.height > inputs.max_height)
        for stream in video
    )
    video_change = (
        any(stream.codec not in families[inputs.target] for stream in video)
        or inputs.encoder is not None
        or bool(priority)
        or (inputs.rate_control == RateControlMode.QUALITY and inputs.quality is not None)
        or inputs.rate_control == RateControlMode.BITRATE
        or inputs.pixel_format is not None
        or inputs.ten_bit
        or inputs.preset is not None
        or dimensions_change
    )
    plan = FFmpegPlan(
        container=inputs.container,
        streams=inputs.stream_policy(),
        video=VideoTarget(
            encoder=encoder,
            encoder_priority=priority,
            rate=rate,
            pixel_format=pixel_format,
            preset=inputs.preset or None,
            max_width=inputs.max_width,
            max_height=inputs.max_height,
        ),
        expected=VerificationSpec(size_ceiling_ratio=inputs.size_ceiling_ratio),
        generate_timestamps=inputs.fix_timestamps and ctx.probe.container in {"avi", "ts"},
        extras=inputs.extras,
        audio_encode=inputs.audio_targets(),
    )
    if not video_change:
        plan.video = None
    plan = apply_metadata(plan, ctx)
    if not changes_media(plan, ctx.probe):
        return {"type": "skip", "reason": f"The file already matches the {inputs.target.upper()} settings."}
    reasons = change_reasons(plan, ctx.probe)
    return {
        "type": "plan",
        "plan": plan,
        "reason": " ".join(reasons) or "Apply stream and container settings without re-encoding video.",
    }
