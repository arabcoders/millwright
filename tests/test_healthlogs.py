from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from server.healthcheck import HealthcheckService
from server.healthcheck import HealthcheckResult
from server.healthlogs import (
    capture_healthcheck_log,
    cleanup_healthcheck_log,
    create_healthcheck_log,
    recover_healthcheck_logs,
    remove_healthcheck_log,
    snapshot_healthcheck_log,
)
from server.models import Job, JobAttempt, JobEvent
from server.retention import prune_jobs
from server.workspace import recovery_jobs
from tests.helpers import database_context
from tests.test_server_healthcheck_service import add_job
from tests.test_staging import T0


async def seed(factory, root: Path, source: Path) -> tuple[int, int]:
    job_id = await add_job(factory, source)
    service = HealthcheckService(factory, root, "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    async with factory() as session:
        attempt = await session.get(JobAttempt, claimed[1])
        assert attempt is not None
        log = await service._create_log(session, job_id, attempt)
        log.append("complete diagnostics\n", sync=True)
        attempt.status, attempt.result, attempt.finished_at = "succeeded", "succeeded", T0
        job = await session.get(Job, job_id)
        assert job is not None
        job.status, job.finished_at = "succeeded", T0
        await session.commit()
    return claimed


@pytest.mark.asyncio
async def test_decode_history_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "healthchecks"

    async def check(source, ffmpeg, ffprobe, log, *, hwaccel, on_progress):
        assert log is not None
        log.append("healthcheck: result succeeded\n", sync=True)
        return HealthcheckResult(healthy=True)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
    async with database_context("decode-history") as factory:
        job_id = await add_job(factory, source)
        service = HealthcheckService(factory, root, "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        await asyncio.wait_for(service.run(*claimed), 3)
        async with factory() as session:
            job = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job is not None and job.status == "succeeded"
            assert attempt is not None and attempt.log_path is not None
            assert attempt.log_data == b"healthcheck: result succeeded\n"
            assert await session.scalar(recovery_jobs().where(Job.id == job_id)) is None
        assert not list(root.iterdir()) and not (tmp_path / "workspace").exists()
        assert source.read_bytes() == b"library media"


@pytest.mark.asyncio
async def test_recovers_flat_logs(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "healthchecks"
    async with database_context("flat-logs") as factory:
        job_id, attempt_id = await seed(factory, root, source)
        assert await prune_jobs(factory, 1, now=T0 + timedelta(days=2)) == 0
        await recover_healthcheck_logs(factory, root)
        async with factory() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            assert attempt is not None and attempt.log_data == b"complete diagnostics\n"
            record = await session.scalar(select(JobEvent).where(JobEvent.type == "healthcheck_log_recovered"))
            assert record is not None and record.payload["removed"]
            assert await session.scalar(recovery_jobs().where(Job.id == job_id)) is None
        assert not list(root.iterdir())
        assert source.read_bytes() == b"library media"
        assert not (tmp_path / "workspace").exists()
        assert await prune_jobs(factory, 1, now=T0 + timedelta(days=2)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["running", "unconfirmed", "unread", "unfinished"])
async def test_retains_unfinished_logs(tmp_path: Path, change: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "healthchecks"
    async with database_context("unfinished-logs") as factory:
        _, attempt_id = await seed(factory, root, source)
        async with factory() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            assert attempt is not None and attempt.log_path is not None
            await snapshot_healthcheck_log(session, root, attempt)
            if change == "running":
                attempt.status = "running"
            elif change == "unconfirmed":
                attempt.result, attempt.error = "interrupted", "process exit unconfirmed"
            elif change == "unread":
                attempt.log_data = None
            elif change == "unfinished":
                attempt.finished_at = None
            await session.commit()
            path = root / attempt.log_path
            await cleanup_healthcheck_log(session, root, attempt_id)
            await session.commit()
        assert path.exists()
        assert await prune_jobs(factory, 1, now=T0 + timedelta(days=2)) == 0


@pytest.mark.parametrize("missing", [False, True])
def test_removes_captured_log(tmp_path: Path, missing: bool) -> None:
    root = tmp_path / "healthchecks"
    log = create_healthcheck_log(root, 1)
    log.append("diagnostics", sync=True)
    data = capture_healthcheck_log(root, log.path.name)
    assert data == b"diagnostics"
    if missing:
        log.path.unlink()
    remove_healthcheck_log(root, log.path.name)
    assert not log.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cleanup_drains_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellations: int) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "healthchecks"
    entered, cancelled = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    wait = asyncio.wait

    def blocked(root: Path, relative: str) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        remove_healthcheck_log(root, relative)

    async def tracked(futures, **kwargs):
        try:
            return await wait(futures, **kwargs)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with database_context("log-drain") as factory:
        job_id, attempt_id = await seed(factory, root, source)
        other_source = tmp_path / "other.mkv"
        other_source.write_bytes(b"independent media")
        other_job = await add_job(factory, other_source)
        async with factory() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            assert attempt is not None
            await snapshot_healthcheck_log(session, root, attempt)
            await session.commit()
        monkeypatch.setattr("server.healthlogs.remove_healthcheck_log", blocked)
        monkeypatch.setattr(asyncio, "wait", tracked)

        async def cleanup() -> None:
            async with factory() as session:
                await cleanup_healthcheck_log(session, root, attempt_id)
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
                    update(Job).where(Job.id == job_id).values(status="queued"),
                    update(JobAttempt).where(JobAttempt.id == attempt_id).values(log_data=b"changed attempt"),
                ):
                    async with factory() as writer:
                        await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                        with pytest.raises(OperationalError) as caught:
                            await writer.execute(statement)
                        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert isinstance(result[0], asyncio.CancelledError)
        monkeypatch.setattr("server.healthlogs.remove_healthcheck_log", remove_healthcheck_log)
        await recover_healthcheck_logs(factory, root)
        assert not list(root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [False, True])
async def test_capture_checkpoint_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: bool) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "healthchecks"
    async with database_context("capture-checkpoint") as factory:
        _, attempt_id = await seed(factory, root, source)
        async with factory() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            assert attempt is not None
            await snapshot_healthcheck_log(session, root, attempt)
            await session.commit()
            if removed:
                assert attempt.log_path is not None
                (root / attempt.log_path).unlink()
        await recover_healthcheck_logs(factory, root)
        async with factory() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            assert attempt is not None and attempt.log_data == b"complete diagnostics\n"
        assert not list(root.iterdir())


@pytest.mark.asyncio
async def test_cancel_after_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    root = tmp_path / "healthchecks"
    captured, release = asyncio.Event(), asyncio.Event()
    commit = AsyncSession.commit

    async def pause_capture(session: AsyncSession) -> None:
        checkpoint = any(isinstance(row, JobEvent) and row.type == "healthcheck_log_recovered" for row in session.new)
        await commit(session)
        if checkpoint:
            captured.set()
            await release.wait()

    async def check(source, ffmpeg, ffprobe, log, *, hwaccel, on_progress):
        log.append("complete diagnostics\n", sync=True)
        return HealthcheckResult(healthy=True)

    async with database_context("cancel-capture") as factory:
        job_id = await add_job(factory, source)
        service = HealthcheckService(factory, root, "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        monkeypatch.setattr(AsyncSession, "commit", pause_capture)
        monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
        task = asyncio.create_task(service.run(*claimed))
        try:
            await asyncio.wait_for(captured.wait(), 3)
            task.cancel()
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert isinstance(result[0], asyncio.CancelledError)
        async with factory() as session:
            job = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, claimed[1])
            assert job is not None and job.status == "cancelled"
            assert attempt is not None and attempt.log_data == b"complete diagnostics\n"
            assert await session.scalar(recovery_jobs().where(Job.id == job_id)) is None
        assert not list(root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [False, True])
async def test_recovery_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup: bool) -> None:
    root = tmp_path / "healthchecks"
    monkeypatch.setattr("server.healthlogs.QUERY_BATCH", 2)
    async with database_context("healthlog-batches") as factory:
        attempts = []
        for index in range(5):
            path = tmp_path / f"movie-{index}.mkv"
            path.write_bytes(b"library media")
            _, attempt_id = await seed(factory, root, path)
            attempts.append(attempt_id)
        (root / f"{attempts[0]}.log").unlink()
        removals = []

        def refuse_remove(root, relative):
            removals.append(relative)
            if relative == f"{attempts[1]}.log":
                raise OSError("log cleanup refused")
            remove_healthcheck_log(root, relative)

        monkeypatch.setattr("server.healthlogs.remove_healthcheck_log", refuse_remove)
        await asyncio.wait_for(recover_healthcheck_logs(factory, root, startup=startup), 3)
        assert removals == [f"{attempt_id}.log" for attempt_id in attempts[1:]]
        assert [path.name for path in root.iterdir()] == [f"{attempts[1]}.log"]
        async with factory() as session:
            rows = (await session.scalars(select(JobAttempt).order_by(JobAttempt.id))).all()
            assert rows[0].log_data is None
            assert all(row.log_data == b"complete diagnostics\n" for row in rows[1:])
            unavailable = (
                await session.scalars(select(JobEvent).where(JobEvent.type == "healthcheck_log_unavailable"))
            ).all()
            assert len(unavailable) == 1 and unavailable[0].attempt_id == attempts[0]
            recovered = (
                await session.scalars(
                    select(JobEvent).where(JobEvent.type == "healthcheck_log_recovered").order_by(JobEvent.attempt_id)
                )
            ).all()
            assert [row.payload["removed"] for row in recovered] == [False, True, True, True]
