from __future__ import annotations

import asyncio
import os
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from common import healthcheck as decode_healthcheck

pytestmark = pytest.mark.usefixtures("local_storage")
from common.media.probe import ProbeData
from common.plan import FFmpegPlan, SubtitleBurn, VideoTarget
from common.enums import JobStatus
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from server import healthcheck, workspace
from server.healthlogs import (
    DiagnosticLog,
    cleanup_healthcheck_log,
    create_healthcheck_log,
    remove_healthcheck_log,
    snapshot_healthcheck_log,
)
from server.retention import delete_eligible_job
from server.flows.engine import FlowResult
from server.fs.source import capture_source
from server.healthcheck import HealthcheckResult, HealthcheckService
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile
from server.nodehub.hub import NodeHub
from server.queue.store import requeue, retry_job
from server.retention import prune_jobs
from server.scanner.scan import _prune_missing
from server.staging import StagingService
from server.events import EventBus
from server.workspaces import WorkspaceService, job_details
from tests.helpers import request_retry, database_context, plugin_host
from tests.test_server_healthcheck import Proc
from tests.test_server_healthcheck_service import add_job
from tests.test_staging import T0, add_job as add_staging_job


def test_subtitle_accepts_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    subtitle = tmp_path / "movie.srt"
    subtitle.write_bytes(b"subtitle")
    root = tmp_path / "workspace"
    captured = capture_source(source)
    workspace._stage_source(root, 1, source, expected_source=captured)
    relative = workspace._stage_subtitle(root, 1, source, subtitle.name)
    staged = root / relative
    linked = root / "retained.srt"
    linked.hardlink_to(staged)

    assert workspace.staged_subtitle(root, 1, subtitle.name) == relative
    assert staged.read_bytes() == linked.read_bytes() == b"subtitle"
    assert subtitle.read_bytes() == b"subtitle"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["shutdown", "cancel"])
async def test_shutdown_preserves_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    entered, release = asyncio.Event(), asyncio.Event()
    async with database_context("shutdown-unacquired") as factory:
        job_id = await add_job(factory, source)
        root.mkdir()
        log_file = root / "existing.log"
        log_file.write_bytes(b"existing recovery log")
        service = HealthcheckService(factory, root, "unused", "unused")
        run = healthcheck._HealthcheckOperation._run

        async def before_acquisition(operation, identifier: int, attempt_id: int) -> None:
            entered.set()
            await release.wait()
            await run(operation, identifier, attempt_id)

        monkeypatch.setattr(healthcheck._HealthcheckOperation, "_run", before_acquisition)
        service.start()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if stop == "shutdown":
                await asyncio.wait_for(service.close(), 3)
            else:
                task = service._running[job_id][1]
                assert service.cancel_job(job_id)
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        finally:
            release.set()
            await asyncio.wait_for(service.close(), 3)
        async with factory() as session:
            attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
            result = "interrupted" if stop == "shutdown" else "cancelled"
            assert attempt is not None and attempt.result == result
        assert log_file.is_file(), "stopping a healthcheck removed an attempt directory it never acquired"
        assert log_file.read_bytes() == b"existing recovery log"
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_scanner_cleanup_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    entered, cancellation_seen = asyncio.Event(), asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    remove = workspace._remove_job
    to_thread, wait = asyncio.to_thread, asyncio.wait
    unshielded = False

    def blocked(folder: Path, identifier: int) -> None:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5)
            remove(folder, identifier)
        finally:
            finished.set()

    async def tracked_thread(operation, *args, **kwargs):
        nonlocal unshielded
        try:
            return await to_thread(operation, *args, **kwargs)
        except asyncio.CancelledError:
            unshielded = True
            cancellation_seen.set()
            raise

    async def tracked_wait(futures, **kwargs):
        try:
            return await wait(futures, **kwargs)
        except asyncio.CancelledError:
            cancellation_seen.set()
            raise

    monkeypatch.setattr("server.staging._remove_job", blocked)
    monkeypatch.setattr(asyncio, "to_thread", tracked_thread)
    monkeypatch.setattr(asyncio, "wait", tracked_wait)
    async with database_context("scanner-cleanup-check") as factory:
        job_id = await add_staging_job(factory, source, status="cancelled")
        workspace._stage_source(root, job_id, source, expected_source=capture_source(source))
        source.unlink()
        marker = root / f"jobs/{job_id}/retry-output"

        async def prune() -> None:
            async with factory() as session:
                library = await session.scalar(select(Library))
                assert library is not None
                library.scan_id = 1
                await session.commit()
                await _prune_missing(
                    session,
                    library,
                    1,
                    staging=StagingService(factory, root, min_free_gb=1),
                    unavailable=set(),
                    excluded=(),
                )
                await session.commit()

        task = asyncio.create_task(prune())
        retried = False
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.wait_for(cancellation_seen.wait(), 3)
            if unshielded:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
                async with factory() as session:
                    request_id = await request_retry(session, job_id)
                    assert await retry_job(session, job_id, request_id=request_id) is not None
                    await session.commit()
                retried = True
                marker.write_bytes(b"retry output")
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            assert await asyncio.to_thread(finished.wait, 3)
        if retried:
            assert marker.is_file(), "cancelled scan cleanup deleted a retry's output"
            assert marker.read_bytes() == b"retry output"


@pytest.mark.asyncio
async def test_continuation_lock_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.flows.engine import FlowContinuation
    from server.nodehub.store import register_node
    from tests.hub_helpers import Clock, FakeConn
    from tests.test_hub import (
        Env,
        file_job,
        finished,
        hello,
        hub as make_hub,
        materialize_offer,
        serve,
    )

    library = tmp_path / "library"
    library.mkdir()
    (library / "movie.srt").write_bytes(b"subtitle")
    root = tmp_path / "workspace"
    locked, continuing, write_allowed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    plan = FFmpegPlan(video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="movie.srt"))

    async def evaluate(*args, **kwargs) -> FlowResult:
        return FlowResult(status="matched", plan=plan)

    monkeypatch.setattr("server.nodehub.hub.evaluate_post", evaluate)
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _root: SimpleNamespace(free=20_000_000_000))
    async with database_context("continuation-lock-order") as factory, plugin_host([]) as host:
        async with factory() as session:
            await register_node(session, "node-a", "tok")
            await session.commit()
        env = Env(factory, library, root, tmp_path / "trash", Clock())
        continuation = FlowContinuation.model_validate(
            {
                "flow": {
                    "entry": "done",
                    "nodes": [{"id": "done", "type": "terminal", "outcome": "done"}],
                    "edges": [],
                },
                "node": "done",
            }
        )
        job_id = await file_job(env, plan=FFmpegPlan(), continuation=continuation)
        hub = make_hub(env, plugin_host=host)
        conn = FakeConn([hello()])
        connection = await serve(hub, conn)
        stage_subtitle = hub.staging.stage_continuation_subtitle
        capacity_warning = hub.staging._capacity_warning
        pending_source = tmp_path / "pending.mkv"
        pending_source.write_bytes(b"pending source")

        async def blocked_capacity(
            session, identifier, source_size, subtitle_size, *, copy_source=True, min_free_gb=None
        ):
            if identifier == pending_id:
                locked.set()
                await write_allowed.wait()
                await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            return await capacity_warning(
                session, identifier, source_size, subtitle_size, copy_source=copy_source, min_free_gb=min_free_gb
            )

        async def continue_subtitle(*args, **kwargs) -> str:
            continuing.set()
            return await stage_subtitle(*args, **kwargs)

        monkeypatch.setattr(hub.staging, "stage_continuation_subtitle", continue_subtitle)
        staging, completion = None, None
        try:
            assert await asyncio.wait_for(hub.dispatch_once(), 3) == 1
            offer = conn.messages("job")[0]["job"]
            materialize_offer(hub, offer)
            pending_id = await add_staging_job(factory, pending_source)
            monkeypatch.setattr(hub.staging, "_capacity_warning", blocked_capacity)
            staging = asyncio.create_task(hub.staging.stage_job(pending_id))
            await asyncio.wait_for(locked.wait(), 3)
            completion = asyncio.create_task(
                hub._handle_finished(
                    hub.entries["node-a"],
                    finished(
                        job_id,
                        offer["attempt_id"],
                        offer["log_path"],
                        "succeeded",
                        output_path=offer["output_path"],
                        output_probe=ProbeData.model_validate(offer["probe"]).model_copy(
                            update={"file_path": offer["output_path"], "size_bytes": 6}
                        ),
                    ),
                )
            )
            await asyncio.wait_for(continuing.wait(), 3)
            write_allowed.set()
            assert await asyncio.wait_for(staging, 3) == 1
            await asyncio.wait_for(completion, 3)
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None and job.status == JobStatus.RUNNING
                stages = list(
                    await session.scalars(
                        select(JobEvent)
                        .where(JobEvent.job_id == job_id, JobEvent.type == "stage")
                        .order_by(JobEvent.id)
                    )
                )
                assert len(stages) == 2 and stages[-1].payload["plan"] == plan.model_dump(mode="json")
        finally:
            write_allowed.set()
            for task in (staging, completion):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*[task for task in (staging, completion) if task is not None], return_exceptions=True), 3
            )
            conn.disconnect()
            await asyncio.wait_for(connection, 3)
            await hub.aclose()
        assert (root / f"jobs/{job_id}/subtitle.srt").read_bytes() == b"subtitle"
        assert (root / f"jobs/{pending_id}/source.mkv").read_bytes() == b"pending source"
        async with factory() as session:
            pending = await session.get(Job, pending_id)
            assert pending is not None and pending.status == JobStatus.QUEUED
        assert (library / "movie.avi").read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["startup", "manual", "history", "scanner", "delete"])
async def test_unacquired_protection(tmp_path: Path, operation: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    async with database_context("unacquired-protection") as factory:
        job_id = await add_job(factory, source)
        service = HealthcheckService(factory, tmp_path / "healthchecks", "unused", "unused")
        staging = StagingService(factory, root, min_free_gb=1)
        claimed = await service.claim_next()
        assert claimed is not None and claimed[0] == job_id
        service.log_dir.mkdir()
        relative = f"{claimed[1]}.log"
        log_file = service.log_dir / relative
        log_file.write_bytes(b"existing log")
        async with factory() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None
            attempt.log_path = relative
            await session.commit()
        await service._finish_attempt(job_id, claimed[1], JobStatus.CANCELLED, "cancelled before acquisition")
        async with factory() as session:
            job = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job is not None
            assert attempt is not None and attempt.log_path == relative and attempt.result == "cancelled"
            job.finished_at = T0 - timedelta(days=2)
            media_id, library_id = job.media_file_id, job.library_id
            owner = (await job_details(session, [job_id]))[job_id]
            assert not owner["eligible"]
            await session.commit()

        if operation == "startup":
            report = await workspace.cleanup_workspace(factory, root)
            assert report == {"removed": 0, "preserved": 0}
        elif operation == "manual":
            outcome = await staging.prune_job(job_id)
            assert outcome["outcome"] == "skipped"
        elif operation == "history":
            assert await prune_jobs(factory, 1, now=T0) == 0
        else:
            async with factory() as session:
                if operation == "scanner":
                    library = await session.get(Library, library_id)
                    assert library is not None
                    library.scan_id = 1
                    await session.commit()
                    assert (
                        await _prune_missing(session, library, 1, staging=staging, unavailable=set(), excluded=()) == 0
                    )
                else:
                    assert not await delete_eligible_job(session, job_id)
                await session.commit()

        async with factory() as session:
            assert await session.get(Job, job_id) is not None
            assert await session.get(JobAttempt, claimed[1]) is not None
            assert await session.get(MediaFile, media_id) is not None
            assert await session.scalar(select(JobEvent.id).where(JobEvent.job_id == job_id)) is not None
        assert log_file.read_bytes() == b"existing log"
        assert source.read_bytes() == b"source"


@pytest.mark.parametrize("entry", ["regular", "symlink", "hardlink", "fifo"])
def test_log_initialization_safe(tmp_path: Path, entry: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    log = tmp_path / "healthchecks/1.log"
    log.parent.mkdir(parents=True)
    if entry == "symlink":
        log.symlink_to(source)
    elif entry == "hardlink":
        os.link(source, log)
    elif entry == "fifo":
        os.mkfifo(log)
    else:
        log.write_bytes(b"previous log")
    with pytest.raises(FileExistsError):
        create_healthcheck_log(log.parent, 1)
    assert source.read_bytes() == b"library media"
    if entry == "regular":
        assert log.read_bytes() == b"previous log"
    elif entry == "symlink":
        assert log.is_symlink()
    elif entry == "hardlink":
        assert log.read_bytes() == b"library media"
    else:
        assert log.exists()


def test_log_missing_parent(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    log = create_healthcheck_log(root, 1)
    log.path.unlink()
    root.rmdir()
    healthcheck._append_log(log, "failure", sync=True)
    assert not root.exists(), "diagnostic failure recreated a removed workspace"


@pytest.mark.parametrize("entry", ["log", "manifest"])
def test_linked_reads_accepted(tmp_path: Path, entry: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "workspace"
    relative = workspace._stage_source(root, 1, source, expected_source=capture_source(source))
    if entry == "log":
        os.link(source, root / "jobs/1/healthcheck.log")
        assert workspace.read_attempt_log(root, "jobs/1/healthcheck.log") == b"library media"
    else:
        os.link(root / "jobs/1/manifest.json", tmp_path / "saved-manifest.json")
        assert workspace.staged_source(root, 1) == relative
    assert source.read_bytes() == b"library media"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initialize", "append"])
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_log_cancel_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, cancellations: int
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    entered, cancelled = asyncio.Event(), asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    append = healthcheck._append_log
    wait = asyncio.wait
    blocked_once = False

    def blocked(path: DiagnosticLog | None, text: str, *, sync: bool = False) -> None:
        nonlocal blocked_once
        selected = "healthcheck: start" in text if phase == "initialize" else "result succeeded" in text
        if selected and not blocked_once:
            blocked_once = True
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5)
                append(path, text, sync=sync)
            finally:
                finished.set()
        else:
            append(path, text, sync=sync)

    async def track_wait(futures, **kwargs):
        try:
            return await wait(futures, **kwargs)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def create(*args: object, **kwargs: object) -> Proc:
        return Proc()

    async def communicate(*args: object) -> tuple[bytes, bytes]:
        return b"{}", b""

    monkeypatch.setattr(healthcheck, "_append_log", blocked)
    monkeypatch.setattr(decode_healthcheck, "_append_log", blocked)
    monkeypatch.setattr(decode_healthcheck, "_communicate", communicate)
    monkeypatch.setattr(decode_healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(asyncio, "wait", track_wait)
    async with database_context("healthcheck-log-drain") as factory:
        job_id = await add_job(factory, source)
        service = HealthcheckService(factory, root, "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        task = asyncio.create_task(service.run(*claimed))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            for _ in range(cancellations):
                cancelled.clear()
                task.cancel()
                await asyncio.wait_for(cancelled.wait(), 3)
                assert not task.done() and not finished.is_set()
                async with factory() as session:
                    job = await session.get(Job, job_id)
                    assert job is not None and job.status == "running"
                assert (root / f"{claimed[1]}.log").is_file()
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            assert await asyncio.to_thread(finished.wait, 3)
        assert isinstance(result[0], asyncio.CancelledError)
        async with factory() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None and attempt.result == "cancelled" and attempt.log_data is not None
            assert b"healthcheck: start" in attempt.log_data
            assert b"cancelled by operator" in attempt.log_data
            if phase == "append":
                assert b"result succeeded" in attempt.log_data
        assert not (root / f"jobs/{job_id}").exists()
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workload", "acquired", "result", "eligible"),
    [
        (None, False, None, True),
        ("transcode", False, "failed", True),
        ("healthcheck", False, "cancelled", False),
        ("healthcheck", True, "cancelled", False),
        (None, True, "interrupted", True),
    ],
)
async def test_completed_cleanup_locks(
    tmp_path: Path, workload: str | None, acquired: bool, result: str | None, eligible: bool
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    async with database_context("completed-cleanup-job_details") as factory:
        job_id = await add_staging_job(factory, source, status="failed")
        relative = workspace.create_attempt_layout(root, job_id, 1, ".mkv")["log"]
        log_file = root / relative
        log_file.write_bytes(b"attempt log")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.attempts = 1
            if workload == "healthcheck":
                job.kind = "healthcheck"
            attempt = JobAttempt(
                job_id=job_id,
                attempt_number=1,
                status="failed",
                workload=workload,
                result=result,
                log_path=relative if acquired else None,
                finished_at=T0,
            )
            session.add(attempt)
            await session.commit()
        assert await StagingService(factory, root, min_free_gb=1).prune_attempt(attempt.id) is eligible
        if eligible:
            assert not log_file.exists()
        else:
            assert log_file.read_bytes() == b"attempt log"
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["symlink", "hardlink"])
async def test_healthcheck_log_links(tmp_path: Path, link: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "workspace"
    async with database_context("healthcheck-log-independent") as factory:
        job_id = await add_job(factory, source)
        service = HealthcheckService(factory, root, "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        owned = create_healthcheck_log(root, claimed[1])
        relative, log = owned.path.name, owned.path
        async with factory() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None
            attempt.log_path = relative
            session.add(
                JobEvent(
                    job_id=job_id,
                    attempt_id=attempt.id,
                    type="healthcheck_log_created",
                    payload={"log_path": relative},
                )
            )
            await session.commit()
        retained = root / "retained.log"
        if link == "symlink":
            log.unlink()
            log.symlink_to(source)
        else:
            retained.hardlink_to(log)
        await asyncio.wait_for(service._finish_attempt(job_id, claimed[1], JobStatus.CANCELLED, "cancelled"), 3)
        assert source.read_bytes() == b"library media", "healthcheck log writes modified linked library media"
        if link == "hardlink":
            expected = b"healthcheck: cancelled by operator\nhealthcheck: result cancelled\n"
            assert retained.read_bytes() == expected
            async with factory() as session:
                attempt = await session.get(JobAttempt, claimed[1])
                assert attempt is not None and attempt.log_data == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_shutdown_cleanup_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellations: int) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    entered, cancelled = asyncio.Event(), asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    remove = remove_healthcheck_log
    wait = asyncio.wait

    def blocked(folder: Path, relative: str) -> None:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5)
            remove(folder, relative)
        finally:
            finished.set()

    async def track_wait(futures, **kwargs):
        try:
            return await wait(futures, **kwargs)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("server.healthlogs.remove_healthcheck_log", blocked)
    monkeypatch.setattr(asyncio, "wait", track_wait)
    async with database_context("healthcheck-cleanup-check") as factory:
        job_id = await add_job(factory, source)
        other_source = tmp_path / "other.mkv"
        other_source.write_bytes(b"independent source")
        other_job = await add_job(factory, other_source)
        service = HealthcheckService(factory, root, "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        async with factory() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None
            log = await service._create_log(session, job_id, attempt)
            relative, log_file = log.path.name, log.path
            log.append("created log")
            await snapshot_healthcheck_log(session, root, attempt)
            assert await requeue(session, job_id) is not None
            await session.commit()

        async def cleanup() -> None:
            async with factory() as session:
                await cleanup_healthcheck_log(session, root, claimed[1])
                await session.commit()

        task = asyncio.create_task(cleanup())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            for _ in range(cancellations):
                cancelled.clear()
                task.cancel()
                await asyncio.wait_for(cancelled.wait(), 3)
                assert not task.done()
                async with factory() as writer:
                    await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    await writer.execute(update(Job).where(Job.id == other_job).values(priority=7))
                    await writer.commit()
                for statement in (
                    update(Job).where(Job.id == job_id).values(priority=1),
                    update(JobAttempt).where(JobAttempt.id == claimed[1]).values(log_data=b"changed job_details"),
                ):
                    async with factory() as writer:
                        await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                        with pytest.raises(OperationalError) as raised:
                            await writer.execute(statement)
                        assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            assert await asyncio.to_thread(finished.wait, 3)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not log_file.exists()
        async with factory() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None and attempt.result == "interrupted" and attempt.log_path == relative
            await session.execute(update(Job).where(Job.id == job_id).values(status=Job.status))
            await session.commit()
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_unacquired_retry_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"

    async def check(source_path, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress) -> HealthcheckResult:
        log_path.path.write_bytes(b"completed retry")
        return HealthcheckResult(healthy=True)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
    async with database_context("unacquired-retry") as factory:
        job_id = await add_job(factory, source)
        service = HealthcheckService(factory, tmp_path / "healthchecks", "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        service.log_dir.mkdir()
        relative = f"{claimed[1]}.log"
        log_file = service.log_dir / relative
        log_file.write_bytes(b"existing log")
        async with factory() as session:
            attempt = await session.get(JobAttempt, claimed[1])
            assert attempt is not None
            attempt.log_path = relative
            await session.commit()
        await service._finish_attempt(job_id, claimed[1], JobStatus.CANCELLED, "cancelled before acquisition")
        async with factory() as session:
            request_id = await request_retry(session, job_id)
            assert await retry_job(session, job_id, request_id=request_id) is not None
            await session.commit()
        retry = await service.claim_next()
        assert retry is not None and retry[0] == job_id and retry[1] != claimed[1]
        await asyncio.wait_for(service.run(*retry), 3)
        async with factory() as session:
            job = await session.get(Job, job_id)
            old = await session.get(JobAttempt, claimed[1])
            new = await session.get(JobAttempt, retry[1])
            assert job is not None and job.status == "succeeded" and job.attempts == 2
            assert old is not None and old.result == "cancelled" and old.log_path == relative
            assert new is not None and new.log_path is not None and new.log_data == b"completed retry"
            job.finished_at = T0 - timedelta(days=2)
            await session.commit()
        assert not (root / f"jobs/{job_id}/attempts/2").exists()
        await workspace.cleanup_workspace(factory, root)
        assert (await StagingService(factory, root, min_free_gb=1).prune_job(job_id))["outcome"] == "skipped"
        assert await prune_jobs(factory, 1, now=T0) == 0
        assert log_file.read_bytes() == b"existing log"
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_empty_healthcheck_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"

    async def check(source_path, ffmpeg_bin, ffprobe_bin, log_path, *, hwaccel, on_progress) -> HealthcheckResult:
        if log_path is not None:
            log_path.append("completed retry")
        return HealthcheckResult(healthy=True)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
    async with database_context("empty-healthcheck") as factory:
        job_id = await add_job(factory, source)
        staging = StagingService(factory, root, min_free_gb=1)
        service = HealthcheckService(factory, tmp_path / "healthchecks", "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        async with factory() as session:
            assert await requeue(session, job_id, now=T0) is not None
            await session.commit()
        retry = await service.claim_next()
        assert retry is not None and retry[1] != claimed[1]
        await asyncio.wait_for(service.run(*retry), 3)
        folder = root / "jobs" / str(job_id)
        await workspace.cleanup_workspace(factory, root)
        assert not folder.exists()
        async with factory() as session:
            old = await session.get(JobAttempt, claimed[1])
            new = await session.get(JobAttempt, retry[1])
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "succeeded"
            assert old is not None and old.result == "interrupted" and old.log_path is None
            assert new is not None and new.log_data == b"completed retry"
        assert source.read_bytes() == b"source"
