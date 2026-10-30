from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.media.language import normalize_language
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    expected: list[str] = Field(
        default=["eng", "und"], description="List allowed audio language codes, such as eng or und."
    )


INPUTS = Inputs


def decide(ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    expected = {normalize_language(code) for code in inputs.expected}
    unexpected = sorted({s.language for s in ctx.probe.audio if s.language and s.language not in expected})
    return {"type": "match", "facets": {"unexpected_languages": unexpected}}
