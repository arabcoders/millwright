from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import InterfaceError, OperationalError

from common.fs.identity import source_identity
from server.settings import ScannerConfig
from server.events import EventBus
from server.fs import library as library_fs
from server.metadata_jobs import enqueue_metadata_job, retry_metadata
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow
from server.queue.store import cancel_pending, processing_backlog, requeue, requeue_startup
from server.probing import ProbeService
from server.retention import delete_eligible_job
from common.media.tools import ProbeError
from server.scanner.scan import scan_library
from server.staging import StagingService
from tests.helpers import database_context

RAW = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}], "format": {"duration": "1"}}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def db() -> AsyncIterator:
    async with database_context("metadata-jobs") as factory:
        yield factory


async def media(db, root: Path, name: str, *, stored: bool = False, priority: int = 0, checks: bool = False) -> int:
    path = root / name
    path.write_bytes(b"original")
    stat = path.stat()
    async with db() as session:
        library = await session.scalar(select(Library))
        if library is None:
            library = Library(name="library", roots=[str(root)], mkv_stats=True, healthcheck_enabled=checks)
            session.add(library)
            await session.flush()
        row = MediaFile(
            library_id=library.id,
            path=str(path),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            source_identity=source_identity(stat),
            first_seen=T0,
            container=path.suffix[1:],
            medium="other",
            health_status="pending",
            last_scanned_at=T0,
            metadata_priority=priority,
        )
        session.add(row)
        await session.flush()
        if stored:
            session.add(ProbeRow(media_file_id=row.id, raw_json=json.dumps(RAW)))
        await session.commit()
        return row.id


async def batch(service: ProbeService) -> None:
    await asyncio.wait_for(service._fill(), 3)
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)


@pytest.mark.asyncio
async def test_remaining_counts_sources(db, tmp_path: Path) -> None:
    await media(db, tmp_path, "pending.mkv")
    overlap = await media(db, tmp_path, "overlap.mkv")
    active = await media(db, tmp_path, "active.mkv")
    completed = await media(db, tmp_path, "completed.mkv")
    async with db() as session:
        library = await session.scalar(select(Library))
        assert library is not None
        for identifier in (active, completed):
            row = await session.get(MediaFile, identifier)
            assert row is not None
            row.metadata_cancelled = True
        session.add_all(
            [
                Job(media_file_id=overlap, library_id=library.id, kind="probe", status="queued"),
                Job(media_file_id=active, library_id=library.id, kind="mkv_stats", status="running"),
                Job(media_file_id=completed, library_id=library.id, kind="probe", status="succeeded"),
            ]
        )
        empty = Library(name="empty", roots=[])
        session.add(empty)
        await session.commit()
        library_id, empty_id = library.id, empty.id
    service = ProbeService(db, "unused", "unused", workspace_dir=tmp_path / "workspace")
    assert await service._remaining() == {library_id: 3, empty_id: 0}


@pytest.mark.asyncio
async def test_slots_precede_progress(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = ProbeService(db, "unused", "unused", workspace_dir=tmp_path / "workspace", max_concurrency=1)
    counted, release = asyncio.Event(), asyncio.Event()
    started = []

    async def start_job(kind):
        started.append(kind)
        return True

    async def progress():
        counted.set()
        await release.wait()

    monkeypatch.setattr(service, "_start_job", start_job)
    monkeypatch.setattr(service, "_refresh_progress", progress)
    task = asyncio.create_task(service._fill())
    try:
        await asyncio.wait_for(counted.wait(), 5)
        assert started == ["probe", "mkv_stats"]
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["probe", "mkv_stats"])
async def test_completion_defers_metadata(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Literal["probe", "mkv_stats"]
) -> None:
    held_id = await media(db, tmp_path, "held.mkv", stored=kind == "mkv_stats")
    other_id = await media(db, tmp_path, "other.mkv", stored=kind == "mkv_stats")

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    async with db() as session:
        library = await session.scalar(select(Library))
        library.mkv_stats = False
        owner = Job(media_file_id=held_id, library_id=library.id, kind="transcode", status="succeeded")
        session.add(owner)
        row = await session.get(MediaFile, held_id)
        held = await enqueue_metadata_job(session, row, library, kind=kind, priority=1)
        row = await session.get(MediaFile, other_id)
        other = await enqueue_metadata_job(session, row, library, kind=kind, priority=0)
        await session.commit()
    busy_jobs = {owner.id}
    service = ProbeService(
        db, "probe", "editor", workspace_dir=tmp_path / "workspace", max_concurrency=1, busy_jobs=busy_jobs
    )
    try:
        await batch(service)
        async with db() as session:
            held = await session.get(Job, held.id)
            other = await session.get(Job, other.id)
            assert held.status == "queued" and held.attempts == 0
            assert other.status == "succeeded"
        busy_jobs.clear()
        await batch(service)
        async with db() as session:
            held = await session.get(Job, held.id)
            row = await session.get(MediaFile, held_id)
            assert held.status == "succeeded" and held.attempts == 1
            assert row.probe_id is not None and row.video_codec == "h264"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_basic_handoff_atomic(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mkv", checks=True)

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    bus = EventBus(min_interval_s=0)
    service = ProbeService(db, "probe", "editor", bus, workspace_dir=tmp_path / "workspace")
    try:
        await batch(service)
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            jobs = list(await session.scalars(select(Job).order_by(Job.id)))
            assert row is not None and row.video_codec == "h264" and row.probe_id is None
            assert [(job.kind, job.status) for job in jobs] == [("probe", "succeeded"), ("mkv_stats", "queued")]
            assert await session.scalar(select(func.count(ProbeRow.id))) == 1
            attempt = await session.scalar(select(JobAttempt))
            assert attempt is not None and attempt.input_probe is not None and attempt.output_probe is not None
        progress = (await service.progress_snapshot())[0]["probing"]
        assert progress["processed"] == 0 and progress["remaining"] == 1
        async with db() as session:
            backlog = await processing_backlog(session, healthcheck_slots=1)
            assert backlog["remaining_files"] == 1 and backlog["remaining_s"] is None
    finally:
        await service.close()
        await bus.aclose()


@pytest.mark.asyncio
async def test_discovery_bypasses_editor(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats_id = await media(db, tmp_path, "statistics.mkv", stored=True)
    old_id = await media(db, tmp_path, "old.mp4")
    entered, release = asyncio.Event(), asyncio.Event()
    edited = set()

    async def probe(_bin, path, **_kwargs):
        raw = json.loads(json.dumps(RAW))
        if path.name in edited or path.suffix == ".mp4":
            raw["streams"][0]["bit_rate"] = "500"
        return raw

    async def edit(_bin, path, *, on_progress, **_kwargs):
        await on_progress(42)
        entered.set()
        await release.wait()
        edited.add(path.name)

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    bus = EventBus(min_interval_s=0)
    service = ProbeService(db, "probe", "editor", bus, max_concurrency=1, max_statistics=1, workspace_dir=tmp_path)
    try:
        await service._fill()
        await asyncio.wait_for(entered.wait(), 3)
        basic_tasks = [task for jid, task in service._tasks.items() if service._task_kinds[jid] == "probe"]
        await asyncio.wait_for(asyncio.gather(*basic_tasks), 3)
        new_id = await media(db, tmp_path, "new.mp4", priority=1)
        backlog = await media(db, tmp_path, "backlog.mp4")
        await service._fill()
        basic_tasks = [task for jid, task in service._tasks.items() if service._task_kinds[jid] == "probe"]
        await asyncio.wait_for(asyncio.gather(*basic_tasks), 3)
        async with db() as session:
            new = await session.get(MediaFile, new_id)
            old = await session.get(MediaFile, backlog)
            assert new is not None and new.probe_id is not None
            assert old is not None and old.probe_id is None
            assert (await session.get(MediaFile, old_id)).probe_id is not None
            running = await session.scalar(select(Job).where(Job.kind == "mkv_stats"))
            assert running is not None and running.media_file_id == stats_id and running.status == "running"
            assert running.progress["tool"] == "mkvpropedit" and running.progress["percent"] == 42
            activity = bus.library_snapshot(running.library_id)["probing"]["active"]
            assert any(item["job_id"] == running.id for item in activity)
    finally:
        release.set()
        await service.close()
        await bus.aclose()


@pytest.mark.asyncio
async def test_independent_lane_bounds(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(3):
        await media(db, tmp_path, f"basic-{index}.mp4")
        await media(db, tmp_path, f"stats-{index}.mkv", stored=True)
    basic_entered, stats_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    basics = stats = 0

    async def probe(*_args, **_kwargs):
        nonlocal basics
        basics += 1
        if basics == 2:
            basic_entered.set()
        await release.wait()
        return RAW

    async def edit(*_args, **_kwargs):
        nonlocal stats
        stats += 1
        stats_entered.set()
        await release.wait()

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    service = ProbeService(db, "probe", "editor", max_concurrency=2, max_statistics=1, workspace_dir=tmp_path)
    try:
        await service._fill()
        await asyncio.wait_for(asyncio.gather(basic_entered.wait(), stats_entered.wait()), 3)
        await service._fill()
        assert basics == 2 and stats == 1 and len(service._tasks) == 3
        async with db() as session:
            assert await session.scalar(select(func.count(Job.id))) == 3
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["BPS", "BPS-eng"])
async def test_statistics_policy_skip(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    await media(db, tmp_path, "movie.mkv")

    async def probe(*_args, **_kwargs):
        return {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {tag: "500"}},
                {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "tags": {"mimetype": "image/jpeg"}},
            ]
        }

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        await batch(service)
        async with db() as session:
            assert list(await session.scalars(select(Job.kind))) == ["probe"]
            assert (await session.scalar(select(MediaFile))).health_status == "healthy"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_cancel_survives_history(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    media_id = await media(db, tmp_path, "movie.mkv", stored=True)
    entered = asyncio.Event()

    async def edit(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        if running:
            await service._fill()
            await asyncio.wait_for(entered.wait(), 3)
            job_id = next(iter(service._tasks))
            assert await service.cancel_job(job_id)
            await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        else:
            async with db() as session:
                row = await session.get(MediaFile, media_id)
                library = await session.get(Library, row.library_id)
                job = await enqueue_metadata_job(session, row, library, kind="mkv_stats", priority=0)
                job_id = job.id
                await cancel_pending(session, job_id)
                await session.commit()
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            job = await session.get(Job, job_id)
            assert row.metadata_cancelled and job.status == "cancelled"
            assert await delete_eligible_job(session, job_id)
            await session.commit()
            library = await session.get(Library, row.library_id)
            await scan_library(
                session, library, config=ScannerConfig(), staging=StagingService(db, tmp_path / "workspace")
            )
        await service.close()
        replacement = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
        try:
            await replacement._fill()
            assert not replacement._tasks
        finally:
            await replacement.close()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_reenables_metadata(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mp4")
    async with db() as session:
        row = await session.get(MediaFile, media_id)
        library = await session.get(Library, row.library_id)
        job = await enqueue_metadata_job(session, row, library, kind="probe", priority=0)
        await cancel_pending(session, job.id)
        await retry_metadata(session, job, row)
        await session.commit()

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        await batch(service)
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            assert not row.metadata_cancelled and row.health_status == "healthy"
            assert (await session.get(Job, job.id)).status == "succeeded"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_statistics_cooldown_replacement(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = [T0]
    for module in ("server.probing", "server.metadata_jobs", "server.fs.library", "server.queue.store"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: current[0])
    media_id = await media(db, tmp_path, "movie.mkv")
    async with db() as session:
        library = await session.scalar(select(Library))
        library.cooldown_s = 300
        await session.commit()
    calls = []

    async def probe(*_args, **_kwargs):
        calls.append("probe")
        return RAW

    async def edit(*_args, **_kwargs):
        calls.append("edit")

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        await batch(service)
        current[0] += timedelta(seconds=300)
        (tmp_path / "movie.mkv").write_bytes(b"replacement source")
        await batch(service)
        assert calls == ["probe", "probe"]
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            assert row.first_seen.replace(tzinfo=UTC) == current[0]
            stats = await session.scalar(select(Job).where(Job.kind == "mkv_stats"))
            assert stats.status == "hold" and stats.hold_until.replace(tzinfo=UTC) == current[0] + timedelta(
                seconds=300
            )
        current[0] += timedelta(seconds=300)
        await batch(service)
        assert calls == ["probe", "probe", "probe", "edit", "probe"]
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("suspended", [False, True])
async def test_interruption_remains_resumable(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suspended: bool
) -> None:
    await media(db, tmp_path, "movie.mkv", stored=True)
    entered = asyncio.Event()

    async def edit(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        await service._fill()
        await asyncio.wait_for(entered.wait(), 3)
        if suspended:
            async with db() as session:
                library = await session.scalar(select(Library))
                library.suspended = True
                await session.commit()
            await service.check_library_access()
        else:
            await service.close()
        async with db() as session:
            row = await session.scalar(select(MediaFile))
            job = await session.scalar(select(Job))
            assert job.status == "queued" and not row.metadata_cancelled
            assert row.health_status == "pending"
            assert (await session.scalar(select(JobAttempt))).result == "interrupted"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_attempt_progress_checked(db, tmp_path: Path) -> None:
    await media(db, tmp_path, "movie.mp4")
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    claimed = await service._claim("probe")
    assert claimed is not None
    job_id, attempt_id, _ = claimed
    async with db() as session:
        job = await session.get(Job, job_id)
        job.attempts += 1
        await session.commit()
    await service._tool(job_id, attempt_id, {"tool": "ffprobe", "percent": None, "stage": "started"})
    async with db() as session:
        assert (await session.get(Job, job_id)).progress is None
    await service.close()


@pytest.mark.asyncio
async def test_tool_failure_log(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await media(db, tmp_path, "movie.mp4")

    async def probe(*_args, **_kwargs):
        raise ProbeError("Broken metadata.", tool="ffprobe", exit_code=2, stderr="invalid input", stdout="diagnostic")

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        await batch(service)
        async with db() as session:
            job = await session.scalar(select(Job))
            attempt = await session.scalar(select(JobAttempt))
            assert job.status == "failed" and job.error == "Broken metadata."
            assert attempt.argv[0] == "probe"
            assert b"Exit code: 2." in attempt.log_data and b"invalid input" in attempt.log_data
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OperationalError, InterfaceError])
async def test_loop_recovers_database(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error) -> None:
    service = ProbeService(db, "unused", "unused", workspace_dir=tmp_path)
    retrying, release, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def fill():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise error("SELECT 1", {}, RuntimeError("Database unavailable."))
        recovered.set()

    async def retry(delay):
        assert delay == 1
        retrying.set()
        await release.wait()

    monkeypatch.setattr(service, "_fill", fill)
    monkeypatch.setattr("server.probing.asyncio.sleep", retry)
    try:
        await service.start()
        await asyncio.wait_for(retrying.wait(), 3)
        assert service.failed and calls == 1
        release.set()
        await asyncio.wait_for(recovered.wait(), 3)
        assert calls == 3 and not service.failed
        assert service._loop_task is not None and not service._loop_task.done()
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
async def test_worker_outage_recovered(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mp4")
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    release = asyncio.Event()

    async def unavailable(*_args, **_kwargs):
        await release.wait()
        raise OperationalError("SELECT 1", {}, RuntimeError("Database unavailable."))

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    try:
        with monkeypatch.context() as outage:
            outage.setattr(service, "_publish", unavailable)
            outage.setattr(service, "_finish_attempt", unavailable)
            await service._fill()
            tasks = list(service._tasks.values())
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
            assert len(results) == 1 and isinstance(results[0], OperationalError)
        assert not service._tasks
        async with db() as session:
            job = await session.scalar(select(Job))
            assert job is not None and job.status == "running"
            job_id = job.id
        await batch(service)
        async with db() as session:
            job = await session.get(Job, job_id)
            row = await session.get(MediaFile, media_id)
            attempts = list(await session.scalars(select(JobAttempt).order_by(JobAttempt.attempt_number)))
            assert job is not None and job.status == "succeeded" and job.attempts == 2
            assert row is not None and row.probe_id is not None
            assert [(attempt.status, attempt.result) for attempt in attempts] == [
                ("failed", "interrupted"),
                ("succeeded", "succeeded"),
            ]
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["probe", "mkv_stats"])
async def test_orphan_attempt_limit(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    media_id = await media(db, tmp_path, "exhausted.mkv", stored=kind == "mkv_stats")
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    try:
        for number in range(3):
            claimed = await service._claim(kind)
            assert claimed is not None
            if number < 2:
                async with db() as session:
                    await requeue(session, claimed[0])
                    await session.commit()
        await batch(service)
        async with db() as session:
            jobs = list(await session.scalars(select(Job)))
            row = await session.get(MediaFile, media_id)
            assert len(jobs) == 1 and jobs[0].status == "failed" and jobs[0].attempts == 3
            assert row is not None and row.health_status == "probe_failed"
    finally:
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
async def test_committed_metadata_wake(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mp4")
    service = ProbeService(db, "unused", "unused", workspace_dir=tmp_path)

    async def unavailable(_job_id):
        raise OperationalError("SELECT 1", {}, RuntimeError("Database unavailable."))

    monkeypatch.setattr(service, "_publish", unavailable)
    with pytest.raises(OperationalError):
        await service.files_pending([media_id])
    assert service._wake.is_set()
    async with db() as session:
        job = await session.scalar(select(Job))
        assert job is not None and job.status == "queued" and job.media_file_id == media_id


@pytest.mark.asyncio
async def test_committed_followups_wake(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await media(db, tmp_path, "movie.mp4", checks=True)
    healthcheck, flow, publishing, release = (asyncio.Event() for _ in range(4))
    service = ProbeService(
        db,
        "probe",
        "editor",
        healthcheck_wake=healthcheck.set,
        on_healthy=flow.set,
        workspace_dir=tmp_path,
    )
    calls = 0

    async def publish(_job_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            publishing.set()
            await release.wait()
            raise OperationalError("SELECT 1", {}, RuntimeError("Database unavailable."))

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(service, "_publish", publish)
    try:
        await service._fill()
        await asyncio.wait_for(publishing.wait(), 3)
        assert not healthcheck.is_set() and not flow.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(healthcheck.wait(), flow.wait()), 3)
        async with db() as session:
            jobs = list(await session.scalars(select(Job).order_by(Job.id)))
            assert [(job.kind, job.status) for job in jobs] == [("probe", "succeeded"), ("healthcheck", "queued")]
        await service._recover_jobs()
        async with db() as session:
            assert await session.scalar(select(func.count(JobAttempt.id))) == 1
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["probe", "mkv_stats"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("runtime", [False, True])
async def test_recovery_waits_worker(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, cancelled: bool, runtime: bool
) -> None:
    media_id = await media(db, tmp_path, "movie.mkv", stored=kind == "mkv_stats")
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    claimed = await service._claim(kind)
    assert claimed is not None
    job_id, attempt_id, _ = claimed
    async with db() as session:
        if cancelled:
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="metadata_cancel_requested", payload={}))
            await session.commit()
        if runtime:
            await service._recover_jobs()
        else:
            assert await requeue_startup(session, T0) == 1
            await session.commit()
        row = await session.get(MediaFile, media_id)
        job = await session.get(Job, job_id)
        assert row is not None and row.metadata_cancelled == cancelled
        assert job is not None and job.status == ("cancelled" if cancelled else "queued")

    async def probe(*_args, **_kwargs):
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "bit_rate": "500"}]}

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    try:
        await batch(service)
        async with db() as session:
            jobs = list(await session.scalars(select(Job)))
            assert len(jobs) == 1 and jobs[0].id == job_id
            assert jobs[0].attempts == (1 if cancelled else 2)
            assert jobs[0].status == ("cancelled" if cancelled else "succeeded")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_rescan_preserves_writer(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mkv", stored=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def edit(_bin, path, **_kwargs):
        with path.open("ab") as handle:
            handle.write(b"statistics")
        entered.set()
        await release.wait()

    async def probe(*_args, **_kwargs):
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "bit_rate": "500"}]}

    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path / "workspace")
    try:
        await service._fill()
        await asyncio.wait_for(entered.wait(), 3)
        async with db() as session:
            library = await session.scalar(select(Library))
            assert library is not None
            report = await scan_library(
                session, library, config=ScannerConfig(), staging=StagingService(db, tmp_path / "workspace")
            )
            assert report.updated == 0 and report.pruned == 0
            row = await session.get(MediaFile, media_id)
            assert row is not None and row.size == len(b"original")
        release.set()
        await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            job = await session.scalar(select(Job))
            assert row is not None and row.size == len(b"originalstatistics") and row.probe_id is not None
            assert row.first_seen.replace(tzinfo=UTC) == T0
            assert job is not None and job.status == "succeeded"
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_cooldown_preserves_capacity(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = T0 + timedelta(seconds=600)
    for module in ("server.probing", "server.metadata_jobs", "server.fs.library"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: now)
    ready = await media(db, tmp_path, "ready.mkv", stored=True)
    cooling = await media(db, tmp_path, "cooling.mkv", stored=True, priority=1)
    async with db() as session:
        library = await session.scalar(select(Library))
        row = await session.get(MediaFile, cooling)
        assert library is not None and row is not None
        row.first_seen = now - timedelta(seconds=200)
        job = await enqueue_metadata_job(session, row, library, kind="mkv_stats", priority=1)
        job_id = job.id
        assert job.status == "queued"
        library.cooldown_s = 300
        await session.commit()
    edits = []

    async def edit(_bin, path, **_kwargs):
        edits.append(path.name)

    async def probe(*_args, **_kwargs):
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "bit_rate": "500"}]}

    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        await batch(service)
        assert edits == ["ready.mkv"]
        async with db() as session:
            completed = await session.scalar(select(Job).where(Job.media_file_id == ready))
            assert completed is not None and completed.status == "succeeded"
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "queued" and job.attempts == 0
        assert service._cooldown_until is not None
        assert (service._cooldown_until - now).total_seconds() == pytest.approx(100, abs=0.001)
        now += timedelta(seconds=100)
        async with db() as session:
            row = await session.get(MediaFile, cooling)
            assert row is not None
            row.first_seen = now
            await session.commit()
        for _ in range(2):
            await batch(service)
            assert not service._tasks and edits == ["ready.mkv"]
            async with db() as session:
                job = await session.get(Job, job_id)
                assert job is not None and job.status == "queued" and job.attempts == 0
            assert service._cooldown_until is not None
            assert (service._cooldown_until - now).total_seconds() == pytest.approx(300, abs=0.001)
    finally:
        await service.close()
