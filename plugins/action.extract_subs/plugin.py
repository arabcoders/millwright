from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.actions import ExtractSubtitles
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    formats: list[str] = Field(default=["srt"], description="Subtitle output formats to extract: srt or vtt.")
    languages: list[str] = Field(default=["eng"], description="ISO 639-2 language tags to extract.")


INPUTS = Inputs


def decide(_ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    action = ExtractSubtitles(formats=inputs.formats, languages=inputs.languages)
    return {"type": "actions", "actions": [action]}
