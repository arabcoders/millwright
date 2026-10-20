from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import common.healthcheck as decode_healthcheck
import server.healthcheck as healthcheck
import server.queue.enqueue as enqueue_module
from common.plan.models import FFmpegPlan
from common.enums import HealthStatus, JobStatus
from server.retention import delete_eligible_job
from server.settings import ScannerConfig
from server.flows.engine import FlowResult
from server.models import Flow, Job, JobAttempt, Library, MediaFile
from server.queue.enqueue import FlowEnqueueService
from server.queue.store import retry_job
from server.plugins.host import PluginHost
from server.retention import prune_jobs
from server.scanner.scan import scan_library
from server.staging import StagingService
from common.fs.identity import source_identity
from server.workspace import cleanup_workspace, recovery_jobs
from tests.test_flow_enqueue import remux_definition, seed
from tests.helpers import request_retry
from tests.test_healthcheck_safety import HeldProcess, database
from tests.test_server_healthcheck_service import add_job


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["probe", "decode"])
@pytest.mark.parametrize("failure", ["signal", "lookup"])
async def test_kill_failure_protected(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, failure: str
):
    class CompletedProbe:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"{}", b""

    proc = HeldProcess(communication_error=True)

    def cannot_kill() -> None:
        if failure == "lookup":
            raise ProcessLookupError("fake process already exited")
        raise PermissionError("fake signal delivery failure")

    async def create(*args, **kwargs):
        if stage == "decode" and args[8] == "probe":
            return CompletedProbe()
        if stage == "decode":
            proc.stderr.set_exception(OSError("fake decoder pipe failure"))
        return proc

    monkeypatch.setattr(proc, "kill", cannot_kill)
    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", create)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(
        database,
        workspace,
        "decode",
        "probe",
    )
    claimed = await service.claim_next()
    assert claimed is not None
    try:
        if failure == "lookup":
            proc.stopped.set()
        await asyncio.wait_for(service.run(*claimed), 2)
        await cleanup_workspace(database, workspace)
        async with database() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None
            protected = await session.scalar(recovery_jobs().where(Job.id == job_id))
            assert proc.returncode is not None or protected == job_id
        assert proc.returncode is not None or attempt.result == "interrupted"
    finally:
        proc.stopped.set()


@pytest.mark.asyncio
async def test_claim_commit_cancel(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    committed, release = asyncio.Event(), asyncio.Event()
    commit = AsyncSession.commit
    injected = False

    async def held_commit(session: AsyncSession) -> None:
        nonlocal injected
        await commit(session)
        if not injected:
            injected = True
            committed.set()
            await release.wait()

    monkeypatch.setattr(AsyncSession, "commit", held_commit)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    service.start()
    try:
        await asyncio.wait_for(committed.wait(), 2)
        closer = asyncio.create_task(service.close())
        release.set()
        await asyncio.wait_for(closer, 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            assert job is not None and job.status == JobStatus.QUEUED
            assert attempt.status == JobStatus.FAILED and attempt.result == "interrupted"
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_cancel_before_start(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    cancelled = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    run = service.run

    def cancel_launch() -> None:
        entry = service._running.get(job_id)
        assert entry is not None and service.cancel_job(job_id)
        tasks.append(entry[1])
        cancelled.set()

    def before_run(identifier: int, attempt_id: int):
        asyncio.get_running_loop().call_soon(cancel_launch)
        return run(identifier, attempt_id)

    async def unexpected_check(*args, **kwargs):
        pytest.fail("cancelled healthcheck must not invoke media tools")

    monkeypatch.setattr(service, "run", before_run)
    monkeypatch.setattr(healthcheck, "_decode_healthcheck", unexpected_check)
    service.start()
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        cleanup_tasks = tuple(task for task in service._tasks if task is not service._loop_task)
        await asyncio.wait_for(asyncio.gather(*cleanup_tasks, return_exceptions=True), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            assert job is not None and job.status == JobStatus.CANCELLED
            assert attempt.status == JobStatus.CANCELLED
    finally:
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["suspend", "roots", "parent"])
async def test_handoff_stays_eligible(database, monkeypatch: pytest.MonkeyPatch, change: str):
    media_id, library_id = await seed(database, definition=remux_definition())
    async with database() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        source = Path(media.path)

    async def evaluate(*args):
        if change == "suspend":
            async with database() as session:
                library = await session.get(Library, library_id)
                assert library is not None
                library.suspended = True
                await session.commit()
        return (
            FlowResult(status="matched", plan=FFmpegPlan())
            if change == "roots"
            else FlowResult(status="skipped", reason="controlled decision")
        )

    if change == "roots":
        async with database() as session:
            library = await session.get(Library, library_id)
            assert library is not None
            library.roots = [str(source.parent / "other")]
            await session.commit()
    elif change == "parent":
        outside = source.parent.with_name(f"{source.parent.name}-outside")
        source.parent.rename(outside)
        source.parent.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(enqueue_module, "evaluate", evaluate)
    host = PluginHost([])
    service = FlowEnqueueService(database, host)
    try:
        assert await asyncio.wait_for(service._pass(), 2) == 0
        async with database() as session:
            assert list((await session.scalars(select(Job))).all()) == []
            media = await session.get(MediaFile, media_id)
            assert media is not None and not media.transcode_decided
    finally:
        await asyncio.wait_for(service.close(), 2)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
async def test_unproven_health_waits(database, monkeypatch: pytest.MonkeyPatch):
    media_id, _ = await seed(database, definition=remux_definition())
    async with database() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.health_source = None
        await session.commit()

    async def unexpected_evaluation(*args, **kwargs):
        pytest.fail("unproven health must not authorize automatic evaluation")

    monkeypatch.setattr(enqueue_module, "evaluate", unexpected_evaluation)
    host = PluginHost([])
    service = FlowEnqueueService(database, host)
    try:
        assert await asyncio.wait_for(service._pass(), 2) == 0
        async with database() as session:
            assert list((await session.scalars(select(Job))).all()) == []
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.health_status == HealthStatus.HEALTHY and not media.transcode_decided
    finally:
        await asyncio.wait_for(service.close(), 2)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
async def test_unread_history_protected(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    logs: list[Path] = []

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        logs.append(log_path.path)
        log_path.path.write_bytes(b"healthcheck: retained log\n")
        return healthcheck.HealthcheckResult(healthy=True)

    def unavailable(*args: object) -> bytes:
        raise OSError("fake temporary log read failure")

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr("server.healthlogs.capture_healthcheck_log", unavailable)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
        await session.commit()
    assert await prune_jobs(database, 30, now=datetime(2026, 1, 1, tzinfo=UTC)) == 0
    async with database() as session:
        assert not await delete_eligible_job(session, job_id)
        await session.commit()
        assert await session.get(JobAttempt, claimed[1]) is not None
    await cleanup_workspace(database, workspace)
    assert logs[0].read_bytes() == b"healthcheck: retained log\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["roots", "parent", "suspend"])
async def test_result_rechecks_roots(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    entered, release = asyncio.Event(), asyncio.Event()
    callbacks: list[str] = []

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        entered.set()
        await release.wait()
        log_path.path.write_bytes(b"healthcheck: completed\n")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(
        database,
        tmp_path / "workspace",
        "unused",
        "unused",
        on_healthy=lambda: callbacks.append("healthy"),
    )
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change in {"roots", "suspend"}:
            async with database() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                library = await session.get(Library, job.library_id)
                assert library is not None
                if change == "roots":
                    library.roots = [str(tmp_path / "other")]
                else:
                    library.suspended = True
                await session.commit()
        else:
            outside = tmp_path / "outside"
            root.rename(outside)
            root.symlink_to(outside, target_is_directory=True)
        release.set()
        await asyncio.wait_for(task, 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.status == JobStatus.FAILED
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and media.health_status == HealthStatus.PENDING and media.health_source is None
        assert callbacks == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [HealthStatus.HEALTHY, HealthStatus.CORRUPT])
async def test_failure_preserves_proof(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: HealthStatus):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    health_source = {"path": str(source), **source_identity(source.stat())}
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        media.health_status, media.health_source = prior, health_source
        await session.commit()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: operation failed\n")
        return healthcheck.HealthcheckResult(healthy=False, failed=True, error="controlled process failure")

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert job is not None and job.status == JobStatus.FAILED
        assert attempt is not None and attempt.health is None
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None and media.health_status == prior and media.health_source == health_source


@pytest.mark.asyncio
async def test_scan_invalidates_proof(database, tmp_path: Path):
    media_id, library_id = await seed(database, definition=remux_definition())
    async with database() as session:
        media = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        assert media is not None and library is not None and media.health_source is not None
        source = Path(media.path)
        source.write_bytes(b"changed source")
        report = await scan_library(
            session,
            library,
            config=ScannerConfig(),
            staging=StagingService(database, tmp_path / "workspace", min_free_gb=1),
        )
        assert report.updated == 1
    async with database() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.health_status == HealthStatus.PENDING
        assert media.health_source is None and media.probe_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["probe", "decode"])
@pytest.mark.parametrize("repeat", [False, True])
async def test_log_cancel_drains(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, repeat: bool):
    proc = HeldProcess()
    acquired, checkpoint, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    class CompletedProbe:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"{}", b""

    check = healthcheck._decode_healthcheck

    async def controlled_create(*args, **kwargs):
        if stage == "decode" and args[8] == "probe":
            return CompletedProbe()
        acquired.set()
        await release.wait()
        return proc

    async def observed_check(*args, **kwargs):
        try:
            return await check(*args, **kwargs)
        finally:
            returned.set()

    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", controlled_create)
    monkeypatch.setattr(healthcheck, "_decode_healthcheck", observed_check)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(
        database,
        workspace,
        "decode",
        "probe",
    )
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    task.add_done_callback(lambda finished: service._finished(job_id, finished))
    service._running[job_id] = (claimed[1], task)
    try:
        await asyncio.wait_for(acquired.wait(), 2)
        assert service.cancel_job(job_id)
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        release.set()
        await asyncio.wait_for(proc.reaping.wait(), 2)
        assert proc.killed and proc.returncode is None
        checkpoint.clear()
        if repeat:
            assert service.cancel_job(job_id)
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not returned.is_set() and not task.done()
    finally:
        release.set()
        proc.stdout.feed_eof()
        proc.stderr.feed_eof()
        proc.stopped.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(proc.wait(), 2)
        service._running.clear()
        await asyncio.wait_for(service.close(), 2)
    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert job is not None and job.status in {JobStatus.CANCELLED, JobStatus.FAILED}
        assert attempt is not None and attempt.health is None


@pytest.mark.asyncio
async def test_cancel_registration_isolated(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    first = await service.claim_next()
    assert first is not None
    completed, release, entered, checked = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancel_attempt = service._cancel_attempt

    async def held_cancellation(identifier: int, attempt_id: int) -> None:
        await cancel_attempt(identifier, attempt_id)
        if attempt_id == first[1]:
            completed.set()
            await release.wait()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: retry started\n")
        entered.set()
        await checked.wait()
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(service, "_cancel_attempt", held_cancellation)
    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    old = asyncio.create_task(service.run(*first))
    old.add_done_callback(lambda task: service._finished(job_id, task))
    service._running[job_id] = (first[1], old)
    tasks = [old]
    try:
        assert service.cancel_job(job_id)
        await asyncio.wait_for(completed.wait(), 2)
        async with database() as session:
            request_id = await request_retry(session, job_id)
            assert await retry_job(session, job_id, request_id=request_id) is not None
            await session.commit()
        second = await service.claim_next()
        assert second is not None and second[1] != first[1]
        current = asyncio.create_task(service.run(*second))
        current.add_done_callback(lambda task: service._finished(job_id, task))
        service._running[job_id] = (second[1], current)
        tasks.append(current)
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(old, return_exceptions=True), 2)
        assert service._running.get(job_id) == (second[1], current)
        assert service.cancel_job(job_id)
        await asyncio.wait_for(asyncio.gather(current, return_exceptions=True), 2)
        assert job_id not in service._running
        async with database() as session:
            job = await session.get(Job, job_id)
            attempts = list((await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).all())
            assert job is not None and job.status == JobStatus.CANCELLED
            assert len(attempts) == 2 and all(attempt.status == JobStatus.CANCELLED for attempt in attempts)
    finally:
        release.set()
        checked.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        service._running.clear()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["roots", "flow", "generation"])
@pytest.mark.parametrize("outcome", ["matched", "skipped"])
async def test_handoff_rechecks_settings(database, monkeypatch: pytest.MonkeyPatch, change: str, outcome: str):
    media_id, library_id = await seed(database, definition=remux_definition())

    async def evaluate(*args):
        async with database() as session:
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            assert media is not None and library is not None
            if change == "roots":
                library.roots = [str(Path(media.path).parent / "other")]
            elif change == "flow":
                replacement = Flow(name="replacement", definition=remux_definition().model_dump_json())
                session.add(replacement)
                await session.flush()
                library.flow_id = replacement.id
            else:
                media.first_seen = datetime(2000, 1, 1, tzinfo=UTC)
            await session.commit()
        return (
            FlowResult(status="matched", plan=FFmpegPlan())
            if outcome == "matched"
            else FlowResult(status="skipped", reason="controlled decision")
        )

    monkeypatch.setattr(enqueue_module, "evaluate", evaluate)
    host = PluginHost([])
    service = FlowEnqueueService(database, host)
    try:
        assert await asyncio.wait_for(service._pass(), 2) == 0
        async with database() as session:
            assert list((await session.scalars(select(Job))).all()) == []
            media = await session.get(MediaFile, media_id)
            assert media is not None and not media.transcode_decided
    finally:
        await asyncio.wait_for(service.close(), 2)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["signal", "wait"])
async def test_log_error_survives(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str):
    proc = HeldProcess()
    acquired, release, checkpoint = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def create(*args, **kwargs):
        acquired.set()
        await release.wait()
        return proc

    def cannot_kill() -> None:
        raise PermissionError("fake signal delivery failure")

    async def cannot_wait() -> int:
        proc.reaping.set()
        raise TimeoutError("fake unconfirmed process exit")

    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", create)
    if failure == "signal":
        monkeypatch.setattr(proc, "kill", cannot_kill)
    else:
        monkeypatch.setattr(proc, "wait", cannot_wait)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    task.add_done_callback(lambda finished: service._finished(job_id, finished))
    service._running[job_id] = (claimed[1], task)
    try:
        await asyncio.wait_for(acquired.wait(), 2)
        assert service.cancel_job(job_id)
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert service.cancel_job(job_id)
        release.set()
        await asyncio.wait_for(task, 2)
        assert proc.returncode is None
        await cleanup_workspace(database, workspace)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job is not None and job.status == JobStatus.FAILED
            assert attempt is not None and attempt.result == "interrupted" and attempt.health is None
            assert await session.scalar(recovery_jobs().where(Job.id == job_id)) == job_id
            assert attempt.log_path is not None and (service.log_dir / attempt.log_path).exists()
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and media.health_status == HealthStatus.PENDING and media.health_source is None
    finally:
        release.set()
        proc.stopped.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        service._running.clear()
        await asyncio.wait_for(service.close(), 2)
