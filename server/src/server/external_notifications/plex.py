from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.actions import NotifyExternal


class _Location(BaseModel):
    path: str


class _Section(BaseModel):
    key: int = Field(gt=0)
    locations: list[_Location] = Field(default=[], alias="Location")


class _Sections(BaseModel):
    sections: list[_Section] = Field(default=[], alias="Directory")


class _Library(BaseModel):
    container: _Sections = Field(alias="MediaContainer")


async def notify_plex(action: NotifyExternal, folder: str) -> bool:
    async with httpx.AsyncClient(
        base_url=str(action.url).rstrip("/") + "/",
        headers={
            "X-Plex-Token": action.api_key,
            "X-Plex-Client-Identifier": "millwright",
            "X-Plex-Product": "Millwright",
            "Accept": "application/json",
        },
        timeout=15,
    ) as client:
        response = await client.get("library/sections")
        response.raise_for_status()
        library = _Library.model_validate(response.json())
        path_type = PureWindowsPath if PureWindowsPath(folder).is_absolute() else PurePosixPath
        path = path_type(folder)
        sections = [
            section
            for section in library.container.sections
            if any(path.is_relative_to(path_type(location.path)) for location in section.locations)
        ]
        if not sections:
            message = f"No Plex library contains {folder!r}. Check the path mappings and Plex library folders."
            raise ValueError(message)
        for section in sections:
            response = await client.get(f"library/sections/{section.key}/refresh", params={"path": folder})
            response.raise_for_status()
    return True
