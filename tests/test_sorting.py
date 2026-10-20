from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.models import Job, JobEvent, Library, MediaFile
from server.queue.store import list_jobs
from server.search import list_files
from tests.helpers import database_context


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("sorting") as factory:
        yield factory


async def seed_files(session: AsyncSession) -> None:
    library = Library(name="sorting-library", roots=["/library"])
    session.add(library)
    await session.flush()
    for path, size, duration in (("/z.mkv", 30, None), ("/a.mkv", 20, 2), ("/b.mkv", 20, 1), ("/c.mkv", 5, 3)):
        session.add(
            MediaFile(
                library_id=library.id,
                path=path,
                size=size,
                mtime_ns=1,
                container="mkv",
                medium="video",
                width=None,
                height=None,
                resolution_bucket=None,
                duration_s=duration,
                bit_rate=None,
                video_codec=None,
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_files_sort_pages(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await seed_files(session)
        rows = await list_files(session, limit=2, offset=0, sort_by="size", sort_dir="asc")
        later = await list_files(session, limit=2, offset=2, sort_by="size", sort_dir="asc")
        assert [row.path for row in rows + later] == ["/c.mkv", "/a.mkv", "/b.mkv", "/z.mkv"]

        rows = await list_files(session, sort_by="size", sort_dir="desc")
        assert [row.path for row in rows] == ["/z.mkv", "/b.mkv", "/a.mkv", "/c.mkv"]
        for direction, expected in (
            ("asc", ["/b.mkv", "/a.mkv", "/c.mkv", "/z.mkv"]),
            ("desc", ["/c.mkv", "/a.mkv", "/b.mkv", "/z.mkv"]),
        ):
            rows = await list_files(session, sort_by="duration_s", sort_dir=direction)
            assert [row.path for row in rows] == expected


@pytest.mark.asyncio
async def test_jobs_sort_updated(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await seed_files(session)
        files = list((await session.execute(select(MediaFile))).scalars())
        base = datetime(2026, 1, 1, tzinfo=UTC)
        updated = [
            base + timedelta(days=30),
            base + timedelta(days=10),
            base + timedelta(days=20),
            base + timedelta(days=1),
        ]
        jobs = [
            Job(
                media_file_id=file.id,
                library_id=file.library_id,
                created_at=base + timedelta(days=index),
                status="queued",
                updated_at=updated[index],
            )
            for index, file in enumerate(files)
        ]
        session.add_all(jobs)
        await session.flush()
        session.add(JobEvent(job_id=jobs[0].id, type="changed", payload={}, ts=base + timedelta(days=40)))
        await session.commit()
        first, total = await list_jobs(session, sort_by="updated_at", sort_dir="desc", limit=2, offset=0)
        second, _ = await list_jobs(session, sort_by="updated_at", sort_dir="desc", limit=2, offset=2)
        assert [row.id for row in first + second] == [jobs[0].id, jobs[2].id, jobs[1].id, jobs[3].id]
        for job in jobs:
            job.created_at = base
        await session.commit()
        rows, _ = await list_jobs(session, sort_by="created_at", sort_dir="asc")
        assert [row.id for row in rows] == sorted(job.id for job in jobs)


@pytest.mark.asyncio
async def test_file_date_pages(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await seed_files(session)
        files = list((await session.execute(select(MediaFile).order_by(MediaFile.id))).scalars())
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for file, days in zip(files, [3, 1, 1, 2], strict=True):
            file.created_at = base + timedelta(days=days)
            file.updated_at = base + timedelta(days=days)
        await session.commit()
        for field in ("created_at", "updated_at"):
            for direction, indexes in (("asc", [1, 2, 3, 0]), ("desc", [0, 3, 2, 1])):
                first = await list_files(session, limit=2, sort_by=field, sort_dir=direction)
                second = await list_files(session, limit=2, offset=2, sort_by=field, sort_dir=direction)
                assert [file.id for file in first + second] == [files[index].id for index in indexes]
