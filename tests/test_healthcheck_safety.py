from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import common.healthcheck as decode_healthcheck
import server.healthcheck as healthcheck
import server.healthlogs as healthlogs
from server.healthlogs import DiagnosticLog, create_healthcheck_log, recover_healthcheck_logs
from common.plan.models import FFmpegPlan
from common.enums import HealthStatus, JobStatus
from server.settings import ScannerConfig
from server.flows.definition import FlowDefinition, TerminalNode
from server.flows.engine import FlowResult
from server.models import Flow, Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.queue.enqueue import FlowEnqueueService
from server.queue.store import enqueue_healthcheck_job, requeue_startup, retry_job
from server.plugins.host import PluginHost
from server.scanner.scan import scan_library
from server.staging import StagingService
from server.fs.library import LibraryFS
from common.fs.identity import source_identity
from server.workspace import cleanup_workspace, recovery_jobs, snapshot_interrupted_logs
from tests.helpers import request_retry, database_context
from tests.test_server_healthcheck_service import add_job
from tests.test_server_healthcheck import Proc


@pytest_asyncio.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("healthcheck-safety") as factory:
        yield factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "action"),
    [("source", action) for action in ("dispatch", "library", "catalog", "cancel")]
    + [("log", action) for action in ("dispatch", "library", "cancel")],
)
async def test_log_creation_checks(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage, action):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    other = tmp_path / "other.mkv"
    other.write_bytes(b"other")
    other_id = await add_job(database, other)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    stat = Path.stat
    acquire_log = healthcheck.create_healthcheck_log
    decoded = []

    def blocked_source(path, *args, **kwargs):
        if path == source and stage == "source" and kwargs.get("follow_symlinks", True):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("Source acquisition was not released.")
        return stat(path, *args, **kwargs)

    def blocked_log(root, attempt_id):
        if stage == "log":
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("Log acquisition was not released.")
        return acquire_log(root, attempt_id)

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        decoded.append(source)
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(Path, "stat", blocked_source)
    monkeypatch.setattr(healthcheck, "create_healthcheck_log", blocked_log)
    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None and claimed[0] == job_id
    task = asyncio.create_task(service.run(*claimed))
    service._running[job_id] = (claimed[1], task)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if action == "dispatch":
            second = await asyncio.wait_for(service.claim_next(), 2)
            assert second is not None and second[0] == other_id
        elif action in {"library", "catalog"}:

            async def change_owner():
                async with database() as session:
                    job = await session.get(Job, job_id)
                    assert job is not None
                    if action == "library":
                        await session.execute(
                            update(Library).where(Library.id == job.library_id).values(suspended=True)
                        )
                    else:
                        await session.execute(
                            update(MediaFile).where(MediaFile.id == job.media_file_id).values(size=MediaFile.size + 1)
                        )
                    await session.commit()

            await asyncio.wait_for(change_owner(), 2)
        else:
            assert service.cancel_job(job_id)
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            assert job.status == (
                JobStatus.SUCCEEDED
                if action == "dispatch"
                else JobStatus.CANCELLED
                if action == "cancel"
                else JobStatus.FAILED
            )
        assert bool(decoded) == (action == "dispatch")
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        service._running.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [HealthStatus.PENDING, HealthStatus.HEALTHY])
@pytest.mark.parametrize("failure", ["spawn", "timeout", "probe", "decode"])
async def test_failure_no_result(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior, failure):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await session.execute(update(MediaFile).where(MediaFile.id == job.media_file_id).values(health_status=prior))
        await session.commit()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_text("healthcheck: execution failed\n", encoding="utf-8")
        if failure == "spawn":
            raise FileNotFoundError("fake decoder unavailable")
        return healthcheck.HealthcheckResult(healthy=False, failed=True, error=f"{failure} failed")

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.FAILED
        media = await session.get(MediaFile, job.media_file_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert attempt is not None and attempt.log_data
        assert media is not None and media.health_status == prior
        assert attempt.health is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", ["parent", "leaf", "roots"])
async def test_rejects_unconfined_source(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    source = library_dir / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    if redirect == "parent":
        outside = tmp_path / "outside"
        library_dir.rename(outside)
        library_dir.symlink_to(outside, target_is_directory=True)
    elif redirect == "leaf":
        outside = tmp_path / "outside.mkv"
        source.rename(outside)
        source.symlink_to(outside)
    else:
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            await session.execute(
                update(Library).where(Library.id == job.library_id).values(roots=[str(tmp_path / "new")])
            )
            await session.commit()
    calls: list[str] = []

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        calls.append(source)
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    assert calls == []
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.FAILED
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None and media.health_status == HealthStatus.PENDING


@pytest.mark.asyncio
async def test_repeated_cancel_finishes(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    entered, finishing = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    append = healthcheck._append_log

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_text("healthcheck: decoder started\n", encoding="utf-8")
        entered.set()
        await asyncio.Event().wait()

    def blocked_log(path: DiagnosticLog | None, text: str, *, sync: bool = False) -> None:
        if "cancelled by operator" in text:
            loop.call_soon_threadsafe(finishing.set)
            if not release.wait(2):
                raise TimeoutError("test log writer was not released")
        append(path, text, sync=sync)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr(healthcheck, "_append_log", blocked_log)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    service._running[job_id] = (claimed[1], task)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert service.cancel_job(job_id)
        await asyncio.wait_for(finishing.wait(), 2)
        assert service.cancel_job(job_id)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job is not None and job.status == JobStatus.CANCELLED
            assert attempt is not None and attempt.status == JobStatus.CANCELLED
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        service._running.clear()


class HeldProcess:
    def __init__(self, *, communication_error: bool = False) -> None:
        self.returncode: int | None = None
        self.communicating = asyncio.Event()
        self.reaping = asyncio.Event()
        self.stopped = asyncio.Event()
        self.killed = False
        self.communication_error = communication_error
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicating.set()
        if self.communication_error:
            raise OSError("fake pipe read failed")
        await self.stopped.wait()
        return b"{}", b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.reaping.set()
        await self.stopped.wait()
        self.returncode = -9
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("stage", ["probe", "decode", "fallback"])
async def test_cancel_drains_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeat: bool, stage: str):
    proc = HeldProcess()
    calls = []

    class CompletedProbe:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'{"streams":[{"index":0,"codec_type":"video","codec_name":"h264"}]}', b""

    async def create(*args, **kwargs):
        calls.append(args)
        if stage != "probe" and args[8] == "probe":
            return CompletedProbe()
        if stage == "fallback" and "-hwaccel:0" in args:
            return Proc(69)
        if stage != "probe":
            proc.communicating.set()
        return proc

    monkeypatch.setattr(decode_healthcheck, "probe_capabilities", AsyncMock())
    monkeypatch.setattr(
        decode_healthcheck, "verify_decoders", AsyncMock(return_value=({"h264": ("qsv", "h264_qsv")}, []))
    )
    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", create)
    task = asyncio.create_task(
        healthcheck._decode_healthcheck(
            "movie",
            "decode",
            "probe",
            create_healthcheck_log(tmp_path, 1) if stage != "probe" else None,
        )
    )
    try:
        await asyncio.wait_for(proc.communicating.wait(), 2)
        task.cancel()
        await asyncio.wait_for(proc.reaping.wait(), 2)
        if repeat:
            task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done()
        proc.stopped.set()
        results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert isinstance(results[0], asyncio.CancelledError)
        assert proc.killed and proc.returncode == -9
        assert len(calls) == {"probe": 1, "decode": 2, "fallback": 3}[stage]
    finally:
        proc.stopped.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["probe", "decode"])
async def test_pipe_failure_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str):
    proc = HeldProcess(communication_error=True)

    class CompletedProbe:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"{}", b""

    async def create(*args, **kwargs):
        if stage == "decode" and args[8] == "probe":
            return CompletedProbe()
        if stage == "decode":
            proc.communicating.set()
            proc.stderr.set_exception(OSError("fake decoder pipe read failed"))
        return proc

    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", create)
    task = asyncio.create_task(
        healthcheck._decode_healthcheck(
            "movie",
            "decode",
            "probe",
            create_healthcheck_log(tmp_path, 1) if stage == "decode" else None,
        )
    )
    try:
        await asyncio.wait_for(proc.communicating.wait(), 2)
        proc.stopped.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert proc.killed and proc.returncode == -9
    finally:
        proc.stopped.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_shutdown_claim_gap(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    publishing = asyncio.Event()

    async def publish(job_id: int, operation: str) -> None:
        if operation == "claim":
            publishing.set()
            await asyncio.Event().wait()

    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    monkeypatch.setattr(service, "_publish_transition", publish)
    service.start()
    try:
        await asyncio.wait_for(publishing.wait(), 2)
        await asyncio.wait_for(service.close(), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            assert job is not None and job.status == JobStatus.QUEUED
            assert attempt.status == JobStatus.FAILED and attempt.result == "interrupted"
        assert not service._tasks and not service._running
    finally:
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_stale_start_rejected(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    old = await service.claim_next()
    assert old is not None
    async with database() as session:
        await requeue_startup(session, utcnow())
        await session.commit()
    current = await service.claim_next()
    assert current is not None and current[1] != old[1]
    calls: list[str] = []

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        calls.append(source)
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    await asyncio.wait_for(service.run(*old), 2)
    assert calls == []
    async with database() as session:
        job = await session.get(Job, job_id)
        old_attempt = await session.get(JobAttempt, old[1])
        current_attempt = await session.get(JobAttempt, current[1])
        assert job is not None and job.status == JobStatus.CLAIMED and job.attempts == 2
        assert old_attempt is not None and old_attempt.status == JobStatus.FAILED
        assert current_attempt is not None and current_attempt.status == JobStatus.CLAIMED


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["complete", "cancel", "shutdown"])
async def test_unread_log_retained(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion: str):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    logs: list[Path] = []

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        logs.append(log_path.path)
        log_path.path.write_bytes(b"healthcheck: decoder log\n")
        if completion != "complete":
            raise asyncio.CancelledError
        return healthcheck.HealthcheckResult(healthy=False, error_count=1, samples=["decode issue"])

    def unavailable(*args: object) -> bytes:
        raise OSError("fake temporary log read failure")

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr("server.healthlogs.capture_healthcheck_log", unavailable)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    service._closed = completion == "shutdown"
    claimed = await service.claim_next()
    assert claimed is not None
    if completion == "complete":
        await asyncio.wait_for(service.run(*claimed), 2)
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(service.run(*claimed), 2)
    await cleanup_workspace(database, tmp_path / "workspace")
    async with database() as session:
        attempt = await session.get(JobAttempt, claimed[1])
        assert attempt is not None
        assert attempt.log_data == b"healthcheck: decoder log\n" or logs[0].exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_fd")
async def test_unchanged_scan_result(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps({"streams": [], "format": {}}))
        session.add(probe)
        await session.flush()
        media.probe_id = probe.id
        media.video_codec = "h264"
        media.source_identity = source_identity(source.stat())
        await session.execute(update(Library).where(Library.id == job.library_id).values(healthcheck_enabled=True))
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        entered.set()
        await release.wait()
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with database() as session:
            library = await session.get(Library, job.library_id)
            assert library is not None
            report = await asyncio.wait_for(
                scan_library(
                    session,
                    library,
                    config=ScannerConfig(),
                    staging=StagingService(database, tmp_path / "workspace", min_free_gb=1),
                ),
                2,
            )
            assert report.unchanged == 1 and report.updated == 0 and not report.errors
        release.set()
        await asyncio.wait_for(task, 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job is not None and job.status == JobStatus.SUCCEEDED and not job.error
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and media.health_status == HealthStatus.HEALTHY
            assert media.health_source == {"path": str(source), **source_identity(source.stat())}
            assert attempt is not None and attempt.health is not None and attempt.health["healthy"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_rescan_preserves_followup(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    source = library_dir / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        await session.execute(update(Library).values(healthcheck_enabled=True))
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        entered.set()
        await release.wait()
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    async def probe(*args, **kwargs):
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}], "format": {}}

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        source.write_bytes(b"replacement generation")
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            library = await session.get(Library, job.library_id)
            assert library is not None
            await scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=StagingService(database, tmp_path / "workspace", min_free_gb=1),
            )
            library_id, media_id = library.id, job.media_file_id
        release.set()
        await asyncio.wait_for(task, 2)
        from tests.helpers import probe_media

        await probe_media(
            LibraryFS(
                library_id,
                sessionmaker=database,
                ffprobe_bin="unused",
                mkvpropedit_bin="unused",
                workspace_dir=tmp_path / "workspace",
            ),
            media_id,
        )
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.status == JobStatus.FAILED
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and media.health_status == HealthStatus.PENDING and media.probe_id is not None
            library = await session.get(Library, job.library_id)
            assert library is not None
            await scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=StagingService(database, tmp_path / "workspace", min_free_gb=1),
            )
            await session.refresh(media)
            assert media.probe_id is not None
        followup = await service.claim_next()
        assert followup is not None and followup[0] != job_id
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("acquired", [False, True])
async def test_startup_preserves_log(database, tmp_path: Path, acquired: bool):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    log_rel = ""
    async with database() as session:
        attempt = await session.get(JobAttempt, claimed[1])
        assert attempt is not None
        if acquired:
            log = create_healthcheck_log(service.log_dir, attempt.id)
            attempt.log_path = log_rel = log.path.name
            log.append("healthcheck: interrupted log\n")
            session.add(
                JobEvent(
                    job_id=job_id,
                    attempt_id=attempt.id,
                    type="healthcheck_log_created",
                    payload={"log_path": log_rel},
                )
            )
        await session.commit()
    await recover_healthcheck_logs(database, service.log_dir, startup=True)
    async with database() as session:
        await snapshot_interrupted_logs(session, workspace)
        await session.commit()
        await requeue_startup(session, utcnow())
        await session.commit()
    await cleanup_workspace(database, workspace)
    async with database() as session:
        attempt = await session.get(JobAttempt, claimed[1])
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.QUEUED
        assert attempt is not None and attempt.result == "interrupted"
        if acquired:
            assert attempt.log_data == b"healthcheck: interrupted log\n"
    if acquired:
        assert (service.log_dir / log_rel).read_bytes() == b"healthcheck: interrupted log\n"
    else:
        assert not service.log_dir.exists()


@pytest.mark.asyncio
async def test_log_rejects_symlink(tmp_path: Path):
    unrelated = tmp_path / "unrelated.mkv"
    unrelated.write_bytes(b"personal media")
    owned = create_healthcheck_log(tmp_path / "healthchecks", 1)
    log = owned.path
    log.unlink()
    log.symlink_to(unrelated)
    healthcheck._append_log(owned, "healthcheck: start\n")
    healthcheck._append_log(owned, "healthcheck: end\n", sync=True)
    assert unrelated.read_bytes() == b"personal media"


def test_log_accepts_hardlink(tmp_path: Path) -> None:
    owned = create_healthcheck_log(tmp_path / "healthchecks", 1)
    retained = owned.path.with_name("retained.log")
    retained.hardlink_to(owned.path)
    healthcheck._append_log(owned, "healthcheck: start\n")
    healthcheck._append_log(owned, "healthcheck: end\n", sync=True)
    assert retained.read_text() == owned.path.read_text() == "healthcheck: start\nhealthcheck: end\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "result"])
async def test_handoff_checks_result(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        library = await session.get(Library, job.library_id)
        assert media is not None and library is not None
        definition = FlowDefinition(entry="done", nodes=[TerminalNode(id="done", outcome="done")], edges=[])
        flow = Flow(name="handoff", definition=definition.model_dump_json())
        probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps({"streams": [], "format": {}}))
        session.add_all([flow, probe])
        await session.flush()
        library.flow_id, media.probe_id = flow.id, probe.id
        media.source_identity = source_identity(source.stat())
        await session.commit()

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    async def corrupt(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_text("healthcheck: decoder issue\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=False, error_count=1, samples=["decoder issue"])

    async def evaluate(*args):
        if change == "result":
            async with database() as session:
                original = await session.get(Job, job_id)
                assert original is not None
                await enqueue_healthcheck_job(session, original.media_file_id, original.library_id)
                await session.commit()
            monkeypatch.setattr(healthcheck, "_decode_healthcheck", corrupt)
            recheck = await service.claim_next()
            assert recheck is not None
            await service.run(*recheck)
        return FlowResult(status="matched", plan=FFmpegPlan())

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr("server.queue.enqueue.evaluate", evaluate)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    host = PluginHost([])
    enqueue = FlowEnqueueService(database, host)
    try:
        assert await asyncio.wait_for(enqueue._pass(), 2) == (1 if change == "none" else 0)
        async with database() as session:
            jobs = list((await session.scalars(select(Job).where(Job.kind == "transcode"))).all())
            assert len(jobs) == (1 if change == "none" else 0)
    finally:
        await asyncio.wait_for(enqueue.close(), 2)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
async def test_retry_keeps_attempts(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_text(f"attempt {log_path.path.stem}\n", encoding="utf-8")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    first = await service.claim_next()
    assert first is not None
    await asyncio.wait_for(service.run(*first), 2)
    async with database() as session:
        request_id = await request_retry(session, job_id)
        assert await retry_job(session, job_id, request_id=request_id) is not None
        await session.commit()
    second = await service.claim_next()
    assert second is not None and second[1] != first[1]
    await asyncio.wait_for(service.run(*second), 2)
    async with database() as session:
        attempts = list((await session.scalars(select(JobAttempt).order_by(JobAttempt.attempt_number))).all())
        assert [attempt.status for attempt in attempts] == [JobStatus.SUCCEEDED, JobStatus.SUCCEEDED]
        assert [attempt.log_data for attempt in attempts] == [b"attempt 1\n", b"attempt 2\n"]


@pytest.mark.asyncio
async def test_timeout_waits_worker(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class UnreapedProcess(HeldProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            raise TimeoutError

        async def wait(self) -> int:
            raise TimeoutError

    proc = UnreapedProcess()

    async def create(*args, **kwargs):
        return proc

    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", create)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    async with database() as session:
        attempt = await session.get(JobAttempt, claimed[1])
        assert attempt is not None
        protected = await session.scalar(recovery_jobs().where(Job.id == job_id))
        assert proc.returncode is not None or protected == job_id
        assert proc.returncode is not None or (service.log_dir / (attempt.log_path or "")).exists()


@pytest.mark.asyncio
async def test_concurrent_shutdown_finishes(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import server.workspace as workspace_module

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    entered, reading, read_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    read_log = healthlogs.capture_healthcheck_log

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: running log\n")
        entered.set()
        await asyncio.Event().wait()

    def held_read(root: Path, relative: str):
        loop.call_soon_threadsafe(reading.set)
        try:
            if not release.wait(2):
                raise TimeoutError("test log reader was not released")
            return read_log(root, relative)
        finally:
            loop.call_soon_threadsafe(read_done.set)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr(healthlogs, "capture_healthcheck_log", held_read)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    service.start()
    closers: list[asyncio.Task[None]] = []
    try:
        await asyncio.wait_for(entered.wait(), 2)
        closers.append(asyncio.create_task(service.close()))
        await asyncio.wait_for(reading.wait(), 2)
        closers.append(asyncio.create_task(service.close()))
        checkpoint = asyncio.Event()
        loop.call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(*closers), 2)
        await asyncio.wait_for(read_done.wait(), 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            assert job is not None and job.status == JobStatus.QUEUED
            assert attempt.result == "interrupted" and attempt.log_data == b"healthcheck: running log\n"
            assert attempt.log_path is not None and not (service.log_dir / attempt.log_path).exists()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*closers, return_exceptions=True), 2)
        if reading.is_set():
            await asyncio.wait_for(read_done.wait(), 2)
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_stale_result_ignored(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    entered, release = asyncio.Event(), asyncio.Event()
    callbacks: list[str] = []

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: first attempt\n")
        entered.set()
        await release.wait()
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    workspace = tmp_path / "workspace"
    service = healthcheck.HealthcheckService(
        database,
        workspace,
        "unused",
        "unused",
        on_healthy=lambda: callbacks.append("healthy"),
    )
    first = await service.claim_next()
    assert first is not None
    task = asyncio.create_task(service.run(*first))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await recover_healthcheck_logs(database, service.log_dir, startup=True)
        async with database() as session:
            await snapshot_interrupted_logs(session, workspace)
            await session.commit()
            await requeue_startup(session, utcnow())
            await session.commit()
        second = await service.claim_next()
        assert second is not None and second[1] != first[1]
        release.set()
        await asyncio.wait_for(task, 2)
        async with database() as session:
            job = await session.get(Job, job_id)
            old_attempt = await session.get(JobAttempt, first[1])
            current_attempt = await session.get(JobAttempt, second[1])
            assert job is not None and job.status == JobStatus.CLAIMED and job.attempts == 2
            assert old_attempt is not None and old_attempt.result == "interrupted"
            assert old_attempt.log_data == b"healthcheck: first attempt\n"
            assert current_attempt is not None and current_attempt.status == JobStatus.CLAIMED
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and media.health_status == HealthStatus.PENDING
        assert callbacks == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "worker"])
async def test_diagnostics_finish(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"checked generation")
    job_id = await add_job(database, source)
    reading, read_done = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    reader = healthlogs.capture_healthcheck_log

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: completed\n")
        return healthcheck.HealthcheckResult(healthy=True)

    def held_read(root: Path, relative: str):
        loop.call_soon_threadsafe(reading.set)
        try:
            if not release.wait(2):
                raise TimeoutError("test reader was not released")
            if failure == "worker" and not read_done.is_set():
                raise RuntimeError("diagnostic worker failed")
            return reader(root, relative)
        finally:
            loop.call_soon_threadsafe(read_done.set)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr(healthlogs, "capture_healthcheck_log", held_read)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    try:
        await asyncio.wait_for(reading.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done()
        release.set()
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(task, 2)
        assert read_done.is_set()
        async with database() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            assert job.status == (JobStatus.CANCELLED if failure == "cancel" else JobStatus.FAILED)
            if failure == "worker":
                assert job.error and "diagnostic worker failed" in job.error
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(read_done.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["attempt", "job"])
async def test_cleanup_error_reported(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    staging = StagingService(database, workspace, min_free_gb=1)

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: result succeeded\n")
        return healthcheck.HealthcheckResult(healthy=True)

    def refused_log(*args: object) -> None:
        raise OSError(f"{failure} cleanup failed")

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    monkeypatch.setattr(healthlogs, "remove_healthcheck_log", refused_log)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    async with database() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert job is not None and job.status == JobStatus.SUCCEEDED
        assert job.warning and f"{failure} cleanup failed" in job.warning
        assert attempt is not None and attempt.log_data == b"healthcheck: result succeeded\n"
        events = list((await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))).all())
        assert any(event.type == "cleanup_failed" and event.payload["warning"] == job.warning for event in events)


@pytest.mark.asyncio
async def test_shared_staging_protects(database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"movie")
    job_id = await add_job(database, source)
    workspace = tmp_path / "workspace"
    staging = StagingService(database, workspace, min_free_gb=1)
    staging.active_jobs.add(job_id)

    async def check(source, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress):
        log_path.path.write_bytes(b"healthcheck: result succeeded\n")
        return healthcheck.HealthcheckResult(healthy=True)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    service = healthcheck.HealthcheckService(database, tmp_path / "healthchecks", "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 2)
    async with database() as session:
        attempt = await session.get(JobAttempt, claimed[1])
        assert attempt is not None and attempt.log_path
        assert attempt.log_data == b"healthcheck: result succeeded\n"
        assert not (service.log_dir / attempt.log_path).exists()
        assert not workspace.exists()
    staging.active_jobs.clear()
