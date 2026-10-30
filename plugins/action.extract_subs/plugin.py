from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.actions import ExtractSubtitles
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    formats: list[str] = Field(default=["srt"], description="Extract subtitles as srt or vtt.")
    languages: list[str] = Field(default=["eng"], description="Choose subtitle language codes, such as eng or jpn.")


INPUTS = Inputs


def decide(_ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    action = ExtractSubtitles(formats=inputs.formats, languages=inputs.languages)
    return {"type": "actions", "actions": [action]}
