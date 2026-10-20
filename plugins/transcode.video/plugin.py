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
    target: Literal["avc", "hevc", "av1"] = Field(default="hevc", description="Target video codec family")
    rate_control: RateControlMode = Field(
        default=RateControlMode.QUALITY,
        description="Quality sets encoder quality with variable size. Bitrate targets a source bitrate ratio",
    )
    quality: int | None = Field(
        default=None, ge=0, le=63, description="Codec-specific video quality value. Used only in quality mode"
    )
    ratio: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Target video bitrate as a fraction of the source. Used only in bitrate mode",
    )
    encoder: str | None = Field(default=None, description="FFmpeg video encoder override")
    encoder_priority: list[str] = Field(
        default=[],
        description="Encoder priority order. Override wins. Empty uses the software default",
    )
    pixel_format: str | None = Field(default=None, description="Pixel format for the encoded video")
    ten_bit: bool = Field(default=False, description="Encode video with 10-bit precision")
    preset: str | None = Field(default=None, description="Encoder speed and compression preset")
    max_width: int | None = Field(default=None, ge=1, description="Maximum output video width in pixels")
    max_height: int | None = Field(default=None, ge=1, description="Maximum output video height in pixels")
    container: Literal["mkv", "mp4"] = Field(default="mkv", description="Output container format")
    bitrate_source: Literal["video", "container", "average"] = Field(
        default="video", description="Bitrate mode source: video, container, or their average when both are available"
    )
    size_ceiling_ratio: float = Field(default=1.05, gt=0, description="Maximum output-to-source file size ratio")
    fix_timestamps: bool = Field(default=False, description="Generate missing timestamps for AVI and TS inputs")
    extras: list[str] = Field(default=[], description="Additional FFmpeg arguments, one argument per entry")


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    video = ctx.probe.real_video
    if not video:
        return {"type": "skip", "reason": "no video stream"}
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
        return {"type": "skip", "reason": f"already {inputs.target}"}
    reasons = change_reasons(plan, ctx.probe)
    return {
        "type": "plan",
        "plan": plan,
        "reason": " ".join(reasons) or "Apply stream and container settings without re-encoding video.",
    }
