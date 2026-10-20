from __future__ import annotations

import asyncio
import errno
import os
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import pytest_asyncio

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from common.fs import effects
from server.models import Library, MediaFile, TrashEntry, TrashRestoreJournal, utcnow
from server.trash import (
    _lock_leaf,
    _purge_leaf,
    delete_trash_entry,
    purge_trash,
    refresh_trash_records,
    recover_trash_restores,
    restore_trash,
    store_trash,
    trash_path,
)
from tests.helpers import database_context, fd_path, make_test_temp_dir

pytestmark = pytest.mark.usefixtures("local_storage")


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[Path, Path, async_sessionmaker[AsyncSession]]]:
    root = make_test_temp_dir("trash-restore")
    trash = root / "trash"
    library = root / "library"
    trash.mkdir()
    library.mkdir()
    async with database_context("trash-restore") as sessions:
        yield trash, library, sessions


async def put_file(
    env: tuple[Path, Path, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch, name: str = "movie.mkv"
) -> tuple[Path, TrashEntry]:
    trash, library, sessions = env
    source = library / name
    source.write_bytes(b"payload")
    identifier = "0" * 15 + ("1" if name == "movie.mkv" else "2")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: identifier)
    async with sessions() as session:
        entry = await store_trash(session, trash, 1, source, library, 30)
        await session.commit()
    return library, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["UTC", "Asia/Kuwait"])
async def test_lists_entries(env, monkeypatch, timezone) -> None:
    trash, library, sessions = env
    stamp = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    library_row = Library(name="movies", roots=[str(library)])
    async with sessions() as session:
        session.add(library_row)
        await session.commit()
    _, entry = await put_file(env, monkeypatch)
    async with sessions() as session:
        stored = await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id))
        assert stored is not None
        stored.trashed_at = stamp
        stored.purge_at = stamp + timedelta(days=30)
        media = MediaFile(
            library_id=1,
            path=entry.original_path,
            size=1,
            mtime_ns=1,
            container="mkv",
            video_codec="",
            medium="other",
            health_status="pending",
            last_scanned_at=utcnow(),
        )
        session.add(media)
        await session.commit()
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(env[1]), tz=timezone))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/trash")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == entry.id
    assert response.json()["items"][0]["current_file_id"] == media.id
    assert response.json()["items"][0]["trashed_at"] == "2026-01-02T03:04:05.123456Z"
    assert response.json()["items"][0]["purge_at"] == "2026-02-01T03:04:05.123456Z"
    assert response.json()["pagination"] == {"limit": 50, "offset": 0, "total": 1}
    assert response.json()["total_size_bytes"] == entry.size_bytes


@pytest.mark.asyncio
async def test_trash_pagination_filters(env, monkeypatch) -> None:
    trash, library, sessions = env
    await put_file(env, monkeypatch)
    _, second = await put_file(env, monkeypatch, "other.mkv")
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/api/trash?limit=1&offset=0&library_id=1")
        next_page = await client.get("/api/trash?limit=1&offset=1&library_id=1")
        filtered = await client.get("/api/trash?query=%20OTHER.MKV%20")
        empty = await client.get("/api/trash?query=missing")
        invalid = await client.get("/api/trash?limit=0&offset=-1&library_id=0")
    assert first.json()["pagination"] == {"limit": 1, "offset": 0, "total": 2}
    assert next_page.json()["pagination"] == {"limit": 1, "offset": 1, "total": 2}
    assert first.json()["items"][0]["id"] == second.id
    assert first.json()["items"][0]["current_file_id"] is None
    assert next_page.json()["items"][0]["id"] != first.json()["items"][0]["id"]
    assert filtered.json()["pagination"]["total"] == 1
    assert filtered.json()["total_size_bytes"] == second.size_bytes
    assert empty.json()["total_size_bytes"] == 0
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_trash_delete_entry(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/trash/{entry.id}")
        missing = await client.delete("/api/trash/0123456789abcdef")
    assert response.status_code == 204 and missing.status_code == 404
    assert not trash_path(trash, 1, entry.id, entry.suffix).parent.exists()
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None


@pytest.mark.asyncio
async def test_trash_delete_conflicts(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    async with sessions() as session:
        session.add(TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path="x", state="pending"))
        await session.commit()
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/trash/{entry.id}")
    assert response.status_code == 409 and trash_path(trash, 1, entry.id, entry.suffix).parent.exists()


@pytest.mark.asyncio
async def test_trash_delete_lock(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    leaf = trash_path(trash, 1, entry.id, entry.suffix).parent
    fd = _lock_leaf(leaf, required=True)
    assert fd is not None
    try:
        app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/trash/{entry.id}")
    finally:
        os.close(fd)
    assert response.status_code == 409
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
async def test_trash_expunge_failures(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, pending = await put_file(env, monkeypatch)
    _, locked = await put_file(env, monkeypatch, "other.mkv")
    async with sessions() as session:
        await session.commit()
    leaf = trash_path(trash, 1, locked.id, locked.suffix).parent
    fd = _lock_leaf(leaf, required=True)
    assert fd is not None
    try:
        app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/api/trash")
    finally:
        os.close(fd)
    assert response.json() == {"deleted": 1, "deleted_size_bytes": pending.size_bytes, "failed": [locked.id]}
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == pending.id)) is None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == locked.id)) is not None


@pytest.mark.asyncio
async def test_restores_empty(env, monkeypatch, caplog) -> None:
    caplog.set_level("INFO", logger="server.trash")
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    await restore_trash(sessions, trash, entry.id)
    assert (library / "movie.mkv").read_bytes() == b"payload"
    restored = [record for record in caplog.records if getattr(record, "entry_id", None) == entry.id]
    assert len(restored) == 1
    assert restored[0].operation == "restore"
    assert restored[0].outcome == "succeeded"
    assert restored[0].entry_id == entry.id
    assert restored[0].library_id == 1
    assert restored[0].restored_path == str(library / "movie.mkv")


@pytest.mark.asyncio
async def test_move_crash_recovers(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    original = effects.publish

    def crash_after_move(source, destination, **kwargs):
        original(source, destination, **kwargs)
        raise OSError("journal interruption")

    monkeypatch.setattr("common.fs.effects.publish", crash_after_move)
    with pytest.raises(OSError):
        await restore_trash(sessions, trash, entry.id)
    async with sessions() as session:
        journal = (await session.scalars(select(TrashRestoreJournal))).one()
        assert journal.state == "pending"
    with pytest.raises(ValueError):
        await restore_trash(sessions, trash, entry.id)
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert (library / "movie.mkv").read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None


@pytest.mark.asyncio
async def test_recovery_from_claim(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    original = effects.publish

    def crash_with_claim(source, _destination, **_kwargs):
        claim_path = source.parent / f".{entry.id}.restore-claim"
        source.replace(claim_path)
        raise OSError("claim interruption")

    monkeypatch.setattr("common.fs.effects.publish", crash_with_claim)
    with pytest.raises(OSError):
        await restore_trash(sessions, trash, entry.id)
    monkeypatch.setattr("common.fs.effects.publish", original)
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert (library / "movie.mkv").read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_http_restore(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, first = await put_file(env, monkeypatch)
    _, second = await put_file(env, monkeypatch, "other.mkv")
    (library / "other.mkv").write_bytes(b"old-bytes")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: "0000000000000003")
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        success = await client.post(f"/api/trash/{first.id}/restore", json={})
        conflict = await client.post(f"/api/trash/{second.id}/restore", json={})
        override = await client.post(
            f"/api/trash/{second.id}/restore", json={"replace_existing": True, "move_to_trash": True}
        )
    assert success.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]
    assert override.status_code == 200
    assert set(override.json()) == {"id", "restored_path"}
    assert (library / "other.mkv").read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == first.id)) is None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == second.id)) is None
        assert await session.scalar(select(TrashRestoreJournal.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_to_disk", [None, False])
async def test_http_restore_sync(env, monkeypatch, sync_to_disk) -> None:
    from server.trash import _fsync

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    synced = []

    def sync(path):
        assert media.read_bytes() == destination.read_bytes() == b"payload"
        synced.append(path)
        _fsync(path)

    monkeypatch.setattr("server.trash._fsync", sync)
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    body = {} if sync_to_disk is None else {"sync_to_disk": sync_to_disk}
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/trash/{entry.id}/restore", json=body)
    assert response.status_code == 200
    assert response.json() == {"id": entry.id, "restored_path": str(destination)}
    assert synced == ([destination, library] if sync_to_disk is None else [])
    assert destination.read_bytes() == b"payload" and not media.parent.exists()
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
        assert await session.scalar(select(TrashRestoreJournal.id)) is None


@pytest.mark.asyncio
async def test_collision_unchanged(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    (library / "movie.mkv").write_bytes(b"old-bytes")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: "0000000000000003")
    with pytest.raises(FileExistsError):
        await restore_trash(sessions, trash, entry.id)
    assert (library / "movie.mkv").read_bytes() == b"old-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("stat_error", [errno.EACCES, errno.EIO])
async def test_restore_keeps_unavailable(env, monkeypatch: pytest.MonkeyPatch, stat_error: int) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    stat = os.stat

    def unavailable(path, *args, **kwargs):
        if path in (destination, str(destination)):
            raise OSError(stat_error, "existing file unavailable")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", unavailable)
        with pytest.raises(OSError) as failure:
            await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
        assert failure.value.errno == stat_error
    assert destination.read_bytes() == b"old-bytes"
    assert media.read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashRestoreJournal.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["media", "claim"])
@pytest.mark.parametrize("stat_error", [errno.EACCES, errno.EIO])
async def test_recovery_keeps_unavailable(env, monkeypatch: pytest.MonkeyPatch, boundary: str, stat_error: int) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    if boundary == "claim":
        media.rename(claim)
    target = media if boundary == "media" else claim
    destination = library / "movie.mkv"
    async with sessions() as session:
        session.add(TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="pending"))
        await session.commit()
    stat = os.stat

    def unavailable(path, *args, **kwargs):
        if path in (target, str(target)):
            raise OSError(stat_error, "retained source unavailable")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", unavailable)
        assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert target.read_bytes() == b"payload"
    assert not destination.exists()
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        journal = await session.scalar(select(TrashRestoreJournal).where(TrashRestoreJournal.id == "j" * 32))
        assert journal is not None and journal.state == "pending"
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert destination.read_bytes() == b"payload" and not target.exists()


@pytest.mark.asyncio
async def test_override_trashes_destination(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    (library / "movie.mkv").write_bytes(b"old-bytes")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: "0000000000000003")
    await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    async with sessions() as session:
        rows = list((await session.execute(select(TrashEntry))).scalars())
    assert (library / "movie.mkv").read_bytes() == b"payload"
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_override_overwrites_destination(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    (library / "movie.mkv").write_bytes(b"old-bytes")
    await restore_trash(sessions, trash, entry.id, replace_existing=True)
    async with sessions() as session:
        rows = list((await session.execute(select(TrashEntry))).scalars())
    assert (library / "movie.mkv").read_bytes() == b"payload"
    assert rows == []


@pytest.mark.asyncio
async def test_overwrite_retains_source(env, monkeypatch, caplog) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"WRONG!!")

    def fail(*args, **kwargs):
        raise OSError("publication failed")

    with monkeypatch.context() as patch:
        patch.setattr("common.fs.effects.publish", fail)
        with pytest.raises(OSError):
            await restore_trash(sessions, trash, entry.id, replace_existing=True)
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert f"Overwrite restore publication is unconfirmed at {destination}" in caplog.text
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    assert destination.read_bytes() == b"WRONG!!"
    assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_restore_diskfull_retry(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    original_replace = os.replace
    copied = []

    def cross_device(source, target, **kwargs):
        if (
            fd_path(source, kwargs.get("src_dir_fd")) == media
            and fd_path(target, kwargs.get("dst_dir_fd")) == destination
        ):
            raise OSError(errno.EXDEV, "different device")
        return original_replace(source, target, **kwargs)

    original_link = os.link

    def cross_link(source, target, **kwargs):
        if (
            fd_path(source, kwargs.get("src_dir_fd")) == media
            and fd_path(target, kwargs.get("dst_dir_fd")).parent == destination.parent
        ):
            raise OSError(errno.EXDEV, "different device")
        return original_link(source, target, **kwargs)

    def no_clone(*_args):
        raise OSError(errno.EOPNOTSUPP, "no reflink")

    def disk_full(source, target):
        copied.append(source.read())
        target.write(b"part")
        raise OSError(errno.ENOSPC, "disk full")

    with monkeypatch.context() as patch:
        patch.setattr("common.fs.effects.os.replace", cross_device)
        patch.setattr("common.fs.effects.os.link", cross_link)
        patch.setattr("common.fs.effects.fcntl.ioctl", no_clone)
        patch.setattr("common.fs.effects.shutil.copyfileobj", disk_full)
        with pytest.raises(OSError) as failure:
            await restore_trash(sessions, trash, entry.id, replace_existing=True)
        assert failure.value.errno == errno.ENOSPC
        async with sessions() as session:
            assert await session.scalar(select(TrashRestoreJournal.id)) is not None
            assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert copied == [b"payload"]
    assert media.read_bytes() == b"payload" and destination.read_bytes() == b"old-bytes"
    assert not list(media.parent.glob("*.restore-claim"))
    assert not list(destination.parent.glob(".*.part"))
    existing_file = library / "saved-existing.mkv"
    destination.rename(existing_file)
    with monkeypatch.context() as patch:
        patch.setattr("common.fs.effects.os.replace", cross_device)
        patch.setattr("common.fs.effects.os.link", cross_link)
        patch.setattr("common.fs.effects.fcntl.ioctl", no_clone)
        assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert destination.read_bytes() == b"payload"
    assert existing_file.read_bytes() == b"old-bytes"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
        assert await session.scalar(select(TrashRestoreJournal.id)) is None


@pytest.mark.asyncio
async def test_restore_ambiguous_failure(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")

    def published_copy(source, target, **_kwargs):
        effects.copy_file(source, target, overwrite=True)
        raise OSError("publication acknowledgement failed")

    monkeypatch.setattr("common.fs.effects.publish", published_copy)
    with pytest.raises(OSError):
        await restore_trash(sessions, trash, entry.id, replace_existing=True)
    async with sessions() as session:
        assert (await session.scalars(select(TrashRestoreJournal))).one().state == "overwrite_pending"
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    with pytest.raises(ValueError):
        await restore_trash(sessions, trash, entry.id, replace_existing=True)
    assert destination.read_bytes() == b"payload"
    assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"payload"
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert destination.read_bytes() == b"payload"
    assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.get(TrashEntry, entry.id) is not None
        assert await session.scalar(select(TrashRestoreJournal.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "retaining", "published"])
async def test_destination_failure_retained(env, monkeypatch, phase: str) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    identifier = "0000000000000003"
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: identifier)
    original_link = os.link
    from server.trash import _fsync

    displaced = trash_path(trash, entry.library_id, identifier, ".mkv")

    def link(source, target, **kwargs):
        if phase == "before" and fd_path(source, kwargs.get("src_dir_fd")) == destination:
            raise OSError(errno.ENOSPC, "disk full")
        return original_link(source, target, **kwargs)

    def sync(path):
        if (phase == "retaining" and path.name.endswith(".partial")) or (
            phase == "published" and path == displaced.parent.parent
        ):
            raise OSError(errno.ENOSPC, "disk full")
        return _fsync(path)

    with monkeypatch.context() as patch:
        patch.setattr("server.trash.os.link", link)
        patch.setattr("server.trash._fsync", sync)
        with pytest.raises(OSError):
            await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"payload"
    async with sessions() as session:
        pending = await session.scalar(select(TrashRestoreJournal.id))
        assert pending is not None
        if phase == "published":
            assert displaced.read_bytes() == b"old-bytes"
        else:
            assert destination.read_bytes() == b"old-bytes"
    with pytest.raises(ValueError):
        await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)


@pytest.mark.asyncio
async def test_override_failure_recovers(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    async with sessions() as session:
        session.add(Library(name="movies", roots=[str(library)]))
        await session.commit()
    (library / "movie.mkv").write_bytes(b"old-bytes")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: "0000000000000003")
    original_move = effects.publish
    calls = 0
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)

    def fail_once(source, destination, **kwargs):
        nonlocal calls
        if source == media:
            calls += 1
            if calls == 1:
                raise OSError("copy failed")
        return original_move(source, destination, **kwargs)

    monkeypatch.setattr("common.fs.effects.publish", fail_once)
    with pytest.raises(OSError):
        await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    async with sessions() as session:
        rows = list((await session.execute(select(TrashEntry))).scalars())
        assert len(rows) == 2
        assert all(trash_path(trash, row.library_id, row.id, row.suffix).exists() for row in rows)
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert (library / "movie.mkv").read_bytes() == b"payload"
    assert any(path.read_bytes() == b"old-bytes" for path in trash.rglob("file.mkv"))


@pytest.mark.asyncio
async def test_cleanup_retains_metadata(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    original_purge = _purge_leaf
    monkeypatch.setattr("server.trash._purge_leaf", lambda *_: (_ for _ in ()).throw(OSError("purge failed")))
    await restore_trash(sessions, trash, entry.id)
    assert (library / "movie.mkv").read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
        assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    assert not list(trash.rglob("file.mkv"))
    assert list(trash.rglob("metadata.json"))
    monkeypatch.setattr("server.trash._purge_leaf", original_purge)
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert not list(trash.rglob("metadata.json"))
    async with sessions() as session:
        assert await session.scalar(select(TrashRestoreJournal.id)) is None


@pytest.mark.asyncio
async def test_duplicate_restore_locked(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_move = effects.publish

    def blocked_move(source, destination, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original_move(source, destination, **kwargs)

    monkeypatch.setattr("common.fs.effects.publish", blocked_move)

    first = asyncio.create_task(restore_trash(sessions, trash, entry.id))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(ValueError):
            await restore_trash(sessions, trash, entry.id)
    finally:
        release.set()
        await asyncio.wait_for(first, 5)


@pytest.mark.asyncio
async def test_recovery_published(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"payload")
    async with sessions() as session:
        session.add(Library(name="movies", roots=[str(library)]))
        await session.commit()
        session.add(TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="moved"))
        await session.commit()
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert not trash_path(trash, 1, entry.id, entry.suffix).parent.exists()


@pytest.mark.asyncio
async def test_rejects_symlink_parent(env, monkeypatch) -> None:
    trash, library, sessions = env
    outside = library.parent / "outside"
    outside.mkdir()
    nested = library / "nested"
    nested.mkdir()
    source = nested / "movie.mkv"
    source.write_bytes(b"payload")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: "0123456789abcdef")
    async with sessions() as session:
        entry = await store_trash(session, trash, 1, source, library, 30)
        await session.commit()
    nested.rmdir()
    nested.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        await restore_trash(sessions, trash, entry.id)


@pytest.mark.asyncio
async def test_refresh_during_restore(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_move = effects.publish

    def failing_move(source, destination, **kwargs):
        if source != media:
            return original_move(source, destination, **kwargs)
        claim_path = source.parent / f".{entry.id}.restore-claim"
        source.rename(claim_path)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        original_move(claim_path, source, overwrite=False)
        raise OSError("copy failure")

    monkeypatch.setattr("common.fs.effects.publish", failing_move)

    async def restoring():
        with pytest.raises(OSError):
            await restore_trash(sessions, trash, entry.id)

    task = asyncio.create_task(restoring())
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await recover_trash_restores(sessions, trash) == (0, 0)
        assert await refresh_trash_records(sessions, trash) == 0
        async with sessions() as session:
            assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert (media.parent / f".{entry.id}.restore-claim").read_bytes() == b"payload"
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert media.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_restore_library_lock(env, monkeypatch) -> None:
    from server.trash import library_mutation_lock

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    async with library_mutation_lock(library, wait=False):
        with pytest.raises(ValueError):
            await restore_trash(sessions, trash, entry.id)
    assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"payload"
    assert not (library / "movie.mkv").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_to_disk", [False, True])
@pytest.mark.parametrize("replace_existing", [False, True])
@pytest.mark.parametrize("cross_device", [False, True])
async def test_restore_sync_order(env, monkeypatch, sync_to_disk, replace_existing, cross_device) -> None:
    trash, library, sessions = env
    nested = library / "first" / "second"
    nested.mkdir(parents=True)
    source = nested / "movie.mkv"
    source.write_bytes(b"payload")
    async with sessions() as session:
        entry = await store_trash(session, trash, 1, source, library, 30, identifier="0123456789abcdef")
        await session.commit()
    nested.rmdir()
    nested.parent.rmdir()
    if replace_existing:
        nested.mkdir(parents=True)
        source.write_bytes(b"old-bytes")
    media = trash_path(trash, 1, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    synced = []
    original_replace, original_link = os.replace, os.link

    def replace(src, dst, **kwargs):
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == media
            and fd_path(dst, kwargs.get("dst_dir_fd")) == source
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_replace(src, dst, **kwargs)

    def link(src, dst, **kwargs):
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == media
            and fd_path(dst, kwargs.get("dst_dir_fd")).parent == source.parent
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_link(src, dst, **kwargs)

    def sync(path):
        assert source.read_bytes() == b"payload"
        assert media.read_bytes() == b"payload"
        assert (media.parent / "metadata.json").is_file()
        synced.append(path)

    monkeypatch.setattr("common.fs.effects.os.replace", replace)
    monkeypatch.setattr("common.fs.effects.os.link", link)
    monkeypatch.setattr("server.trash._fsync", sync)
    await restore_trash(sessions, trash, entry.id, replace_existing=replace_existing, sync_to_disk=sync_to_disk)
    async with sessions() as session:
        assert await session.scalar(select(TrashRestoreJournal.id)) is None
    assert synced == ([source, nested, nested.parent, library] if sync_to_disk else [])
    assert source.read_bytes() == b"payload" and not media.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_existing", [False, True])
@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("failure_path", ["file", "directory"])
async def test_restore_sync_failure(env, monkeypatch, replace_existing, cross_device, failure_path) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    destination = library / "movie.mkv"
    if replace_existing:
        destination.write_bytes(b"old-bytes")
    original_replace, original_link = os.replace, os.link

    def replace(src, dst, **kwargs):
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == media
            and fd_path(dst, kwargs.get("dst_dir_fd")) == destination
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_replace(src, dst, **kwargs)

    def link(src, dst, **kwargs):
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == media
            and fd_path(dst, kwargs.get("dst_dir_fd")).parent == destination.parent
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_link(src, dst, **kwargs)

    def fail_sync(path):
        if path == (destination if failure_path == "file" else library):
            raise OSError("sync failed")

    with monkeypatch.context() as patch:
        patch.setattr("common.fs.effects.os.replace", replace)
        patch.setattr("common.fs.effects.os.link", link)
        patch.setattr("server.trash._fsync", fail_sync)
        with pytest.raises(OSError):
            await restore_trash(sessions, trash, entry.id, replace_existing=replace_existing)
        async with sessions() as session:
            assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
            assert (await session.scalars(select(TrashRestoreJournal))).one().state == (
                "overwrite_pending" if replace_existing else "pending"
            )
        assert destination.read_bytes() == b"payload"
        assert media.read_bytes() == b"payload"
        assert await recover_trash_restores(sessions, trash) == (0, 1)
        assert await refresh_trash_records(sessions, trash) == 0
        assert await purge_trash(sessions, trash, now=entry.purge_at) == 0
        with pytest.raises(ValueError):
            await delete_trash_entry(sessions, trash, entry.id)
        async with sessions() as session:
            assert await session.scalar(select(TrashRestoreJournal.id)) is not None
        assert (media.parent / "metadata.json").is_file()
        assert media.read_bytes() == b"payload"
    assert await recover_trash_restores(sessions, trash) == ((0, 1) if replace_existing else (1, 0))
    assert destination.read_bytes() == b"payload"
    if replace_existing:
        assert media.read_bytes() == b"payload"
        async with sessions() as session:
            assert await session.get(TrashEntry, entry.id) is not None
            assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    else:
        assert not media.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("move_to_trash", [False, True])
async def test_overwrite_cancel_retains(env, monkeypatch, cross_device: bool, move_to_trash: bool) -> None:
    from server.trash import _fsync

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_link = os.link
    changed = False

    def link(source, target, **kwargs):
        if (
            cross_device
            and fd_path(source, kwargs.get("src_dir_fd")) == media
            and fd_path(target, kwargs.get("dst_dir_fd")).parent == destination.parent
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_link(source, target, **kwargs)

    def sync(path):
        nonlocal changed
        _fsync(path)
        if path == destination:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            changed = True
            raise OSError("restore sync failed")

    monkeypatch.setattr("common.fs.effects.os.link", link)
    monkeypatch.setattr("server.trash._fsync", sync)
    task = asyncio.create_task(
        restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=move_to_trash)
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        checkpoint = asyncio.Event()
        loop.call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 5)
        assert task.cancelling() == 1 and not task.done()
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    # A drained worker failure takes precedence over the pending cancellation.
    assert isinstance(outcomes[0], OSError) and changed
    retained = media
    assert retained.read_bytes() == b"payload"
    assert destination.read_bytes() == b"payload"
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert await refresh_trash_records(sessions, trash) == 0
    assert await purge_trash(sessions, trash, now=entry.purge_at + timedelta(days=1)) == 0
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    assert retained.read_bytes() == b"payload" and (media.parent / "metadata.json").is_file()
    if move_to_trash:
        assert trash_path(trash, entry.library_id, displaced_id, entry.suffix).read_bytes() == b"old-bytes"


@pytest.mark.asyncio
async def test_recovery_committed_sync(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    effects.copy_file(media, destination)
    async with sessions() as session:
        session.add(
            TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="committed")
        )
        await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
        await session.commit()
    synced = []

    def fail_sync(path):
        synced.append(path)
        if path == library:
            raise OSError("sync failed")

    with monkeypatch.context() as patch:
        patch.setattr("server.trash._fsync", fail_sync)
        assert await recover_trash_restores(sessions, trash) == (0, 1)
        assert await refresh_trash_records(sessions, trash) == 0
        async with sessions() as session:
            assert await session.scalar(select(TrashRestoreJournal.id)) is not None
        assert synced == [destination, library]
        assert media.read_bytes() == b"payload" and (media.parent / "metadata.json").is_file()
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert destination.read_bytes() == b"payload" and not media.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_device", [False, True])
async def test_recovery_claim_sync(env, monkeypatch, cross_device) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    media.rename(claim)
    destination = library / "movie.mkv"
    async with sessions() as session:
        session.add(TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="pending"))
        await session.commit()
    original_link = os.link
    synced = []

    def link(src, dst, **kwargs):
        if (
            cross_device
            and fd_path(src, kwargs.get("src_dir_fd")) == claim
            and fd_path(dst, kwargs.get("dst_dir_fd")) == destination
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_link(src, dst, **kwargs)

    def fail_sync(path):
        assert claim.read_bytes() == destination.read_bytes() == b"payload"
        synced.append(path)
        if path == library:
            raise OSError("sync failed")

    with monkeypatch.context() as patch:
        patch.setattr("common.fs.effects.os.link", link)
        patch.setattr("server.trash._fsync", fail_sync)
        assert await recover_trash_restores(sessions, trash) == (0, 1)
        assert synced == [destination, library]
        assert claim.read_bytes() == b"payload"
        assert (media.parent / "metadata.json").is_file()
        async with sessions() as session:
            assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
            assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert destination.read_bytes() == b"payload" and not media.parent.exists()


@pytest.mark.asyncio
async def test_recovery_missing_leaf(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    await restore_trash(sessions, trash, entry.id, sync_to_disk=False)
    async with sessions() as session:
        session.add(
            TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=entry.original_path, state="committed")
        )
        await session.commit()

    def fail_sync(_path):
        raise OSError("sync failed")

    with monkeypatch.context() as patch:
        patch.setattr("server.trash._fsync", fail_sync)
        assert await recover_trash_restores(sessions, trash) == (1, 0)
        async with sessions() as session:
            assert await session.scalar(select(TrashRestoreJournal.id)) is None
    assert await recover_trash_restores(sessions, trash) == (0, 0)
    assert (library / "movie.mkv").read_bytes() == b"payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_recovery_sync_locks(env, monkeypatch, committed) -> None:
    from server.trash import _fsync, _unlock_leaf, library_mutation_lock

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, 1, entry.id, entry.suffix)
    destination = library / "movie.mkv"
    effects.copy_file(media, destination)
    async with sessions() as session:
        session.add(
            TrashRestoreJournal(
                id="j" * 32,
                entry_id=entry.id,
                target_path=str(destination),
                state="committed" if committed else "pending",
            )
        )
        if committed:
            await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
        await session.commit()
    started = asyncio.Event()
    completed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_sync(path):
        if path != destination:
            return _fsync(path)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            _fsync(path)
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr("server.trash._fsync", blocked_sync)
    task = asyncio.create_task(recover_trash_restores(sessions, trash))
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done() and not completed.is_set()
            fd = _lock_leaf(media.parent)
            if fd is not None:
                _unlock_leaf(fd)
            assert fd is None
            with pytest.raises(ValueError):
                async with library_mutation_lock(library, wait=False):
                    pytest.fail("recovery released library locks")
            assert media.read_bytes() == b"payload"
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert media.read_bytes() == b"payload" and (media.parent / "metadata.json").is_file()
    async with sessions() as session:
        assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert destination.read_bytes() == b"payload" and not media.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_device", [False, True])
async def test_recovery_retains_destination(env, monkeypatch, cross_device: bool) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    displaced = trash_path(trash, entry.library_id, displaced_id, entry.suffix)
    claim = destination.with_name(f".{displaced_id}.claim")
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_link = os.link
    original_publish = effects.publish
    original_copy = effects.copy_file
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)

    def link(source, target, **kwargs):
        if (
            cross_device
            and fd_path(source, kwargs.get("src_dir_fd")) == claim
            and fd_path(target, kwargs.get("dst_dir_fd")).parent.name.endswith(".partial")
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_link(source, target, **kwargs)

    def publish(source, target, **kwargs):
        result = original_publish(source, target, **kwargs)
        if not cross_device and source == claim and target.parent.name.endswith(".partial"):
            raise asyncio.CancelledError
        return result

    def blocked_copy(source, target, **kwargs):
        if source == claim:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        return original_copy(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("server.trash.os.link", link)
        patch.setattr(effects, "publish", publish)
        patch.setattr(effects, "copy_file", blocked_copy)
        task = asyncio.create_task(restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True))
        try:
            if cross_device:
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
        finally:
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    staged = displaced.parent.with_name(displaced.parent.name + ".partial") / displaced.name
    assert not destination.exists()
    if cross_device:
        assert displaced.read_bytes() == b"old-bytes" and not staged.exists()
        async with sessions() as session:
            assert await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id)) is not None
        assert await recover_trash_restores(sessions, trash) == (1, 0)
        assert destination.read_bytes() == b"payload" and displaced.read_bytes() == b"old-bytes"
        return
    assert staged.read_bytes() == b"old-bytes"
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert await refresh_trash_records(sessions, trash) == 0
    async with sessions() as session:
        retained = await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id))
        pending = await session.scalar(select(TrashRestoreJournal.id))
    assert retained is None and pending is not None
    assert claim.read_bytes() == staged.read_bytes() == b"old-bytes"
    assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True])
async def test_restore_historical_root(env, monkeypatch, deleted: bool) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    other = library.parent / "other-library"
    other.mkdir()
    async with sessions() as session:
        row = Library(id=entry.library_id, name="movies", roots=[str(library)])
        session.add(row)
        await session.commit()
        if deleted:
            await session.delete(row)
        else:
            row.roots = [str(other)]
        await session.commit()
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/trash/{entry.id}/restore", json={})
    assert response.status_code == 200
    assert (library / "movie.mkv").read_bytes() == b"payload"
    assert not list(other.iterdir())


@pytest.mark.asyncio
async def test_refresh_late_restore(env, monkeypatch) -> None:
    from server.trash import _try_library_lock

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    started = asyncio.Event()
    published = asyncio.Event()
    release = threading.Event()
    restore_release = threading.Event()
    loop = asyncio.get_running_loop()
    original_move = effects.publish

    def blocked_lock(root, *, shared=False, workspace=False):
        if not started.is_set():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        return _try_library_lock(root, shared=shared, workspace=workspace)

    def blocked_move(source, destination, **kwargs):
        result = original_move(source, destination, **kwargs)
        loop.call_soon_threadsafe(published.set)
        assert restore_release.wait(5)
        return result

    monkeypatch.setattr("server.trash._try_library_lock", blocked_lock)
    monkeypatch.setattr(effects, "publish", blocked_move)

    task = asyncio.create_task(refresh_trash_records(sessions, trash))
    restore_task = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        restore_task = asyncio.create_task(restore_trash(sessions, trash, entry.id))
        await asyncio.wait_for(published.wait(), 5)
        restore_task.cancel()
        restore_release.set()
        restored = await asyncio.wait_for(asyncio.gather(restore_task, return_exceptions=True), 5)
        assert isinstance(restored[0], asyncio.CancelledError)
        async with sessions() as session:
            assert await session.scalar(select(TrashRestoreJournal.id)) is not None
        assert (media.parent / "metadata.json").is_file()
    finally:
        restore_release.set()
        release.set()
        tasks = [task] if restore_task is None else [task, restore_task]
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    async with sessions() as session:
        retained = await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id))
        pending = await session.scalar(select(TrashRestoreJournal.id))
    observed = (outcomes[0], (media.parent / "metadata.json").is_file(), retained is not None, pending is not None)
    assert observed == (0, True, True, True)
    assert await recover_trash_restores(sessions, trash) == (1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "expunge", "expiry", "restore", "refresh"])
@pytest.mark.parametrize("state", ["displaced", "committed"])
async def test_displaced_entry_protection(env, monkeypatch, operation: str, state: str) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    displaced_media = trash_path(trash, entry.library_id, displaced_id, entry.suffix)
    original_media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    original_move = effects.publish
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)

    def interrupted_move(source, target, **kwargs):
        original_move(source, target, **kwargs)
        if source == original_media:
            raise OSError("publication acknowledgement failed")

    def failed_cleanup(*_args):
        raise OSError("cleanup failed")

    with monkeypatch.context() as patch:
        if state == "displaced":
            patch.setattr(effects, "publish", interrupted_move)
        else:
            patch.setattr("server.trash._purge_leaf", failed_cleanup)
        if state == "displaced":
            with pytest.raises(OSError):
                await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
        else:
            await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    assert destination.read_bytes() == b"payload"
    assert displaced_media.read_bytes() == b"old-bytes"
    if operation == "expunge":
        app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/api/trash")
        assert response.json()["deleted"] == 0
        assert set(response.json()["failed"]) == ({entry.id, displaced_id} if state == "displaced" else {displaced_id})
    else:
        async with sessions() as session:
            displaced = await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id))
            assert displaced is not None
        if operation == "delete":
            with pytest.raises(ValueError):
                await delete_trash_entry(sessions, trash, displaced.id)
        elif operation == "expiry":
            assert await purge_trash(sessions, trash, now=entry.purge_at + timedelta(days=1)) == 0
        elif operation == "restore":
            with pytest.raises(ValueError):
                await restore_trash(sessions, trash, displaced.id, replace_existing=True)
        else:
            retained_copy = displaced_media.with_name("existing.retained_copy")
            displaced_media.rename(retained_copy)
            try:
                assert await refresh_trash_records(sessions, trash) == 0
                assert (displaced_media.parent / "metadata.json").is_file()
            finally:
                retained_copy.rename(displaced_media)
    async with sessions() as session:
        journal = (await session.scalars(select(TrashRestoreJournal))).one()
        assert journal.displaced_entry_id == displaced_id
        assert journal.state == state
        assert (await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None) == (
            state == "displaced"
        )
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id)) is not None
    assert displaced_media.read_bytes() == b"old-bytes"
    assert (original_media.parent / "metadata.json").is_file()
    assert await recover_trash_restores(sessions, trash) == (1, 0)
    assert destination.read_bytes() == b"payload"
    assert displaced_media.read_bytes() == b"old-bytes"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id)) is not None
        assert await session.scalar(select(TrashRestoreJournal.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sync", "claim"])
async def test_published_displacement_protection(env, monkeypatch, failure: str, caplog) -> None:
    from server.trash import _fsync

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    displaced_media = trash_path(trash, entry.library_id, displaced_id, entry.suffix)
    original_media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    claim = destination.with_name(f".{displaced_id}.claim")
    original_link = os.link
    original_unlink = os.unlink
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)

    def link(source, target, **kwargs):
        if (
            failure == "claim"
            and fd_path(source, kwargs.get("src_dir_fd")) == claim
            and fd_path(target, kwargs.get("dst_dir_fd")).parent.name.endswith(".partial")
        ):
            raise OSError(errno.EXDEV, "cross device")
        return original_link(source, target, **kwargs)

    def sync(path):
        if failure == "sync" and path == displaced_media.parent.parent:
            raise OSError("retention acknowledgement failed")
        return _fsync(path)

    def unlink(path, *args, **kwargs):
        if fd_path(path, kwargs.get("dir_fd")) == claim:
            raise OSError("retention acknowledgement failed")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("server.trash.os.link", link)
        patch.setattr("server.trash._fsync", sync)
        patch.setattr("server.trash.os.unlink", unlink)
        with pytest.raises(OSError):
            await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    assert displaced_media.read_bytes() == b"old-bytes"
    assert (displaced_media.parent / "metadata.json").is_file()
    assert claim.exists()
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert await refresh_trash_records(sessions, trash) == 0
    assert await purge_trash(sessions, trash, now=entry.purge_at + timedelta(days=1)) == 0
    async with sessions() as session:
        journal = (await session.scalars(select(TrashRestoreJournal))).one()
        assert journal.displaced_entry_id == displaced_id
        assert journal.state == "pending"
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id)) is None
    assert not destination.exists()
    assert original_media.read_bytes() == b"payload"
    assert displaced_media.read_bytes() == b"old-bytes"
    assert claim.read_bytes() == b"old-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "size", "metadata"])
async def test_recovery_displaced_verification(env, monkeypatch, damage: str) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    destination = library / "movie.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    displaced_media = trash_path(trash, entry.library_id, displaced_id, entry.suffix)
    original_media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)

    original_move = effects.publish

    def failed_move(source, target, **kwargs):
        if source == original_media:
            raise OSError("publication failed")
        original_move(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(effects, "publish", failed_move)
        with pytest.raises(OSError):
            await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    if damage == "missing":
        displaced_media.unlink()
    elif damage == "size":
        displaced_media.write_bytes(b"bad")
    else:
        (displaced_media.parent / "metadata.json").write_text("{", encoding="utf-8")
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert await refresh_trash_records(sessions, trash) == 0
    assert await purge_trash(sessions, trash, now=entry.purge_at + timedelta(days=1)) == 0
    async with sessions() as session:
        journal = (await session.scalars(select(TrashRestoreJournal))).one()
        assert journal.displaced_entry_id == displaced_id
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id)) is not None
    assert original_media.read_bytes() == b"payload"
    assert (original_media.parent / "metadata.json").is_file()
    assert (displaced_media.parent / "metadata.json").is_file()
    assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_state", ["media", "claim", "deleted"])
async def test_refresh_late_files(env, monkeypatch, file_state: str) -> None:
    from server.trash import _check_trash_records

    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    retained_copy = media.with_name("retained.retained_copy")
    media.rename(retained_copy)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_scan(root, rows, active):
        result = _check_trash_records(root, rows, active)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return result

    monkeypatch.setattr("server.trash._check_trash_records", blocked_scan)

    task = asyncio.create_task(refresh_trash_records(sessions, trash))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if file_state == "media":
            retained_copy.rename(media)
        elif file_state == "claim":
            retained_copy.rename(claim)
        else:
            app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.delete(f"/api/trash/{entry.id}")
            assert response.status_code == 204
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert outcomes == [0]
    async with sessions() as session:
        assert (await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None) == (
            file_state != "deleted"
        )
    if file_state == "deleted":
        assert not media.parent.exists()
    else:
        assert (media if file_state == "media" else claim).read_bytes() == b"payload"
        assert (media.parent / "metadata.json").is_file()


@pytest.mark.asyncio
async def test_missing_leaf_conflict(env, monkeypatch) -> None:
    trash, library, sessions = env
    _, entry = await put_file(env, monkeypatch)
    _purge_leaf(trash_path(trash, entry.library_id, entry.id, entry.suffix).parent, trash)
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/trash/{entry.id}")
    assert response.status_code == 409
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert await refresh_trash_records(sessions, trash) == 1
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
