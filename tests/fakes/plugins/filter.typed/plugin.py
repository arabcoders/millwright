from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Mode(StrEnum):
    FAST = "fast"
    SLOW = "slow"


class Inputs(BaseModel):
    threshold: int = Field(default=5, ge=0, le=10)
    mode: Mode = Mode.FAST
    note: str = "hi"


INPUTS = Inputs


def decide(_ctx: PluginContext, inputs: Inputs) -> dict[str, Any]:
    return {"type": "match", "facets": {"mode": inputs.mode, "threshold": inputs.threshold}}
