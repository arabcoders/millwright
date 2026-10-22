from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.media.language import normalize_language
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext
    from common.media.probe import ProbeStream


class Inputs(BaseModel):
    min_audio: int = Field(
        default=1, ge=0, description="Require at least this many audio streams. Set to zero to disable."
    )
    min_subtitles: int = Field(
        default=0, ge=0, description="Require at least this many subtitle streams. Set to zero to disable."
    )
    audio_languages: list[str] = Field(
        default=[],
        description=("Count these audio language codes. Leave empty to count all. Use und for untagged tracks."),
    )
    subtitle_languages: list[str] = Field(
        default=[],
        description=("Count these subtitle language codes. Leave empty to count all. Use und for untagged tracks."),
    )


INPUTS = Inputs


def _count(streams: list[ProbeStream], languages: list[str]) -> int:
    allowed = {normalize_language(language.strip().lower()) for language in languages}
    return sum(1 for stream in streams if not allowed or (stream.language or "und").lower() in allowed)


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    if ctx.source_probe is None:
        return {"type": "skip", "reason": "Input file information is unavailable for this check."}

    audio = _count(ctx.probe.audio, inputs.audio_languages)
    subtitles = _count(ctx.probe.subtitle, inputs.subtitle_languages)
    if audio < inputs.min_audio:
        reason = f"Audio stream count is {audio}, below the minimum of {inputs.min_audio}"
        if inputs.audio_languages:
            reason += f" (languages: {', '.join(inputs.audio_languages)})"
        return {"type": "veto", "reason": reason + "."}
    if inputs.min_subtitles and subtitles < inputs.min_subtitles:
        reason = f"Subtitle stream count is {subtitles}, below the minimum of {inputs.min_subtitles}"
        if inputs.subtitle_languages:
            reason += f" (languages: {', '.join(inputs.subtitle_languages)})"
        return {"type": "veto", "reason": reason + "."}
    return {"type": "match"}
