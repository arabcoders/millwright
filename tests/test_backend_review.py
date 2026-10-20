from __future__ import annotations

import asyncio
import errno
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from common.fs import effects
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, TrashEntry, TrashRestoreJournal, utcnow
from server.staging import StagingService
from server.trash import (
    delete_trash_entry,
    purge_trash,
    refresh_trash_records,
    recover_trash_restores,
    restore_trash,
    store_trash,
    trash_path,
)
from server.workspace import recovery_jobs, retain_unread_log
from tests.helpers import database_context, fd_path
from tests.hub_helpers import FakeConn
from tests.test_hub import Env, env, file_job, finished, hello, hub, materialize_offer, serve
from tests.test_trash_restore import env as restore_env, put_file

pytestmark = pytest.mark.usefixtures("local_storage")


@pytest.mark.asyncio
@pytest.mark.parametrize("stat_error", [None, errno.EACCES, errno.EIO])
async def test_prune_keeps_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stat_error: int | None) -> None:
    root = tmp_path / "workspace"
    async with database_context("review-log") as sessions:
        async with sessions() as session:
            library = Library(name="review", roots=[str(tmp_path / "library")])
            session.add(library)
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(tmp_path / "library/movie.mkv"),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
            session.add(media)
            await session.flush()
            job = Job(media_file_id=media.id, library_id=library.id, status="failed", attempts=1)
            session.add(job)
            await session.flush()
            relative = f"jobs/{job.id}/attempts/1/ffmpeg.log"
            log = root / relative
            log.parent.mkdir(parents=True)
            log.write_bytes(b"execution log")
            attempt = JobAttempt(
                job_id=job.id,
                attempt_number=1,
                workload="transcode",
                status="failed",
                result="failed",
                log_path=relative,
            )
            session.add(attempt)
            await session.flush()
            stat = os.stat

            def unavailable(path, *args, **kwargs):
                if path in (log, str(log)) and stat_error is not None:
                    raise OSError(stat_error, "log unavailable")
                return stat(path, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(os, "stat", unavailable)
                await retain_unread_log(session, root, attempt, OSError("read failed"))
            await session.commit()
            job_id = job.id
        async with sessions() as session:
            assert await session.scalar(recovery_jobs().where(Job.id == job_id)) is None
            assert (
                await session.scalar(
                    select(JobEvent.id).where(JobEvent.job_id == job_id, JobEvent.type == "execution_log_unavailable")
                )
                is not None
            )
        result = await StagingService(sessions, root, min_free_gb=1).prune_job(job_id)
        assert not log.exists()
        assert result["outcome"] == "removed"
        async with sessions() as session:
            saved = await session.get(JobAttempt, attempt.id)
            assert saved is not None and saved.log_data == b"execution log"
            assert (
                await session.scalar(select(JobEvent.id).where(JobEvent.type == "execution_log_unavailable"))
                is not None
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "moved", "committed"])
@pytest.mark.parametrize("source", ["media", "claim"])
async def test_recovery_retains_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, source: str
) -> None:
    from server import trash

    root, library = tmp_path / "trash", tmp_path / "library"
    root.mkdir()
    library.mkdir()
    destination = library / "movie.mkv"
    destination.write_bytes(b"retained original")
    async with database_context("review-restore-recovery") as sessions:
        async with sessions() as session:
            entry = await store_trash(session, root, 1, destination, library, 30)
            await session.commit()
            retained = trash_path(root, 1, entry.id, entry.suffix)
            effects.copy_file(retained, destination)
            if source == "claim":
                claim = retained.parent / f".{entry.id}.restore-claim"
                retained.rename(claim)
                retained = claim
            journal_id = "j" * 32
            session.add(
                TrashRestoreJournal(id=journal_id, entry_id=entry.id, target_path=str(destination), state=state)
            )
            if state == "committed":
                await session.delete(entry)
            await session.commit()
        sync = trash._fsync
        changed = False

        def change_destination(path: Path) -> None:
            nonlocal changed
            sync(path)
            if path == destination and not changed:
                changed = True
                raise OSError("recovery sync failed")

        monkeypatch.setattr(trash, "_fsync", change_destination)
        result = await recover_trash_restores(sessions, root)
        assert changed
        assert destination.read_bytes() == b"retained original"
        assert retained.is_file(), f"Recovery discarded its original after sync failed. Outcome: {result}."
        assert retained.read_bytes() == b"retained original"
        assert (retained.parent / "metadata.json").is_file()
        assert result == (0, 1)
        async with sessions() as session:
            assert (
                await session.scalar(select(TrashRestoreJournal).where(TrashRestoreJournal.id == journal_id))
                is not None
            )
            assert (await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None) == (
                state != "committed"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("all_unused", [False, True])
async def test_prune_api_diagnostics(env: Env, monkeypatch: pytest.MonkeyPatch, all_unused: bool) -> None:
    job_id = await file_job(env)
    worker, connection = hub(env), FakeConn([hello()])
    cleanup = worker._cleanup_workspace

    async def deferred_cleanup(*args):
        pass

    monkeypatch.setattr(worker, "_cleanup_workspace", deferred_cleanup)
    task = await serve(worker, connection)

    def unread(*args):
        raise OSError("Injected read failure.")

    try:
        assert await asyncio.wait_for(worker.dispatch_once(), 3) == 1
        offer = connection.messages("job")[0]["job"]
        materialize_offer(worker, offer)
        monkeypatch.setattr("server.nodehub.hub.read_attempt_log", unread)
        await asyncio.wait_for(
            worker._handle_finished(
                worker.entries["node-a"], finished(job_id, offer["attempt_id"], offer["log_path"], "failed")
            ),
            3,
        )
    finally:
        connection.disconnect()
        await asyncio.wait_for(task, 3)
    log = env.workspace / offer["log_path"]
    async with env.factory() as session:
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, offer["attempt_id"])
        assert job is not None and job.status == "failed"
        assert attempt is not None and attempt.status == attempt.result == "failed"
        assert await session.scalar(recovery_jobs().where(Job.id == job_id)) is None
    assert log.read_bytes() == b"log"
    app = create_app(env.factory, config=ServerConfig(workspace_dir=str(env.workspace), trash_dir=str(env.trash)))
    try:
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.get("/api/workspaces")
            assert listed.status_code == 200
            row = next(item for item in listed.json()["items"] if item["job_id"] == job_id)
            body = {"all_unused": True} if all_unused else {"job_ids": [job_id]}
            response = await client.post("/api/workspaces/prune", json=body)
            assert response.status_code == 200
            assert not log.exists()
            assert row["eligible"]
            assert response.json()["removed"] == 1
            async with env.factory() as session:
                assert await session.scalar(recovery_jobs().where(Job.id == job_id)) is None
                saved = await session.get(JobAttempt, offer["attempt_id"])
                assert saved is not None and saved.log_data == b"log"
    finally:
        monkeypatch.setattr(worker, "_cleanup_workspace", cleanup)
        await asyncio.wait_for(app.state.workspaces.aclose(), 3)
        await asyncio.wait_for(app.state.events.aclose(), 3)
        await asyncio.wait_for(app.state.plugin_host.aclose(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("source", ["media", "claim"])
async def test_recovery_sync_retains(restore_env, monkeypatch: pytest.MonkeyPatch, cross_device: bool, source: str):
    from server import trash

    root, library, sessions = restore_env
    _, entry = await put_file(restore_env, monkeypatch)
    media = trash_path(root, entry.library_id, entry.id, entry.suffix)
    retained = media
    if source == "claim":
        retained = media.parent / f".{entry.id}.restore-claim"
        media.rename(retained)
    destination = library / "movie.mkv"
    assert not destination.exists()
    async with sessions() as session:
        session.add(TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="pending"))
        await session.commit()
    link, sync = os.link, trash._fsync
    changed = crossed = False

    def cross_link(src, dst, **kwargs):
        nonlocal crossed
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == retained
            and fd_path(dst, kwargs.get("dst_dir_fd")).parent == destination.parent
        ):
            crossed = True
            raise OSError(errno.EXDEV, "Cross-device link.")
        return link(src, dst, **kwargs)

    def change_destination(path: Path) -> None:
        nonlocal changed
        sync(path)
        if path == destination and not changed:
            changed = True
        if path == destination:
            raise OSError("recovery sync failed")

    monkeypatch.setattr("common.fs.effects.os.link", cross_link)
    monkeypatch.setattr(trash, "_fsync", change_destination)
    assert await recover_trash_restores(sessions, root) == (0, 1)
    assert changed and crossed == cross_device
    assert await recover_trash_restores(sessions, root) == (0, 1)
    assert await refresh_trash_records(sessions, root) == 0
    assert await purge_trash(sessions, root, now=entry.purge_at + timedelta(days=1)) == 0
    with pytest.raises(ValueError):
        await delete_trash_entry(sessions, root, entry.id)
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashRestoreJournal).where(TrashRestoreJournal.id == "j" * 32)) is not None
    assert destination.read_bytes() == b"payload"
    assert retained.read_bytes() == b"payload"
    assert (media.parent / "metadata.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(("cross_device", "sync_to_disk"), [(False, True), (True, True), (True, False)])
async def test_displaced_failure_retained(
    restore_env, monkeypatch: pytest.MonkeyPatch, cross_device: bool, sync_to_disk: bool
):
    from server import trash

    root, library, sessions = restore_env
    _, entry = await put_file(restore_env, monkeypatch)
    media = trash_path(root, entry.library_id, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)
    link, replace, publish, sync = os.link, os.replace, effects.publish, trash._fsync
    changed = crossed = False

    def cross_link(src, dst, **kwargs):
        nonlocal crossed
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == media
            and fd_path(dst, kwargs.get("dst_dir_fd")).parent == destination.parent
        ):
            crossed = True
            raise OSError(errno.EXDEV, "Cross-device link.")
        return link(src, dst, **kwargs)

    def cross_replace(src, dst, **kwargs):
        nonlocal crossed
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == media
            and fd_path(dst, kwargs.get("dst_dir_fd")) == destination
        ):
            crossed = True
            raise OSError(errno.EXDEV, "Cross-device rename.")
        return replace(src, dst, **kwargs)

    def failed_publish(src, dst, **kwargs):
        nonlocal changed
        publish(src, dst, **kwargs)
        if not sync_to_disk and src == media:
            changed = True
            raise OSError("publication acknowledgement failed")

    def changed_sync(path: Path) -> None:
        nonlocal changed
        sync(path)
        if path == destination:
            assert sync_to_disk
            changed = True
            raise OSError("restore sync failed")

    monkeypatch.setattr("common.fs.effects.os.link", cross_link)
    monkeypatch.setattr("common.fs.effects.os.replace", cross_replace)
    monkeypatch.setattr(effects, "publish", failed_publish)
    monkeypatch.setattr(trash, "_fsync", changed_sync)
    with pytest.raises(OSError):
        await restore_trash(
            sessions, root, entry.id, replace_existing=True, move_to_trash=True, sync_to_disk=sync_to_disk
        )
    assert changed and crossed == cross_device
    displaced_media = trash_path(root, entry.library_id, displaced_id, entry.suffix)
    assert await refresh_trash_records(sessions, root) == 0
    assert await purge_trash(sessions, root, now=entry.purge_at + timedelta(days=1)) == 0
    async with sessions() as session:
        displaced = await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id))
        assert displaced is not None
    for retained_entry in (entry, displaced):
        with pytest.raises(ValueError):
            await delete_trash_entry(sessions, root, retained_entry.id)
    async with sessions() as session:
        journal = (await session.scalars(select(TrashRestoreJournal))).one()
        assert journal.state == "displaced" and journal.displaced_entry_id == displaced_id
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert media.read_bytes() == b"payload"
    assert displaced_media.read_bytes() == b"old-bytes"
    assert destination.read_bytes() == b"payload"
    assert (media.parent / "metadata.json").is_file()
    assert (displaced_media.parent / "metadata.json").is_file()


@pytest.mark.asyncio
async def test_recovery_size_refuses(restore_env, monkeypatch: pytest.MonkeyPatch):
    root, library, sessions = restore_env
    _, entry = await put_file(restore_env, monkeypatch)
    media = trash_path(root, entry.library_id, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    effects.copy_file(media, destination)
    retained = media
    async with sessions() as session:
        session.add(TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="pending"))
        await session.commit()
    saved_original = retained.with_name("saved-original")
    retained.rename(saved_original)
    retained.write_bytes(b"unrelated retained size")
    assert await recover_trash_restores(sessions, root) == (0, 1)
    assert await refresh_trash_records(sessions, root) == 0
    assert await purge_trash(sessions, root, now=entry.purge_at + timedelta(days=1)) == 0
    async with sessions() as session:
        assert await session.scalar(select(TrashRestoreJournal).where(TrashRestoreJournal.id == "j" * 32)) is not None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert retained.read_bytes() == b"unrelated retained size"
    assert saved_original.read_bytes() == destination.read_bytes() == b"payload"
    assert (media.parent / "metadata.json").is_file()


@pytest.mark.parametrize("collision", [False, True])
def test_move_publication_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collision: bool):
    source, destination, partial = tmp_path / "source", tmp_path / "destination", tmp_path / ".fixed.part"
    source.write_bytes(b"payload")
    destination.write_bytes(b"old-bytes")
    if collision:
        partial.write_bytes(b"unrelated partial")

    def failed_replace(*args, **kwargs):
        raise OSError("publication failed")

    monkeypatch.setattr("common.fs.effects.uuid.uuid4", lambda: SimpleNamespace(hex="fixed"))
    monkeypatch.setattr("common.fs.effects.os.replace", failed_replace)
    with pytest.raises(OSError):
        effects.move_file(source, destination, sync_destination=lambda _: pytest.fail("Unpublished destination sync."))
    if collision:
        assert partial.read_bytes() == b"unrelated partial"
    else:
        assert not partial.exists()
    assert source.read_bytes() == b"payload"
    assert destination.read_bytes() == b"old-bytes"


@pytest.mark.parametrize("change", ["parent", "leaf"])
def test_move_symlink_confined(tmp_path: Path, change: str):
    source_parent, destination_parent, outside = tmp_path / "source", tmp_path / "destination", tmp_path / "outside"
    for directory in (source_parent, destination_parent, outside):
        directory.mkdir()
    source, destination = source_parent / "original", destination_parent / "target"
    saved_parent = tmp_path / "saved-owned-parent"
    source.write_bytes(b"payload")
    destination.write_bytes(b"old-bytes")
    (outside / destination.name).write_bytes(b"outside old-bytes")
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    if change == "parent":
        destination_parent.rename(saved_parent)
        destination_parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            effects.move_file(source, destination, sync_destination=lambda _: None)
        assert source.read_bytes() == b"payload"
    else:
        destination.unlink()
        destination.symlink_to(outside / destination.name)
        effects.move_file(source, destination, sync_destination=lambda _: None)
        assert not source.exists()
        assert not destination.is_symlink() and destination.read_bytes() == b"payload"
    after = {path.name: path.read_bytes() for path in outside.iterdir()}
    assert after == before, "Overwrite modified the symlink target outside its acquired parent."
