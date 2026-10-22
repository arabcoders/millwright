from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from common.actions import NotifyExternal


async def notify_media_server(action: NotifyExternal, folder: str) -> bool:
    headers = (
        {"Authorization": f'MediaBrowser Token="{action.api_key}"'}
        if action.application == "jellyfin"
        else {"X-Emby-Token": action.api_key}
    )
    async with httpx.AsyncClient(base_url=str(action.url).rstrip("/") + "/", headers=headers, timeout=15) as client:
        response = await client.post(
            "Library/Media/Updated", json={"Updates": [{"Path": folder, "UpdateType": "Modified"}]}
        )
        response.raise_for_status()
    return True
