from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from common.plan.models import FFmpegPlan, VideoTarget
from common.enums import JobStatus
from server.event_rows import job_row, media_file_row
from server.events import EventBus, publish_file_status, publish_job_status
from server.models import Job, JobEvent, Library, MediaFile
from server.queue.store import enqueue_file_job, finish
from server.serialize import iso_utc
from common.fs.identity import source_identity
from tests.helpers import database_context, make_test_temp_dir


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    # These assertions flush explicitly. Database latency must not split the batch.
    monkeypatch.setattr("server.events.EVENT_BATCH_INTERVAL", 60)
    async with database_context("event-rows") as factory:
        yield factory


async def add_file(session, name: str) -> tuple[int, int]:
    path = make_test_temp_dir("event-media") / name
    path.write_bytes(b"x")
    identity = source_identity(path.stat())
    library = Library(name=name, roots=[str(path.parent)])
    session.add(library)
    await session.flush()
    media = MediaFile(
        library_id=library.id,
        path=str(path),
        size=identity["size"],
        mtime_ns=path.stat().st_mtime_ns,
        source_identity=identity,
        container="mkv",
        medium="movie",
        last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(media)
    await session.flush()
    return media.id, library.id


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [JobStatus.FAILED, JobStatus.CANCELLED])
async def test_status_rows(db, outcome: JobStatus) -> None:
    async with db() as session:
        file_id, library_id = await add_file(session, "a")
        job = await enqueue_file_job(session, file_id, library_id, FFmpegPlan(video=VideoTarget(encoder="libx264")))
        job.status = JobStatus.QUEUED.value
        job.updated_at = datetime(2026, 1, 2, tzinfo=UTC)
        job.progress = {"percent": 25, "attempt_id": 1}
        session.add(JobEvent(job_id=job.id, type="changed", payload={}, ts=datetime(2026, 1, 3, tzinfo=UTC)))
        await session.commit()
        job_id = job.id

    bus = EventBus()
    sub = bus.subscribe()
    await publish_job_status(bus, db, job_id)
    bus.flush_bulk()
    batch = await asyncio.wait_for(sub.next(), 1)
    assert batch is not None
    rows = {item["type"]: item["payload"] for item in batch.payload["events"]}
    job_event = type("Event", (), {"payload": rows["job.status"]})
    file_event = type("Event", (), {"payload": rows["file.status"]})
    assert job_event is not None and job_event.payload["job"]
    assert job_event.payload["job"]["cooldown_started_at"] is None
    assert file_event is not None and file_event.payload["file"]["active_job_status"] == JobStatus.QUEUED.value
    assert file_event.payload["file"]["active_job_id"] == job_id
    assert file_event.payload["file"]["transcode_status"] == "queued"
    assert file_event.payload["file"]["active_job_progress"] == {"percent": 25, "attempt_id": 1}
    assert file_event.payload["file"]["active_job_updated_at"] == "2026-01-02T00:00:00Z"

    async with db() as session:
        stored_job = await session.get(Job, job_id)
        stored_file = await session.get(MediaFile, file_id)
        assert stored_job is not None and stored_file is not None
        assert job_event.payload["job"]["updated_at"] == iso_utc(stored_job.updated_at)
        assert job_event.payload["job"] == job_row(stored_job, stored_file)
        library = await session.get(Library, stored_file.library_id)
        assert file_event.payload["file"] == media_file_row(stored_file, stored_job, library=library)
        await finish(session, job_id, outcome, now=datetime(2026, 1, 1, 12, 1, tzinfo=UTC))
        await session.commit()

    await publish_job_status(bus, db, job_id)
    bus.flush_bulk()
    terminal = await asyncio.wait_for(sub.next(), 1)
    assert terminal is not None
    terminal_job = next(item["payload"] for item in terminal.payload["events"] if item["type"] == "job.status")
    assert terminal_job["job"]["cooldown_started_at"] is None
    terminal_file = next(item["payload"] for item in terminal.payload["events"] if item["type"] == "file.status")
    assert terminal_file["file"]["active_job_kind"] is None
    assert terminal_file["file"]["active_job_status"] is None
    assert terminal_file["file"]["transcode_status"] == outcome.value
    assert terminal_file["file"]["active_job_id"] is None
    assert terminal_file["file"]["active_job_progress"] is None
    assert terminal_file["file"]["active_job_updated_at"] is None
    await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cooldown_s", [0, 300])
async def test_cooldown_rows(db, cooldown_s: int) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    async with db() as session:
        file_id, library_id = await add_file(session, "cooldown.mkv")
        library = await session.get(Library, library_id)
        assert library is not None
        library.cooldown_s = cooldown_s
        job = await enqueue_file_job(
            session, file_id, library_id, FFmpegPlan(video=VideoTarget(encoder="libx264")), now=now, hold_until=now
        )
        await session.commit()
        job_id = job.id

    bus = EventBus()
    sub = bus.subscribe()
    try:
        for minutes, scale in ((-2, 1), (1, 1), (1, 2)):
            seen = now + timedelta(minutes=minutes)
            deadline = seen + timedelta(seconds=300 * scale)
            async with db() as session:
                media = await session.get(MediaFile, file_id)
                job = await session.get(Job, job_id)
                library = await session.get(Library, library_id)
                assert media is not None and job is not None and library is not None
                media.first_seen = seen
                library.cooldown_s = cooldown_s * scale
                job.hold_until = deadline
                await session.commit()

            await publish_job_status(bus, db, job_id)
            bus.flush_bulk()
            batch = await asyncio.wait_for(sub.next(), 1)
            assert batch is not None
            rows = {item["type"]: item["payload"] for item in batch.payload["events"]}
            job_payload = rows["job.status"]["job"]
            file_payload = rows["file.status"]["file"]
            assert job_payload["cooldown_started_at"] == iso_utc(seen)
            assert job_payload["hold_until"] == iso_utc(deadline)
            assert job_payload["created_at"] == "2026-01-01T12:00:00Z"
            assert file_payload["cooldown_started_at"] == (iso_utc(seen) if cooldown_s else None)
            assert file_payload["cooldown_until"] == (iso_utc(deadline) if cooldown_s else None)
    finally:
        await bus.aclose()


def test_cooldown_utc() -> None:
    seen = datetime(2026, 1, 1, 14, tzinfo=timezone(timedelta(hours=2)))
    media = MediaFile(first_seen=seen, mtime_ns=0)
    library = Library(cooldown_s=300)
    job = Job(status=JobStatus.HOLD.value, hold_until=seen + timedelta(minutes=5))
    for value in (seen, seen.replace(tzinfo=None) - timedelta(hours=2)):
        media.first_seen = value
        job.hold_until = value + timedelta(minutes=5)
        file_payload = media_file_row(media, library=library)
        job_payload = job_row(job, media)
        assert file_payload["cooldown_started_at"] == job_payload["cooldown_started_at"] == "2026-01-01T12:00:00Z"
        assert file_payload["cooldown_until"] == job_payload["hold_until"] == "2026-01-01T12:05:00Z"
    assert media_file_row(media, library=None)["cooldown_started_at"] is None
    assert job_row(job)["cooldown_started_at"] is None


@pytest.mark.asyncio
async def test_file_event_keys(db) -> None:
    async with db() as session:
        first, _ = await add_file(session, "a")
        second, _ = await add_file(session, "b")
        await session.commit()

    bus = EventBus()
    sub = bus.subscribe()
    await publish_file_status(bus, db, first)
    await publish_file_status(bus, db, second)
    bus.flush_bulk()
    bus.publish("file.status", {"library_id": first, "count": 2}, key=f"file.status:{first}")
    batch = await asyncio.wait_for(sub.next(), 1)
    assert batch is not None
    assert {item["payload"]["media_file_id"] for item in batch.payload["events"]} == {first, second}
    aggregate = await asyncio.wait_for(sub.next(), 1)
    assert aggregate is not None and aggregate.payload == {"library_id": first, "count": 2}
    await bus.aclose()
