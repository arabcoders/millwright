from __future__ import annotations

import asyncio
import mimetypes
import random
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from starlette.responses import FileResponse, Response

from server.api.auth import current_principal
from server.models import Library
from server.scanner.scan import _wanted_files
from server.workspace import wait_workspace_io

router = APIRouter(prefix="/api/background", tags=["settings"], dependencies=[Depends(current_principal)])


@dataclass(kw_only=True)
class BackgroundImage:
    path: Path
    library_id: int
    expires_at: float


def _available(path: Path, roots: list[str]) -> bool:
    return path.is_file() and any(path.resolve().is_relative_to(Path(root).resolve()) for root in roots)


def _find_background(
    libraries: list[tuple[int, list[str]]],
    previous: Path | None,
    ignored: set[str],
    excluded: tuple[Path, ...],
) -> tuple[Path, int] | None:
    mimetypes.init()
    extensions = {
        suffix.lstrip(".") for suffix, mimetype in mimetypes.types_map.items() if mimetype.startswith("image/")
    }
    candidates: list[tuple[Path, int]] = []
    errors: list[str] = []
    for library_id, roots in libraries:
        paths = _wanted_files([Path(root) for root in roots], ignored, extensions, errors, set(), excluded)
        candidates.extend((path, library_id) for path in paths)
    if errors:
        raise OSError("\n".join(errors))
    random.shuffle(candidates)
    candidates.sort(
        key=lambda item: (
            item[0] == previous,
            not item[0].stem.lower().endswith(("fanart", "backdrop", "background", "landscape")),
        )
    )
    roots_by_library = dict(libraries)
    return next(
        ((path, library_id) for path, library_id in candidates if _available(path, roots_by_library[library_id])),
        None,
    )


@router.get("", summary="Show library artwork as the page background")
async def get_background(request: Request, force: bool = False) -> Response:
    cached: BackgroundImage | None = request.app.state.background_image
    async with request.app.state.db() as session:
        if cached is not None and not force and cached.expires_at > time.monotonic():
            roots = await session.scalar(
                select(Library.roots).where(Library.id == cached.library_id, Library.deleted.is_(False))
            )
            if roots is not None and await asyncio.to_thread(_available, cached.path, roots):
                return FileResponse(cached.path, headers={"Cache-Control": "private, no-store"})
        rows = (await session.execute(select(Library.id, Library.roots).where(Library.deleted.is_(False)))).all()
        excluded = session.info["library_excluded_roots"]
    found = await wait_workspace_io(
        partial(
            _find_background,
            [(library_id, roots) for library_id, roots in rows],
            cached.path if cached is not None else None,
            set(request.app.state.settings.current.scanner.ignore_dirs),
            excluded,
        )
    )
    if found is None:
        request.app.state.background_image = None
        return Response(status_code=204, headers={"Cache-Control": "private, no-store"})
    path, library_id = found
    request.app.state.background_image = BackgroundImage(
        path=path, library_id=library_id, expires_at=time.monotonic() + 3600
    )
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})
