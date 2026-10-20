from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)

import pytest
from common.enums import JobStatus
from common.plan import SubtitleBurn
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile

from common.fs import effects
from server import workspace
from server.fs.source import capture_source
from server.workspace import (
    _remove_attempt,
    _remove_job,
    create_attempt_layout,
    read_attempt_log,
    cleanup_workspace,
    staged_subtitle,
)
from tests.helpers import database_context, make_test_temp_dir


def stage_source(root: Path, job_id: int, source: Path, *, expected_source: dict | None = None) -> str:
    if expected_source is None:
        captured = capture_source(source)
    else:
        captured = expected_source
    return workspace._stage_source(root, job_id, source, expected_source=captured)


def stage_subtitle(root: Path, job_id: int, source: Path, filename: str) -> str:
    SubtitleBurn(filename=filename)
    captured = capture_source(source)
    stage_source(root, job_id, source, expected_source=captured)
    return workspace._stage_subtitle(root, job_id, source, filename)


def test_stage_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("workspace-stage")
    source = root / "library" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"media")
    dates = capture_source(source)["dates"]
    monkeypatch.setattr(effects.fcntl, "ioctl", lambda *_: (_ for _ in ()).throw(OSError(22, "unsupported")))
    relative = stage_source(root, 7, source)
    destination = root / relative
    assert relative == "jobs/7/source.mkv"
    assert destination.read_bytes() == source.read_bytes()
    manifest = json.loads((destination.parent / "manifest.json").read_text())
    assert manifest == {
        "job_id": 7,
        "source_relative_path": relative,
        "dates": dates,
        "path": str(source),
        **workspace.source_identity(source),
        "subtitles": {},
    }
    assert not (destination.parent / "source.mkv.partial").exists()


def test_original_size_boundary(tmp_path: Path) -> None:
    source = tmp_path / "library" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"original")
    relative = stage_source(tmp_path, 1, source)
    staged = tmp_path / relative
    staged.write_bytes(b"transformed intermediate")
    assert workspace.verify_staged_source(tmp_path, 1, source) == workspace.source_identity(source)
    dates = source.stat().st_atime_ns, source.stat().st_mtime_ns
    source.write_bytes(b"changed size")
    os.utime(source, ns=dates)
    with pytest.raises(ValueError):
        workspace.verify_staged_source(tmp_path, 1, source)


@pytest.mark.parametrize("surface", ["reuse", "subtitle"])
def test_reuse_accepts_hardlink(tmp_path: Path, surface: str) -> None:
    root = tmp_path / "workspace"
    source = tmp_path / "library" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"media")
    if surface == "subtitle":
        (source.parent / "captions.srt").write_bytes(b"text")
        relative = stage_subtitle(root, 1, source, "captions.srt")
        assert staged_subtitle(root, 1, "captions.srt") == relative
    else:
        relative = stage_source(root, 1, source)
        assert stage_source(root, 1, source) == relative
        assert workspace.verify_staged_source(root, 1, source) == workspace.source_identity(source)
    staged = root / relative
    outside = root / "copies"
    outside.mkdir()
    alias = outside / staged.name
    os.link(staged, alias)
    if surface == "reuse":
        assert stage_source(root, 1, source) == relative
    else:
        assert staged_subtitle(root, 1, "captions.srt") == relative
    assert alias.read_bytes() == (b"text" if surface == "subtitle" else b"media")


def test_dates_use_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"media")
    os.utime(source, ns=(1_500_000_000_000_000_000, 1_600_000_000_000_000_000))
    captured = capture_source(source)
    stat = Path.stat

    def read_stat(path, *args, **kwargs):
        value = stat(path, *args, **kwargs)
        if path == source:
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            return SimpleNamespace(**{**fields, "st_atime_ns": 1_700_000_000_000_000_000, "st_atime": 1_700_000_000.0})
        return value

    monkeypatch.setattr(Path, "stat", read_stat)
    relative = stage_source(tmp_path, 1, source, expected_source=captured)
    assert (tmp_path / relative).is_file()
    assert workspace.staged_dates(tmp_path, 1) == captured["dates"]
    assert workspace.verify_staged_source(tmp_path, 1, source) == workspace.source_identity(source)


def test_dispatch_rechecks_size() -> None:
    root = make_test_temp_dir("workspace-change")
    source = root / "movie.mkv"
    source.write_bytes(b"media")
    relative = stage_source(root, 1, source)
    source.write_bytes(b"changed media")
    destination = root / relative
    with pytest.raises(ValueError):
        workspace.verify_staged_source(root, 1, source)
    assert destination.read_bytes() == b"media"


def test_stage_rejects_symlinks() -> None:
    root = make_test_temp_dir("workspace-links")
    source = root / "movie.mkv"
    target = root / "target.mkv"
    target.write_bytes(b"media")
    source.symlink_to(target)
    with pytest.raises(ValueError):
        workspace._stage_source(root, 1, source, expected_source={})
    linked_root = make_test_temp_dir("workspace-linked-jobs")
    (linked_root / "jobs").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        stage_source(linked_root, 2, target)


def test_stage_subtitle_copy() -> None:
    root = make_test_temp_dir("workspace-subtitle")
    source = root / "movie.mkv"
    source.write_bytes(b"media")
    subtitle = source.parent / "movie.eng.srt"
    subtitle.write_bytes(b"text")
    relative = stage_subtitle(root, 1, source, "movie.eng.srt")
    assert relative == "jobs/1/subtitle.srt"
    assert staged_subtitle(root, 1, "subtitle.srt") == relative
    assert staged_subtitle(root, 1, "movie.eng.srt") == relative
    assert (root / relative).read_bytes() == b"text"


def test_subtitle_rejects_unsafe() -> None:
    root = make_test_temp_dir("workspace-subtitle-unsafe")
    source = root / "movie.mkv"
    source.write_bytes(b"media")
    with pytest.raises(ValueError):
        stage_subtitle(root, 1, source, "../captions.srt")
    target = root / "captions.srt"
    target.write_bytes(b"text")
    (source.parent / "selected.srt").symlink_to(target)
    with pytest.raises(ValueError):
        stage_subtitle(root, 1, source, "selected.srt")


def test_attempt_cleanup() -> None:
    root = make_test_temp_dir("workspace-attempt")
    (root / "jobs" / "4").mkdir(parents=True)
    layout = create_attempt_layout(root, 4, 2, ".mkv")
    assert (root / layout["artifacts"]).is_dir()
    (root / layout["output"]).write_bytes(b"output")
    with pytest.raises(ValueError):
        create_attempt_layout(root, 4, 3, "/escape")
    _remove_job(root, 4)
    assert not (root / "jobs" / "4").exists()
    _remove_job(root, 4)
    outside = make_test_temp_dir("workspace-safe")
    (outside / "keep").write_text("x")
    with pytest.raises(ValueError):
        _remove_job(root, -1)
    assert (outside / "keep").exists()


def test_attempt_symlink_parent() -> None:
    root = make_test_temp_dir("workspace-attempt-link")
    (root / "jobs" / "4").mkdir(parents=True)
    target = make_test_temp_dir("workspace-attempt-target")
    (root / "jobs" / "4" / "attempts").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        create_attempt_layout(root, 4, 2, ".mkv")


def test_ingests_binary_log() -> None:
    root = make_test_temp_dir("workspace-log")
    path = root / "jobs" / "2" / "attempts" / "1" / "ffmpeg.log"
    path.parent.mkdir(parents=True)
    data = b"\x00\xffbinary\n"
    path.write_bytes(data)
    assert read_attempt_log(root, "jobs/2/attempts/1/ffmpeg.log") == data


def test_cleanup_preserves_source() -> None:
    root = make_test_temp_dir("workspace-attempt-clean")
    job = root / "jobs" / "3"
    (job / "attempts" / "1").mkdir(parents=True)
    (job / "source.mkv").write_bytes(b"source")
    _remove_attempt(root, 3, 1)
    assert (job / "source.mkv").exists()
    assert not (job / "attempts" / "1").exists()


async def _workspace_job(factory, status: str, attempt_status: str | None = None) -> int:
    async with factory() as session:
        library = Library(name="library", roots=["/tmp"])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path="x",
            size=1,
            mtime_ns=1,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(media_file_id=media.id, library_id=library.id, status=status, attempts=1 if attempt_status else 0)
        session.add(job)
        await session.flush()
        if attempt_status:
            session.add(JobAttempt(job_id=job.id, attempt_number=1, status=attempt_status))
        await session.commit()
        return job.id


@pytest.mark.asyncio
async def test_interrupted_log_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import inspect, select

    monkeypatch.setattr(workspace, "_WORKSPACE_BATCH", 2)
    saved = []

    async def snapshot(session, root, attempt):
        assert inspect(attempt).unloaded.issuperset({"input_probe", "output_probe", "argv"})
        saved.append(attempt.id)
        attempt.log_data = b"saved"

    monkeypatch.setattr(workspace, "snapshot_attempt_log", snapshot)
    async with database_context("interrupted-log-batches") as factory:
        first = await _workspace_job(factory, "running", "running")
        async with factory() as session:
            original = await session.get(Job, first)
            assert original is not None
            media = [
                MediaFile(
                    library_id=original.library_id,
                    path=str(tmp_path / f"{index}.mkv"),
                    size=1,
                    mtime_ns=1,
                    container="mkv",
                    medium="video",
                    last_scanned_at=T0,
                )
                for index in range(3)
            ]
            session.add_all(media)
            await session.flush()
            jobs = [
                Job(media_file_id=file.id, library_id=original.library_id, status="running", attempts=1)
                for file in media
            ]
            session.add_all(jobs)
            await session.flush()
            for index, job in enumerate(jobs):
                session.add(
                    JobAttempt(
                        job_id=job.id,
                        attempt_number=1,
                        workload="transcode",
                        status="running",
                        log_path=f"jobs/{job.id}/attempts/1/execution.log",
                        log_data=b"already saved" if index == 2 else None,
                    )
                )
            initial = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == first))).one()
            initial.workload = "transcode"
            initial.log_path = f"jobs/{first}/attempts/1/execution.log"
            await session.commit()
        async with factory() as session:
            await workspace.snapshot_interrupted_logs(session, tmp_path)
            await session.commit()
        assert len(saved) == 3


@pytest.mark.asyncio
async def test_cleanup_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import inspect, select

    monkeypatch.setattr(workspace, "_WORKSPACE_BATCH", 2)
    batches = []
    logged = []
    cleanup = workspace._cleanup_filesystem

    def filesystem(root, names, *args):
        batches.append(list(names))
        return cleanup(root, names, *args)

    async def snapshot(session, root, attempt):
        logged.append(attempt.id)
        assert inspect(attempt).unloaded.issuperset({"input_probe", "output_probe", "argv"})
        attempt.log_data = b"saved"

    monkeypatch.setattr(workspace, "_cleanup_filesystem", filesystem)
    monkeypatch.setattr(workspace, "snapshot_attempt_log", snapshot)
    root = tmp_path / "workspace"
    async with database_context("cleanup-batches") as factory:
        first = await _workspace_job(factory, "succeeded", "succeeded")
        async with factory() as session:
            original = await session.get(Job, first)
            assert original is not None
            added = [
                Job(
                    media_file_id=original.media_file_id, library_id=original.library_id, status="succeeded", attempts=1
                )
                for _ in range(4)
            ]
            session.add_all(added)
            await session.flush()
            session.add_all([JobAttempt(job_id=job.id, attempt_number=1, status="succeeded") for job in added])
            ids = [first, *(job.id for job in added)]
            for index, job_id in enumerate(ids):
                job = await session.get(Job, job_id)
                assert job is not None
                job.flow_snapshot = {"unused": "x" * 10000}
                attempt = (await session.execute(select(JobAttempt).where(JobAttempt.job_id == job_id))).scalar_one()
                attempt.log_path = f"jobs/{job_id}/attempts/1/execution.log"
                attempt.input_probe = {"unused": "x" * 10000}
                if index < 3:
                    (root / "jobs" / str(job_id)).mkdir(parents=True)
            await session.commit()
        report = await cleanup_workspace(factory, root)
        assert report == {"removed": 3, "preserved": 0}
        assert sorted(name for batch in batches for name in batch) == sorted(str(job_id) for job_id in ids[:3])
        assert all(len(batch) <= 2 for batch in batches)
        assert len(logged) == 5


@pytest.mark.asyncio
async def test_cleans_unknown() -> None:
    root = make_test_temp_dir("cleanup-unknown")
    (root / "jobs" / "999").mkdir(parents=True)
    (root / "jobs" / "stray").mkdir()
    (root / "jobs" / "stray.log").write_text("x")
    async with database_context("cleanup-unknown-db") as factory:
        report = await cleanup_workspace(factory, root)
        assert report == {"removed": 3, "preserved": 0}
        assert not (root / "jobs" / "999").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("supersession", ["none", "attempt", "job", "completed"])
async def test_artifact_recovery_locks(tmp_path: Path, supersession: str) -> None:
    from sqlalchemy import select
    from server.queue.store import processing_backlog

    root = tmp_path / "workspace"
    async with database_context("artifact-recovery") as factory:
        job_id = await _workspace_job(factory, "succeeded", "succeeded")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.attempts = 1
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None
            media.health_status = "healthy"
            media.transcode_decided = True
            attempt_id = await session.scalar(select(JobAttempt.id).where(JobAttempt.job_id == job_id))
            marker = JobEvent(job_id=job_id, attempt_id=attempt_id, type="artifact_check_pending")
            session.add(marker)
            if supersession == "attempt":
                job.attempts = 2
                session.add(JobAttempt(job_id=job_id, attempt_number=2, status="succeeded"))
            elif supersession == "job":
                session.add(Job(media_file_id=media.id, library_id=job.library_id, status="succeeded"))
            elif supersession == "completed":
                marker.type = "artifact_check_finished"
            await session.commit()

        output = root / "jobs" / str(job_id) / "attempts" / "1" / "output.mkv"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"publication output")
        # No invocation owns this persisted marker after a crash.
        await cleanup_workspace(factory, root)
        async with factory() as session:
            expected = 1 if supersession == "none" else 0
            backlog = await processing_backlog(session, healthcheck_slots=1, now=T0)
            assert backlog["remaining_files"] == expected
            if expected:
                assert backlog["remaining_s"] is None
                assert await session.scalar(workspace.recovery_jobs().where(Job.id == job_id)) == job_id
            else:
                assert await session.scalar(workspace.recovery_jobs().where(Job.id == job_id)) is None
            assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["declared", "stage", "post_actions"])
async def test_recovered_actions_completion(tmp_path: Path, event_type: str) -> None:
    from sqlalchemy import select

    from server.queue.store import ActiveJobError, check_file_idle
    from server.retention import prune_jobs
    from tests.test_staging import add_job

    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    actions = [{"kind": "extract_subtitles", "formats": ["srt"]}]
    async with database_context("recovered-actions") as factory:
        job_id = await add_job(factory, source, status="succeeded")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.attempts = 2
            job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
            previous = JobAttempt(job_id=job_id, attempt_number=1, status="succeeded", action_results=[{"ok": True}])
            current = JobAttempt(job_id=job_id, attempt_number=2, status="succeeded")
            session.add_all([previous, current])
            await session.flush()
            session.add(JobEvent(job_id=job_id, attempt_id=previous.id, type="actions_finished"))
            if event_type == "declared":
                job.post_actions = actions
            else:
                session.add(
                    JobEvent(
                        job_id=job_id,
                        attempt_id=current.id,
                        type=event_type,
                        payload={"post_actions": actions, "actions": []}
                        if event_type == "stage"
                        else {"actions": actions},
                    )
                )
            session.add(JobEvent(job_id=job_id, attempt_id=current.id, type="finalization_recovered"))
            session.add(
                JobEvent(
                    job_id=job_id,
                    attempt_id=previous.id,
                    type="stage",
                    payload={"post_actions": [{"kind": "preserve_timestamps"}]},
                )
            )
            await session.commit()
            previous_id, current_id = previous.id, current.id

        async with factory() as session:
            assert await session.scalar(workspace.post_action_jobs().where(Job.id == job_id)) == job_id
            assert await session.scalar(workspace.recovery_jobs().where(Job.id == job_id)) == job_id
            with pytest.raises(ActiveJobError):
                await check_file_idle(session, job.media_file_id)
        assert await prune_jobs(factory, 1, now=T0) == 0
        async with factory() as session:
            await workspace.finish_workspace_actions(session, job_id, attempt_id=previous_id)
            await session.commit()
            assert await session.scalar(workspace.post_action_jobs().where(Job.id == job_id)) == job_id

        root = tmp_path / "workspace"
        artifact = root / "jobs" / str(job_id) / "attempts" / "2" / "output.eng.srt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"subtitle")
        await cleanup_workspace(factory, root)
        assert not artifact.exists()
        assert source.read_bytes() == b"source"
        async with factory() as session:
            assert await session.scalar(workspace.post_action_jobs().where(Job.id == job_id)) is None
            completed = await session.scalar(
                select(JobEvent).where(
                    JobEvent.job_id == job_id, JobEvent.attempt_id == current_id, JobEvent.type == "actions_finished"
                )
            )
            assert completed is not None and completed.payload["abandoned"]
            assert completed.payload["results"] == [
                {"kind": "extract_subtitles", "ok": False, "error": completed.payload["error"], "artifacts": []}
            ]
            previous = await session.get(JobAttempt, previous_id)
            current = await session.get(JobAttempt, current_id)
            assert previous is not None and previous.action_results == [{"ok": True}]
            assert current is not None and current.action_results == completed.payload["results"]
        assert await prune_jobs(factory, 1, now=T0) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_historical_action_locks(pending: bool) -> None:
    from sqlalchemy import select

    async with database_context("historical-actions") as factory:
        job_id = await _workspace_job(factory, "succeeded", "succeeded")
        async with factory() as session:
            job = await session.get(Job, job_id)
            previous = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
            assert job is not None and previous is not None
            job.attempts = 2
            current = JobAttempt(job_id=job_id, attempt_number=2, status="succeeded")
            session.add(current)
            await session.flush()
            actions = [{"kind": "extract_subtitles", "formats": ["srt"]}]
            session.add(
                JobEvent(job_id=job_id, attempt_id=previous.id, type="stage", payload={"post_actions": actions})
            )
            if pending:
                session.add(
                    JobEvent(
                        job_id=job_id, attempt_id=previous.id, type="actions_pending", payload={"actions": actions}
                    )
                )
                session.add(JobEvent(job_id=job_id, attempt_id=current.id, type="actions_finished"))
            await session.commit()
            assert await session.scalar(workspace.post_action_jobs().where(Job.id == job_id)) == (
                job_id if pending else None
            )
            await workspace.finish_workspace_actions(session, job_id, attempt_id=current.id)
            await session.commit()
            assert await session.scalar(workspace.post_action_jobs().where(Job.id == job_id)) == (
                job_id if pending else None
            )
            if pending:
                await workspace.finish_workspace_actions(session, job_id, attempt_id=previous.id)
                await session.commit()
                assert await session.scalar(workspace.post_action_jobs().where(Job.id == job_id)) is None
                assert previous.action_results is not None and previous.action_results[0]["ok"] is False


@pytest.mark.asyncio
async def test_preserves_active() -> None:
    root = make_test_temp_dir("cleanup-active")
    async with database_context("cleanup-active-db") as factory:
        job_id = await _workspace_job(factory, JobStatus.QUEUED.value)
        job = root / "jobs" / str(job_id)
        (job / "attempts" / "4").mkdir(parents=True)
        (job / "source.mkv").write_bytes(b"source")
        await cleanup_workspace(factory, root)
        assert (job / "source.mkv").exists() and not (job / "attempts" / "4").exists()


@pytest.mark.asyncio
async def test_preserves_running() -> None:
    root = make_test_temp_dir("cleanup-running")
    async with database_context("cleanup-running-db") as factory:
        job_id = await _workspace_job(factory, JobStatus.RUNNING.value, "running")
        job = root / "jobs" / str(job_id)
        (job / "attempts" / "1").mkdir(parents=True)
        (job / "attempts" / "2").mkdir()
        await cleanup_workspace(factory, root)
        assert (job / "attempts" / "1").exists() and not (job / "attempts" / "2").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.VERIFYING.value, JobStatus.FINALIZING.value])
async def test_prunes_stopped_recovery(status: str) -> None:
    root = make_test_temp_dir(f"cleanup-{status}")
    async with database_context(f"cleanup-{status}-db") as factory:
        job_id = await _workspace_job(factory, status, "succeeded")
        async with factory() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            row.attempts = 2
            session.add(JobAttempt(job_id=job_id, attempt_number=2, status="succeeded"))
            await session.commit()
        job = root / "jobs" / str(job_id)
        (job / "attempts" / "1").mkdir(parents=True)
        (job / "attempts" / "2").mkdir()
        await cleanup_workspace(factory, root)
        assert not job.exists()


@pytest.mark.asyncio
async def test_cleans_symlinks() -> None:
    root = make_test_temp_dir("cleanup-links")
    target = make_test_temp_dir("cleanup-target")
    (target / "keep").write_text("keep")
    (root / "jobs").mkdir()
    (root / "jobs" / "9").symlink_to(target, target_is_directory=True)
    (root / "jobs" / "stray").symlink_to(target, target_is_directory=True)
    async with database_context("cleanup-links-db") as factory:
        await cleanup_workspace(factory, root)
        assert target.joinpath("keep").exists() and not (root / "jobs" / "9").exists()


@pytest.mark.asyncio
async def test_cleans_challenges() -> None:
    root = make_test_temp_dir("cleanup-challenges")
    target = make_test_temp_dir("challenge-target")
    (target / "keep").write_text("keep")
    (root / "nodes").mkdir()
    (root / "nodes" / "node-1").mkdir()
    (root / "nodes" / "link").symlink_to(target, target_is_directory=True)
    async with database_context("cleanup-challenges-db") as factory:
        await cleanup_workspace(factory, root)
        assert target.joinpath("keep").exists() and not (root / "nodes" / "node-1").exists()
        (root / "nodes").rmdir()
        (root / "nodes").symlink_to(target, target_is_directory=True)
        await cleanup_workspace(factory, root)
        assert target.joinpath("keep").exists() and not (root / "nodes").exists()


def test_log_parent_symlink() -> None:
    root = make_test_temp_dir("workspace-log-parent-link")
    target = make_test_temp_dir("workspace-log-target")
    (target / "ffmpeg.log").write_bytes(b"x")
    (root / "jobs").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        read_attempt_log(root, "jobs/1/ffmpeg.log")
