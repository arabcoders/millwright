from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.plan.models import FFmpegPlan
from common.enums import JobStatus, TranscodeStatus
from server.retention import delete_eligible_job
from server.events import EventBus
from server.flows.definition import ConditionNode, FlowDefinition, FlowEdge, PluginNode, TerminalNode
from server.flows.engine import FlowResult
from server.fs import library as library_fs
from server.healthcheck import HealthcheckResult, HealthcheckService
from server.models import FinalizationJournal, Flow, Job, JobAttempt, Library, MediaFile, ProbeRow, utcnow
from server.metadata_jobs import enqueue_metadata_job
from server.queue.enqueue import FlowEnqueueService
from server.queue import enqueue as enqueue_module
from server.queue.store import (
    ActiveJobError,
    cancel_pending,
    enqueue_file_job,
    finish,
    processing_backlog,
    request_file_job,
)
from server.plugins.host import PluginHost
from server.backup import export_data, restore_data
from server.probing import ProbeService
from server.retention import prune_jobs
from server.scanner.service import ScannerService
from server.staging import StagingService
from common.fs.identity import source_identity
from tests.helpers import make_test_temp_dir
from tests.helpers import database_context

STARTER_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"

RAW_PROBE = {
    "streams": [{"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1920, "height": 1080}],
    "format": {"duration": "100.0", "size": "5000000", "format_name": "matroska,webm"},
}


def pnode(node_id: str, plugin: str, **inputs: Any) -> PluginNode:
    return PluginNode(id=node_id, plugin=plugin, inputs=inputs)


def cnode(node_id: str, when: dict[str, Any]) -> ConditionNode:
    return ConditionNode(id=node_id, when=when)


def tnode(node_id: str, outcome: Literal["skip", "done", "veto"] = "done") -> TerminalNode:
    return TerminalNode(id=node_id, outcome=outcome)


def graph_flow(entry: str, nodes: list, edges: list[tuple[str, str, str]]) -> FlowDefinition:
    return FlowDefinition(
        entry=entry, nodes=nodes, edges=[FlowEdge(src=src, port=port, dst=dst) for src, port, dst in edges]
    )


def remux_definition() -> FlowDefinition:
    return graph_flow(
        "remux", [pnode("remux", "transcode.remux", container="mkv"), tnode("done")], [("remux", "plan", "done")]
    )


def skip_definition() -> FlowDefinition:
    return graph_flow(
        "gate",
        [cnode("gate", {"media.file.size_bytes": {"gt": 999999999999}}), tnode("done"), tnode("miss", "skip")],
        [("gate", "true", "done"), ("gate", "false", "miss")],
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("flow-enqueue") as factory:
        yield factory


@pytest_asyncio.fixture
async def host() -> AsyncIterator[PluginHost]:
    plugin_host = PluginHost([STARTER_PLUGINS])
    try:
        yield plugin_host
    finally:
        await plugin_host.aclose()


async def seed(
    db: async_sessionmaker[AsyncSession],
    *,
    definition: FlowDefinition | None = None,
    job: dict[str, str] | None = None,
    cooldown_s: int = 0,
    first_seen: datetime | None = None,
    checks: bool = False,
    health: str = "healthy",
) -> tuple[int, int]:
    path = make_test_temp_dir("flow-enqueue") / f"file-{uuid.uuid4().hex}.mp4"
    path.write_bytes(b"x")
    identity = source_identity(path.stat())
    async with db() as session:
        flow_id = None
        if definition is not None:
            flow = Flow(name=f"flow-{uuid.uuid4().hex}", definition=definition.model_dump_json())
            session.add(flow)
            await session.flush()
            flow_id = flow.id
        library = Library(
            name=f"library-{uuid.uuid4().hex}",
            roots=[str(path.parent)],
            flow_id=flow_id,
            cooldown_s=cooldown_s,
            healthcheck_enabled=checks,
        )
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=identity["size"],
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=identity,
            container="mp4",
            video_codec="h264",
            medium="video",
            health_status=health,
            health_source={"path": str(path), **identity} if health == "healthy" else None,
            first_seen=first_seen or utcnow(),
            last_scanned_at=utcnow(),
        )
        session.add(media)
        await session.flush()
        probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps(RAW_PROBE))
        session.add(probe)
        await session.flush()
        media.probe_id = probe.id
        if job is not None:
            session.add(Job(media_file_id=media.id, library_id=library.id, **job))
            media.transcode_decided = job.get("kind", "transcode") == "transcode"
        await session.commit()
        return media.id, library.id


@pytest.mark.asyncio
async def test_failed_batch_fairness(db, host, monkeypatch) -> None:
    ids = [await seed(db, definition=remux_definition()) for _ in range(3)]
    service = FlowEnqueueService(db, host, batch=2)
    visited = []

    async def process(identifier):
        visited.append(identifier)
        return identifier == ids[-1][0]

    monkeypatch.setattr(service, "_process", process)
    assert await service._pass() == 0 and service._more
    assert await service._pass() == 1 and not service._more
    assert visited == [item[0] for item in ids]
    assert service._cursor == 0


@pytest.mark.asyncio
async def test_restore_suspends_work(db, host, monkeypatch) -> None:
    root = make_test_temp_dir("restored-library")
    path = root / "movie.mkv"
    path.write_bytes(b"fixture")
    new_path = root / "new.mp4"
    new_path.write_bytes(b"new fixture")
    media_id, library_id = await seed(db, definition=remux_definition())
    async with db() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        flow = await session.get(Flow, library.flow_id)
        assert flow is not None
        flow.name, library.name, media.path = "flow", "library", str(path)
        library.roots, library.watch = [str(root)], True
        media.size, media.mtime_ns = path.stat().st_size, path.stat().st_mtime_ns
        media.source_identity = source_identity(path.stat())
        media.health_source = {"path": str(path), **source_identity(path.stat())}
        pending = MediaFile(
            library_id=library_id,
            path=str(root / "pending.mkv"),
            size=1,
            mtime_ns=1,
            container="mkv",
            medium="video",
            health_status="pending",
            last_scanned_at=utcnow(),
        )
        session.add(pending)
        await session.commit()
        payload = await export_data(session)
    watching, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scheduler = ScannerService(db, staging=StagingService(db, make_test_temp_dir("restored-workspace"), min_free_gb=1))

    async def watch(_library):
        try:
            watching.set()
            await asyncio.wait_for(release.wait(), 3)
        finally:
            stopped.set()

    monkeypatch.setattr(scheduler, "_watch_library", watch)
    scheduler._running = True
    await scheduler.update_watchers()
    probes = ProbeService(db, ffprobe_bin=root / "unused", mkvpropedit_bin=root / "unused", workspace_dir=root)
    enqueue = FlowEnqueueService(db, host)
    try:
        await asyncio.wait_for(watching.wait(), 3)
        async with scheduler.library_mutation(), db() as session:
            await restore_data(session, payload, {"flows": ["flow"], "libraries": ["library"], "replace": True})
            await session.commit()
        assert stopped.is_set() and not scheduler._watch_tasks
        await scheduler.scan_now(library_id)
        await probes._fill()
        assert not probes._tasks and await enqueue._pass() == 0
        async with db() as session:
            assert not list((await session.scalars(select(Job))).all())
            assert await session.scalar(select(MediaFile.id).where(MediaFile.path == str(new_path))) is None
            library = await session.get(Library, library_id)
            assert library is not None and library.suspended
            library.name = "reviewed"
            await session.commit()
            assert library.suspended
            library.suspended = False
            await session.commit()
        await probes._fill()
        await asyncio.wait_for(asyncio.gather(*list(probes._tasks.values())), 3)
        async with db() as session:
            job = await session.scalar(select(Job).where(Job.media_file_id == pending.id, Job.kind == "probe"))
            assert job is not None and job.attempts == 1 and job.status == "failed"
        await scheduler.scan_now(library_id)
        async with db() as session:
            assert await session.scalar(select(MediaFile.id).where(MediaFile.path == str(new_path))) is not None
        assert await enqueue._pass() == 1
    finally:
        release.set()
        await asyncio.wait_for(scheduler.aclose(), 3)
        await asyncio.wait_for(probes.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [False, True])
async def test_enqueues_matched_transcode(db, host, checks: bool) -> None:
    hub_wakes = []
    staging_wakes = []
    media_id, library_id = await seed(db, definition=remux_definition(), checks=checks)
    service = FlowEnqueueService(
        db, host, hub_wake=lambda: hub_wakes.append(1), staging_wake=lambda: staging_wakes.append(1)
    )
    try:
        assert await service._pass() == 1
    finally:
        await service.close()
    async with db() as session:
        job = (await session.execute(select(Job).where(Job.media_file_id == media_id))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
        flow = await session.get(Flow, library.flow_id)
        assert flow is not None
        assert job.kind == "transcode"
        assert job.status == JobStatus.STAGING.value
        assert job.decision_log
        assert job.plan is not None
        assert job.flow_snapshot == {
            "id": flow.id,
            "name": flow.name,
            "version": 1,
            "definition": remux_definition().model_dump(mode="json"),
        }
        flow.definition = skip_definition().model_dump_json()
        flow.version = 2
        await session.commit()
    async with db() as session:
        saved = (await session.execute(select(Job).where(Job.media_file_id == media_id))).scalar_one()
        assert saved.flow_snapshot["version"] == 1
        assert saved.flow_snapshot["definition"] == remux_definition().model_dump(mode="json")
    assert hub_wakes == [1]
    assert staging_wakes == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["healthy", "pending"])
async def test_evaluation_rejects_replacement(db, host, tmp_path: Path, monkeypatch, health: str) -> None:
    media_id, library_id = await seed(db, definition=remux_definition(), checks=True, health=health)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        assert media is not None and library is not None
        library.roots = [str(tmp_path)]
        media.path, media.size, media.mtime_ns = str(source), source.stat().st_size, source.stat().st_mtime_ns
        media.source_identity = source_identity(source.stat())
        media.health_source = {"path": str(source), **source_identity(source.stat())}
        await session.commit()

    async def evaluate(*args):
        source.write_bytes(b"changed source")
        async with db() as session:
            media = await session.get(MediaFile, media_id)
            media.size, media.mtime_ns = source.stat().st_size, source.stat().st_mtime_ns
            await session.commit()
        return FlowResult(status="matched", plan=FFmpegPlan())

    monkeypatch.setattr("server.queue.enqueue.evaluate", evaluate)
    assert await FlowEnqueueService(db, host)._pass() == 0
    async with db() as session:
        assert not list((await session.scalars(select(Job))).all())
        media = await session.get(MediaFile, media_id)
        assert not media.transcode_decided


@pytest.mark.asyncio
async def test_manual_queue_competes(db, host, monkeypatch) -> None:
    media_id, library_id = await seed(db, definition=remux_definition())
    service = FlowEnqueueService(db, host)
    media_locked, manual_started = asyncio.Event(), asyncio.Event()
    async with db() as automatic, db() as manual:
        # The former media-to-library UPDATE order deadlocks against this owner.
        await manual.get(Library, library_id, with_for_update={"read": True})
        scalar, get = automatic.scalar, manual.get

        async def lock_media(statement, *args, **kwargs):
            result = await scalar(statement, *args, **kwargs)
            media_statement = (statement.is_select and statement.column_descriptions[0]["entity"] is MediaFile) or (
                statement.is_update and statement.table.name == MediaFile.__tablename__
            )
            if media_statement and result is not None and not media_locked.is_set():
                media_locked.set()
                await manual_started.wait()
            return result

        async def manual_get(entity, identifier, **kwargs):
            if entity is MediaFile and kwargs.get("with_for_update"):
                manual_started.set()
            return await get(entity, identifier, **kwargs)

        monkeypatch.setattr(automatic, "scalar", lock_media)
        monkeypatch.setattr(manual, "get", manual_get)

        async def automatic_queue():
            media = await automatic.get(MediaFile, media_id)
            assert media is not None
            return await service._decide(automatic, media)

        async def manual_queue():
            await media_locked.wait()
            with pytest.raises(ActiveJobError):
                await enqueue_file_job(manual, media_id, library_id, FFmpegPlan())
            await manual.rollback()

        tasks = [asyncio.create_task(automatic_queue()), asyncio.create_task(manual_queue())]
        try:
            queued, _ = await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert queued
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    async with db() as session:
        jobs = list(await session.scalars(select(Job).where(Job.media_file_id == media_id)))
        assert len(jobs) == 1 and jobs[0].status == JobStatus.STAGING


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["deleted", "flow_id", "processing_cutoff"])
async def test_evaluation_rechecks_library(db, host, monkeypatch, setting: str) -> None:
    media_id, library_id = await seed(db, definition=remux_definition())

    async def evaluate(*args):
        async with db() as session:
            library = await session.get(Library, library_id)
            assert library is not None
            value = {
                "deleted": True,
                "flow_id": None,
                "processing_cutoff": {
                    "at": "2100-01-01T00:00:00+00:00",
                    "fields": ["first_seen"],
                    "match": "all",
                },
            }[setting]
            setattr(library, setting, value)
            await session.commit()
        return FlowResult(status="matched", plan=FFmpegPlan())

    monkeypatch.setattr(enqueue_module, "evaluate", evaluate)
    assert await FlowEnqueueService(db, host)._pass() == 0
    async with db() as session:
        assert await session.scalar(select(Job.id).where(Job.media_file_id == media_id)) is None
        media = await session.get(MediaFile, media_id)
        assert media is not None and not media.transcode_decided


@pytest.mark.asyncio
async def test_restarts_after_close(db, host) -> None:
    processed = asyncio.Event()
    service = FlowEnqueueService(db, host, staging_wake=processed.set)
    await service.start()
    await service.close()

    media_id, _ = await seed(db, definition=skip_definition())
    await service.start()
    try:
        await asyncio.wait_for(processed.wait(), 2)
        async with db() as session:
            job = await session.scalar(select(Job).where(Job.media_file_id == media_id))
            assert job is not None and job.status == JobStatus.SKIPPED
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [False, True])
async def test_reports_flow_error(db, host, monkeypatch, returned: bool) -> None:
    error_text = "opaque-fixture"
    media_id, library_id = await seed(db, definition=remux_definition())
    bus = EventBus()
    sub = bus.subscribe()

    async def fail(*_args, **_kwargs):
        if returned:
            return FlowResult(status="error", reason=error_text)
        raise RuntimeError(error_text)

    monkeypatch.setattr("server.queue.enqueue.evaluate", fail)
    service = FlowEnqueueService(db, host, events=bus)

    try:
        assert await service._pass() == 0
    finally:
        await service.close()

    event = await asyncio.wait_for(sub.next(), 1)
    assert event is not None
    assert event.type == "operator.alert"
    assert event.payload["operation"] == "flow evaluation"
    assert event.payload["media_id"] == media_id
    assert event.payload["library_id"] == library_id
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and event.payload["media_path"] == media.path
        assert media.transcode_status == TranscodeStatus.FAILED.value
    assert event.payload["library_name"].startswith("library-")
    assert event.payload["error"] == error_text


@pytest.mark.asyncio
async def test_active_race_skips(db, host, monkeypatch) -> None:
    media_id, _ = await seed(db, definition=remux_definition())
    bus = EventBus()
    sub = bus.subscribe()
    service = FlowEnqueueService(db, host, events=bus)

    async def lose_race(*_args, **_kwargs):
        raise ActiveJobError("already active")

    monkeypatch.setattr(service, "_decide", lose_race)
    try:
        assert not await service._process(media_id)
        assert sub.backlog == 0
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["healthy", "pending"])
async def test_cooldown_defers_work(db, host, monkeypatch, health: str) -> None:
    current = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("server.models.utcnow", lambda: current)
    monkeypatch.setattr("server.queue.enqueue.utcnow", lambda: current)
    monkeypatch.setattr("server.queue.store.utcnow", lambda: current)
    seen = current - timedelta(seconds=30)
    media_id, _ = await seed(
        db, definition=remux_definition(), cooldown_s=300, first_seen=seen, checks=True, health=health
    )
    service = FlowEnqueueService(db, host)
    try:
        assert await service._pass() == 0
        async with db() as session:
            assert await session.scalar(select(Job.id).where(Job.media_file_id == media_id)) is None
            media = await session.get(MediaFile, media_id)
            assert media is not None and not media.transcode_decided
            media.first_seen = current
            await session.commit()
        current = seen + timedelta(seconds=300)
        assert await service._pass() == 0
        current = seen + timedelta(seconds=330)
        assert await service._pass() == 1
    finally:
        await service.close()
    async with db() as session:
        job = (await session.execute(select(Job).where(Job.media_file_id == media_id))).scalar_one()
        assert job.kind == ("healthcheck" if health == "pending" else "transcode")
        assert job.status == (JobStatus.QUEUED if health == "pending" else JobStatus.STAGING)
        assert job.hold_until is None


@pytest.mark.asyncio
async def test_cooldown_wakes_flow(db, host, monkeypatch) -> None:
    current = datetime(2026, 1, 1, tzinfo=UTC)
    seen = current
    monkeypatch.setattr(enqueue_module, "utcnow", lambda: current)
    media_id, _ = await seed(db, definition=remux_definition(), cooldown_s=300, first_seen=seen)
    service = FlowEnqueueService(db, host)
    waits = []

    async def wait():
        nonlocal current
        waits.append(service._cooldown_until)
        if service._cooldown_until is not None:
            current = service._cooldown_until
            raise TimeoutError
        service._closed = True

    monkeypatch.setattr(service._wake, "wait", wait)
    try:
        await asyncio.wait_for(service._run(), 2)
        assert waits == [seen + timedelta(seconds=300), None]
        async with db() as session:
            job = await session.scalar(select(Job).where(Job.media_file_id == media_id))
            assert job is not None and job.status == JobStatus.STAGING
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["healthy", "pending"])
async def test_records_skipped_once(db, host, monkeypatch, health: str) -> None:
    current = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(enqueue_module, "utcnow", lambda: current)
    media_id, _ = await seed(
        db, definition=skip_definition(), cooldown_s=300, first_seen=current, checks=True, health=health
    )
    service = FlowEnqueueService(db, host)
    try:
        assert await service._pass() == 1
        assert await service._pass() == 0
    finally:
        await service.close()
    async with db() as session:
        jobs = list((await session.execute(select(Job).where(Job.media_file_id == media_id))).scalars())
        assert [(job.kind, job.status) for job in jobs] == [("transcode", JobStatus.SKIPPED.value)]
        assert jobs[0].flow_snapshot["definition"] == skip_definition().model_dump(mode="json")
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.transcode_status == TranscodeStatus.NOT_NEEDED.value
        assert media.health_status == health
        assert await delete_eligible_job(session, jobs[0].id)
        await session.commit()
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.transcode_status == TranscodeStatus.NOT_NEEDED.value
        job = await enqueue_file_job(session, media_id, media.library_id, FFmpegPlan())
        assert media.transcode_status == TranscodeStatus.PENDING.value
        assert await finish(session, job.id, JobStatus.FAILED)
        await session.commit()
        assert media.transcode_status == TranscodeStatus.FAILED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_probe_flow_queue(db, host, tmp_path: Path, monkeypatch, bound: bool) -> None:
    media_id, library_id = await seed(
        db,
        definition=skip_definition() if bound else None,
        checks=True,
        health="pending",
    )
    async with db() as session:
        row = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        await enqueue_metadata_job(session, row, library, kind="probe", priority=1)
        await session.commit()

    async def probe(*_args, **_kwargs):
        return RAW_PROBE

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    probes = ProbeService(db, "unused", "unused", workspace_dir=tmp_path / "workspace")
    flow = FlowEnqueueService(db, host)
    try:
        await probes._fill()
        await asyncio.wait_for(asyncio.gather(*probes._tasks.values()), 3)
        assert await flow._pass() == int(bound)
        async with db() as session:
            jobs = list(await session.scalars(select(Job).where(Job.media_file_id == media_id).order_by(Job.id)))
            assert [(job.kind, job.status) for job in jobs] == [
                ("probe", "succeeded"),
                ("transcode", "skipped") if bound else ("healthcheck", "queued"),
            ]
    finally:
        await probes.close()
        await flow.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["healthy", "corrupt", "failed", "cancelled", "cancelled_running"])
async def test_flow_health_gate(db, host, tmp_path: Path, monkeypatch, result: str) -> None:
    media_id, _ = await seed(db, definition=remux_definition(), checks=True, health="pending")
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.transcode_status = "failed"
        await session.commit()
    awakened = asyncio.Event()
    decoding = asyncio.Event()
    running: asyncio.Task[None] | None = None
    flow = FlowEnqueueService(db, host, healthcheck_wake=awakened.set)
    checks = HealthcheckService(db, tmp_path / "healthchecks", "unused", "unused", on_healthy=flow.wake)

    async def decode(_source, _ffmpeg, _ffprobe, log_path, **_kwargs):
        if result == "cancelled_running":
            decoding.set()
            await asyncio.Event().wait()
        log_path.path.write_text("healthcheck: completed\n")
        return HealthcheckResult(
            healthy=result == "healthy",
            failed=result == "failed",
            probe=RAW_PROBE,
        )

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    try:
        assert await flow._pass() == 1 and awakened.is_set()
        assert await flow._pass() == 0
        async with db() as session:
            job = (await session.scalars(select(Job).where(Job.media_file_id == media_id))).one()
            assert job.kind == "healthcheck" and job.status == JobStatus.QUEUED
            assert job.decision_log[-1]["outcome"] == "plan"
            assert job.flow_snapshot["definition"] == remux_definition().model_dump(mode="json")
            assert (await session.get(MediaFile, media_id)).transcode_decided
            assert (await session.get(MediaFile, media_id)).transcode_status == "pending"
            backlog = await processing_backlog(session, healthcheck_slots=1)
            assert backlog["remaining_files"] == 1 and backlog["remaining_s"] is None
            if result == "cancelled":
                assert await cancel_pending(session, job.id) is not None
                await session.commit()
        if result != "cancelled":
            claimed = await checks.claim_next()
            assert claimed is not None and claimed[0] == job.id
            if result == "cancelled_running":
                running = asyncio.create_task(checks.run(*claimed))
                await asyncio.wait_for(decoding.wait(), 2)
                running.cancel()
                await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 3)
            else:
                await asyncio.wait_for(checks.run(*claimed), 3)
        assert await flow._pass() == int(result == "healthy")
        async with db() as session:
            jobs = list(await session.scalars(select(Job).where(Job.media_file_id == media_id).order_by(Job.id)))
            if result == "healthy":
                assert [(item.kind, item.status) for item in jobs] == [
                    ("healthcheck", "succeeded"),
                    ("transcode", "staging"),
                ]
                return
            assert len(jobs) == 1
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.transcode_decided
            assert media.transcode_status == ("cancelled" if result.startswith("cancelled") else "pending")
            assert await delete_eligible_job(session, job.id)
            await session.commit()
        assert await flow._pass() == 0
        async with db() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None
            assert media.transcode_status == ("cancelled" if result.startswith("cancelled") else "pending")
        # An explicit recheck can recover blocked automatic work without a source edit.
        result = "healthy"
        async with db() as session:
            media = await session.get(MediaFile, media_id)
            manual = await request_file_job(session, media, kind="healthcheck", refresh=False)
            await session.commit()
        claimed = await checks.claim_next()
        assert claimed is not None and claimed[0] == manual.id
        await asyncio.wait_for(checks.run(*claimed), 3)
        assert await flow._pass() == 1
        async with db() as session:
            transcode = await session.scalar(select(Job).where(Job.media_file_id == media_id, Job.kind == "transcode"))
            assert transcode is not None and transcode.status == JobStatus.STAGING
    finally:
        if running is not None:
            running.cancel()
            await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 3)
        await asyncio.wait_for(checks.close(), 3)
        await flow.close()


@pytest.mark.asyncio
async def test_health_rechecks_generation(db, host, tmp_path: Path, monkeypatch) -> None:
    media_id, library_id = await seed(db, definition=remux_definition(), checks=True, health="pending")
    flow = FlowEnqueueService(db, host)
    checks = HealthcheckService(db, tmp_path / "healthchecks", "unused", "unused", on_healthy=flow.wake)

    async def decode(_source, _ffmpeg, _ffprobe, log_path, **_kwargs):
        log_path.path.write_text("healthcheck: completed\n")
        async with db() as session:
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            assert media is not None and library is not None
            path = Path(media.path)
            path.write_bytes(b"changed source")
            await library_fs.apply_probe_metadata(
                session,
                path=path,
                stat=path.stat(),
                raw=RAW_PROBE,
                row=media,
                library=library,
                purpose="probe",
            )
            definition = await session.get(Flow, library.flow_id)
            assert definition is not None
            definition.definition = skip_definition().model_dump_json()
            definition.version += 1
            await session.commit()
        return HealthcheckResult(healthy=True, probe=RAW_PROBE)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", decode)
    try:
        assert await flow._pass() == 1
        claimed = await checks.claim_next()
        assert claimed is not None
        await asyncio.wait_for(checks.run(*claimed), 3)
        assert flow._wake.is_set()
        assert await flow._pass() == 1
        async with db() as session:
            jobs = list(await session.scalars(select(Job).where(Job.media_file_id == media_id).order_by(Job.id)))
            assert [(job.kind, job.status) for job in jobs] == [("healthcheck", "failed"), ("transcode", "skipped")]
            assert jobs[-1].flow_snapshot["version"] == 2
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.health_status == "pending" and media.transcode_status == "not_needed"
    finally:
        await asyncio.wait_for(checks.close(), 3)
        await flow.close()


@pytest.mark.asyncio
async def test_health_defers_recovery(db, host, tmp_path: Path) -> None:
    media_id, library_id = await seed(db, definition=remux_definition(), checks=True, health="pending")
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        job = Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="failed", attempts=1)
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
        session.add(attempt)
        await session.flush()
        journal = FinalizationJournal(
            attempt_id=attempt.id,
            source_path=media.path,
            destination_path=media.path,
            partial_path=str(tmp_path / "output.partial"),
            trash_path=str(tmp_path / "original.discard"),
            state="discard_prepared",
        )
        session.add(journal)
        await session.commit()
    flow = FlowEnqueueService(db, host)
    assert await flow._pass() == 0
    async with db() as session:
        assert list(await session.scalars(select(Job.id).where(Job.media_file_id == media_id))) == [job.id]
        assert not (await session.get(MediaFile, media_id)).transcode_decided
        await session.delete(await session.get(FinalizationJournal, journal.id))
        await session.commit()
    assert await flow._pass() == 1
    async with db() as session:
        assert await session.scalar(select(Job.id).where(Job.media_file_id == media_id, Job.kind == "healthcheck"))


@pytest.mark.asyncio
async def test_health_wakes_committed(db, host, monkeypatch) -> None:
    media_id, _ = await seed(db, definition=remux_definition(), checks=True, health="pending")
    awakened = asyncio.Event()

    async def failed_publication(*_args, **_kwargs):
        raise OSError("Event publication unavailable.")

    monkeypatch.setattr(enqueue_module, "publish_job_status", failed_publication)
    flow = FlowEnqueueService(db, host, events=EventBus(), healthcheck_wake=awakened.set)
    assert await flow._pass() == 0
    assert awakened.is_set()
    async with db() as session:
        job = await session.scalar(select(Job).where(Job.media_file_id == media_id))
        assert job is not None and job.kind == "healthcheck" and job.status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [JobStatus.SUCCEEDED, JobStatus.CANCELLED])
async def test_history_preserves_decision(db, host, outcome: JobStatus) -> None:
    media_id, library_id = await seed(db, definition=remux_definition())
    service = FlowEnqueueService(db, host)
    assert await service._pass() == 1
    old = datetime(2020, 1, 1, tzinfo=UTC)
    async with db() as session:
        job = (await session.scalars(select(Job))).one()
        plan = FFmpegPlan.model_validate(job.plan)
        if outcome == JobStatus.SUCCEEDED:
            assert await finish(session, job.id, outcome, now=old)
        else:
            assert await cancel_pending(session, job.id, now=old) is not None
        await session.commit()
    if outcome == JobStatus.SUCCEEDED:
        assert await prune_jobs(db, 30, now=datetime(2026, 1, 1, tzinfo=UTC)) == 1
    else:
        async with db() as session:
            assert await delete_eligible_job(session, job.id)
            await session.commit()
    assert await service._pass() == 0
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.transcode_decided
        assert not (await session.scalars(select(Job))).all()
        manual = await enqueue_file_job(session, media_id, library_id, plan=plan)
        await session.commit()
        assert manual.status == JobStatus.STAGING.value


@pytest.mark.asyncio
async def test_requires_bound_flow(db, host) -> None:
    await seed(db)
    service = FlowEnqueueService(db, host)
    try:
        assert await service._pass() == 0
    finally:
        await service.close()
    async with db() as session:
        assert (await session.execute(select(Job))).scalars().all() == []


@pytest.mark.asyncio
async def test_excludes_active_job(db, host) -> None:
    media_id, _ = await seed(
        db, definition=remux_definition(), job={"kind": "healthcheck", "status": JobStatus.QUEUED.value}
    )
    service = FlowEnqueueService(db, host)
    try:
        assert await service._pass() == 0
    finally:
        await service.close()
    async with db() as session:
        jobs = list((await session.execute(select(Job).where(Job.media_file_id == media_id))).scalars())
        assert [job.status for job in jobs] == [JobStatus.QUEUED.value]
