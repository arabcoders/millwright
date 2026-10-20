from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.ctx import PluginContext


def decide(_ctx: PluginContext, _inputs: dict[str, Any]) -> dict[str, Any]:
    time.sleep(30)
    return {"type": "match"}
