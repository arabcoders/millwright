from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("local_storage")
import pytest_asyncio
from common.fs.identity import source_identity
from common.plan import FFmpegPlan, SubtitleBurn, VideoTarget
from common.enums import JobStatus
from common.workspace import workspace_lock
from sqlalchemy import select

from server import staging, workspace
from server.fs.source import MediaSnapshot
from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NotificationDelivery,
    utcnow,
)
from server.queue.store import capture_source
from server.staging import StagingService
from tests.helpers import database_context


@pytest_asyncio.fixture
async def staged_env(tmp_path: Path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    (tmp_path / "movie.srt").write_bytes(b"subtitle")
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _: SimpleNamespace(free=20_000_000_000))
    async with database_context("staging-effects") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(source),
                size=source.stat().st_size,
                mtime_ns=source.stat().st_mtime_ns,
                last_scanned_at=utcnow(),
                container="mkv",
                medium="movie",
                source_identity=source_identity(source.stat()),
            )
            session.add(media)
            await session.flush()
            job = Job(
                media_file_id=media.id,
                library_id=library.id,
                status=JobStatus.STAGING,
                plan=FFmpegPlan().model_dump(mode="json"),
            )
            session.add(job)
            await session.flush()
            session.add(JobEvent(job_id=job.id, type="enqueued", payload={"source": await capture_source(media)}))
            await session.commit()
        service = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        try:
            yield factory, service, job.id, source
        finally:
            await asyncio.wait_for(service.aclose(), 3)


@pytest.mark.asyncio
async def test_retry_refreshes_inputs(staged_env):
    factory, service, job_id, source = staged_env
    folder = service.workspace_dir / "jobs" / str(job_id)
    attempt = folder / "attempts" / "1"
    attempt.mkdir(parents=True)
    (attempt / "output.mkv").write_bytes(b"old attempt output")
    (folder / "source.mkv").write_bytes(b"stale staged source")
    (folder / "subtitle.srt").write_bytes(b"stale subtitle")
    (folder / "manifest.json").write_text("invalid manifest")
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.attempts = 1
        job.plan = FFmpegPlan(
            video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="movie.srt")
        ).model_dump(mode="json")
        session.add(JobAttempt(job_id=job_id, attempt_number=1, status="running", result="interrupted"))
        await session.commit()
    with workspace_lock(attempt):
        assert await service.stage_job(job_id) == 1
        assert (attempt / "output.mkv").read_bytes() == b"old attempt output"
    assert (folder / "source.mkv").read_bytes() == source.read_bytes() == b"source"
    assert (folder / "subtitle.srt").read_bytes() == b"subtitle"
    assert workspace.staged_source(service.workspace_dir, job_id) == f"jobs/{job_id}/source.mkv"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["suspended", "roots"])
async def test_stage_current_authority(staged_env, change: str):
    factory, service, job_id, source = staged_env
    async with factory() as session:
        job = await session.get(Job, job_id)
        library = await session.get(Library, job.library_id)
        if change == "suspended":
            library.suspended = True
        else:
            library.roots = [str(source.parent / "other")]
        await session.commit()
    assert await service.stage_job(job_id) == 0
    assert not (service.workspace_dir / "jobs").exists()
    assert source.read_bytes() == b"source"
    async with factory() as session:
        assert (await session.get(Job, job_id)).status == JobStatus.STAGING


@pytest.mark.asyncio
async def test_stage_rechecks_authority(staged_env, monkeypatch):
    factory, service, job_id, source = staged_env
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    copy = workspace._stage_source

    def blocked(*args, **kwargs):
        result = copy(*args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return result

    monkeypatch.setattr("server.staging._stage_source", blocked)
    task = asyncio.create_task(service.stage_job(job_id))
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with factory() as session:
            job = await session.get(Job, job_id)
            (await session.get(Library, job.library_id)).suspended = True
            await session.commit()
        release.set()
        assert await asyncio.wait_for(task, 3) == 0
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
    async with factory() as session:
        assert (await session.get(Job, job_id)).status == JobStatus.STAGING
    assert source.read_bytes() == b"source"
    assert (service.workspace_dir / f"jobs/{job_id}/source.mkv").read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("inspection", ["root", "capacity", "subtitle"])
async def test_inspection_releases_writer(staged_env, monkeypatch, inspection):
    from server.fs import source as source_module

    factory, service, job_id, source = staged_env
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    checks = 0
    if inspection == "subtitle":
        async with factory() as session:
            job = await session.get(Job, job_id)
            job.plan = FFmpegPlan(
                video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="movie.srt")
            ).model_dump(mode="json")
            await session.commit()
    target = source_module if inspection == "root" else staging
    name = {"root": "check_source_root", "capacity": "_workspace_free", "subtitle": "_subtitle_size"}[inspection]
    inspect = getattr(target, name)

    def blocked(*args, **kwargs):
        nonlocal checks
        checks += 1
        if inspection != "root" or checks == 2:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        return inspect(*args, **kwargs)

    monkeypatch.setattr(target, name, blocked)
    assert service.schedule(job_id)
    try:
        await asyncio.wait_for(started.wait(), 3)

        async def queue():
            async with factory() as session:
                session.add(Library(name="unrelated", roots=[str(source.parent / "unrelated")]))
                await session.commit()

        await asyncio.wait_for(queue(), 3)
        assert not release.is_set()
        release.set()
        await asyncio.wait_for(service.wait_idle(), 3)
        async with factory() as session:
            assert (await session.get(Job, job_id)).status == JobStatus.QUEUED
    finally:
        release.set()
        await asyncio.wait_for(service.aclose(), 3)


@pytest.mark.asyncio
async def test_cancel_retains_pending(staged_env, monkeypatch):
    factory, service, job_id, _ = staged_env
    started, cancelled = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    copy, wait = workspace._stage_source, asyncio.wait

    def blocked(*args, **kwargs):
        result = copy(*args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return result

    async def tracked_wait(futures, **kwargs):
        try:
            return await wait(futures, **kwargs)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("server.staging._stage_source", blocked)
    monkeypatch.setattr(asyncio, "wait", tracked_wait)
    assert service.schedule(job_id)
    try:
        await asyncio.wait_for(started.wait(), 3)
        task = service.tasks[job_id]
        for _ in range(2):
            cancelled.clear()
            task.cancel()
            await asyncio.wait_for(cancelled.wait(), 3)
            assert not task.done() and job_id in service.active_jobs
            assert not service.schedule(job_id)
        async with factory() as session:
            (await session.get(Job, job_id)).status = JobStatus.PENDING
            await session.commit()
        release.set()
        await asyncio.wait_for(service.wait_idle(), 3)
        assert task.cancelled() and not service.tasks and not service.active_jobs
        assert (service.workspace_dir / "jobs" / str(job_id) / "source.mkv").read_bytes() == b"source"
        async with factory() as session:
            assert (await session.get(Job, job_id)).status == JobStatus.PENDING
    finally:
        release.set()
        await asyncio.wait_for(service.aclose(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["library", "media"])
async def test_queue_checks_inspection(staged_env, monkeypatch, change):
    factory, service, job_id, source = staged_env
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    capacity = staging._workspace_free

    def blocked(path):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return capacity(path)

    monkeypatch.setattr(staging, "_workspace_free", blocked)
    assert service.schedule(job_id)
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with factory() as session:
            job = await session.get(Job, job_id)
            if change == "library":
                (await session.get(Library, job.library_id)).roots = [str(source.parent / "other")]
            else:
                (await session.get(MediaFile, job.media_file_id)).size += 1
            await session.commit()
        release.set()
        await asyncio.wait_for(service.wait_idle(), 3)
        assert not (service.workspace_dir / "jobs").exists()
        async with factory() as session:
            assert (await session.get(Job, job_id)).status == JobStatus.STAGING
    finally:
        release.set()
        await asyncio.wait_for(service.aclose(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["source.mkv", "subtitle.srt", "manifest.json"])
async def test_stage_cleans_conflicts(staged_env, entry: str):
    factory, service, job_id, source = staged_env
    folder = service.workspace_dir / "jobs" / str(job_id)
    folder.mkdir(parents=True)
    existing_file = folder / entry
    existing_file.write_bytes(b"another job's output")
    if entry == "subtitle.srt":
        async with factory() as session:
            job = await session.get(Job, job_id)
            job.plan = FFmpegPlan(
                video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="movie.srt")
            ).model_dump(mode="json")
            await session.commit()
    assert await service.stage_job(job_id) == 0
    assert not folder.exists()
    await workspace.cleanup_workspace(factory, service.workspace_dir)
    assert not folder.exists()
    assert source.read_bytes() == b"source"
    async with factory() as session:
        assert (await session.get(Job, job_id)).status == JobStatus.FAILED
        assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "staging_unresolved")) is not None


@pytest.mark.asyncio
async def test_subtitle_copies_current(staged_env, monkeypatch):
    factory, service, job_id, source = staged_env
    subtitle = source.parent / "movie.srt"
    async with factory() as session:
        job = await session.get(Job, job_id)
        job.plan = FFmpegPlan(
            video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename=subtitle.name)
        ).model_dump(mode="json")
        await session.commit()

    def capacity(_):
        subtitle.write_bytes(b"larger subtitle after queue")
        return SimpleNamespace(free=20_000_000_000)

    monkeypatch.setattr("server.staging.shutil.disk_usage", capacity)
    assert await service.stage_job(job_id) == 1
    assert (service.workspace_dir / f"jobs/{job_id}/subtitle.srt").read_bytes() == b"larger subtitle after queue"
    assert subtitle.read_bytes() == b"larger subtitle after queue"
    assert source.read_bytes() == b"source"
    async with factory() as session:
        assert (await session.get(Job, job_id)).status == JobStatus.QUEUED


async def continuation(staged_env) -> int:
    factory, service, job_id, _ = staged_env
    assert await service.stage_job(job_id) == 1
    async with factory() as session:
        job = await session.get(Job, job_id)
        job.attempts = 1
        job.status = JobStatus.VERIFYING
        attempt = JobAttempt(job_id=job_id, attempt_number=1, status=JobStatus.VERIFYING)
        session.add(attempt)
        await session.flush()
        session.add(
            JobEvent(job_id=job_id, attempt_id=attempt.id, type="stage", payload={"output_probe": {"size_bytes": 12}})
        )
        await session.commit()
        return attempt.id


@pytest.mark.asyncio
async def test_continuation_copy_cleanup(staged_env):
    _, service, job_id, source = staged_env
    attempt_id = await continuation(staged_env)
    source.write_bytes(b"changed original after staging")
    relative = await service.stage_continuation_subtitle(attempt_id, "movie.srt")
    assert relative == f"jobs/{job_id}/subtitle.srt"
    assert (service.workspace_dir / relative).read_bytes() == b"subtitle"
    (service.workspace_dir / relative).write_bytes(b"edited")
    assert (source.parent / "movie.srt").read_bytes() == b"subtitle"
    assert source.read_bytes() == b"changed original after staging"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["suspended", "roots", "attempt"])
async def test_continuation_refuses_changes(staged_env, change: str):
    factory, service, job_id, source = staged_env
    attempt_id = await continuation(staged_env)
    async with factory() as session:
        job = await session.get(Job, job_id)
        library = await session.get(Library, job.library_id)
        if change == "suspended":
            library.suspended = True
        elif change == "roots":
            library.roots = [str(source.parent / "other")]
        else:
            job.attempts = 2
        await session.commit()
    with pytest.raises(ValueError):
        await service.stage_continuation_subtitle(attempt_id, "movie.srt")
    assert not (service.workspace_dir / f"jobs/{job_id}/subtitle.srt").exists()
    assert (source.parent / "movie.srt").read_bytes() == b"subtitle"
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("workload", ["healthcheck", "transcode"])
async def test_prune_healthcheck_logs(staged_env, workload: str):
    factory, service, job_id, source = staged_env
    relative = workspace.create_attempt_layout(service.workspace_dir, job_id, 1, ".mkv")["log"]
    log = service.workspace_dir / relative
    log.write_bytes(b"healthcheck log")
    async with factory() as session:
        job = await session.get(Job, job_id)
        job.kind, job.status, job.attempts = "healthcheck", JobStatus.QUEUED, 1
        attempt = JobAttempt(
            job_id=job_id,
            attempt_number=1,
            status=JobStatus.FAILED,
            result="interrupted",
            workload=workload,
            log_path=relative,
            log_data=log.read_bytes(),
            finished_at=utcnow(),
            error="requeued",
        )
        session.add(attempt)
        await session.flush()
        await session.commit()
    eligible = False
    assert await service.prune_attempt(attempt.id) is eligible
    assert log.exists() is not eligible
    async with factory() as session:
        saved = await session.get(JobAttempt, attempt.id)
        assert saved is not None and saved.result == "interrupted"
        assert saved.log_data == b"healthcheck log"
    assert source.read_bytes() == b"source"


async def catalog_request(staged_env, *, missing: bool = True):
    factory, service, job_id, source = staged_env
    assert await service.stage_job(job_id) == 1
    async with factory() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.FAILED
        media = await session.get(MediaFile, job.media_file_id)
        library = await session.get(Library, job.library_id)
        library.scan_id = 1
        await session.flush()
        snapshot, stamp = MediaSnapshot.from_media(media), library.updated_at
        await session.commit()
    if missing:
        source.unlink()
    return snapshot, stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transcode", "healthcheck", "mkv_stats"])
async def test_catalog_prunes_locked(staged_env, kind):
    factory, service, job_id, source = staged_env
    snapshot, stamp = await catalog_request(staged_env)
    if kind != "transcode":
        async with factory() as session:
            job = await session.get(Job, job_id)
            job.kind, job.attempts = kind, 1
            session.add(
                JobAttempt(
                    job_id=job_id,
                    attempt_number=1,
                    workload=kind,
                    capabilities={},
                    status="failed",
                    result="failed",
                    finished_at=utcnow(),
                    log_data=b"staging failed",
                )
            )
            await session.commit()
    retained = source.parent / "retained.mkv"
    retained.write_bytes(b"original retained elsewhere")
    assert (
        await service.prune_catalog([snapshot], library_updated_at=stamp, scan_id=1, unavailable=set(), excluded=())
        == 1
    )
    assert not (service.workspace_dir / "jobs" / str(job_id)).exists()
    async with factory() as session:
        assert await session.get(MediaFile, snapshot.id) is None
        assert await session.get(Job, job_id) is None
        assert list(await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))) == []
    assert retained.read_bytes() == b"original retained elsewhere"


@pytest.mark.asyncio
async def test_catalog_excludes_active(staged_env):
    factory, service, job_id, source = staged_env
    snapshot, stamp = await catalog_request(staged_env, missing=False)
    assert (
        await service.prune_catalog(
            [snapshot], library_updated_at=stamp, scan_id=1, unavailable=set(), excluded=(source.parent,)
        )
        == 1
    )
    async with factory() as session:
        assert await session.get(MediaFile, snapshot.id) is None
        assert await session.get(Job, job_id) is None
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_catalog_prunes_unknown(staged_env):
    factory, service, job_id, source = staged_env
    snapshot, stamp = await catalog_request(staged_env, missing=False)
    async with factory() as session:
        media = await session.get(MediaFile, snapshot.id)
        media.source_identity = None
        await session.flush()
        snapshot = MediaSnapshot.from_media(media)
        await session.commit()
    assert (
        await service.prune_catalog([snapshot], library_updated_at=stamp, scan_id=1, unavailable=set(), excluded=())
        == 1
    )
    async with factory() as session:
        assert await session.get(MediaFile, snapshot.id) is None
        assert await session.get(Job, job_id) is None
    assert source.read_bytes() == b"source"
    assert not (service.workspace_dir / f"jobs/{job_id}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["catalog", "library", "unavailable", "root", "notification", "active", "recovery"])
async def test_catalog_rechecks_attempt(staged_env, monkeypatch, change: str):
    factory, service, job_id, source = staged_env
    snapshot, stamp = await catalog_request(staged_env)
    unavailable = {source.parent} if change == "unavailable" else set()
    async with factory() as session:
        if change == "catalog":
            media = await session.get(MediaFile, snapshot.id)
            media.path = str(source.parent / "new-generation.mkv")
        elif change == "library":
            (await session.get(Library, snapshot.library_id)).roots = [str(source.parent / "other")]
        elif change == "notification":
            event_id = await session.scalar(select(JobEvent.id).where(JobEvent.job_id == job_id))
            session.add(
                NotificationDelivery(job_id=job_id, job_event_id=event_id, target_name="operator", status="pending")
            )
        elif change == "recovery":
            attempt = JobAttempt(job_id=job_id, attempt_number=1, status="failed")
            session.add(attempt)
            await session.flush()
            session.add(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path=str(source),
                    destination_path=str(source),
                    partial_path=str(source.parent / ".1.part"),
                    trash_path=str(source.parent / ".1.discard"),
                    state="discard_prepared",
                )
            )
        await session.commit()
    if change == "root":
        acquire = staging.directory_fd

        def unavailable_root(path, **kwargs):
            if path == source.parent:
                raise PermissionError("library became unavailable")
            return acquire(path, **kwargs)

        monkeypatch.setattr("server.staging.directory_fd", unavailable_root)
    elif change == "active":
        service.active_jobs.add(job_id)
    try:
        assert (
            await service.prune_catalog(
                [snapshot], library_updated_at=stamp, scan_id=1, unavailable=unavailable, excluded=()
            )
            == 0
        )
    finally:
        service.active_jobs.discard(job_id)
    assert (service.workspace_dir / f"jobs/{job_id}/source.mkv").read_bytes() == b"source"
    async with factory() as session:
        assert await session.get(MediaFile, snapshot.id) is not None
        assert await session.get(Job, job_id) is not None


@pytest.mark.asyncio
async def test_catalog_keeps_revived(staged_env, monkeypatch):
    factory, service, job_id, source = staged_env
    snapshot, stamp = await catalog_request(staged_env)
    remove = workspace._remove_job

    def revived(root: Path, identifier: int):
        remove(root, identifier)
        source.write_bytes(b"revived source generation")

    monkeypatch.setattr("server.staging._remove_job", revived)
    assert (
        await service.prune_catalog([snapshot], library_updated_at=stamp, scan_id=1, unavailable=set(), excluded=())
        == 1
    )
    async with factory() as session:
        assert await session.get(MediaFile, snapshot.id) is None
        assert await session.get(Job, job_id) is None
    assert source.read_bytes() == b"revived source generation"


@pytest.mark.asyncio
async def test_partial_directory_conflict(staged_env, monkeypatch):
    _, service, job_id, source = staged_env
    folder = service.workspace_dir / "jobs" / str(job_id)
    folder.mkdir(parents=True)
    partial_dir = folder / ".fixed.partial"
    partial_dir.mkdir()
    existing_file = partial_dir / "file"
    existing_file.write_bytes(b"unexpected partial")
    monkeypatch.setattr("common.fs.acquire.uuid.uuid4", lambda: SimpleNamespace(hex="fixed"))
    assert await service.stage_job(job_id) == 0
    assert not folder.exists()
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_close_drains_writer(staged_env, monkeypatch):
    _, service, job_id, source = staged_env
    started, closing_started = asyncio.Event(), asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    copy, close, shield = workspace._stage_source, service._close, asyncio.shield

    def blocked(*args, **kwargs):
        result = copy(*args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return result

    async def closing():
        closing_started.set()
        await close()

    def tracked_shield(future):
        protected = shield(future)
        protected.add_done_callback(lambda result: cancellation_seen.set() if result.cancelled() else None)
        return protected

    monkeypatch.setattr("server.staging._stage_source", blocked)
    monkeypatch.setattr(service, "_close", closing)
    monkeypatch.setattr(asyncio, "shield", tracked_shield)
    closers = []
    service.start()
    staging = asyncio.create_task(service.stage_job(job_id))
    try:
        await asyncio.wait_for(started.wait(), 3)
        first = asyncio.create_task(service.aclose())
        closers.append(first)
        await asyncio.wait_for(closing_started.wait(), 3)
        second = asyncio.create_task(service.aclose())
        closers.append(second)
        for _ in range(2):
            cancellation_seen.clear()
            first.cancel()
            await asyncio.wait_for(cancellation_seen.wait(), 3)
            assert not first.done() and not second.done()
            assert job_id in service.active_jobs
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 3)
        await asyncio.wait_for(second, 3)
        assert await asyncio.wait_for(staging, 3) == 1
        assert service._task is None and not service.active_jobs
        assert await service.stage_job(job_id) == 0
        assert source.read_bytes() == b"source"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(staging, *closers, return_exceptions=True), 3)
        await asyncio.wait_for(service.aclose(), 3)
