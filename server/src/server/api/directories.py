from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from server.api.auth import current_principal

router = APIRouter(prefix="/api/directories", tags=["libraries"], dependencies=[Depends(current_principal)])

# The directory picker only navigates. A cap keeps huge trees from flooding the response.
MAX_ENTRIES = 500


def _scan_subdirectories(path: Path) -> list[dict]:
    with os.scandir(path) as entries:
        found = [
            {"name": entry.name, "path": entry.path}
            for entry in entries
            if not entry.name.startswith(".") and entry.is_dir()
        ]
    found.sort(key=lambda item: item["name"])
    return found[:MAX_ENTRIES]


@router.get("", summary="List subdirectories of a path")
async def read_directories(path: Annotated[str, Query()] = "/") -> dict:
    # resolve() normalizes ".." segments and symlinks, so traversal cannot smuggle
    # a path outside the tree the client already saw.
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")
    try:
        items = await asyncio.to_thread(_scan_subdirectories, resolved)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"directory not readable: {exc}") from exc
    return {"path": str(resolved), "items": items}
