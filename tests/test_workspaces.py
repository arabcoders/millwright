from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from common.workspace import workspace_lock

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.retention import delete_eligible_job
from server.backup import reset_data
from server.core.config import ServerConfig
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, MediaFile
from server.queue.store import cancel_pending, retry_job
from server.staging import StagingService
from server.workspace import create_attempt_layout, cleanup_workspace
from server.workspaces import WorkspaceService
from server import workspace as workspace_ops
from tests.helpers import request_retry, database_context, fd_path
from tests.test_staging import add_job


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Path, StagingService]]:
    root = tmp_path / "workspace"
    (root / "jobs").mkdir(parents=True)
    async with database_context("workspaces") as factory:
        staging = StagingService(factory, root, min_free_gb=1)
        try:
            yield factory, root, staging
        finally:
            await staging.aclose()


async def workspace(env, *, status: str = "failed", result: str | None = "failed") -> tuple[int, int]:
    factory, root, _ = env
    source = root.parent / f"library-{uuid4().hex}" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"source")
    job_id = await add_job(factory, source, status=status)
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.attempts = 1
        attempt = JobAttempt(job_id=job_id, attempt_number=1, status=status, result=result)
        session.add(attempt)
        await session.flush()
        attempt_id = attempt.id
        await session.commit()
    folder = root / "jobs" / str(job_id)
    (folder / "attempts" / "1" / "artifacts").mkdir(parents=True)
    (folder / "source.mkv").write_bytes(b"source")
    (folder / "manifest.json").write_text('{"private": "original identity"}')
    (folder / "attempts" / "1" / "output.mkv").write_bytes(b"output")
    (folder / "attempts" / "1" / "output-2.mkv").write_bytes(b"next")
    (folder / "attempts" / "1" / "ffmpeg.log").write_bytes(b"log")
    (folder / "attempts" / "1" / "artifacts" / "report.txt").write_bytes(b"artifact")
    return job_id, attempt_id


@pytest_asyncio.fixture
async def workspace_client(env) -> AsyncIterator[httpx.AsyncClient]:
    factory, root, _ = env
    app = create_app(factory, config=ServerConfig(workspace_dir=str(root)))
    try:
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        await app.state.workspaces.aclose()
        await app.state.events.aclose()
        await app.state.plugin_host.aclose()


@pytest_asyncio.fixture
async def listing(env) -> list[int]:
    factory, root, _ = env
    ids = []
    for filename, status, node_name, size, age in [
        ("alpha.mkv", "failed", "delta", 30, 3),
        ("Bravo.mkv", "running", "Echo", 10, 1),
        ("charlie.mkv", "succeeded", "delta", 20, 2),
    ]:
        job_id, attempt_id = await workspace(env, status=status, result=status)
        ids.append(job_id)
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            media = await session.get(MediaFile, job.media_file_id)
            attempt = await session.get(JobAttempt, attempt_id)
            assert media is not None and attempt is not None
            source = Path(media.path)
            source.rename(source.with_name(filename))
            media.path = str(source.with_name(filename))
            attempt.node_name = node_name
            await session.commit()
        folder = root / "jobs" / str(job_id)
        (folder / "source.mkv").write_bytes(b"x" * size)
        for path in [folder, *folder.rglob("*")]:
            os.utime(path, (1_700_000_000 + age, 1_700_000_000 + age))
    orphan = root / "jobs" / "0"
    orphan.mkdir()
    os.utime(orphan, (1_700_000_004, 1_700_000_004))
    (root / "jobs" / "999").symlink_to(orphan, target_is_directory=True)
    return ids


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transcode", "healthcheck", "mkv_stats"])
async def test_lists_actual_workspaces(env, kind: str) -> None:
    from server.queue.store import remove_node
    from server.nodehub.store import register_node

    factory, root, staging = env
    job_id, attempt_id = await workspace(env)
    async with factory() as session:
        node = await register_node(session, "node-workspace", "token")
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, attempt_id)
        assert job is not None and attempt is not None
        job.kind = attempt.workload = kind
        job.node_id = attempt.node_id = node.id
        attempt.capabilities = {"ffmpeg_version": "fake"}
        attempt.log_data = b"log"
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        source = Path(media.path)
        assert await remove_node(session, node.id)
        assert attempt.node_id is None and attempt.capabilities == {"ffmpeg_version": "fake"}
        await session.commit()
    await add_job(factory, None, status="succeeded")
    (root / "jobs" / "notes").mkdir()
    (root / "jobs" / "03").mkdir()
    service = WorkspaceService(factory, root, staging=staging)
    snapshot = await service.sample()
    assert snapshot["summary"]["count"] == 1
    assert snapshot["summary"]["unused_count"] == 1
    assert snapshot["summary"]["active_count"] == 0
    assert snapshot["summary"]["free_bytes"] > 0
    row = snapshot["items"][0]
    assert row["workspace_id"] == row["job_id"] == job_id
    assert row["filename"] == "movie.mkv"
    assert row["source_bytes"] == 6 and row["output_bytes"] == 10
    assert row["file_count"] == 6
    assert row["files"] and row["attempts"] and row["sampled_at"] == snapshot["sampled_at"]
    assert snapshot["summary"]["unused_size_bytes"] == row["size_bytes"]
    assert row["size_bytes"] == sum(
        file.stat().st_size for file in (root / "jobs" / str(job_id)).rglob("*") if file.is_file()
    )
    detail = await service.detail(job_id)
    assert detail["attempts"][0]["attempt_number"] == 1
    assert all(file["path"].startswith(f"jobs/{job_id}/") for file in detail["files"])
    assert {file["path"].split("/")[-1] for file in detail["files"]} >= {
        "source.mkv",
        "ffmpeg.log",
        "output-2.mkv",
        "report.txt",
    }
    assert all(file["modified_at"] for file in detail["files"])
    assert await service.cleanup() == 1
    assert not (root / "jobs" / str(job_id)).exists()
    assert source.read_bytes() == b"source"
    async with factory() as session:
        retained = await session.get(JobAttempt, attempt_id)
        assert retained is not None and retained.log_data == b"log"
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["verifying", "finalizing"])
async def test_prunes_stopped_publication(env, status: str) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env, status=status)
    service = WorkspaceService(factory, root, staging=staging)
    try:
        assert (await service.detail(job_id))["eligible"]
        summary = (await service.sample())["summary"]
        assert summary["unused_count"] == 1 and summary["active_count"] == 0
        assert await service.cleanup() == 1
        assert not (root / "jobs" / str(job_id)).exists()
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["job", "attempt"])
@pytest.mark.parametrize("orphan", [False, True])
async def test_writer_blocks_cleanup(env, target: str, orphan: bool) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env)
    folder = root / "jobs" / str(job_id)
    if orphan:
        async with factory() as session:
            assert await delete_eligible_job(session, job_id)
            await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    path = folder if target == "job" else folder / "attempts" / "1"
    try:
        with workspace_lock(path):
            row = await service.detail(job_id)
            assert not row["eligible"]
            summary = (await service.sample())["summary"]
            assert summary["active_count"] == 1 and summary["unused_count"] == 0
            result = await staging.prune_job(job_id)
            assert result["outcome"] == "skipped"
            if target == "attempt" and not orphan:
                assert not await staging.prune_attempt(attempt_id)
            assert (await cleanup_workspace(factory, root))["preserved"] == 1
            assert (folder / "attempts" / "1" / "output.mkv").read_bytes() == b"output"
        assert await service.cleanup() == 1
        assert not folder.exists()
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["job", "attempt"])
async def test_snapshot_rechecks_processing(env, monkeypatch, target: str) -> None:
    _, root, staging = env
    job_id, attempt_id = await workspace(env)

    async def snapshot(_session, _root, _attempt):
        staging.busy_jobs.add(job_id)

    with monkeypatch.context() as patch:
        patch.setattr("server.staging.snapshot_attempt_log", snapshot)
        if target == "job":
            result = await staging.prune_job(job_id)
            assert result["outcome"] == "skipped"
        else:
            assert not await staging.prune_attempt(attempt_id)
    folder = root / "jobs" / str(job_id)
    assert (folder / "attempts" / "1" / "output.mkv").read_bytes() == b"output"
    staging.busy_jobs.remove(job_id)
    assert (await staging.prune_job(job_id))["outcome"] == "removed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["staging", "queued", "hold"])
async def test_prunes_queued_attempt(env, status: str) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env)
    folder = root / "jobs" / str(job_id)
    attempt_folder = folder / "attempts" / "1"
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = status
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        with workspace_lock(attempt_folder):
            assert not await staging.prune_attempt(attempt_id)
            assert await service.cleanup() == 0
            assert (attempt_folder / "output.mkv").read_bytes() == b"output"
        assert await service.cleanup() == 1
        assert not attempt_folder.exists()
        assert (folder / "source.mkv").read_bytes() == b"source"
        assert (folder / "manifest.json").exists()
        assert not (await service.detail(job_id))["eligible"]
        assert await service.cleanup() == 0
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_prunes_previous_attempt(env) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env)
    folder = root / "jobs" / str(job_id)
    current = folder / "attempts" / "2"
    current.mkdir()
    (current / "output.mkv").write_bytes(b"current output")
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = "running"
        job.attempts = 2
        session.add(JobAttempt(job_id=job_id, attempt_number=2, status="running"))
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        with workspace_lock(current):
            assert await service.cleanup() == 1
            assert not (folder / "attempts" / "1").exists()
            assert (current / "output.mkv").read_bytes() == b"current output"
            assert (folder / "source.mkv").read_bytes() == b"source"
        assert await service.cleanup() == 0
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["job", "attempt"])
async def test_cleanup_detaches_directory(env, monkeypatch, target: str) -> None:
    _, root, staging = env
    job_id, attempt_id = await workspace(env)
    folder = root / "jobs" / str(job_id)
    original = folder if target == "job" else folder / "attempts" / "1"
    remove = workspace_ops.shutil.rmtree
    checked = False

    def removing(name, *, dir_fd):
        nonlocal checked
        with pytest.raises(FileNotFoundError), workspace_lock(original):
            pytest.fail("A worker acquired a replacement lock during deletion.")
        detached = fd_path(name, dir_fd)
        with contextlib.ExitStack() as locks:
            locks.enter_context(workspace_lock(detached, exclusive=True))
            if target == "job":
                locks.enter_context(workspace_lock(detached / "attempts" / "1", exclusive=True))
        checked = True
        remove(name, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_ops.shutil, "rmtree", removing)
    if target == "job":
        assert (await staging.prune_job(job_id))["outcome"] == "removed"
    else:
        assert await staging.prune_attempt(attempt_id)
    assert checked and not original.exists()


@pytest.mark.asyncio
async def test_abandons_pending_actions(env) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status="succeeded")
    async with factory() as session:
        session.add(
            JobEvent(
                job_id=job_id,
                attempt_id=attempt_id,
                type="actions_pending",
                payload={"actions": [{"kind": "extract_subtitles"}]},
            )
        )
        await session.commit()
    folder = root / "jobs" / str(job_id)
    with workspace_lock(folder / "attempts" / "1"):
        assert (await staging.prune_job(job_id))["outcome"] == "skipped"
        async with factory() as session:
            assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "actions_finished")) is None
    assert (await staging.prune_job(job_id))["outcome"] == "removed"
    async with factory() as session:
        event = await session.scalar(select(JobEvent).where(JobEvent.type == "actions_finished"))
        attempt = await session.get(JobAttempt, attempt_id)
        job = await session.get(Job, job_id)
        assert event is not None and event.payload["abandoned"]
        assert attempt is not None and attempt.action_results[-1]["ok"] is False
        assert job is not None and job.warning == event.payload["error"]
        assert await session.scalar(workspace_ops.post_action_jobs().where(Job.id == job_id)) is None


@pytest.mark.asyncio
async def test_pending_workspace(env) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env)
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = "pending"
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        snapshot = await service.sample()
        assert snapshot["summary"]["active_count"] == 1
        assert snapshot["summary"]["unused_count"] == 0
        assert not snapshot["items"][0]["eligible"]
        assert (await staging.prune_job(job_id))["outcome"] == "skipped"
        assert (root / "jobs" / str(job_id) / "source.mkv").exists()
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["hold", "staging", "queued", "claimed", "running", "verifying", "finalizing"])
async def test_protects_active_workspaces(env, status: str) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env, status=status)
    if status in {"verifying", "finalizing"}:
        staging.busy_jobs.add(job_id)
    service = WorkspaceService(factory, root, staging=staging)
    snapshot = await service.sample()
    assert not snapshot["items"][0]["eligible"]
    # Old filesystem activity does not prove that execution has stopped.
    assert (await staging.prune_job(job_id))["outcome"] == "skipped"
    assert (root / "jobs" / str(job_id) / "source.mkv").exists()
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["interrupted", "unprepared", "journal", "attempt", "post_actions"])
async def test_prunes_diagnostic_history(env, record_type: str) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status="succeeded")
    async with factory() as session:
        attempt = await session.get(JobAttempt, attempt_id)
        assert attempt is not None
        if record_type == "interrupted":
            attempt.result = "interrupted"
        elif record_type == "unprepared":
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="finalization_unprepared"))
        elif record_type == "journal":
            session.add(
                FinalizationJournal(
                    attempt_id=attempt_id,
                    source_path="/library/source",
                    destination_path="/library/dest",
                    partial_path="/library/partial",
                    trash_path="/trash/original",
                    state="prepared",
                )
            )
        elif record_type == "attempt":
            attempt.status = "running"
        else:
            job = await session.get(Job, job_id)
            assert job is not None
            job.post_actions = [{"kind": "notify"}]
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    assert (await service.sample())["items"][0]["eligible"]
    assert (await staging.prune_job(job_id))["outcome"] == "removed"
    assert not (root / "jobs" / str(job_id)).exists()
    async with factory() as session:
        assert await session.get(JobAttempt, attempt_id) is not None
        if record_type == "journal":
            assert await session.scalar(select(FinalizationJournal.id)) is not None
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_type",
    [
        "current",
        "active",
        "journal",
        "post_actions",
        "actions_pending",
        "healthcheck_log_unavailable",
        "execution_log_unavailable",
        "finalization_unprepared",
    ],
)
async def test_empty_history_pruned(env, record_type: str) -> None:
    factory, root, staging = env
    job_id = await add_job(factory, None, status="succeeded")
    folder = root / "jobs" / str(job_id)
    (folder / "attempts" / "1").mkdir(parents=True)
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.attempts = 2
        session.add(
            JobAttempt(job_id=job_id, attempt_number=1, status="failed", result="interrupted", workload="healthcheck")
        )
        current = JobAttempt(job_id=job_id, attempt_number=2, status="succeeded", result="succeeded")
        session.add(current)
        await session.flush()
        if record_type == "current":
            current.result = "interrupted"
        elif record_type == "active":
            current.status = "running"
        elif record_type == "journal":
            session.add(
                FinalizationJournal(
                    attempt_id=current.id,
                    source_path="/library/source",
                    destination_path="/library/dest",
                    partial_path="/library/partial",
                    trash_path="/trash/original",
                    state="prepared",
                )
            )
        elif record_type == "post_actions":
            job.post_actions = [{"kind": "notify"}]
        else:
            session.add(JobEvent(job_id=job_id, attempt_id=current.id, type=record_type))
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        detail = await service.detail(job_id)
        assert detail["file_count"] == 0 and detail["eligible"]
        assert (await staging.prune_job(job_id))["outcome"] == "removed"
        assert await cleanup_workspace(factory, root) == {"removed": 0, "preserved": 0}
        assert not folder.exists()
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "succeeded"])
@pytest.mark.parametrize("cleanup", ["job", "attempt", "startup", "scheduler"])
async def test_prunes_interrupted_workspace(env, status: str, cleanup: str) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status=status, result="interrupted")
    async with factory() as session:
        attempt = await session.get(JobAttempt, attempt_id)
        job = await session.get(Job, job_id)
        assert attempt is not None and job is not None
        attempt.error = "Node no longer owns this attempt."
        attempt.workload = "transcode"
        attempt.log_data = b"saved diagnostic log"
        if status == "succeeded":
            job.attempts = 2
            attempt.status = "failed"
            session.add(JobAttempt(job_id=job_id, attempt_number=2, status="succeeded", result="succeeded"))
        session.add(JobEvent(job_id=job_id, type="staging_unresolved", payload={"error": "old staging conflict"}))
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        source = Path(media.path)
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        row = await service.detail(job_id)
        assert row["eligible"]
        if cleanup == "job":
            assert (await staging.prune_job(job_id))["outcome"] == "removed"
        elif cleanup == "attempt":
            assert await staging.prune_attempt(attempt_id)
            assert (await staging.prune_job(job_id))["outcome"] == "removed"
        elif cleanup == "startup":
            assert (await cleanup_workspace(factory, root))["removed"] == 1
        else:
            assert await service.cleanup() == 1
        assert not (root / "jobs" / str(job_id)).exists()
        assert source.read_bytes() == b"source"
        async with factory() as session:
            assert await session.get(Job, job_id) is not None
            saved = await session.get(JobAttempt, attempt_id)
            assert saved is not None and saved.log_data == b"saved diagnostic log"
            assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "staging_unresolved")) is not None
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "succeeded"])
async def test_prunes_staging_history(env, status: str) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status=status)
    async with factory() as session:
        session.add(JobEvent(job_id=job_id, type="staging_unresolved", payload={"error": "old source rejection"}))
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        assert (await service.detail(job_id))["eligible"]
        assert await staging.prune_attempt(attempt_id)
        assert (await staging.prune_job(job_id))["outcome"] == "removed"
        assert not (root / "jobs" / str(job_id)).exists()
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_type", ["journal", "finalization_unprepared", "actions_pending", "execution_log_unavailable"]
)
async def test_missing_releases_workspace(env, record_type: str) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, result="interrupted")
    async with factory() as session:
        attempt = await session.get(JobAttempt, attempt_id)
        assert attempt is not None
        attempt.error = "Node no longer owns this attempt."
        attempt.workload = "transcode"
        if record_type == "journal":
            session.add(
                FinalizationJournal(
                    attempt_id=attempt_id,
                    source_path="source",
                    destination_path="destination",
                    partial_path="partial",
                    trash_path="trash",
                    state="prepared",
                )
            )
        else:
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type=record_type))
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    try:
        assert (await service.detail(job_id))["eligible"]
        assert await staging.prune_attempt(attempt_id)
        assert (await staging.prune_job(job_id))["outcome"] == "removed"
        assert (await cleanup_workspace(factory, root))["preserved"] == 0
        assert not (root / "jobs" / str(job_id)).exists()
        async with factory() as session:
            assert await session.get(JobAttempt, attempt_id) is not None
            if record_type == "journal":
                assert await session.scalar(select(FinalizationJournal.id)) is not None
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_prunes_orphans(env) -> None:
    factory, root, staging = env
    (root / "jobs" / "999").mkdir()
    (root / "jobs" / "999" / "source.mkv").write_bytes(b"orphan")
    service = WorkspaceService(factory, root, staging=staging)
    row = (await service.sample())["items"][0]
    assert row["workspace_id"] == 999 and row["job_id"] is None
    assert row["filename"] is None and row["eligible"]
    assert await service.cleanup() == 1
    assert not (root / "jobs" / "999").exists()
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", [False, True])
async def test_journal_keeps_history(env, reset: bool) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, result="interrupted")
    artifact = root / "jobs" / str(job_id) / "attempts" / "1" / "output.mkv"
    async with factory() as session:
        session.add(
            FinalizationJournal(
                attempt_id=attempt_id,
                source_path="source",
                destination_path="destination",
                partial_path="partial",
                trash_path="trash",
                state="prepared",
            )
        )
        await session.commit()
        if reset:
            with pytest.raises(ValueError):
                await reset_data(session, set())
        else:
            assert not await delete_eligible_job(session, job_id)
        await session.commit()
    new_id = await add_job(factory, None, status="failed")
    assert new_id > job_id
    create_attempt_layout(root, new_id, 1, ".mkv")
    service = WorkspaceService(factory, root, staging=staging)
    row = await service.detail(job_id)
    assert row["job_id"] == job_id and row["eligible"]
    assert (await staging.prune_job(job_id))["outcome"] == "removed"
    await cleanup_workspace(factory, root)
    assert not artifact.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["completed", "discard_completed", "rolled_back", "discard_rolled_back", "prepared", "discard_prepared"]
)
async def test_prunes_completed_journals(env, state: str) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env)
    async with factory() as session:
        session.add(
            FinalizationJournal(
                attempt_id=attempt_id,
                source_path="source",
                destination_path="destination",
                partial_path="partial",
                trash_path="trash",
                state=state,
            )
        )
        await session.commit()
    row = await WorkspaceService(factory, root, staging=staging).detail(job_id)
    assert row["eligible"]
    assert (await staging.prune_job(job_id))["outcome"] == "removed"
    assert not (root / "jobs" / str(job_id)).exists()
    async with factory() as session:
        assert await session.get(Job, job_id) is not None
        assert await session.get(JobAttempt, attempt_id) is not None
        assert await session.scalar(select(FinalizationJournal.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["root", "jobs", "job", "nested"])
async def test_rejects_link_traversal(env, tmp_path: Path, location: str) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    if location == "root":
        link = tmp_path / "linked"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif location == "jobs":
        (root / "jobs").rename(root / "old-jobs")
        (root / "jobs").symlink_to(root / "old-jobs", target_is_directory=True)
    elif location == "job":
        (root / "jobs" / str(job_id)).rename(root / "old-job")
        (root / "jobs" / str(job_id)).symlink_to(outside, target_is_directory=True)
    else:
        (root / "jobs" / str(job_id) / "linked").symlink_to(outside, target_is_directory=True)
    staging.workspace_dir = root
    service = WorkspaceService(factory, root, staging=staging)
    snapshot = await service.sample()
    if location in {"root", "jobs"}:
        assert snapshot["error"] and snapshot["summary"]["size_bytes"] is None
    elif location == "nested":
        assert not snapshot["items"][0]["eligible"]
        assert snapshot["items"][0]["size_bytes"] is None
    else:
        assert len(snapshot["items"]) == 1
        assert not snapshot["items"][0]["eligible"] and snapshot["summary"]["size_bytes"] is None
    assert (await staging.prune_job(job_id))["outcome"] == ("removed" if location == "nested" else "error")
    assert (outside / "keep").read_bytes() == b"keep"
    await service.aclose()


@pytest.mark.asyncio
async def test_reports_unavailable_root(env, caplog: pytest.LogCaptureFixture) -> None:
    factory, root, staging = env
    service = WorkspaceService(factory, root / "missing", staging=staging)
    snapshot = await service.sample()
    assert snapshot["error"]
    assert any(record.exc_info and isinstance(record.exc_info[1], FileNotFoundError) for record in caplog.records)
    assert all(value is None for value in snapshot["summary"].values())
    await service.aclose()


@pytest.mark.asyncio
async def test_reports_inspection_error(env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env)

    def inspect(directory: int, identifier: int, stopped: threading.Event) -> dict:
        raise PermissionError(13, "Permission denied", str(root / "jobs" / str(identifier)))

    monkeypatch.setattr(workspace_ops, "inspect_directory", inspect)
    service = WorkspaceService(factory, root, staging=staging)
    try:
        snapshot = await service.sample()
        row = snapshot["items"][0]
        assert row["workspace_id"] == job_id and not row["eligible"]
        assert str(root / "jobs" / str(job_id)) in row["error"]
        assert any(record.exc_info and isinstance(record.exc_info[1], PermissionError) for record in caplog.records)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_empty_workspace_root(env) -> None:
    factory, root, staging = env
    (root / "jobs").rmdir()
    service = WorkspaceService(factory, root, staging=staging)
    snapshot = await service.sample()
    assert snapshot["error"] is None
    assert snapshot["summary"]["count"] == snapshot["summary"]["size_bytes"] == 0
    assert snapshot["summary"]["free_bytes"] is not None
    await service.aclose()


@pytest.mark.asyncio
async def test_keeps_history_media(env, tmp_path: Path) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env)
    library = tmp_path / "library.mkv"
    trash = tmp_path / "trash.mkv"
    library.write_bytes(b"library")
    trash.write_bytes(b"trash")
    assert (await staging.prune_job(job_id))["outcome"] == "removed"
    assert not (root / "jobs" / str(job_id)).exists()
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None and Path(media.path).read_bytes() == b"source"
        assert await session.get(JobAttempt, attempt_id) is not None
        assert await session.scalar(select(JobEvent.id).where(JobEvent.job_id == job_id)) is not None
    assert library.read_bytes() == b"library" and trash.read_bytes() == b"trash"


@pytest.mark.asyncio
async def test_rechecks_retry_attempt(env) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env)
    service = WorkspaceService(factory, root, staging=staging)
    assert (await service.sample())["items"][0]["eligible"]
    async with factory() as session:
        request_id = await request_retry(session, job_id)
        assert await retry_job(session, job_id, request_id=request_id) is not None
        await session.commit()
    assert (await staging.prune_job(job_id))["outcome"] == "skipped"
    assert (root / "jobs" / str(job_id)).exists()
    await service.aclose()


@pytest.mark.asyncio
async def test_locks_during_removal(env, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, root, staging = env
    job_id, _ = await workspace(env)
    independent_id, _ = await workspace(env)
    entered = asyncio.Event()
    updating = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    from server.workspace import _remove_job

    def remove(folder: Path, identifier: int) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        _remove_job(folder, identifier)

    monkeypatch.setattr("server.staging._remove_job", remove)
    pruning = asyncio.create_task(staging.prune_job(job_id))
    changing = None

    async def change_owner() -> None:
        async with factory() as session:
            connection = await session.connection()

            def before_update(_connection, _cursor, statement, _parameters, _context, _many) -> None:
                if statement.startswith("UPDATE jobs"):
                    updating.set()

            event.listen(connection.sync_connection, "before_cursor_execute", before_update)
            await session.execute(update(Job).where(Job.id == job_id).values(status="queued"))
            await session.commit()

    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        changing = asyncio.create_task(change_owner())
        await asyncio.wait_for(updating.wait(), 2)
        assert not changing.done()
        async with factory() as session:
            await asyncio.wait_for(
                session.execute(update(Job).where(Job.id == independent_id).values(status="queued")), 2
            )
            await asyncio.wait_for(session.commit(), 2)
        pruning.cancel()
        checkpoint = asyncio.Event()
        loop.call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not pruning.done() and not changing.done()
        assert (root / "jobs" / str(job_id)).exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pruning, 5)
        await asyncio.wait_for(changing, 2)
    finally:
        release.set()
        tasks = [pruning] + ([changing] if changing is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 7)
    assert not (root / "jobs" / str(job_id)).exists()
    assert (root / "jobs" / str(independent_id)).exists()


@pytest.mark.asyncio
async def test_protects_staging_copy(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_storage) -> None:
    factory, root, staging = env
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(factory, source)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def copy(*args, **kwargs):
        folder = root / "jobs" / str(job_id)
        folder.mkdir()
        (folder / "source.mkv").write_bytes(b"source")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr("server.staging._stage_source", copy)
    task = asyncio.create_task(staging.stage_job(job_id))
    service = WorkspaceService(factory, root, staging=staging)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with factory() as session:
            assert await asyncio.wait_for(cancel_pending(session, job_id), 2) is not None
            await asyncio.wait_for(session.commit(), 2)
        assert not (await asyncio.wait_for(service.sample(), 2))["items"][0]["eligible"]
        assert not (await asyncio.wait_for(service.detail(job_id), 2))["eligible"]
        assert (await asyncio.wait_for(staging.prune_job(job_id), 2))["outcome"] == "skipped"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 7)
        await asyncio.wait_for(service.aclose(), 2)
    assert task.result() == 0
    assert not staging.active_jobs
    assert not (root / "jobs" / str(job_id)).exists()
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["listing", "detail"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_stops_disk_scan(env, monkeypatch: pytest.MonkeyPatch, operation: str, cancelled: bool) -> None:
    factory, root, staging = env
    entered = asyncio.Event()
    stopping = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def scan(stopped: threading.Event):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert stopped.wait(timeout=5)
            loop.call_soon_threadsafe(stopping.set)
            assert release.wait(timeout=5)
            raise InterruptedError("sampling stopped")
        finally:
            finished.set()

    monkeypatch.setattr("server.workspaces.scan_disk", lambda _root, stopped: scan(stopped))
    monkeypatch.setattr("server.workspaces.workspace.inspect_directory", lambda _fd, _id, stopped: scan(stopped))
    service = WorkspaceService(factory, root, staging=staging)
    sampling = asyncio.create_task(service.listing() if operation == "listing" else service.detail(1))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancelled:
            sampling.cancel()
        closing = asyncio.create_task(service.aclose())
        await asyncio.wait_for(stopping.wait(), 2)
        if cancelled:
            closing.cancel()
        assert not sampling.done() and not closing.done() and not finished.is_set()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(sampling, closing, return_exceptions=True), 2)
        assert finished.is_set()
        if cancelled:
            assert isinstance(results[0], (asyncio.CancelledError, InterruptedError))
            assert isinstance(results[1], asyncio.CancelledError)
        elif operation == "listing":
            assert isinstance(results[0], dict)
            assert results[0]["error"]
            assert results[1] is None
        else:
            assert isinstance(results[0], InterruptedError) and results[1] is None
    finally:
        release.set()
        await service.aclose()
        tasks = [sampling] + ([closing] if closing is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 7)


@pytest.mark.asyncio
async def test_api_search(workspace_client: httpx.AsyncClient, listing: list[int]) -> None:
    for query, positions in [
        ("  ALPHA  ", [0]),
        ("eChO", [1]),
        ("SUCCEEDED", [2]),
        ("delta", [2, 0]),
        ("%", []),
        ("   ", [2, 1, 0]),
    ]:
        response = await workspace_client.get("/api/workspaces", params={"query": query, "eligibility": "all"})
        assert response.status_code == 200
        result = response.json()
        expected = [listing[position] for position in positions]
        if not query.strip():
            expected = [999, *expected, 0]
        assert [item["workspace_id"] for item in result["items"]] == expected
        assert result["total"] == len(expected)
        assert result["summary"]["count"] == 5


@pytest.mark.asyncio
async def test_api_id_search(workspace_client: httpx.AsyncClient, listing: list[int]) -> None:
    for identifier in (0, listing[1], 999):
        by_id = (await workspace_client.get("/api/workspaces", params={"query": str(identifier)})).json()
        assert [item["workspace_id"] for item in by_id["items"]] == [identifier]


@pytest.mark.asyncio
async def test_api_eligibility(workspace_client: httpx.AsyncClient, listing: list[int]) -> None:
    first, active, last = listing
    for eligibility, expected in {
        "all": [999, last, active, first, 0],
        "unused": [last, first, 0],
        "protected": [999, active],
    }.items():
        result = (await workspace_client.get("/api/workspaces", params={"eligibility": eligibility})).json()
        assert [item["workspace_id"] for item in result["items"]] == expected
        assert result["total"] == len(expected)
        assert result["summary"]["unused_count"] == 3
        assert result["summary"]["protected_count"] == 2
        combined = (
            await workspace_client.get("/api/workspaces", params={"eligibility": eligibility, "query": "delta"})
        ).json()
        assert combined["total"] == (0 if eligibility == "protected" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_api_sorting(workspace_client: httpx.AsyncClient, listing: list[int], direction: str) -> None:
    first, active, last = listing
    # Sampling creates lock files that change activity times after the first request.
    known = {
        "last_activity_at": [active, last, first, 0],
        "workspace_id": [0, first, active, last, 999],
        "filename": [first, active, last],
        "status": [first, active, last],
        "node_name": [first, last, active],
        "size_bytes": [0, active, last, first],
    }
    unknown = {
        "workspace_id": [],
        "filename": [0, 999],
        "status": [0, 999],
        "node_name": [0, 999],
        "size_bytes": [999],
        "last_activity_at": [999],
    }
    for field in known:
        expected = known[field] + unknown[field] if direction == "asc" else known[field][::-1] + unknown[field][::-1]
        response = await workspace_client.get("/api/workspaces", params={"sort_by": field, "sort_dir": direction})
        assert response.status_code == 200
        result = response.json()
        assert [item["workspace_id"] for item in result["items"]] == expected
        assert result["total"] == 5


@pytest.mark.asyncio
async def test_api_pagination(workspace_client: httpx.AsyncClient, listing: list[int]) -> None:
    params = {
        "query": "delta",
        "eligibility": "unused",
        "sort_by": "filename",
        "sort_dir": "asc",
        "limit": 1,
        "offset": 1,
    }
    response = await workspace_client.get("/api/workspaces", params=params)
    assert response.status_code == 200
    result = response.json()
    assert [item["workspace_id"] for item in result["items"]] == [listing[2]]
    assert result["total"] == 2
    assert result["summary"]["count"] == 5
    assert result["sampled_at"] == result["items"][0]["sampled_at"]
    assert result["error"] is None
    params["offset"] = 2
    empty = (await workspace_client.get("/api/workspaces", params=params)).json()
    assert empty["items"] == [] and empty["total"] == 2
    maximum = await workspace_client.get("/api/workspaces", params={"limit": 999})
    assert maximum.status_code == 200 and len(maximum.json()["items"]) == 5


@pytest.mark.asyncio
async def test_api_list_validation(workspace_client: httpx.AsyncClient) -> None:
    for params in [
        {"eligibility": "eligible"},
        {"sort_by": "job_id"},
        {"sort_dir": "up"},
        {"limit": 0},
        {"limit": 1000},
        {"offset": -1},
    ]:
        assert (await workspace_client.get("/api/workspaces", params=params)).status_code == 422


@pytest.mark.asyncio
async def test_prune_all_unpaged(env, workspace_client: httpx.AsyncClient) -> None:
    factory, root, _ = env
    for index in range(51):
        source = root.parent / f"source-{index}.mkv"
        source.write_bytes(b"source")
        identifier = await add_job(factory, source, status="succeeded")
        (root / "jobs" / str(identifier)).mkdir()
    listed = (await workspace_client.get("/api/workspaces")).json()
    assert listed["total"] == listed["summary"]["count"] == 51
    assert len(listed["items"]) == 50
    pruned = await workspace_client.post("/api/workspaces/prune", json={"all_unused": True})
    assert pruned.status_code == 200
    result = pruned.json()
    assert result["removed"] == 51
    assert result["snapshot"]["summary"]["count"] == 0
    assert set(result["snapshot"]) == {"summary", "sampled_at", "error"}


@pytest.mark.asyncio
async def test_api_inspect_prune(env) -> None:
    factory, root, _ = env
    job_id, _ = await workspace(env)
    protected_id, _ = await workspace(env, status="running")
    app = create_app(factory, config=ServerConfig(workspace_dir=str(root)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        listed = await client.get("/api/workspaces")
        assert listed.status_code == 200 and listed.json()["summary"]["count"] == 2
        detail = await client.get(f"/api/workspaces/{job_id}")
        assert detail.status_code == 200 and detail.json()["files"]
        assert (await client.post("/api/workspaces/prune", json={})).status_code == 422
        assert (await client.post("/api/workspaces/prune", json={"job_ids": [-1]})).status_code == 422
        assert (await client.post("/api/workspaces/prune", json={"job_ids": [job_id] * 1000})).status_code == 422
        assert (
            await client.post("/api/workspaces/prune", json={"job_ids": [job_id], "all_unused": True})
        ).status_code == 422
        result = await client.post("/api/workspaces/prune", json={"job_ids": [job_id] * 998 + [protected_id]})
        assert result.status_code == 200
        assert result.json()["removed"] == result.json()["skipped"] == 1
        assert result.json()["snapshot"]["summary"]["count"] == 1
        assert set(result.json()["snapshot"]) == {"summary", "sampled_at", "error"}
        assert (await client.get(f"/api/workspaces/{job_id}")).status_code == 404
        second_id, _ = await workspace(env)
        all_unused = await client.post("/api/workspaces/prune", json={"all_unused": True})
        assert all_unused.json()["removed"] == 1
        assert all_unused.json()["items"][0]["workspace_id"] == second_id
        assert set(all_unused.json()["snapshot"]) == {"summary", "sampled_at", "error"}
        assert (root / "jobs" / str(protected_id)).exists()
        app.state.workspaces.root = root / "missing"
        assert (await client.post("/api/workspaces/prune", json={"all_unused": True})).status_code == 503
    await app.state.workspaces.aclose()
    await app.state.events.aclose()
    await app.state.plugin_host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["post_actions", "stage"])
@pytest.mark.parametrize("has_actions", [False, True])
async def test_protects_deferred_actions(env, event_type: str, has_actions: bool) -> None:
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status="succeeded", result="succeeded")
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.post_actions is None
        key = "actions" if event_type == "post_actions" else "post_actions"
        session.add(
            JobEvent(
                job_id=job_id,
                attempt_id=attempt_id,
                type=event_type,
                payload={key: [{"kind": "notify", "on": ["job.succeeded"]}] if has_actions else []},
            )
        )
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    assert (await service.sample())["items"][0]["eligible"]
    assert (await service.detail(job_id))["eligible"]
    result = await staging.prune_job(job_id)
    assert result["outcome"] == "removed"
    assert not (root / "jobs" / str(job_id)).exists()
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["declared", "deferred", "pending", "report"])
async def test_releases_completed_actions(env, action_state):
    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status="succeeded", result="succeeded")
    async with factory() as session:
        if action_state == "declared":
            job = await session.get(Job, job_id)
            job.post_actions = [{"kind": "extract_subtitles", "formats": ["srt"]}]
        elif action_state == "deferred":
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="post_actions", payload={"actions": [{}]}))
        else:
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="actions_finished"))
            await session.flush()
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="actions_pending"))
            if action_state == "report":
                session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="action_report"))
        await session.commit()
    service = WorkspaceService(factory, root, staging=staging)
    staging.busy_jobs.add(job_id)
    assert not (await service.detail(job_id))["eligible"]
    assert (await staging.prune_job(job_id))["outcome"] == "skipped"
    staging.busy_jobs.remove(job_id)
    async with factory() as session:
        session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="actions_finished"))
        await session.commit()
    assert (await service.detail(job_id))["eligible"]
    assert (await staging.prune_job(job_id))["outcome"] == "removed"
    await service.aclose()


@pytest.mark.asyncio
async def test_scanner_preserves_actions(env):
    from server.models import Library
    from server.scanner.scan import _prune_missing

    factory, root, staging = env
    job_id, attempt_id = await workspace(env, status="succeeded", result="succeeded")
    artifact = root / "jobs" / str(job_id) / "attempts/1/output.mkv"
    async with factory() as session:
        job = await session.get(Job, job_id)
        media_id, library_id = job.media_file_id, job.library_id
        media = await session.get(MediaFile, media_id)
        Path(media.path).unlink()
        session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="actions_pending"))
        await session.commit()
    async with factory() as session:
        library = await session.get(Library, library_id)
        library.scan_id = 1
        await session.commit()
        staging.busy_jobs.add(job_id)
        assert await _prune_missing(session, library, 1, staging=staging, unavailable=set(), excluded=()) == 0
        await session.commit()
        assert await session.get(Job, job_id) is not None
        assert await session.get(MediaFile, media_id) is not None
        session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="actions_finished"))
        await session.commit()
    staging.busy_jobs.remove(job_id)
    assert artifact.read_bytes() == b"output"
    async with factory() as session:
        library = await session.get(Library, library_id)
        assert await _prune_missing(session, library, 1, staging=staging, unavailable=set(), excluded=()) == 1
        await session.commit()
    assert not (root / "jobs" / str(job_id)).exists()


@pytest.mark.asyncio
async def test_summary_classifies_owners(env) -> None:
    factory, root, staging = env
    await workspace(env, status="running")
    unused_id, _ = await workspace(env)
    await workspace(env, result="interrupted")
    (root / "jobs" / "999").mkdir()
    service = WorkspaceService(factory, root, staging=staging)
    snapshot = await service.sample()
    summary = snapshot["summary"]
    assert summary["count"] == 4 and summary["active_count"] == 1
    assert summary["unused_count"] == 3 and summary["protected_count"] == 1
    assert summary["unused_size_bytes"] == sum(item["size_bytes"] for item in snapshot["items"] if item["eligible"])
    assert any(item["workspace_id"] == unused_id and item["eligible"] for item in snapshot["items"])
    assert summary["size_bytes"] == sum(item["size_bytes"] for item in snapshot["items"])
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("vanished", ["file", "directory"])
async def test_tolerates_vanished_entries(env, monkeypatch: pytest.MonkeyPatch, vanished: str) -> None:
    factory, root, staging = env
    await workspace(env, status="running")
    if vanished == "file":
        real_scan = os.scandir

        def missing(**kwargs):
            raise FileNotFoundError("file vanished")

        @contextlib.contextmanager
        def scan(fd):
            with real_scan(fd) as entries:
                yield [
                    SimpleNamespace(name=entry.name, stat=missing) if entry.name == "ffmpeg.log" else entry
                    for entry in entries
                ]

        monkeypatch.setattr(workspace_ops.os, "scandir", scan)
    else:
        real_open = os.open

        def open_directory(path, flags, *args, **kwargs):
            if path == "artifacts":
                raise FileNotFoundError("directory vanished")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(workspace_ops.os, "open", open_directory)
    service = WorkspaceService(factory, root, staging=staging)
    snapshot = await service.sample()
    assert snapshot["summary"]["count"] == snapshot["summary"]["active_count"] == 1
    assert snapshot["items"][0]["file_count"] == 5
    assert snapshot["items"][0]["error"] is None
    await service.aclose()


@pytest.mark.asyncio
async def test_get_samples_fresh(env) -> None:
    factory, root, _ = env
    job_id, _ = await workspace(env)
    app = create_app(factory, config=ServerConfig(workspace_dir=str(root)))
    try:
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            first = (await client.get("/api/workspaces")).json()
            (root / "jobs" / str(job_id) / "source.mkv").write_bytes(b"larger source")
            second = (await client.get("/api/workspaces")).json()
            assert first["items"][0]["source_bytes"] == 6
            assert second["items"][0]["source_bytes"] == 13
            assert second["items"][0]["files"] != first["items"][0]["files"]
            app.state.workspaces.root = root / "missing"
            unavailable = (await client.get("/api/workspaces")).json()
            assert unavailable["items"] == [] and unavailable["total"] == 0
            assert unavailable["error"]
            assert all(value is None for value in unavailable["summary"].values())
    finally:
        await app.state.workspaces.aclose()
        await app.state.events.aclose()
        await app.state.plugin_host.aclose()
