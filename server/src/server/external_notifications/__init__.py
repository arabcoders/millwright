from __future__ import annotations

from typing import TYPE_CHECKING

from common.actions import NotifyExternal

from server.external_notifications.arr import notify_arr

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


async def notify_external(payload: dict, save_progress: Callable[[dict], Awaitable[None]] | None = None) -> bool:
    action = NotifyExternal.model_validate(payload["action"])
    match action.application:
        case "radarr" | "sonarr":
            return await notify_arr(action, payload, save_progress)
