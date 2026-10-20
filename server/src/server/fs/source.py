from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from common.fs.identity import source_identity
from common.media.tools import ProbeError
from sqlalchemy import and_, select

from server.models import Job, JobAttempt, Library, MediaFile
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


@dataclass(kw_only=True, slots=True)
class MediaSnapshot:
    id: int
    library_id: int
    path: str
    size: int
    mtime_ns: int
    source_identity: dict[str, int] | None
    created_at: datetime
    first_seen: datetime
    updated_at: datetime
    probe_id: int | None
    health_status: str

    @classmethod
    def from_media(cls, row: MediaFile) -> MediaSnapshot:
        return cls(
            id=row.id,
            library_id=row.library_id,
            path=row.path,
            size=row.size,
            mtime_ns=row.mtime_ns,
            source_identity=dict(row.source_identity) if row.source_identity is not None else None,
            created_at=row.created_at,
            first_seen=row.first_seen,
            updated_at=row.updated_at,
            probe_id=row.probe_id,
            health_status=row.health_status,
        )


def check_source_root(session: AsyncSession, row: MediaFile | MediaSnapshot, library: Library) -> None:
    path = Path(row.path)
    resolved = path.resolve()
    if row.library_id != library.id or sum(resolved.is_relative_to(Path(root)) for root in library.roots) != 1:
        raise ValueError("Current file must match exactly one library root.")
    if any(resolved.is_relative_to(root) for root in session.info["library_excluded_roots"]):
        message = f"Source is inside a workspace, trash, or healthcheck directory: {path}"
        raise ValueError(message)


async def authorized_library(session: AsyncSession, library_id: int, row: MediaFile | MediaSnapshot) -> Library | None:
    library = await session.scalar(
        select(Library)
        .where(Library.id == library_id)
        .with_for_update(read=True, of=Library)
        .execution_options(populate_existing=True)
    )
    if library is None or library.suspended or library.deleted:
        return None
    try:
        await wait_workspace_io(lambda: check_source_root(session, row, library))
    except ValueError:
        return None
    return library


def catalog_generation(row: MediaFile | MediaSnapshot) -> ColumnElement[bool]:
    return and_(
        MediaFile.id == row.id,
        MediaFile.library_id == row.library_id,
        MediaFile.path == row.path,
        MediaFile.size == row.size,
        # Probe IDs can be reused without changing the source generation.
        MediaFile.updated_at == row.updated_at,
        MediaFile.probe_id.is_not_distinct_from(row.probe_id),
    )


async def claim_catalog(session: AsyncSession, expected: MediaFile | MediaSnapshot) -> int | None:
    await session.flush()
    return await session.scalar(select(MediaFile.id).where(catalog_generation(expected)).with_for_update(of=MediaFile))


async def lock_job_source(
    session: AsyncSession, job_id: int, *, attempt_id: int | None = None, skip_locked: bool = False
) -> Job | None:
    source_ids = (
        await session.execute(select(Job.library_id, Job.media_file_id).where(Job.id == job_id))
    ).one_or_none()
    if source_ids is None:
        return None
    library = (
        await session.get(
            Library,
            source_ids.library_id,
            populate_existing=True,
            with_for_update={"read": True, "skip_locked": skip_locked},
        )
        if source_ids.library_id is not None
        else None
    )
    if skip_locked and library is None:
        return None
    media = (
        await session.get(
            MediaFile, source_ids.media_file_id, populate_existing=True, with_for_update={"skip_locked": skip_locked}
        )
        if source_ids.media_file_id is not None
        else None
    )
    if skip_locked and media is None:
        return None
    job = await session.get(Job, job_id, populate_existing=True, with_for_update={"skip_locked": skip_locked})
    if job is not None and (job.library_id, job.media_file_id) != (source_ids.library_id, source_ids.media_file_id):
        raise ValueError("Job source changed. Retry the operation.")
    if job is not None and attempt_id is not None:
        attempt = await session.get(
            JobAttempt, attempt_id, populate_existing=True, with_for_update={"skip_locked": skip_locked}
        )
        if skip_locked and attempt is None:
            return None
    # The identity map is weak. Keep the locked source rows reachable for callers
    # that inspect their refreshed state immediately after this helper returns.
    session.info["locked_job_source"] = (library, media)
    return job


def capture_source(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        **source_identity(stat),
        "dates": {"atime_ns": stat.st_atime_ns, "mtime_ns": stat.st_mtime_ns},
    }


def restore_source_dates(path: Path, dates: tuple[int, int]) -> os.stat_result:
    try:
        os.utime(path, ns=dates)
    except OSError as exc:
        error = f"could not restore media dates: {path}: {exc}"
        raise ProbeError(error) from exc
    return path.stat()
