from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from common.enums import JobStatus
from sqlalchemy import select

from server.fs.source import MediaSnapshot
from server.models import Job, JobEvent, Library, MediaFile, utcnow
from server.queue.store import _insert_job, check_file_idle, cooldown_hold_until

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


METADATA_JOB_KINDS = frozenset({"probe", "mkv_stats"})


def metadata_source(row: MediaFile | MediaSnapshot) -> dict:
    return {
        "path": row.path,
        "size": row.size,
    }


async def enqueue_metadata_job(
    session: AsyncSession,
    row: MediaFile,
    library: Library,
    *,
    kind: Literal["probe", "mkv_stats"],
    priority: int,
) -> Job:
    await session.get(Library, library.id, with_for_update={"read": True}, populate_existing=True)
    await session.get(MediaFile, row.id, with_for_update=True, populate_existing=True)
    await check_file_idle(session, row.id, metadata=True)
    until = (
        cooldown_hold_until(library.cooldown_s, row.first_seen, utcnow())
        if kind == "mkv_stats" and library.mkv_stats and Path(row.path).suffix.lower() == ".mkv"
        else None
    )
    job = await _insert_job(session, row.id, library.id, kind=kind, priority=priority, now=None, hold_until=until)
    session.add(JobEvent(job_id=job.id, type="metadata_source", payload=metadata_source(row)))
    return job


async def cancel_metadata(session: AsyncSession, job: Job) -> None:
    row = await session.get(MediaFile, job.media_file_id, populate_existing=True)
    expected = await session.scalar(
        select(JobEvent.payload)
        .where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    if row is not None and row.library_id == job.library_id and metadata_source(row) == expected:
        row.metadata_cancelled = True
        row.health_status = "pending"
        row.health_source = None


async def retry_metadata(session: AsyncSession, job: Job, row: MediaFile) -> None:
    row.metadata_cancelled = False
    row.metadata_priority = 1
    row.health_status = "pending"
    row.health_source = None
    if job.kind == "probe":
        row.probe_id = None
    job.priority = max(1, job.priority)
    job.status = JobStatus.QUEUED
    session.add(JobEvent(job_id=job.id, type="metadata_source", payload=metadata_source(row)))


async def fail_metadata(session: AsyncSession, job: Job) -> None:
    row = await session.get(MediaFile, job.media_file_id, populate_existing=True)
    expected = await session.scalar(
        select(JobEvent.payload)
        .where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    if row is not None and row.library_id == job.library_id and metadata_source(row) == expected:
        row.health_status = "probe_failed"
        row.health_source = None
