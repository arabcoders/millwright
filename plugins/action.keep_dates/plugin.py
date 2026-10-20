from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.actions import PreserveTimestamps
from pydantic import BaseModel

if TYPE_CHECKING:
    from common.ctx import PluginContext


class Inputs(BaseModel):
    pass


INPUTS = Inputs


def decide(_ctx: PluginContext, _inputs: Inputs) -> dict[str, Any]:
    return {"type": "actions", "actions": [PreserveTimestamps()]}
