from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.ctx import PluginContext


def decide(_ctx: PluginContext, _inputs: dict[str, Any]) -> dict[str, Any]:
    return {"type": "match"}
