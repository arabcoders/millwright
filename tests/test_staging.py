from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("local_storage")
import pytest_asyncio
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.media.probe import ProbeData, ProbeStream
from common.fs.identity import source_identity
from common.plan import FFmpegPlan, SubtitleBurn, VideoTarget
from common.protocol import Cancel, NodeCapabilities
from common.enums import JobStatus
from server.events import EventBus, Subscription
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, Library, MediaFile
from server.nodehub.hub import NodeEntry
from server.nodehub.store import register_node
from server.queue.store import capture_source, retry_job
from server import workspace
from server.staging import StagingService
from server.scanner import scan
from server.fs.library import LibraryFS
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from tests.test_workspace import stage_source
from tests.helpers import request_retry, database_context, make_test_disk_path, make_test_temp_dir


@pytest.fixture(autouse=True)
def workspace_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _: SimpleNamespace(free=20_000_000_000))


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Parallel staging jobs need independent database transactions.
    async with database_context("staging") as factory:
        yield factory


async def add_job(
    factory: async_sessionmaker[AsyncSession],
    source: Path | None,
    *,
    cooldown_s: int = 0,
    first_seen: datetime | None = None,
    status: str = JobStatus.STAGING.value,
    hold_until: datetime | None = None,
    plan: dict | None = None,
) -> int:
    path = source if source is not None else Path("/missing/private/movie.mkv")
    identity = source_identity(source.stat()) if source is not None else None
    async with factory() as session:
        library = Library(name=f"library-{uuid.uuid4().hex}", roots=[str(path.parent)], cooldown_s=cooldown_s)
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=identity["size"] if identity is not None else 1,
            mtime_ns=source.stat().st_mtime_ns if source is not None else 1,
            source_identity=identity,
            container="mkv",
            medium="movie",
            first_seen=first_seen or T0,
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(
            media_file_id=media.id,
            library_id=library.id,
            kind="transcode",
            priority=0,
            status=status,
            hold_until=hold_until,
            plan=plan or FFmpegPlan().model_dump(mode="json"),
            created_at=T0,
        )
        session.add(job)
        await session.flush()
        captured = await capture_source(media) if source is not None else {"path": media.path, "size": media.size}
        session.add(JobEvent(job_id=job.id, type="enqueued", payload={"source": captured}, ts=T0))
        await session.commit()
        return job.id


async def get_job(factory: async_sessionmaker[AsyncSession], job_id: int) -> Job:
    async with factory() as session:
        return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_catalog_cancel_drains(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_task: bool) -> None:
    from server import staging as staging_module

    monkeypatch.setattr(scan, "COMMIT_BATCH", 1)
    root, workspace = tmp_path / "library", tmp_path / "workspace"
    root.mkdir()
    async with db() as session:
        library = Library(name="library", roots=[str(root)], scan_id=1)
        session.add(library)
        await session.flush()
        rows, jobs = [], []
        for index in range(3):
            path = root / f"{index}.mkv"
            path.write_bytes(b"retained library source")
            row = MediaFile(
                library_id=library.id,
                path=str(path),
                size=path.stat().st_size,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=T0,
            )
            session.add(row)
            await session.flush()
            job = Job(library_id=library.id, media_file_id=row.id, kind="transcode", status="failed")
            session.add(job)
            rows.append(row)
            jobs.append(job)
        await session.commit()
    for job in jobs:
        marker = workspace / "jobs" / str(job.id) / "source.mkv"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"workspace artifact")

    bus = EventBus(min_interval_s=0)
    staging = StagingService(db, workspace, min_free_gb=1, events=bus)
    entered, release, drained = asyncio.Event(), threading.Event(), threading.Event()
    stopped = threading.Event()
    loop = asyncio.get_running_loop()
    remove = staging_module._remove_job
    events = []
    publish = bus.publish

    def capture(type_, payload, *args, **kwargs):
        events.append((type_, payload))
        publish(type_, payload, *args, **kwargs)

    def blocked_remove(root: Path, job_id: int) -> None:
        if job_id == jobs[1].id:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            assert staging._stage_lock.locked()
        remove(root, job_id)
        if job_id == jobs[1].id:
            drained.set()

    monkeypatch.setattr(staging_module, "_remove_job", blocked_remove)
    monkeypatch.setattr(bus, "publish", capture)

    async def prune():
        async with db() as session:
            return await scan._prune_missing(
                session, library, 1, staging=staging, unavailable=set(), excluded=(), stopped=stopped
            )

    task = asyncio.create_task(prune())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if cancel_task:
            task.cancel()
        else:
            stopped.set()
        release.set()
        if cancel_task:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            assert await asyncio.wait_for(task, 3) == 1
        assert drained.is_set() and not staging._stage_lock.locked()
        async with db() as session:
            assert await session.get(MediaFile, rows[0].id) is None
            assert await session.get(Job, jobs[0].id) is None
            for row, job in zip(rows[1:], jobs[1:], strict=True):
                assert await session.get(MediaFile, row.id) is not None
                assert await session.get(Job, job.id) is not None
        assert [(type_, payload.get("media_file_id", payload.get("job_id"))) for type_, payload in events] == [
            ("job.deleted", jobs[0].id),
            ("file.status", rows[0].id),
        ]
        assert all(Path(row.path).read_bytes() == b"retained library source" for row in rows)
        assert not (workspace / "jobs" / str(jobs[1].id)).exists()
        assert (workspace / "jobs" / str(jobs[2].id) / "source.mkv").read_bytes() == b"workspace artifact"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await bus.aclose()


@asynccontextmanager
async def subscribed() -> AsyncIterator[tuple[EventBus, Subscription]]:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        yield bus, sub
    finally:
        bus.unsubscribe(sub)
        await asyncio.wait_for(bus.aclose(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["size", "mtime"])
async def test_staging_source_changes(db: async_sessionmaker[AsyncSession], changed: str) -> None:
    source = make_test_disk_path("staging", "stale.mkv")
    source.write_bytes(b"source")
    stamp = source.stat().st_mtime_ns
    job_id = await add_job(db, source)
    if changed == "size":
        source.write_bytes(b"changed source")
        os.utime(source, ns=(stamp, stamp))
    else:
        os.utime(source, ns=(stamp + 1, stamp + 1))
    workspace = make_test_temp_dir("workspace-stale")
    assert await StagingService(db, workspace, min_free_gb=1).stage_job(job_id) == (0 if changed == "size" else 1)
    job = await get_job(db, job_id)
    staged = workspace / "jobs" / str(job_id) / "source.mkv"
    if changed == "size":
        assert job.status == JobStatus.FAILED.value
        assert job.error is not None
        assert not staged.exists()
    else:
        assert job.status == JobStatus.QUEUED.value
        assert staged.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_stats", [False, True])
@pytest.mark.parametrize("staged", [False, True])
async def test_refreshed_source_size(db, tmp_path: Path, same_stats: bool, staged: bool) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    stamp = source.stat().st_mtime_ns
    job_id = await add_job(db, source)
    workspace = tmp_path / "workspace"
    service = StagingService(db, workspace, min_free_gb=1)
    if staged:
        assert await service.stage_job(job_id) == 1
    replacement = tmp_path / "replacement.mkv"
    contents = b"change" if same_stats else b"changed source"
    replacement.write_bytes(contents)
    if same_stats:
        os.utime(replacement, ns=(stamp, stamp))
    replacement.replace(source)
    async with db() as session:
        job = await session.get(Job, job_id)
        library = await session.get(Library, job.library_id)
        library_id, media_id = library.id, job.media_file_id
    from server.fs.library import apply_probe_metadata
    from tests.fakes.ffprobe import probe_output

    async with db() as session:
        row = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        await apply_probe_metadata(
            session,
            path=source,
            stat=source.stat(),
            raw=json.loads(probe_output(source)),
            row=row,
            library=library,
            purpose="current",
        )
        await session.commit()
    async with db() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.STAGING.value
        await session.commit()

    assert await service.stage_job(job_id) == (1 if same_stats else 0)
    job = await get_job(db, job_id)
    copy = workspace / "jobs" / str(job_id) / "source.mkv"
    if same_stats:
        assert job.status == JobStatus.QUEUED.value
        assert copy.read_bytes() == (b"source" if staged else contents)
    else:
        assert job.status == JobStatus.FAILED.value
        assert job.error is not None
        assert not copy.exists()
    assert source.read_bytes() == contents


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_retry_staging(db: async_sessionmaker[AsyncSession], published: bool) -> None:
    source = make_test_disk_path("staging", "retry.mkv")
    source.write_bytes(b"source")
    job_id = await add_job(db, source)
    workspace = make_test_temp_dir("workspace-retry")
    service = StagingService(db, workspace, min_free_gb=1)
    assert await service.stage_job(job_id) == 1
    status = JobStatus.SUCCEEDED if published else JobStatus.FAILED
    async with db() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = status.value
        job.attempts = 1
        session.add(
            JobAttempt(
                job_id=job_id,
                attempt_number=1,
                status=status.value,
                input_probe=ProbeData(file_path=str(source), duration_s=10.0, size_bytes=6, streams=[]).model_dump(
                    mode="json"
                ),
            )
        )
        if published:
            source.write_bytes(b"published output")
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None
            media.size = source.stat().st_size
            media.mtime_ns = source.stat().st_mtime_ns
        await session.commit()
        if published:
            # Exercise staging's defense independently of the API/store precheck.
            job.status = JobStatus.STAGING.value
        else:
            request_id = await request_retry(session, job_id)
            assert await retry_job(session, job_id, request_id=request_id) is not None
        await session.commit()

    assert await service.stage_job(job_id) == (0 if published else 1)
    job = await get_job(db, job_id)
    assert job.status == (JobStatus.FAILED.value if published else JobStatus.QUEUED.value)
    if published:
        assert job.error is not None
        assert source.read_bytes() == b"published output"
    if published:
        assert not (workspace / "jobs" / str(job_id)).exists()
    else:
        assert (workspace / "jobs" / str(job_id) / "source.mkv").read_bytes() == b"source"


@pytest.mark.asyncio
async def test_capacity_serializes_copies(db, tmp_path: Path, monkeypatch) -> None:
    sources = [tmp_path / f"{index}.mkv" for index in range(2)]
    for source in sources:
        source.write_bytes(b"12345678")
    identifiers = [await add_job(db, source) for source in sources]
    workspace = tmp_path / "workspace"
    service = StagingService(db, workspace, min_free_gb=1)
    free = math.ceil(service.min_free_gb * 1e9) + 18
    measurements = []
    started, entered = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def usage(root):
        measurements.append(root)
        return SimpleNamespace(free=free)

    def staged(root, job_id, source, **kwargs):
        nonlocal free
        from server.workspace import _stage_source

        result = _stage_source(root, job_id, source, **kwargs)
        free -= source.stat().st_size
        if job_id == identifiers[0]:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        return result

    async def another_copy():
        entered.set()
        return await service.stage_job(identifiers[1])

    monkeypatch.setattr("server.staging.shutil.disk_usage", usage)
    monkeypatch.setattr("server.staging._stage_source", staged)
    first = asyncio.create_task(service.stage_job(identifiers[0]))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        second = asyncio.create_task(another_copy())
        await asyncio.wait_for(entered.wait(), 3)
        assert len(measurements) == 1
        release.set()
        assert await asyncio.wait_for(first, 3) == 1
        assert await asyncio.wait_for(second, 3) == 0
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True), 3)

    blocked = await get_job(db, identifiers[1])
    assert blocked.status == JobStatus.STAGING and blocked.attempts == 0 and blocked.error is None
    assert blocked.warning is not None
    assert not (workspace / "jobs" / str(blocked.id)).exists()
    assert (await get_job(db, identifiers[0])).status == JobStatus.QUEUED
    free = math.ceil(service.min_free_gb * 1e9) + 28
    assert await service.stage_job(blocked.id) == 1
    assert (await get_job(db, blocked.id)).status == JobStatus.QUEUED
    assert (await get_job(db, blocked.id)).warning is None
    assert (workspace / "jobs" / str(blocked.id) / "source.mkv").read_bytes() == b"12345678"


@pytest.mark.asyncio
async def test_capacity_measurement_retries(db, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    identifier = await add_job(db, source)
    service = StagingService(db, tmp_path / "workspace", min_free_gb=1)

    def unavailable(_root):
        raise OSError("measurement unavailable")

    monkeypatch.setattr("server.staging.shutil.disk_usage", unavailable)
    assert await service.stage_job(identifier) == 0
    blocked = await get_job(db, identifier)
    assert blocked.status == JobStatus.STAGING and blocked.error is None
    assert blocked.warning is not None
    assert not (service.workspace_dir / "jobs").exists()
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _root: SimpleNamespace(free=20_000_000_000))
    assert await service.stage_job(identifier) == 1
    assert (await get_job(db, identifier)).warning is None


@pytest.mark.asyncio
async def test_capacity_smaller_job(db, tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    large, small = tmp_path / "large.mkv", tmp_path / "small.mkv"
    large.write_bytes(b"12345678")
    small.write_bytes(b"x")
    blocked = await add_job(db, large)
    identifier = await add_job(db, small)
    service = StagingService(db, tmp_path / "workspace", min_free_gb=1)
    monkeypatch.setattr(
        "server.staging.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=math.ceil(service.min_free_gb * 1e9) + 15),
    )
    assert await service.stage_job(blocked) == 0
    warning = (await get_job(db, blocked)).warning
    assert warning is not None
    assert await service.stage_job(blocked) == 0
    logged = len(caplog.records)
    assert await service.stage_job(blocked) == 0
    assert len(caplog.records) == logged
    assert await service.stage_job(identifier) == 1
    assert (await get_job(db, identifier)).status == JobStatus.QUEUED
    assert (service.workspace_dir / "jobs" / str(identifier) / "source.mkv").read_bytes() == b"x"
    job = await get_job(db, blocked)
    assert job.status == JobStatus.STAGING and job.error is None and job.warning == warning
    assert not (service.workspace_dir / "jobs" / str(blocked)).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
async def test_capacity_reserves_outputs(db, tmp_path: Path, monkeypatch, status: JobStatus) -> None:
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    first.write_bytes(b"12345678")
    second.write_bytes(b"12345678")
    existing = await add_job(db, first, status=status.value)
    identifier = await add_job(db, second)
    async with db() as session:
        session.add(
            JobEvent(
                job_id=existing,
                type="stage",
                payload={
                    "probe": ProbeData(file_path="input.mkv", duration_s=10, size_bytes=16, streams=[]).model_dump(
                        mode="json"
                    ),
                    "output_probe": {"size_bytes": 24},
                },
            )
        )
        await session.commit()
    service = StagingService(db, tmp_path / "workspace", min_free_gb=1)
    monkeypatch.setattr(
        "server.staging.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=math.ceil(service.min_free_gb * 1e9) + 47),
    )
    assert await service.stage_job(identifier) == 0
    blocked = await get_job(db, identifier)
    assert blocked.status == JobStatus.STAGING and blocked.error is None
    assert blocked.warning is not None
    assert not (service.workspace_dir / "jobs").exists()
    monkeypatch.setattr(
        "server.staging.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=math.ceil(service.min_free_gb * 1e9) + 48),
    )
    assert await service.stage_job(identifier) == 1


@pytest.mark.asyncio
async def test_capacity_reserves_repairs(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"12345678")
    identifier = await add_job(db, source)
    probe = ProbeData(
        file_path=str(source),
        container="mkv",
        duration_s=10,
        size_bytes=8,
        streams=[ProbeStream(index=0, codec="", codec_type="subtitle")],
    )
    async with db() as session:
        event = (await session.scalars(select(JobEvent).where(JobEvent.job_id == identifier))).one()
        event.payload = {**event.payload, "probe": probe.model_dump(mode="json")}
        await session.commit()
    service = StagingService(db, tmp_path / "workspace", min_free_gb=1)
    monkeypatch.setattr(
        "server.staging.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=math.ceil(service.min_free_gb * 1e9) + 49),
    )
    assert await service.stage_job(identifier) == 0
    assert not (service.workspace_dir / "jobs").exists()
    monkeypatch.setattr(
        "server.staging.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=math.ceil(service.min_free_gb * 1e9) + 50),
    )
    assert await service.stage_job(identifier) == 1


@pytest.mark.asyncio
async def test_stages_burn_subtitle(db: async_sessionmaker[AsyncSession]) -> None:
    source = make_test_disk_path("staging", "movie.mkv")
    source.write_bytes(b"source")
    (source.parent / "movie.eng.srt").write_bytes(b"subtitle")
    plan = FFmpegPlan(
        video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="movie.eng.srt")
    ).model_dump(mode="json")
    job_id = await add_job(db, source, plan=plan)
    workspace = make_test_temp_dir("workspace-burn-subtitle")

    assert await StagingService(db, workspace, min_free_gb=1).stage_job(job_id) == 1
    assert (workspace / "jobs" / str(job_id) / "subtitle.srt").read_bytes() == b"subtitle"
    assert (await get_job(db, job_id)).status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_subtitle_failure_cleans(db, tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    plan = FFmpegPlan(video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="movie.srt"))
    job_id = await add_job(db, source, plan=plan.model_dump(mode="json"))
    workspace = tmp_path / "workspace"
    assert await StagingService(db, workspace, min_free_gb=1).stage_job(job_id) == 0
    assert (await get_job(db, job_id)).status == JobStatus.FAILED
    assert not (workspace / "jobs" / str(job_id)).exists()
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.STAGING, JobStatus.HOLD])
@pytest.mark.parametrize("bulk", [False, True])
async def test_pending_cancel_cleans(db, tmp_path: Path, status: JobStatus, bulk: bool) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(db, source, status=status.value)
    workspace = tmp_path / "workspace"
    stage_source(workspace, job_id, source)
    app = create_app(db, config=ServerConfig(workspace_dir=str(workspace)))
    await app.state.settings.update({"min_free_gb": 1})
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = (
            await client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": [job_id]})
            if bulk
            else await client.post(f"/api/jobs/{job_id}/cancel")
        )
        assert response.status_code == 200
    assert (await get_job(db, job_id)).status == JobStatus.CANCELLED
    assert not (workspace / "jobs" / str(job_id)).exists()
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_bulk_cancel_writers(db, tmp_path: Path, reverse: bool) -> None:
    workspace = tmp_path / "workspace"
    sources = [tmp_path / f"{index}.mkv" for index in range(3)]
    for source in sources:
        source.write_bytes(b"source")
    queued = await add_job(db, sources[0], status=JobStatus.QUEUED)
    unassigned = await add_job(db, sources[1], status=JobStatus.RUNNING)
    connected = await add_job(db, sources[2], status=JobStatus.RUNNING)
    for identifier, source in zip((queued, unassigned, connected), sources, strict=True):
        stage_source(workspace, identifier, source)
    async with db() as session:
        node = await register_node(session, "cancel-node", "test-token")
        attempt_ids = {}
        for identifier in (unassigned, connected):
            job = await session.get(Job, identifier)
            job.attempts = 1
            job.node_id = node.id if identifier == connected else None
            attempt = JobAttempt(
                job_id=identifier,
                attempt_number=1,
                status=JobStatus.RUNNING,
                input_probe=ProbeData(file_path="source.mkv", duration_s=60, size_bytes=6, streams=[]).model_dump(
                    mode="json"
                ),
            )
            session.add(attempt)
            await session.flush()
            attempt_ids[identifier] = attempt.id
        await session.commit()

    class Connection:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.sent.append(data)

        async def recv_json(self) -> dict | None:
            return None

        async def close(self, code: int = 1000) -> None:
            pass

    app = create_app(db, config=ServerConfig(workspace_dir=str(workspace)))
    await app.state.settings.update({"min_free_gb": 1})
    connection = Connection()
    app.state.hub.entries[node.name] = NodeEntry(
        name=node.name,
        node_db_id=node.id,
        capabilities=NodeCapabilities(),
        workspace_ready=True,
        conn=connection,
        in_flight={connected: "transcode"},
    )
    identifiers = [queued, unassigned, connected]
    if reverse:
        identifiers.reverse()
    bus = app.state.events
    sub = bus.subscribe()
    try:
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": identifiers}), 3
            )
        assert response.status_code == 200
        assert response.json() == {"succeeded": identifiers, "failed": []}
        bus.flush_bulk()
        received = [await asyncio.wait_for(sub.next(), 1) for _ in range(sub.backlog)]
        status = {
            inner["payload"]["job_id"]: inner["payload"]["status"]
            for event in received
            if event is not None and event.type == "events.batch"
            for inner in event.payload["events"]
            if inner["type"] == "job.status"
        }
        assert status == {queued: "cancelled", unassigned: "cancelled"}
        assert {
            event.payload["job_id"] for event in received if event is not None and event.type == "job.cancel_requested"
        } == {unassigned, connected}
        for identifier in (queued, unassigned):
            assert (await get_job(db, identifier)).status == JobStatus.CANCELLED
            assert not (workspace / "jobs" / str(identifier)).exists()
        assert (await get_job(db, connected)).status == JobStatus.RUNNING
        assert (workspace / "jobs" / str(connected) / "source.mkv").exists()
        assert connection.sent == [
            Cancel(job_id=connected, attempt_id=attempt_ids[connected], reason="cancelled", log_path=None).model_dump(
                mode="json"
            )
        ]
        async with db() as session:
            attempt = await session.get(JobAttempt, attempt_ids[unassigned])
            assert attempt is not None and attempt.status == JobStatus.CANCELLED
            cancelled = list(
                (
                    await session.scalars(
                        select(JobEvent.job_id).where(JobEvent.type == "cancelled").order_by(JobEvent.job_id)
                    )
                ).all()
            )
            assert cancelled == sorted([queued, unassigned])
        assert all(source.read_bytes() == b"source" for source in sources)
    finally:
        bus.unsubscribe(sub)
        await asyncio.wait_for(bus.aclose(), 1)


@pytest.mark.asyncio
async def test_cancel_waits_writer(db, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(db, source)
    workspace = tmp_path / "workspace"
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(root, identifier, path, **kwargs):
        from server.workspace import _stage_source

        result = _stage_source(root, identifier, path, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return result

    monkeypatch.setattr("server.staging._stage_source", blocked)
    service = StagingService(db, workspace, min_free_gb=1)
    app = create_app(db, config=ServerConfig(workspace_dir=str(workspace)))
    await app.state.settings.update({"min_free_gb": 1})
    app.state.staging = service
    task = asyncio.create_task(service.stage_job(job_id))
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post(f"/api/jobs/{job_id}/cancel")).status_code == 200
        assert (workspace / "jobs" / str(job_id) / "source.mkv").read_bytes() == b"source"
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert (await get_job(db, job_id)).status == JobStatus.CANCELLED
    assert not (workspace / "jobs" / str(job_id)).exists()


@pytest.mark.asyncio
async def test_cancel_releases_queue(db, tmp_path: Path, monkeypatch) -> None:
    sources = [tmp_path / f"{n}.mkv" for n in range(2)]
    for source in sources:
        source.write_bytes(b"12345678")
    identifiers = [await add_job(db, source) for source in sources]
    workspace = tmp_path / "workspace"
    service = StagingService(db, workspace, min_free_gb=1)
    started, entered = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    measurements = []

    def usage(root):
        measurements.append(root)
        used = sum(path.stat().st_size for path in workspace.glob("jobs/*/source.mkv"))
        return SimpleNamespace(free=math.ceil(service.min_free_gb * 1e9) + 18 - used)

    def blocked(root, job_id, source, **kwargs):
        from server.workspace import _stage_source

        result = _stage_source(root, job_id, source, **kwargs)
        if job_id == identifiers[0]:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        return result

    async def another_copy():
        entered.set()
        return await service.stage_job(identifiers[1])

    monkeypatch.setattr("server.staging.shutil.disk_usage", usage)
    monkeypatch.setattr("server.staging._stage_source", blocked)
    first = asyncio.create_task(service.stage_job(identifiers[0]))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        app = create_app(db, config=ServerConfig(workspace_dir=str(workspace)))
        await app.state.settings.update({"min_free_gb": 1})
        app.state.staging = service
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post(f"/api/jobs/{identifiers[0]}/cancel")).status_code == 200
        first.cancel()
        second = asyncio.create_task(another_copy())
        await asyncio.wait_for(entered.wait(), 3)
        assert not first.done() and len(measurements) == 1
        assert identifiers[0] in service.active_jobs
        assert (workspace / "jobs" / str(identifiers[0]) / "source.mkv").exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 3)
        assert await asyncio.wait_for(second, 3) == 1
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True), 3)
    assert not service.active_jobs
    assert not (workspace / "jobs" / str(identifiers[0])).exists()
    assert (await get_job(db, identifiers[1])).status == JobStatus.QUEUED
    assert all(source.read_bytes() == b"12345678" for source in sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["interrupted", "finalization_unprepared", "journal", "new_attempt"])
async def test_cleanup_preserves_original(db, tmp_path: Path, record_type: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(db, source, status=JobStatus.CANCELLED.value)
    workspace = tmp_path / "workspace"
    stage_source(workspace, job_id, source)
    artifact = workspace / "jobs" / str(job_id) / "attempts" / "1" / "output.eng.srt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"action output")
    async with db() as session:
        if record_type == "interrupted":
            session.add(
                JobAttempt(job_id=job_id, attempt_number=1, status=JobStatus.FAILED.value, result="interrupted")
            )
        elif record_type == "finalization_unprepared":
            session.add(JobEvent(job_id=job_id, type=record_type, payload={}, ts=T0))
        elif record_type == "journal":
            attempt = JobAttempt(job_id=job_id, attempt_number=1, status=JobStatus.FAILED.value)
            session.add(attempt)
            await session.flush()
            session.add(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path=str(source),
                    destination_path=str(source),
                    partial_path=str(tmp_path / ".1.part"),
                    trash_path=str(tmp_path / ".1.discard"),
                    state="prepared",
                )
            )
        else:
            (await session.get(Job, job_id)).attempts = 1
        await session.commit()
    await StagingService(db, workspace, min_free_gb=1).prune_job(job_id, attempt_number=0)
    assert source.read_bytes() == b"source"
    if record_type == "new_attempt":
        assert artifact.read_bytes() == b"action output"
    else:
        assert not (workspace / "jobs" / str(job_id)).exists()


@pytest.mark.asyncio
async def test_hold_release_stages(db: async_sessionmaker[AsyncSession]) -> None:
    source = make_test_disk_path("staging", "elapsed.mkv")
    source.write_bytes(b"source")
    seen = datetime(2026, 1, 1, tzinfo=UTC)
    job_id = await add_job(db, source, status=JobStatus.HOLD.value, hold_until=seen + timedelta(seconds=300))
    workspace = make_test_temp_dir("workspace")

    service = StagingService(db, workspace, min_free_gb=1, clock=lambda: seen + timedelta(seconds=60))
    assert await service.release_holds() == 0
    assert await service.stage_job(job_id) == 0
    assert (await get_job(db, job_id)).status == JobStatus.HOLD.value
    assert not (workspace / "jobs" / str(job_id)).exists()
    service.clock = lambda: seen + timedelta(seconds=300)
    assert await service.release_holds() == 1
    assert (await get_job(db, job_id)).status == JobStatus.STAGING.value
    assert not (workspace / "jobs" / str(job_id)).exists()
    assert await service.stage_job(job_id) == 1
    assert (workspace / "jobs" / str(job_id) / "source.mkv").read_bytes() == b"source"
    assert (await get_job(db, job_id)).status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_hold_commit_failure(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    seen = datetime(2026, 1, 1, tzinfo=UTC)
    source = make_test_disk_path("staging", "commit-failure.mkv")
    source.write_bytes(b"source")
    await add_job(db, source, status=JobStatus.HOLD.value, hold_until=seen)
    async with subscribed() as (bus, sub):

        async def fail_commit(_self) -> None:
            raise RuntimeError("commit failed")

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError):
            await StagingService(
                db, make_test_temp_dir("workspace"), min_free_gb=1, clock=lambda: seen, events=bus
            ).release_holds()
        bus.flush_bulk()
        assert bus.staged_count() == 0 and sub.backlog == 0


@pytest.mark.asyncio
async def test_missing_source_cancels(db: async_sessionmaker[AsyncSession]) -> None:
    source = make_test_disk_path("staging", "gone.mkv")
    source.write_bytes(b"source")
    job_id = await add_job(db, source)
    source.unlink()
    assert await StagingService(db, make_test_temp_dir("workspace"), min_free_gb=1).stage_job(job_id) == 0
    job = await get_job(db, job_id)
    assert job.status == JobStatus.CANCELLED.value
    assert job.error is not None


@pytest.mark.asyncio
async def test_cancel_event(db: async_sessionmaker[AsyncSession]) -> None:
    job_id = await add_job(db, None)
    async with subscribed() as (bus, sub):
        service = StagingService(db, make_test_temp_dir("workspace"), min_free_gb=1, events=bus)
        try:
            assert await service.stage_job(job_id) == 0
        finally:
            await asyncio.wait_for(service.aclose(), 2)
        bus.flush_bulk()

        assert (await get_job(db, job_id)).status == JobStatus.CANCELLED.value
        received = [await asyncio.wait_for(sub.next(), timeout=1) for _ in range(sub.backlog)]
        event = next(event for event in received if event is not None and event.type == "events.batch")
        inner = next(inner for inner in event.payload["events"] if inner["type"] == "job.status")
        assert inner["payload"]["job_id"] == job_id
        assert inner["payload"]["status"] == JobStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_staged_event(
    db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(db, source)
    async with subscribed() as (bus, sub):
        service = StagingService(db, tmp_path / "workspace", min_free_gb=1, events=bus)
        try:
            assert await asyncio.wait_for(service.stage_job(job_id), 2) == 1
        finally:
            await asyncio.wait_for(service.aclose(), 2)
        bus.flush_bulk()

        assert (await get_job(db, job_id)).status == JobStatus.QUEUED.value
        assert (tmp_path / "workspace" / "jobs" / str(job_id) / "source.mkv").read_bytes() == b"source"
        assert source.read_bytes() == b"source"
        async with db() as session:
            events = list(
                await session.scalars(select(JobEvent.type).where(JobEvent.job_id == job_id).order_by(JobEvent.id))
            )
        assert events == ["enqueued", "staged", "queued"]
        received = [await asyncio.wait_for(sub.next(), timeout=1) for _ in range(sub.backlog)]
        statuses = {
            inner["payload"]["job_id"]: inner["payload"]["status"]
            for event in received
            if event is not None and event.type == "events.batch"
            for inner in event.payload["events"]
            if inner["type"] == "job.status"
        }
        assert statuses == {job_id: JobStatus.QUEUED.value}


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_message", [False, True])
async def test_staging_errors(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    empty_message: bool,
) -> None:
    source = make_test_disk_path("staging", "copy-error.mkv")
    source.write_bytes(b"source")
    job_id = await add_job(db, source)
    error = RuntimeError() if empty_message else OSError(str(source))
    monkeypatch.setattr("server.staging._stage_source", lambda *_, **__: (_ for _ in ()).throw(error))
    async with db() as session:
        current = await session.get(Job, job_id)
        assert current is not None
        current.attempts = 1
        session.add(
            JobAttempt(job_id=job_id, attempt_number=1, status=JobStatus.FAILED.value, error="previous failure")
        )
        await session.commit()

    await StagingService(db, make_test_temp_dir("workspace"), min_free_gb=1).stage_job(job_id)
    job = await get_job(db, job_id)
    async with db() as session:
        attempt = (await session.execute(select(JobAttempt).where(JobAttempt.job_id == job_id))).scalar_one()
        event = (
            await session.execute(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "failed"))
        ).scalar_one()
    assert job.status == JobStatus.FAILED.value
    if not empty_message:
        assert job.error and str(source) in job.error
    assert attempt.error == "previous failure" and event.attempt_id is None


@pytest.mark.asyncio
async def test_continues_after_failure(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_test_disk_path("staging", "first.mkv")
    second = make_test_disk_path("staging", "second.mkv")
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    first_id, second_id = await add_job(db, first), await add_job(db, second)
    copy = workspace._stage_source

    def stage(root: Path, job_id: int, source: Path, **kwargs) -> str:
        if job_id == first_id:
            raise OSError("copy failed")
        return copy(root, job_id, source, **kwargs)

    monkeypatch.setattr("server.staging._stage_source", stage)
    service = StagingService(db, make_test_temp_dir("workspace"), min_free_gb=1)
    assert await service.stage_job(first_id) == 0
    assert await service.stage_job(second_id) == 1
    assert (await get_job(db, first_id)).status == JobStatus.FAILED.value
    assert (await get_job(db, second_id)).status == JobStatus.QUEUED.value
    assert not (service.workspace_dir / "jobs" / str(first_id)).exists()
    assert (service.workspace_dir / "jobs" / str(second_id) / "source.mkv").read_bytes() == b"2"
    assert first.read_bytes() == b"1" and second.read_bytes() == b"2"
