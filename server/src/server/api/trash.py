from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from server.api.auth import current_principal
from server.models import MediaFile, TrashEntry
from server.serialize import iso_utc
from server.trash import delete_trash_entry, restore_trash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

router = APIRouter(prefix="/api/trash", tags=["trash"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replace_existing: bool = False
    move_to_trash: bool = False
    sync_to_disk: bool = True


def _entry_dict(entry: TrashEntry, current_file_id: int | None) -> dict:
    return {
        "id": entry.id,
        "created_at": iso_utc(entry.created_at),
        "updated_at": iso_utc(entry.updated_at),
        "library_id": entry.library_id,
        "original_path": entry.original_path,
        "suffix": entry.suffix,
        "size_bytes": entry.size_bytes,
        "mtime_ns": entry.mtime_ns,
        "trashed_at": iso_utc(entry.trashed_at),
        "purge_at": iso_utc(entry.purge_at),
        "current_file_id": current_file_id,
    }


@router.get("", summary="List trash entries")
async def list_trash(
    request: Request,
    query: Annotated[str | None, Query()] = None,
    library_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=999)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[Literal["trashed_at", "purge_at", "size_bytes", "original_path"], Query()] = "trashed_at",
    sort_dir: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> dict:
    db: async_sessionmaker = request.app.state.db
    column = {
        "trashed_at": TrashEntry.trashed_at,
        "purge_at": TrashEntry.purge_at,
        "size_bytes": TrashEntry.size_bytes,
        "original_path": TrashEntry.original_path,
    }[sort_by]
    ordering = column.asc() if sort_dir == "asc" else column.desc()
    tie = TrashEntry.id.asc() if sort_dir == "asc" else TrashEntry.id.desc()
    async with db() as session:
        filters = []
        if library_id is not None:
            filters.append(TrashEntry.library_id == library_id)
        search = query.strip().lower() if query else None
        if search:
            filters.append(func.lower(TrashEntry.original_path).contains(search, autoescape=True))
        entries = (
            await session.execute(
                select(TrashEntry, MediaFile.id)
                .outerjoin(
                    MediaFile,
                    (MediaFile.path == TrashEntry.original_path) & (MediaFile.library_id == TrashEntry.library_id),
                )
                .where(*filters)
                .order_by(ordering, tie)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        total = await session.scalar(select(func.count()).select_from(TrashEntry).where(*filters))
        total_size = await session.scalar(select(func.coalesce(func.sum(TrashEntry.size_bytes), 0)).where(*filters))
    return {
        "items": [_entry_dict(entry, current_file_id) for entry, current_file_id in entries],
        "pagination": {"limit": limit, "offset": offset, "total": total},
        "total_size_bytes": int(total_size),
    }


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a trash entry")
async def delete_entry(entry_id: str, request: Request) -> None:
    try:
        deleted = await delete_trash_entry(request.app.state.db, request.app.state.trash_dir, entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="trash deletion failed") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="trash entry not found")


@router.delete("", summary="Delete all trash entries")
async def delete_all(request: Request) -> dict:
    async with request.app.state.db() as session:
        entries = (await session.execute(select(TrashEntry.id, TrashEntry.size_bytes).order_by(TrashEntry.id))).all()
    deleted = 0
    deleted_size_bytes = 0
    failed = []
    for entry_id, size_bytes in entries:
        try:
            if await delete_trash_entry(request.app.state.db, request.app.state.trash_dir, entry_id):
                deleted += 1
                deleted_size_bytes += size_bytes
        except Exception:
            LOG.exception("trash expunge failed for %s", entry_id)
            failed.append(entry_id)
    return {"deleted": deleted, "deleted_size_bytes": deleted_size_bytes, "failed": failed}


@router.post("/{entry_id}/restore", summary="Restore a trash entry")
async def restore(entry_id: str, body: RestoreBody, request: Request) -> dict:
    try:
        destination = await restore_trash(
            request.app.state.db,
            request.app.state.trash_dir,
            entry_id,
            replace_existing=body.replace_existing,
            move_to_trash=body.move_to_trash,
            sync_to_disk=body.sync_to_disk,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="destination exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Restore failed. Check server logs for details.") from exc
    return {"id": entry_id, "restored_path": str(destination)}
