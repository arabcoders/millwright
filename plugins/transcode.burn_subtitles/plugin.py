from __future__ import annotations

import re
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Literal

from common.plan import FFmpegPlan, RateControl, StreamPolicy, StreamSelection, SubtitleBurn, VideoTarget
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    language: str = Field(default="eng", description="Lowercase ISO-639-2 language code, such as eng")
    name_priority: list[str] = Field(
        default=["video_language", "video", "language"],
        description="Name patterns in priority order: video_language=movie.eng.srt, video=movie.srt, language=eng.srt",
    )
    format_priority: list[str] = Field(
        default=["srt", "ass", "ssa", "vtt"], description="Subtitle extensions in priority order"
    )
    priority_order: Literal["name_first", "format_first"] = Field(
        default="name_first", description="Rank matches by name first or format first"
    )
    on_multiple: Literal["first_match", "error"] = Field(
        default="first_match", description="For multiple matches, choose by priority or return an error"
    )
    on_missing: Literal["skip", "error"] = Field(
        default="skip", description="When no sidecar matches, skip or return an error"
    )
    encoder: str = Field(default="libx264", description="FFmpeg video encoder used for burning subtitles")
    container: Literal["mkv", "mp4"] = Field(default="mkv", description="Output container format")
    quality: int = Field(default=23, ge=0, le=63, description="Video quality value passed to the encoder")

    @field_validator("language")
    @classmethod
    def check_language(cls, value: str) -> str:
        if re.fullmatch(r"[a-z]{3}", value) is None:
            raise ValueError("language must be lowercase ISO-639-2")
        return value

    @field_validator("name_priority")
    @classmethod
    def check_names(cls, value: list[str]) -> list[str]:
        allowed = {"video_language", "video", "language"}
        if not value or len(value) != len(set(value)) or any(item not in allowed for item in value):
            raise ValueError("name_priority must be nonempty, unique, and use allowed names")
        return value

    @field_validator("format_priority")
    @classmethod
    def check_formats(cls, value: list[str]) -> list[str]:
        allowed = {"srt", "ass", "ssa", "vtt"}
        if not value or len(value) != len(set(value)) or any(item not in allowed for item in value):
            raise ValueError("format_priority must be nonempty, unique, and use allowed formats")
        return value


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    if not ctx.probe.real_video:
        return {"type": "skip", "reason": "no video stream"}
    stem = PurePath(ctx.original_filename).stem
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    patterns = {
        "video_language": f"{stem}.{inputs.language}",
        "video": stem,
        "language": inputs.language,
    }
    for name_index, name in enumerate(inputs.name_priority):
        for format_index, extension in enumerate(inputs.format_priority):
            expected = f"{patterns[name]}.{extension}"
            for sidecar in ctx.sidecar_subtitles:
                if sidecar.casefold() == expected.casefold() and sidecar not in seen:
                    candidates.append((name_index, format_index, sidecar))
                    seen.add(sidecar)
    candidates.sort(
        key=lambda item: (
            (item[0], item[1], item[2]) if inputs.priority_order == "name_first" else (item[1], item[0], item[2])
        )
    )
    if len(candidates) > 1 and inputs.on_multiple == "error":
        matches = ", ".join(sorted(candidate[2] for candidate in candidates))
        return {"type": "error", "error": f"multiple matching subtitle sidecars: {matches}"}
    if not candidates:
        if inputs.on_missing == "error":
            return {"type": "error", "error": "no matching subtitle sidecar"}
        return {"type": "skip", "reason": "no matching subtitle sidecar"}
    selected = candidates[0][2]
    plan = FFmpegPlan(
        container=inputs.container,
        streams=(
            StreamPolicy(subtitle=StreamSelection(keep_languages=[], keep_forced=False))
            if inputs.container == "mp4"
            else None
        ),
        video=VideoTarget(
            encoder=inputs.encoder,
            rate=RateControl(
                quality=inputs.quality,
                quality_option=(
                    "cq"
                    if "nvenc" in inputs.encoder
                    else "global_quality"
                    if "qsv" in inputs.encoder
                    else "qp"
                    if "vaapi" in inputs.encoder
                    else "crf"
                ),
            ),
        ),
        burn_subtitle=SubtitleBurn(filename=selected),
    )
    name = inputs.name_priority[candidates[0][0]]
    extension = inputs.format_priority[candidates[0][1]]
    return {"type": "plan", "plan": plan, "reason": f"selected {selected} ({name}, {extension} priority)"}
