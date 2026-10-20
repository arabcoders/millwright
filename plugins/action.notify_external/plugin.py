from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.actions import ExternalNotificationConfig, NotifyExternal

if TYPE_CHECKING:
    from common.ctx import PluginContext

INPUTS = ExternalNotificationConfig


def decide(_ctx: PluginContext, inputs: ExternalNotificationConfig) -> dict[str, Any]:
    return {"type": "actions", "actions": [NotifyExternal(**inputs.model_dump())]}
