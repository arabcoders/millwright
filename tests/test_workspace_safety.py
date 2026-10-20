from __future__ import annotations

import asyncio
import os
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("local_storage")
from common.media.probe import ProbeData
from common.plan import FFmpegPlan, SubtitleBurn, VideoTarget
from common.protocol import NodeCapabilities
from common.enums import JobStatus
from common.workspace import workspace_lock
from sqlalchemy import select
from server import workspace
from server.flows.engine import FlowResult
from server.healthcheck import HealthcheckService
from server.models import Job, JobAttempt, JobEvent
from server.nodehub.hub import NodeEntry, NodeHub
from server.nodehub.store import register_node
from server.queue import store
from server.staging import StagingService
from tests.helpers import request_retry, database_context, plugin_host
from tests.test_staging import T0, add_job, get_job
from tests.test_workspace import stage_source, stage_subtitle


def test_stage_root_link(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    other = root / "jobs/2"
    other.mkdir(parents=True)
    existing_file = other / "source.mkv"
    existing_file.write_bytes(b"another job's media")
    (root / "jobs/1").symlink_to(other, target_is_directory=True)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"incoming media")

    with pytest.raises(ValueError):
        stage_source(root, 1, source)
    assert existing_file.read_bytes() == b"another job's media"


def test_cleanup_root_link(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    other = root / "jobs/2"
    other.mkdir(parents=True)
    existing_file = other / "output.mkv"
    existing_file.write_bytes(b"another job's output")
    (root / "jobs/1").symlink_to(other, target_is_directory=True)

    with pytest.raises(ValueError):
        workspace._remove_job(root, 1)
    assert existing_file.read_bytes() == b"another job's output"


@pytest.mark.parametrize("entry_kind", ["file", "symlink"])
def test_cleanup_preserves_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_kind: str) -> None:
    root = tmp_path / "workspace"
    folder = root / "jobs" / "1"
    (folder / "attempts" / "1").mkdir(parents=True)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"library media")
    late = folder / "late"
    remove = os.rmdir

    def add_entry(name, *, dir_fd=None):
        if name == "1" and dir_fd is not None and Path(f"/proc/self/fd/{dir_fd}").resolve() == root / "jobs":
            if entry_kind == "file":
                late.write_bytes(b"")
            else:
                late.symlink_to(source)
        return remove(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", add_entry)
    assert not workspace._remove_job(root, 1, empty_only=True)
    assert folder.exists() and late.exists()
    assert source.read_bytes() == b"library media"
    if entry_kind == "file":
        assert late.read_bytes() == b""
    else:
        assert late.is_symlink()


@pytest.mark.parametrize("entry", ["source", "manifest", "log"])
def test_staged_root_links(tmp_path: Path, entry: str) -> None:
    root = tmp_path / "workspace"
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    stage_source(root, 1, source)
    other = root / "jobs/2"
    other.mkdir()
    if entry == "log":
        destination = root / "jobs/1/ffmpeg.log"
        target = other / "ffmpeg.log"
        target.write_bytes(b"another worker's log")
    else:
        destination = root / f"jobs/1/{'source.mkv' if entry == 'source' else 'manifest.json'}"
        target = other / destination.name
        destination.rename(target)
    destination.symlink_to(target)

    with pytest.raises((OSError, ValueError)):
        if entry == "log":
            workspace.read_attempt_log(root, "jobs/1/ffmpeg.log")
        else:
            workspace.staged_source(root, 1)


def test_subtitle_copy_independent(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    subtitle = tmp_path / "movie.srt"
    subtitle.write_bytes(b"subtitle")
    root = tmp_path / "workspace"
    relative = stage_subtitle(root, 1, source, subtitle.name)
    (root / relative).write_bytes(b"edited")
    assert subtitle.read_bytes() == b"subtitle"
    assert source.read_bytes() == b"source"


@pytest.mark.parametrize("source_name", ["movie.mkv", "alias.mkv"])
def test_stages_library_hardlinks(tmp_path: Path, source_name: str) -> None:
    library = tmp_path / "library"
    library.mkdir()
    source = library / "movie.mkv"
    alias = library / "alias.mkv"
    source.write_bytes(b"original")
    os.link(source, alias)
    root = tmp_path / "workspace"

    relative = stage_source(root, 1, library / source_name)
    staged = root / relative
    assert not staged.samefile(source)
    assert not staged.samefile(alias)
    assert staged.read_bytes() == b"original"
    assert stage_source(root, 1, library / source_name) == relative
    assert workspace.staged_source(root, 1) == relative

    staged.write_bytes(b"workspace edit")
    for path in (source, alias):
        assert path.read_bytes() == b"original"


@pytest.mark.parametrize("surface", ["source", "subtitle", "log"])
def test_workspace_alias_retained(tmp_path: Path, surface: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"original")
    root = tmp_path / "workspace"
    relative = stage_source(root, 1, source)
    if surface == "subtitle":
        (source.parent / "movie.srt").write_bytes(b"original")
        relative = stage_subtitle(root, 1, source, "movie.srt")
    elif surface == "log":
        relative = "jobs/1/attempts/1/ffmpeg.log"
        log = root / relative
        log.parent.mkdir(parents=True)
        log.write_bytes(b"original")
    staged = root / relative
    partial_dir = staged.parent / f".{32 * '0'}.partial"
    partial_dir.mkdir(mode=0o700)
    alias = partial_dir / "file"
    os.link(staged, alias)
    assert staged.samefile(alias)
    assert not staged.samefile(source)

    if surface == "source":
        assert stage_source(root, 1, source) == relative
        assert workspace.staged_source(root, 1) == relative
    elif surface == "subtitle":
        assert workspace.staged_subtitle(root, 1, "movie.srt") == relative
    else:
        assert workspace.read_attempt_log(root, relative) == b"original"
    assert staged.read_bytes() == alias.read_bytes() == source.read_bytes() == b"original"


@pytest.mark.parametrize("entry", ["directory", "fifo", "symlink"])
def test_cached_source_nonregular(tmp_path: Path, entry: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"original")
    root = tmp_path / "workspace"
    staged = root / stage_source(root, 1, source)
    manifest = staged.parent / "manifest.json"
    saved = manifest.read_bytes()
    staged.unlink()
    if entry == "directory":
        staged.mkdir()
    elif entry == "fifo":
        os.mkfifo(staged)
    else:
        staged.symlink_to(source)

    with pytest.raises(workspace._StagingConflictError):
        stage_source(root, 1, source)
    with pytest.raises((OSError, ValueError)):
        workspace.staged_source(root, 1)
    assert manifest.read_bytes() == saved
    assert source.read_bytes() == b"original"


def test_manifest_alias_retained(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"original")
    root = tmp_path / "workspace"
    relative = stage_source(root, 1, source)
    dates = workspace.staged_dates(root, 1)
    manifest = (root / relative).parent / "manifest.json"
    alias = manifest.parent / "retained-manifest.json"
    os.link(manifest, alias)

    assert stage_source(root, 1, source) == relative
    assert workspace.staged_source(root, 1) == relative
    assert workspace.verify_staged_source(root, 1, source) == {"size": len(b"original")}
    assert workspace.staged_dates(root, 1) == dates
    assert alias.read_bytes() == manifest.read_bytes()
    assert (root / relative).read_bytes() == b"original"


@pytest.mark.asyncio
async def test_cleanup_cancel_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    cancellation_seen = asyncio.Event()
    unshielded = False
    loop = asyncio.get_running_loop()
    remove = workspace._remove_job
    to_thread = asyncio.to_thread
    wait = asyncio.wait

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
    async with database_context("cleanup-cancel-check") as factory:
        job_id = await add_job(factory, source, status=JobStatus.CANCELLED)
        stage_source(root, job_id, source)

        async def cleanup() -> None:
            await StagingService(factory, root, min_free_gb=1).prune_job(job_id, attempt_number=0)

        task = asyncio.create_task(cleanup())
        retried = False
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.wait_for(cancellation_seen.wait(), 3)
            if unshielded:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
                async with factory() as session:
                    request_id = await request_retry(session, job_id)
                    assert await store.retry_job(session, job_id, request_id=request_id) is not None
                    await session.commit()
                retried = True
                (root / f"jobs/{job_id}/retry-output").write_bytes(b"retry output")
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            assert await asyncio.to_thread(finished.wait, 3)
        if retried:
            assert (root / f"jobs/{job_id}/retry-output").is_file(), "old cleanup deleted the retry's output"


@pytest.mark.asyncio
async def test_staging_cancel_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    async with database_context("staging-cancel-commit") as factory:
        job_id = await add_job(factory, source)
        copied = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        copy = workspace._stage_source

        def blocked_copy(*args, **kwargs):
            result = copy(*args, **kwargs)
            loop.call_soon_threadsafe(copied.set)
            assert release.wait(5)
            return result

        monkeypatch.setattr("server.staging._stage_source", blocked_copy)
        staging = StagingService(factory, root, min_free_gb=1)
        task = asyncio.create_task(staging.stage_job(job_id))
        try:
            await asyncio.wait_for(copied.wait(), 3)
            async with factory() as other:
                assert await asyncio.wait_for(store.cancel_pending(other, job_id), 3) is not None
                await asyncio.wait_for(other.commit(), 3)
            assert not task.done() and job_id in staging.active_jobs
            assert (root / f"jobs/{job_id}/source.mkv").read_bytes() == b"source"
            release.set()
            assert await asyncio.wait_for(task, 3) == 0
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert (await get_job(factory, job_id)).status == JobStatus.CANCELLED
        assert not staging.active_jobs
        assert not (root / f"jobs/{job_id}").exists()
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", ["send_error", "send_cancel", "expired"])
async def test_lost_attempt_cleanup(tmp_path: Path, handoff: str) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    offers: list[dict] = []

    class Connection:
        async def send_json(self, data: dict) -> None:
            offers.append(data)
            output = root / data["job"]["output_path"]
            output.write_bytes(b"unfinished output")
            (root / data["job"]["log_path"]).write_bytes(b"worker output")
            if handoff == "send_error":
                raise OSError("send failed after delivery")
            if handoff == "send_cancel":
                raise asyncio.CancelledError

        async def recv_json(self) -> dict | None:
            return None

        async def close(self, code: int = 1000) -> None:
            pass

    async with database_context("ambiguous-attempt") as factory:
        job_id = await add_job(factory, source, status=JobStatus.QUEUED)
        stage_source(root, job_id, source)
        async with factory() as session:
            event = await session.scalar(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "enqueued"))
            assert event is not None
            event.payload = {
                **event.payload,
                "probe": ProbeData(file_path=str(source), duration_s=60, size_bytes=6, streams=[]).model_dump(
                    mode="json"
                ),
            }
            node = await register_node(session, "worker", "test-token")
            await session.commit()
        hub = NodeHub(factory, workspace_dir=root, clock=lambda: T0, min_free_gb=1)
        hub.entries[node.name] = NodeEntry(
            name=node.name,
            node_db_id=node.id,
            capabilities=NodeCapabilities(),
            workspace_ready=True,
            conn=Connection(),
        )
        if handoff == "send_cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(hub.dispatch_once(), 3)
        else:
            await asyncio.wait_for(hub.dispatch_once(), 3)
        assert len(offers) == 1
        offer = offers[0]["job"]
        if handoff != "expired":
            async with factory() as session:
                attempt = await session.get(JobAttempt, offer["attempt_id"])
                current = await session.get(Job, job_id)
                assert attempt is not None and attempt.status == JobStatus.CLAIMED and attempt.result is None
                assert current is not None and current.status == JobStatus.CLAIMED and current.node_id == node.id
            assert job_id in hub.entries[node.name].in_flight
            assert (root / offer["output_path"]).read_bytes() == b"unfinished output"
            assert (await hub.staging.prune_job(job_id))["outcome"] == "skipped"
        with workspace_lock((root / offer["output_path"]).parent):
            if handoff == "expired":
                hub.entries.clear()
            else:
                await asyncio.wait_for(hub._disconnect_entry(hub.entries[node.name]), 3)
            hub.clock = lambda: T0 + timedelta(seconds=301)
            await asyncio.wait_for(hub.sweep_once(), 3)
            async with factory() as session:
                attempt = await session.get(JobAttempt, offer["attempt_id"])
                current = await session.get(Job, job_id)
                assert attempt is not None and attempt.result == "failed"
                assert attempt.log_data == b"worker output"
                assert current is not None and current.status == JobStatus.STAGING and current.node_id is None
            assert (root / offer["output_path"]).read_bytes() == b"unfinished output"
            assert not await hub.staging.prune_attempt(offer["attempt_id"])
        assert await hub.staging.prune_attempt(offer["attempt_id"])
        assert not (root / offer["output_path"]).exists()
        assert not (root / offer["log_path"]).exists()
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_source", [False, True])
@pytest.mark.parametrize("max_attempts", [1, 3])
async def test_attempt_collision_locked(tmp_path: Path, invalid_source: bool, max_attempts: int) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"

    class Connection:
        async def send_json(self, data: dict) -> None:
            pytest.fail("colliding attempt must not be offered")

        async def recv_json(self) -> dict | None:
            return None

        async def close(self, code: int = 1000) -> None:
            pass

    async with database_context("attempt-collision") as factory:
        job_id = await add_job(factory, source, status=JobStatus.QUEUED)
        stage_source(root, job_id, source)
        async with factory() as session:
            event = await session.scalar(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "enqueued"))
            assert event is not None
            event.payload = {
                **event.payload,
                "probe": ProbeData(file_path=str(source), duration_s=60, size_bytes=6, streams=[]).model_dump(
                    mode="json"
                ),
            }
            node = await register_node(session, "worker", "test-token")
            await session.commit()
        layout = workspace.create_attempt_layout(root, job_id, 1, ".mkv")
        output = root / layout["output"]
        output.write_bytes(b"pre-existing recovery output")
        if invalid_source:
            (root / f"jobs/{job_id}/source.mkv").unlink()
        hub = NodeHub(factory, workspace_dir=root, clock=lambda: T0, max_attempts=max_attempts, min_free_gb=1)
        hub.entries[node.name] = NodeEntry(
            name=node.name,
            node_db_id=node.id,
            capabilities=NodeCapabilities(),
            workspace_ready=True,
            conn=Connection(),
        )

        with workspace_lock(output.parent):
            assert await asyncio.wait_for(hub.dispatch_once(), 3) == 0
            assert output.read_bytes() == b"pre-existing recovery output"
            assert (await hub.staging.prune_job(job_id))["outcome"] == "skipped"
        assert source.read_bytes() == b"source"
        async with factory() as session:
            attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
            assert attempt is not None and attempt.result == "interrupted"
        assert await hub.staging.prune_attempt(attempt.id)
        assert not output.exists()
        if max_attempts == 1:
            assert (await hub.staging.prune_job(job_id))["outcome"] == "removed"
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_continuation_subtitle_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    subtitle = tmp_path / "movie.srt"
    subtitle.write_bytes(b"subtitle")
    root = tmp_path / "workspace"
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _root: SimpleNamespace(free=0))
    async with database_context("continuation-subtitle") as factory:
        job_id = await add_job(factory, source, status=JobStatus.RUNNING)
        stage_source(root, job_id, source)
        probe = ProbeData(file_path="jobs/1/attempts/1/output.mkv", duration_s=60, size_bytes=6, streams=[])
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.attempts = 1
            attempt = JobAttempt(job_id=job_id, attempt_number=1, status=JobStatus.RUNNING)
            session.add(attempt)
            await session.flush()
            session.add(
                JobEvent(
                    job_id=job_id,
                    attempt_id=attempt.id,
                    type="stage",
                    payload={"output_probe": probe.model_dump(mode="json")},
                )
            )
            await session.commit()
        with pytest.raises(ValueError):
            await asyncio.wait_for(
                StagingService(factory, root, min_free_gb=1).stage_continuation_subtitle(attempt.id, subtitle.name), 3
            )
        assert not (root / f"jobs/{job_id}/subtitle.srt").exists(), "continuation copied a subtitle without disk queue"
        assert subtitle.read_bytes() == b"subtitle"


@pytest.mark.asyncio
async def test_healthcheck_collision_preserved(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    async with database_context("healthcheck-collision") as factory:
        job_id = await add_job(factory, source, status=JobStatus.QUEUED)
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.kind = "healthcheck"
            await session.commit()
        root = tmp_path / "healthchecks"
        root.mkdir()
        service = HealthcheckService(factory, root, "unused", "unused")
        claimed = await service.claim_next()
        assert claimed is not None
        output = root / f"{claimed[1]}.log"
        output.write_bytes(b"pre-existing recovery output")
        await asyncio.wait_for(service.run(*claimed), 3)
        assert output.is_file(), "a failed directory claim cannot authorize deleting its existing directory"
        assert output.read_bytes() == b"pre-existing recovery output"
        assert source.read_bytes() == b"source"
