from __future__ import annotations

from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from common.actions import NotifyExternal

from server.external_notifications.arr import notify_arr
from server.external_notifications.media_server import notify_media_server
from server.external_notifications.plex import notify_plex

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


async def notify_external(payload: dict, save_progress: Callable[[dict], Awaitable[None]] | None = None) -> bool:
    action = NotifyExternal.model_validate(payload["action"])
    path = PurePath(payload["path"])
    if action.path_mappings:
        matches = [mapping for mapping in action.path_mappings if path.parent.is_relative_to(mapping.source)]
        if not matches:
            message = f"No path mapping matches {str(path.parent)!r}."
            raise ValueError(message)
        mapping = max(matches, key=lambda item: len(PurePath(item.source).parts))
        relative = path.relative_to(mapping.source)
        destination = (
            PureWindowsPath(mapping.destination)
            if PureWindowsPath(mapping.destination).is_absolute()
            else PurePosixPath(mapping.destination)
        )
        path = destination.joinpath(*relative.parts)
    match action.application:
        case "radarr" | "sonarr":
            return await notify_arr(action, payload, str(path), save_progress)
        case "plex" | "jellyfin" | "emby":
            if action.application == "plex":
                return await notify_plex(action, str(path.parent))
            return await notify_media_server(action, str(path.parent))
