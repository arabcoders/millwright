from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Update, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import InterfaceError, OperationalError

from common.actions import Notify
from common.fs.identity import source_identity
from common.enums import HealthStatus, JobStatus
from server.events import Event, EventBus, Subscription
from server.healthcheck import HealthcheckResult, HealthcheckService
from server.healthcheck import HealthcheckExitError
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, NotificationDelivery, utcnow
from server.healthlogs import DiagnosticLog
from server.queue.store import enqueue_healthcheck_job, request_file_job
from server.staging import StagingService
from tests.helpers import database_context, make_test_temp_dir


@pytest_asyncio.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Cancellation and recovery must retain data after a connection is invalidated.
    async with database_context("health") as factory:
        yield factory


async def add_job(
    db: async_sessionmaker[AsyncSession],
    path: Path,
    *,
    notify_on: Literal["job.succeeded", "job.failed", "job.cancelled"] | None = None,
) -> int:
    async with db() as session:
        library = Library(name=str(uuid4()), roots=[str(path.parent)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            container="mkv",
            medium="movie",
            last_scanned_at=utcnow(),
        )
        session.add(media)
        await session.flush()
        job = Job(media_file_id=media.id, library_id=library.id, kind="healthcheck")
        if notify_on is not None:
            job.pre_actions = [Notify(on=[notify_on], targets=["primary"]).model_dump(mode="json")]
        session.add(job)
        await session.commit()
        return job.id


@pytest.mark.asyncio
async def test_cooldown_holds_healthcheck(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = datetime(2026, 1, 1, tzinfo=UTC)
    seen = current
    monkeypatch.setattr("server.queue.store.utcnow", lambda: current)
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"movie")
    async with database() as session:
        library = Library(name="cooldown", roots=[str(tmp_path)], cooldown_s=300)
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=source_identity(path.stat()),
            container="mkv",
            first_seen=seen,
            medium="movie",
            last_scanned_at=seen,
        )
        session.add(media)
        await session.flush()
        job = await enqueue_healthcheck_job(session, media.id, library.id, now=current)
        assert job.status == JobStatus.HOLD
        assert job.hold_until == seen + timedelta(seconds=300)
        await session.commit()
        job_id, media_id = job.id, media.id

    service = HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    wakes = []
    staging = StagingService(
        database,
        tmp_path / "workspace",
        min_free_gb=1,
        clock=lambda: current,
        healthcheck_wake=lambda: wakes.append(True),
    )
    assert await service.claim_next() is None
    assert await staging.release_holds() == 0
    async with database() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.first_seen = seen + timedelta(seconds=100)
        await session.commit()
    current = seen + timedelta(seconds=300)
    assert await staging.release_holds() == 0
    assert await service.claim_next() is None
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.hold_until.replace(tzinfo=UTC) == seen + timedelta(seconds=400)
    current = seen + timedelta(seconds=400)
    assert await staging.release_holds() == 1
    assert wakes == [True]
    claimed = await service.claim_next()
    assert claimed is not None and claimed[0] == job_id
    await service._cancel_attempt(*claimed)
    async with database() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.first_seen = current
        manual = await request_file_job(session, media, kind="healthcheck", refresh=False)
        assert manual.status == JobStatus.QUEUED
        assert manual.hold_until is None
        await session.commit()


@pytest.mark.asyncio
async def test_activity_pauses_healthchecks(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [tmp_path / f"movie-{number}.mkv" for number in range(2)]
    for path in paths:
        path.write_bytes(b"movie")
    ids = [await add_job(database, path) for path in paths]
    paused = False
    entered, release, blocked, completed = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def decode(source, ffmpeg_bin, ffprobe_bin, log_path, **kwargs):
        calls.append(source)
        if len(calls) == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        log_path.path.write_bytes(b"healthcheck: result succeeded\n")
        return HealthcheckResult(healthy=True)

    service = HealthcheckService(
        database, tmp_path / "healthchecks", "unused", "unused", activity_paused=lambda: paused
    )
    claim = service.claim_next
    finished = service._finished

    def finish(job_id, task):
        finished(job_id, task)
        if job_id == ids[1]:
            completed.set()

    async def claim_next():
        result = await claim()
        if paused:
            assert result is None
            blocked.set()
        return result

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    monkeypatch.setattr(service, "claim_next", claim_next)
    monkeypatch.setattr(service, "_finished", finish)
    service.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with service.dispatch_lock:
            paused = True
        release.set()
        await asyncio.wait_for(blocked.wait(), 2)
        async with database() as session:
            first, second = await session.get(Job, ids[0]), await session.get(Job, ids[1])
            assert first is not None and first.status == JobStatus.SUCCEEDED
            assert second is not None and second.status == JobStatus.QUEUED
        assert len(calls) == 1
        paused = False
        service.wake()
        await asyncio.wait_for(completed.wait(), 2)
        async with database() as session:
            second = await session.get(Job, ids[1])
            assert second is not None and second.status == JobStatus.SUCCEEDED
        assert len(calls) == 2
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["size", "missing"])
@pytest.mark.parametrize("healthy", [True, False])
async def test_changed_source_result(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change, healthy):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        await session.execute(
            update(MediaFile).where(MediaFile.id == job.media_file_id).values(health_status=HealthStatus.HEALTHY)
        )
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        entered.set()
        await release.wait()
        log_path.path.write_text("healthcheck: completed\n", encoding="utf-8")
        return HealthcheckResult(healthy=healthy)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
    service = HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "size":
            source.write_bytes(b"changed size")
        else:
            source.unlink()
        release.set()
        await asyncio.wait_for(task, 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            media = await session.get(MediaFile, job.media_file_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job.status == JobStatus.FAILED
            assert job.error
            assert media.health_status == HealthStatus.PENDING
            assert attempt.health is None
            assert attempt.log_data == b"healthcheck: completed\n"
        assert not (tmp_path / "workspace" / "jobs" / str(job_id)).exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_suspended_resume(database: async_sessionmaker[AsyncSession]) -> None:
    root = make_test_temp_dir("health-suspended")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        library_id = job.library_id
        job.attempts = 2
        await session.execute(update(Library).where(Library.id == library_id).values(suspended=True))
        await session.commit()

    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    assert await service.claim_next() is None
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.QUEUED.value and job.attempts == 2
        assert not (await session.scalars(select(JobAttempt))).all()
        assert not (await session.scalars(select(JobEvent))).all()
        await session.execute(update(Library).where(Library.id == library_id).values(suspended=False))
        await session.commit()

    claimed = await service.claim_next()
    assert claimed is not None and claimed[0] == job_id
    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert job is not None and job.status == JobStatus.CLAIMED.value and job.attempts == 3
        assert job.lease_expires_at is None and job.started_at is None
        assert attempt is not None and attempt.attempt_number == 3 and attempt.node_name == "server"


@pytest.mark.asyncio
async def test_skips_suspended(database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("health-eligible")
    first, second = root / "a.mkv", root / "b.mkv"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    first_id, second_id = await add_job(database, first), await add_job(database, second)
    async with database() as session:
        job = await session.get(Job, first_id)
        assert job is not None
        job.priority = 100
        await session.execute(update(Library).where(Library.id == job.library_id).values(suspended=True))
        await session.commit()

    async def run(
        source: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        assert Path(source).samefile(second)
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return HealthcheckResult(healthy=True, samples=[])

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    claimed = await service.claim_next()
    assert claimed is not None and claimed[0] == second_id
    await asyncio.wait_for(service.run(*claimed), 2)
    assert await service.claim_next() is None
    async with database() as session:
        first_job = await session.get(Job, first_id)
        second_job = await session.get(Job, second_id)
        assert first_job is not None and first_job.status == JobStatus.QUEUED.value and first_job.attempts == 0
        assert second_job is not None and second_job.status == JobStatus.SUCCEEDED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before", "after"])
async def test_suspension_wins_claim(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    root = make_test_temp_dir("health-suspension-race")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        library_id = job.library_id

    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    async with database() as claim_session:
        get = claim_session.get
        observed, release, updating = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def controlled_get(entity, identifier, **kwargs):
            if entity is Library and boundary == "before":
                observed.set()
                await release.wait()
            result = await get(entity, identifier, **kwargs)
            if entity is Library and boundary == "after":
                observed.set()
                await release.wait()
            return result

        async def suspend() -> None:
            async with database() as session:
                connection = await session.connection()

                def before_update(_connection, _cursor, statement, _params, _context, _many) -> None:
                    if statement.startswith("UPDATE libraries"):
                        updating.set()

                event.listen(connection.sync_connection, "before_cursor_execute", before_update)
                await session.execute(update(Library).where(Library.id == library_id).values(suspended=True))
                await session.commit()

        monkeypatch.setattr(service, "db", lambda: claim_session)
        monkeypatch.setattr(claim_session, "get", controlled_get)
        claiming = asyncio.create_task(service.claim_next())
        suspending = None
        try:
            await asyncio.wait_for(observed.wait(), 2)
            suspending = asyncio.create_task(suspend())
            await asyncio.wait_for(updating.wait(), 2)
            if boundary == "before":
                await asyncio.wait_for(suspending, 2)
            else:
                assert not suspending.done()
            release.set()
            claimed = await asyncio.wait_for(claiming, 2)
            assert (claimed is None) is (boundary == "before")
            await asyncio.wait_for(suspending, 2)
        finally:
            release.set()
            tasks = [claiming] + ([suspending] if suspending is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)

    service.db = database
    assert await asyncio.wait_for(service.claim_next(), 2) is None
    async with database() as session:
        job = await session.get(Job, job_id)
        library = await session.get(Library, library_id)
        assert library is not None and library.suspended
        assert job is not None and job.status == ("queued" if boundary == "before" else "claimed")
        assert job.attempts == (0 if boundary == "before" else 1)
        assert len(list(await session.scalars(select(JobAttempt)))) == job.attempts
        assert len(list(await session.scalars(select(JobEvent)))) == job.attempts


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_concurrency", "configured"), [(1, False), (1, True), (6, True)])
async def test_limits_claims(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, max_concurrency: int, configured: bool
) -> None:
    tmp_path = make_test_temp_dir("health-service")
    job_ids = []
    for index in range(max_concurrency + 1):
        source = tmp_path / f"{index}.mkv"
        source.write_bytes(b"movie")
        job_ids.append(await add_job(database, source))
    claimed, entered = asyncio.Event(), asyncio.Event()
    publish_release, release = asyncio.Event(), asyncio.Event()
    publishing, running = set(), set()

    async def publish(job_id: int, operation: str) -> None:
        if operation == "claim":
            publishing.add(job_id)
            if len(publishing) == max_concurrency:
                claimed.set()
            await publish_release.wait()

    async def run(
        source: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        running.add(source)
        if len(running) == max_concurrency:
            entered.set()
        await release.wait()
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return HealthcheckResult(healthy=True, samples=[])

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    if configured:
        service = HealthcheckService(database, tmp_path, "ffmpeg", "ffprobe", max_concurrency=max_concurrency)
    else:
        service = HealthcheckService(database, tmp_path, "ffmpeg", "ffprobe")
    monkeypatch.setattr(service, "_publish_transition", publish)
    service.start()
    try:
        await asyncio.wait_for(claimed.wait(), 2)
        async with database() as session:
            statuses = {row[0]: row[1] for row in (await session.execute(select(Job.id, Job.status))).all()}
        assert all(statuses[job_id] == JobStatus.CLAIMED.value for job_id in job_ids[:-1])
        assert statuses[job_ids[-1]] == JobStatus.QUEUED.value
        publish_release.set()
        await asyncio.wait_for(entered.wait(), 2)
        async with database() as session:
            statuses = {row[0]: row[1] for row in (await session.execute(select(Job.id, Job.status))).all()}
        assert all(statuses[job_id] == JobStatus.RUNNING.value for job_id in job_ids[:-1])
        assert statuses[job_ids[-1]] == JobStatus.QUEUED.value
    finally:
        publish_release.set()
        release.set()
        await asyncio.wait_for(service.close(), 2)


async def wait_status(
    db: async_sessionmaker[AsyncSession], service: HealthcheckService, job_id: int, status: JobStatus
) -> None:
    async with asyncio.timeout(2):
        await service._wake.wait()
        async with db() as session:
            value = await session.scalar(select(Job.status).where(Job.id == job_id))
        assert value == status.value


@pytest.mark.asyncio
async def test_snapshot_lookup_drains(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    lookup, release, cancelling = (asyncio.Event() for _ in range(3))
    decoded = False
    lookup_cancelled = False
    get = AsyncSession.get

    async def decode(source, ffmpeg_bin, ffprobe_bin, log, **kwargs):
        nonlocal decoded
        log.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        decoded = True
        return HealthcheckResult(healthy=True, samples=[])

    async def gated_get(session, entity, identity, **kwargs):
        nonlocal lookup_cancelled
        if decoded and entity is JobAttempt and not lookup.is_set():
            lookup.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                lookup_cancelled = True
                raise
        return await get(session, entity, identity, **kwargs)

    service = HealthcheckService(database, tmp_path, "ffmpeg", "ffprobe", max_concurrency=1)
    cancel = service.cancel_job

    def cancel_job(identity):
        result = cancel(identity)
        cancelling.set()
        return result

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    monkeypatch.setattr(AsyncSession, "get", gated_get)
    monkeypatch.setattr(service, "cancel_job", cancel_job)
    service.start()
    closing = None
    try:
        await asyncio.wait_for(lookup.wait(), 2)
        closing = asyncio.create_task(service.close())
        await asyncio.wait_for(cancelling.wait(), 2)
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not lookup_cancelled and not closing.done()
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)
        if closing is not None:
            await asyncio.wait_for(closing, 2)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.QUEUED.value


async def wait_idle(service: HealthcheckService) -> None:
    async with asyncio.timeout(2):
        while service._running:
            await service._wake.wait()
            service._wake.clear()


async def drain_events(bus: EventBus, subscription: Subscription) -> list[Event]:
    bus.flush_bulk()
    events: list[Event] = []
    while subscription.backlog:
        event = await asyncio.wait_for(subscription.next(), 1)
        assert event is not None
        if event.type == "events.batch":
            events.extend(
                Event(type=item["type"], payload=item["payload"], key="batch") for item in event.payload["events"]
            )
        else:
            events.append(event)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(("healthy", "status"), [(True, HealthStatus.HEALTHY), (False, HealthStatus.CORRUPT)])
async def test_persists_result(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, healthy: bool, status: HealthStatus
) -> None:
    root = make_test_temp_dir("health-persist")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_bytes(b"healthcheck: result succeeded\n")
        return HealthcheckResult(
            healthy=healthy, error_count=0 if healthy else 2, samples=["bad"] if not healthy else []
        )

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    service.start()
    try:
        await wait_status(database, service, job_id, JobStatus.SUCCEEDED)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            events = list(
                (await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id))).all()
            )
        assert job is not None and attempt is not None
        async with database() as session:
            media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        assert attempt.node_name == "server" and attempt.workload == "healthcheck"
        assert attempt.source_path is None and attempt.output_path is None
        assert job.started_at and job.finished_at and attempt.started_at and attempt.finished_at
        assert attempt.health is not None
        assert attempt.health["healthy"] is healthy
        assert attempt.health["error_count"] == (0 if healthy else 2)
        assert attempt.health["samples"] == (["bad"] if not healthy else [])
        assert attempt.log_path is not None
        data = attempt.log_data
        assert data is not None
        assert not (root / "jobs" / str(job_id)).exists()
        assert [event.type for event in events] == [
            "claimed",
            "healthcheck_log_created",
            "started",
            "healthcheck_log_recovered",
            "succeeded",
        ]
        assert events[-2].payload["removed"] is True
        assert media.health_status == status.value
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("failed", "deliveries"), [(True, 1), (False, 0)])
async def test_notifies_terminal_failure(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, failed: bool, deliveries: int
) -> None:
    root = make_test_temp_dir("health-notify")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source, notify_on="job.failed")

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_bytes(b"healthcheck: result failed\n" if failed else b"healthcheck: result succeeded\n")
        return HealthcheckResult(healthy=not failed, failed=failed, error="decode failed" if failed else None)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    service.start()
    try:
        await wait_status(database, service, job_id, JobStatus.FAILED if failed else JobStatus.SUCCEEDED)
    finally:
        await service.close()

    async with database() as session:
        rows = list(
            (await session.scalars(select(NotificationDelivery).where(NotificationDelivery.job_id == job_id))).all()
        )
    assert len(rows) == deliveries


@pytest.mark.asyncio
async def test_missing_log_result(database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("health-missing-log")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.unlink()
        return HealthcheckResult(healthy=True)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    service.start()
    try:
        await wait_status(database, service, job_id, JobStatus.SUCCEEDED)
    finally:
        await service.close()

    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
    assert job is not None and attempt is not None
    assert job.status == JobStatus.SUCCEEDED.value
    assert attempt.health == {"healthy": True, "error_count": 0, "samples": []}
    assert attempt.log_data is None


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [True, False])
async def test_on_healthy_callback(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, healthy: bool
) -> None:
    root = make_test_temp_dir("health-callback")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_bytes(b"healthcheck: result succeeded\n")
        return HealthcheckResult(healthy=healthy, failed=not healthy)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    calls: list[int] = []
    service = HealthcheckService(
        database,
        root,
        "ffmpeg",
        "ffprobe",
        on_healthy=lambda: calls.append(job_id),
    )
    service.start()
    try:
        await wait_status(database, service, job_id, JobStatus.SUCCEEDED if healthy else JobStatus.FAILED)
        await wait_idle(service)
    finally:
        await service.close()
    assert calls == ([job_id] if healthy else [])


@pytest.mark.asyncio
async def test_publishes_failure(database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("health-events")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    bus = EventBus(min_interval_s=0)
    subscription = bus.subscribe()

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_text("healthcheck: result failed\n", encoding="utf-8")
        return HealthcheckResult(healthy=False, error="ffmpeg exited 1: decode failed", failed=True)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe", events=bus)
    statuses: list[dict] = []
    try:
        service.start()
        await wait_status(database, service, job_id, JobStatus.FAILED)
        await wait_idle(service)
        for event in await drain_events(bus, subscription):
            if event.type == "job.status" and event.payload["job_id"] == job_id:
                statuses.append(event.payload)
        assert [payload["status"] for payload in statuses] == [JobStatus.FAILED.value]
        assert statuses[-1]["error"] is not None
        assert statuses[-1]["finished_at"]
    finally:
        await service.close()
        bus.unsubscribe(subscription)
        await bus.aclose()


@pytest.mark.asyncio
async def test_publishes_cancellation(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_test_temp_dir("health-cancel-events")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    entered = asyncio.Event()
    release = asyncio.Event()
    bus = EventBus(min_interval_s=0)
    subscription = bus.subscribe()

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_bytes(b"healthcheck: start\n")
        entered.set()
        await release.wait()
        return HealthcheckResult(healthy=True, samples=[])

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe", events=bus)
    try:
        service.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert service.cancel_job(job_id)
        await wait_status(database, service, job_id, JobStatus.CANCELLED)
        await wait_idle(service)
        terminal = None
        for event in await drain_events(bus, subscription):
            if event.type == "job.status" and event.payload["job_id"] == job_id:
                if event.payload["status"] == JobStatus.CANCELLED.value:
                    terminal = event.payload
        assert terminal is not None
        assert terminal["error"] is not None
        assert terminal["finished_at"]
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and media.transcode_status == "pending" and not media.transcode_decided
    finally:
        release.set()
        await service.close()
        bus.unsubscribe(subscription)
        await bus.aclose()


@pytest.mark.asyncio
async def test_cancels_exact(database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("health-cancel")
    first, second = root / "a.mkv", root / "b.mkv"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    first_id, second_id = await add_job(database, first), await add_job(database, second)
    entered: dict[str, asyncio.Event] = {"a.mkv": asyncio.Event(), "b.mkv": asyncio.Event()}
    release: dict[str, asyncio.Event] = {key: asyncio.Event() for key in entered}

    async def run(
        source: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        key = Path(source).resolve(strict=True).name
        entered[key].set()
        await release[key].wait()
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return HealthcheckResult(healthy=True, samples=[])

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe", max_concurrency=2)
    service.start()
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        assert service.cancel_job(first_id)
        await wait_status(database, service, first_id, JobStatus.CANCELLED)
        async with database() as session:
            attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == first_id))).one()
            assert attempt.error is not None
            assert attempt.log_path is not None
        release["b.mkv"].set()
        await wait_status(database, service, second_id, JobStatus.SUCCEEDED)
    finally:
        release["a.mkv"].set()
        release["b.mkv"].set()
        await service.close()


@pytest.mark.asyncio
async def test_shutdown_requeues(database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("health-shutdown")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_bytes(b"healthcheck: start\n")
        entered.set()
        await release.wait()
        return HealthcheckResult(healthy=True, samples=[])

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    service.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        marker = root / "unrelated.marker"
        marker.write_text("keep", encoding="utf-8")
    finally:
        release.set()
        await service.close()
    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
    assert job and job.status == JobStatus.QUEUED.value
    assert attempt.status == JobStatus.FAILED.value and attempt.result == "interrupted"
    assert marker.exists()
    assert not (root / "jobs" / str(job_id) / "attempts" / str(attempt.attempt_number)).exists()
    assert not service._tasks
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_failure_details(
    database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing: bool,
) -> None:
    root = make_test_temp_dir("health-failure")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    if missing:
        source.unlink()
    else:

        async def run(
            source_path: str,
            ffmpeg_bin: str,
            ffprobe_bin: str,
            log_path: DiagnosticLog,
            *,
            on_progress: object = None,
            hwaccel: object = None,
        ) -> HealthcheckResult:
            raise RuntimeError

        monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    with caplog.at_level(logging.ERROR):
        service.start()
        try:
            await wait_status(database, service, job_id, JobStatus.FAILED)
            await wait_idle(service)
        finally:
            await service.close()
    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
        event = (
            await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "failed"))
        ).one()
    assert job and attempt and job.error and attempt.error
    assert event.payload["error"] == job.error
    assert attempt.status == JobStatus.FAILED.value and job.status == JobStatus.FAILED.value


@pytest.mark.asyncio
async def test_cleanup_warning(database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("health-cleanup-warning")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)

    async def run(
        source_path: str,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        log_path: DiagnosticLog,
        *,
        on_progress: object = None,
        hwaccel: object = None,
    ) -> HealthcheckResult:
        log_path.path.write_bytes(b"healthcheck: result succeeded\n")
        return HealthcheckResult(healthy=True, samples=[])

    def fail_cleanup(*args: object) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", run)
    monkeypatch.setattr("server.healthlogs.remove_healthcheck_log", fail_cleanup)
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe")
    try:
        service.start()
        await wait_status(database, service, job_id, JobStatus.SUCCEEDED)
        await wait_idle(service)
    finally:
        await service.close()
    async with database() as session:
        job = await session.get(Job, job_id)
        event = (
            await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "cleanup_failed"))
        ).one()
    assert job and job.warning == event.payload["warning"]


@pytest.mark.asyncio
async def test_rejects_stale_progress(database: async_sessionmaker[AsyncSession]) -> None:
    root = make_test_temp_dir("health-stale-progress")
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value
        job.attempts = 1
        attempt = JobAttempt(job_id=job_id, attempt_number=1, status=JobStatus.RUNNING.value, workload="healthcheck")
        session.add(attempt)
        await session.commit()
        await session.refresh(attempt)
        attempt_id = attempt.id
    bus = EventBus(min_interval_s=0)
    subscription = bus.subscribe()
    service = HealthcheckService(database, root, "ffmpeg", "ffprobe", events=bus)
    try:
        await service._record_progress(job_id, attempt_id, {"stage": "decode", "percent": 20})
        events = await drain_events(bus, subscription)
        event = events[0] if events else None
        assert event is not None and event.type == "job.progress" and event.payload["progress"]["percent"] == 20
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.status = JobStatus.QUEUED.value
            await session.commit()
        await service._record_progress(job_id, attempt_id, {"stage": "decode", "percent": 90})
        await service._record_progress(job_id, attempt_id + 999, {"stage": "decode", "percent": 80})
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.progress == {"stage": "decode", "percent": 20}
    finally:
        bus.unsubscribe(subscription)
        await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OperationalError, InterfaceError])
@pytest.mark.parametrize("committed", [False, True])
async def test_claim_outage_recovers(database, tmp_path, monkeypatch, error, committed):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    service = HealthcheckService(database, tmp_path / "logs", "unused", "unused")
    waiting, release, finished = (asyncio.Event() for _ in range(3))
    claim = service.claim_next
    failed = False

    async def claim_next():
        nonlocal failed
        if not failed:
            failed = True
            if committed:
                assert await claim() is not None
            raise error("claim", {}, OSError("database unavailable"))
        return await claim()

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    async def decode(source, ffmpeg, ffprobe, log, **kwargs):
        log.path.write_bytes(b"healthcheck: result succeeded\n")
        return HealthcheckResult(healthy=True)

    service.on_finished = finished.set
    monkeypatch.setattr(service, "claim_next", claim_next)
    monkeypatch.setattr("server.healthcheck.asyncio.sleep", timer)
    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    service.start()
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert service._loop_task is not None and not service._loop_task.done()
        assert not service._running
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job.status == ("claimed" if committed else "queued")
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job.status == "succeeded" and job.attempts == (2 if committed else 1)
            attempts = list(await session.scalars(select(JobAttempt).order_by(JobAttempt.id)))
            if committed:
                assert attempts[0].result == "interrupted"
                assert attempts[0].log_data == b"healthcheck: interrupted during database outage\n"
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_worker_outage_recovers(database, tmp_path, monkeypatch, exhausted):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    if exhausted:
        async with database() as session:
            job = await session.get(Job, job_id)
            job.attempts = 2
            await session.commit()
    service = HealthcheckService(database, tmp_path / "logs", "unused", "unused")
    cleaning, cleanup_release, waiting, retry_release, finished = (asyncio.Event() for _ in range(5))
    calls = 0

    async def decode(source, ffmpeg, ffprobe, log, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            log.path.write_bytes(b"healthcheck: outage diagnostics\n")
            try:
                raise OperationalError("progress", {}, OSError("database unavailable"))
            finally:
                cleaning.set()
                await drain_cleanup()
        log.path.write_bytes(b"healthcheck: result succeeded\n")
        return HealthcheckResult(healthy=True)

    async def drain_cleanup():
        await asyncio.wait_for(cleanup_release.wait(), 2)

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await retry_release.wait()

    recovered = service._recover_jobs

    async def recover():
        await recovered()
        if waiting.is_set() and exhausted:
            finished.set()

    if not exhausted:
        service.on_finished = lambda: finished.set() if calls == 2 else None
    monkeypatch.setattr(service, "_recover_jobs", recover)
    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    monkeypatch.setattr("server.healthcheck.asyncio.sleep", timer)
    service.start()
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        async with service.dispatch_lock:
            await service._recover_jobs()
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job.status == "running"
        assert job_id in service._running and not waiting.is_set()
        cleanup_release.set()
        await asyncio.wait_for(waiting.wait(), 2)
        assert not service._running
        retry_release.set()
        await asyncio.wait_for(finished.wait(), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job.status == ("failed" if exhausted else "succeeded")
            attempt = await session.scalar(select(JobAttempt).order_by(JobAttempt.id))
            assert attempt.status == "failed" and attempt.result == "interrupted"
            assert attempt.log_data == b"healthcheck: outage diagnostics\n"
            assert attempt.error
            assert not (service.log_dir / attempt.log_path).exists()
        assert calls == (1 if exhausted else 2)
    finally:
        cleanup_release.set()
        retry_release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_unreaped_outage_retained(database, tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    service = HealthcheckService(database, tmp_path / "logs", "unused", "unused")
    waiting, release, recovered = (asyncio.Event() for _ in range(3))
    complete = service._finish_attempt
    failed = False

    async def complete_attempt(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise InterfaceError("complete", {}, OSError("database unavailable"))
        await complete(*args, **kwargs)
        recovered.set()

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    async def decode(source, ffmpeg, ffprobe, log, **kwargs):
        log.path.write_bytes(b"healthcheck: exit unconfirmed\n")
        raise HealthcheckExitError("Process exit is unconfirmed.")

    monkeypatch.setattr(service, "_finish_attempt", complete_attempt)
    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    monkeypatch.setattr("server.healthcheck.asyncio.sleep", timer)
    service.start()
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        release.set()
        await asyncio.wait_for(recovered.wait(), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
            assert job.status == "failed" and job.attempts == 1
            assert attempt.result == "interrupted" and attempt.log_data is None
            assert (service.log_dir / attempt.log_path).read_bytes().startswith(b"healthcheck: exit unconfirmed\n")
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancel_outage_drains(database, tmp_path, monkeypatch, shutdown):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    service = HealthcheckService(database, tmp_path / "logs", "unused", "unused")
    entered, waiting, release, recovered = (asyncio.Event() for _ in range(4))
    complete = service._finish_attempt
    failed = False

    async def complete_attempt(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OperationalError("cancel", {}, OSError("database unavailable"))
        await complete(*args, **kwargs)
        recovered.set()

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    async def decode(source, ffmpeg, ffprobe, log, **kwargs):
        log.path.write_bytes(b"healthcheck: started\n")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_finish_attempt", complete_attempt)
    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    monkeypatch.setattr("server.healthcheck.asyncio.sleep", timer)
    service.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert service.cancel_job(job_id)
        await asyncio.wait_for(waiting.wait(), 2)
        assert not service._running
        if shutdown:
            await asyncio.wait_for(service.close(), 2)
            assert not release.is_set()
        else:
            release.set()
            await asyncio.wait_for(recovered.wait(), 2)
            async with database() as session:
                job = await session.get(Job, job_id)
                assert job.status == "cancelled" and job.attempts == 1
                attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
                assert attempt.log_data.startswith(b"healthcheck: started\n")
                assert not (service.log_dir / attempt.log_path).exists()
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)
