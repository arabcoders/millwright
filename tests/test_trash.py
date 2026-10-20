from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("local_storage")
import pytest_asyncio
from common.fs import effects
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    TrashEntry,
    TrashRestoreJournal,
)
from server.trash import (
    _lock_leaf,
    _manifest,
    _purge_leaf,
    _unlock_leaf,
    cleanup_trash,
    delete_trash_entry,
    library_mutation_lock,
    purge_trash,
    refresh_trash_records,
    recover_trash_restore,
    recover_trash_restores,
    restore_trash,
    store_trash,
    trash_path,
)
from tests.helpers import database_context, fd_path, make_test_temp_dir

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[Path, Path, async_sessionmaker[AsyncSession]]]:
    root = make_test_temp_dir("trash")
    (root / "trash").mkdir()
    library = root / "library"
    library.mkdir()
    async with database_context("trash") as sessions:
        yield root, library, sessions


async def _store(env, monkeypatch, token="0123456789abcdef"):
    root, library, sessions = env
    source = library / "file.mkv"
    source.write_bytes(b"payload")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: token)
    async with sessions() as session:
        entry = await store_trash(session, root / "trash", 1, source, library, 7, clock=lambda: T0)
        await session.commit()
    return root, library, sessions, entry


async def _store_batch(env, count: int) -> list[TrashEntry]:
    root, library, sessions = env
    entries = []
    async with sessions() as session:
        for index in range(count):
            source = library / f"file-{index}.mkv"
            source.write_bytes(b"payload")
            entries.append(
                await store_trash(
                    session, root / "trash", 1, source, library, 7, identifier=f"{index:016x}", clock=lambda: T0
                )
            )
        await session.commit()
    return entries


async def _pending_finalization(sessions, trash: Path, library: Path, entry: TrashEntry) -> int:
    async with sessions() as session:
        session.add(Library(id=entry.library_id, name="movies", roots=[str(library)]))
        await session.flush()
        media = MediaFile(
            library_id=entry.library_id,
            path=entry.original_path,
            size=entry.size_bytes,
            mtime_ns=entry.mtime_ns,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(media_file_id=media.id, library_id=entry.library_id, status="finalizing", attempts=1)
        session.add(job)
        await session.flush()
        attempt = JobAttempt(
            job_id=job.id, attempt_number=1, status="running", source_size=entry.size_bytes, output_size=0
        )
        session.add(attempt)
        await session.flush()
        journal = FinalizationJournal(
            attempt_id=attempt.id,
            source_path=entry.original_path,
            destination_path=entry.original_path,
            partial_path=str(library / f".{attempt.id}.part"),
            trash_path=str(trash_path(trash, entry.library_id, entry.id, entry.suffix)),
            state="original_moved",
        )
        session.add(journal)
        session.add(
            JobEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                type="publication",
                payload={
                    "library_id": entry.library_id,
                    "roots": [str(library)],
                    "preserve_timestamps": False,
                },
            )
        )
        await session.commit()
        return journal.id


def test_rejects_invalid_path() -> None:
    with pytest.raises(ValueError):
        trash_path(Path("/trash"), 0, "0123456789abcdef", ".mkv")
    with pytest.raises(ValueError):
        trash_path(Path("/trash"), 1, "0123456789abcdeg", ".mkv")


@pytest.mark.asyncio
async def test_stores_sharded_entry(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    leaf = root / "trash/1/01/23/456789abcdef"
    assert not (library / "file.mkv").exists() and (leaf / "file.mkv").read_bytes() == b"payload"
    assert entry.size_bytes == 7 and entry.original_path == str(library / "file.mkv")
    async with sessions() as session:
        assert (await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id))).purge_at == (
            T0 + timedelta(days=7)
        )


@pytest.mark.asyncio
async def test_copies_across_devices(env, monkeypatch) -> None:
    real = os.link
    copied = False

    def link(src, dst, **kwargs):
        nonlocal copied
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        if src_path.name.endswith(".claim") and dst_path.parent.name.endswith(".partial"):
            copied = True
            raise OSError(errno.EXDEV, "cross device")
        return real(src, dst, **kwargs)

    monkeypatch.setattr("server.trash.os.link", link)
    root, library, _, _ = await _store(env, monkeypatch)
    assert copied and not (library / "file.mkv").exists()
    assert next((root / "trash").rglob("file.mkv")).read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_preserves_recreated_source(env, monkeypatch) -> None:
    real = os.link
    real_copy = effects.copy_file
    original = Path()

    def link(src, dst, **kwargs):
        nonlocal original
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        if src_path.name.endswith(".claim") and dst_path.parent.name.endswith(".partial"):
            original = src_path.parent / "file.mkv"
            raise OSError(errno.EXDEV, "cross device")
        return real(src, dst, **kwargs)

    def copy_claim(src, dst, **kwargs):
        result = real_copy(src, dst, **kwargs)
        original.write_bytes(b"replacement")
        return result

    monkeypatch.setattr("server.trash.os.link", link)
    monkeypatch.setattr(effects, "copy_file", copy_claim)
    root, library, _, _ = await _store(env, monkeypatch)
    assert (library / "file.mkv").read_bytes() == b"replacement"
    assert (root / "trash").rglob("file.mkv")


@pytest.mark.asyncio
async def test_rejects_existing_claim(env, monkeypatch) -> None:
    root, library, sessions = env
    source = library / "file.mkv"
    source.write_bytes(b"payload")
    claim = source.with_name(".0123456789abcdef.claim")
    claim.write_bytes(b"claim")
    with pytest.raises(ValueError):
        async with sessions() as session:
            await store_trash(session, root / "trash", 1, source, library, 7, identifier="0123456789abcdef")
    assert not (root / "trash" / "1").exists()
    assert claim.read_bytes() == b"claim" and source.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_preserves_copy_failure(env, monkeypatch) -> None:
    def replace(src, dst, **kwargs):
        if str(src).endswith("file.mkv") and ".partial" in str(src):
            raise OSError(errno.EXDEV, "cross device")
        raise OSError("publish")

    monkeypatch.setattr("server.trash.os.replace", replace)
    root, library, sessions = env
    source = library / "file.mkv"
    source.write_bytes(b"payload")
    identifier = "0123456789abcdef"
    media = trash_path(root / "trash", 1, identifier, ".mkv")
    staged = media.parent.with_name(media.parent.name + ".partial") / media.name
    claim = source.with_name(f".{identifier}.claim")
    with pytest.raises(OSError):
        async with sessions() as session:
            await store_trash(session, root / "trash", 1, source, library, 7, identifier=identifier, clock=lambda: T0)
    assert claim.read_bytes() == staged.read_bytes() == b"payload"
    assert (staged.parent / "metadata.json").is_file()
    assert not source.exists() and not media.exists()
    assert media.parent.is_dir() and not list(media.parent.iterdir())


@pytest.mark.asyncio
async def test_rolls_back_move(env, monkeypatch) -> None:
    monkeypatch.setattr("server.trash._fsync", lambda _: (_ for _ in ()).throw(OSError("prepublish")))
    root, library, sessions = env
    source = library / "file.mkv"
    source.write_bytes(b"payload")
    with pytest.raises(OSError):
        async with sessions() as session:
            await store_trash(session, root / "trash", 1, source, library, 7, clock=lambda: T0)
    assert source.read_bytes() == b"payload" and not list((root / "trash").rglob("file.mkv"))


@pytest.mark.asyncio
async def test_retries_collision(env, monkeypatch) -> None:
    await _store(env, monkeypatch, "0123456789abcdef")
    tokens = iter(("0123456789abcdef", "fedcba9876543210"))
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: next(tokens))
    root, library, sessions = env
    source = library / "second.mkv"
    source.write_bytes(b"two")
    async with sessions() as session:
        await store_trash(session, root / "trash", 1, source, library, 7, clock=lambda: T0)
    assert (root / "trash/1/fe/dc/ba9876543210/file.mkv").exists()


@pytest.mark.asyncio
async def test_rejects_outside_root(env) -> None:
    root, library, sessions = env
    outside = root / "outside.mkv"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        async with sessions() as session:
            await store_trash(session, root / "trash", 1, outside, library, 7)


@pytest.mark.asyncio
async def test_rejects_symlink_source(env) -> None:
    root, library, sessions = env
    target = root / "target"
    target.write_bytes(b"x")
    link = library / "file.mkv"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        async with sessions() as session:
            await store_trash(session, root / "trash", 1, link, library, 7)
    assert target.read_bytes() == b"x"


@pytest.mark.asyncio
async def test_preserves_unavailable_root(env) -> None:
    root, _, sessions = env
    async with sessions() as session:
        session.add(
            TrashEntry(
                id="0123456789abcdef",
                library_id=1,
                original_path="x",
                library_root="y",
                suffix=".mkv",
                size_bytes=1,
                mtime_ns=1,
                trashed_at=T0,
                purge_at=T0,
            )
        )
        await session.commit()
    assert await refresh_trash_records(sessions, root / "missing") == 0


@pytest.mark.asyncio
async def test_removes_missing_leaf(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    import shutil

    shutil.rmtree(root / "trash/1/01/23/456789abcdef")
    await refresh_trash_records(sessions, root / "trash")
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None


@pytest.mark.asyncio
async def test_removes_missing_media(env, monkeypatch) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    leaf = root / "trash/1/01/23/456789abcdef"
    (leaf / "file.mkv").unlink()
    await refresh_trash_records(sessions, root / "trash")
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["media", "claim"])
@pytest.mark.parametrize("stat_error", [errno.EACCES, errno.EIO])
async def test_refresh_keeps_unavailable(env, monkeypatch: pytest.MonkeyPatch, boundary: str, stat_error: int) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    media = trash_path(root / "trash", entry.library_id, entry.id, entry.suffix)
    claim = media.parent / f".{entry.id}.restore-claim"
    if boundary == "claim":
        media.unlink()
        claim.write_bytes(b"restore claim")
    target = media if boundary == "media" else claim
    stat = os.stat

    def unavailable(path, *args, **kwargs):
        if path in (target, str(target)):
            raise OSError(stat_error, "trash storage unavailable")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", unavailable)
        with pytest.raises(OSError) as failure:
            await refresh_trash_records(sessions, root / "trash")
        assert failure.value.errno == stat_error
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert target.read_bytes() == (b"payload" if boundary == "media" else b"restore claim")


@pytest.mark.asyncio
@pytest.mark.parametrize("stat_error", [errno.EACCES, errno.EIO])
async def test_purge_keeps_unavailable(env, monkeypatch: pytest.MonkeyPatch, stat_error: int) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    media = trash_path(root / "trash", entry.library_id, entry.id, entry.suffix)
    stat = os.stat

    def unavailable(path, *args, **kwargs):
        if path in (media.parent, str(media.parent)):
            raise OSError(stat_error, "trash leaf unavailable")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", unavailable)
        assert await purge_trash(sessions, root / "trash", now=T0 + timedelta(days=8)) == 0
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert media.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_retains_bad_metadata(env, monkeypatch) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    (root / "trash/1/01/23/456789abcdef/metadata.json").write_text("bad")
    await refresh_trash_records(sessions, root / "trash")
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
async def test_recovers_orphan_entry(env, monkeypatch) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    async with sessions() as session:
        await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
        await session.commit()
    await refresh_trash_records(sessions, root / "trash")
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
async def test_refresh_batches_records(env, monkeypatch, caplog) -> None:
    monkeypatch.setattr("server.trash._TRASH_BATCH_SIZE", 2)
    root, library, sessions = env
    trash = root / "trash"
    entries = await _store_batch(env, 10)
    media = [trash_path(trash, 1, entry.id, entry.suffix) for entry in entries]
    media[1].unlink()
    _purge_leaf(media[2].parent, trash)
    (media[3].parent / "metadata.json").write_text("bad")
    media[4].rename(media[4].parent / f".{entries[4].id}.restore-claim")
    media[5].unlink()
    media[6].unlink()
    await _pending_finalization(sessions, trash, library, entries[6])
    async with sessions() as session:
        session.add(
            TrashRestoreJournal(
                id="a" * 32,
                entry_id=entries[4].id,
                displaced_entry_id=entries[5].id,
                target_path=entries[4].original_path,
                state="pending",
            )
        )
        session.add(
            TrashRestoreJournal(
                id="b" * 32, entry_id=entries[9].id, target_path=entries[9].original_path, state="pending"
            )
        )
        for entry in entries[7:]:
            await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
        await session.commit()

    assert await refresh_trash_records(sessions, trash) == 4
    async with sessions() as session:
        retained = set(await session.scalars(select(TrashEntry.id)))
        assert retained == {entries[index].id for index in (0, 3, 4, 5, 6, 7, 8)}
        assert len(list(await session.scalars(select(TrashRestoreJournal.id)))) == 2
        assert await session.scalar(select(FinalizationJournal.id)) is not None
    assert (
        media[0].read_bytes() == media[7].read_bytes() == media[8].read_bytes() == media[9].read_bytes() == b"payload"
    )
    assert (media[3].parent / "metadata.json").read_text() == "bad"
    assert (media[4].parent / f".{entries[4].id}.restore-claim").read_bytes() == b"payload"
    assert all((media[index].parent / "metadata.json").is_file() for index in (4, 5, 6, 9))
    assert any(
        record.name == "server.trash" and record.levelno == logging.ERROR and getattr(record, "error", None)
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_refresh_protected_orphan(env, monkeypatch) -> None:
    monkeypatch.setattr("server.trash._TRASH_BATCH_SIZE", 1)
    root, library, sessions = env
    trash = root / "trash"
    entries = await _store_batch(env, 3)
    await _pending_finalization(sessions, trash, library, entries[1])
    async with sessions() as session:
        for entry in entries:
            await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
        await session.commit()
    assert await refresh_trash_records(sessions, trash) == 2
    async with sessions() as session:
        assert set(await session.scalars(select(TrashEntry.id))) == {entries[0].id, entries[2].id}
    assert all(trash_path(trash, 1, entry.id, entry.suffix).read_bytes() == b"payload" for entry in entries)


@pytest.mark.asyncio
async def test_rejects_path_mismatch(env, monkeypatch) -> None:
    root, _, sessions, _ = await _store(env, monkeypatch)
    meta = root / "trash/1/01/23/456789abcdef/metadata.json"
    data = json.loads(meta.read_text())
    data["id"] = "fedcba9876543210"
    meta.write_text(json.dumps(data))
    await refresh_trash_records(sessions, root / "trash")
    async with sessions() as session:
        assert not (await session.execute(select(TrashEntry).where(TrashEntry.id == data["id"]))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_purges_expired_entry(env, monkeypatch) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    assert await purge_trash(sessions, root / "trash", now=T0 + timedelta(days=8)) == 1
    assert not (root / "trash/1").exists() and (root / "trash").exists()
    assert await awaitable_false(sessions, entry.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["purge", "delete"])
async def test_delete_library_locks(env, monkeypatch, operation: str) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    media = trash_path(root / "trash", 1, entry.id, entry.suffix)
    async with library_mutation_lock(library, wait=False), sessions() as session:
        if operation == "purge":
            assert await purge_trash(sessions, root / "trash", now=T0 + timedelta(days=8)) == 0
        else:
            with pytest.raises(ValueError):
                await delete_trash_entry(sessions, root / "trash", entry.id)
        assert media.read_bytes() == b"payload"
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["purge", "delete"])
async def test_deletes_retired_library(env, monkeypatch, operation: str) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    library.rmdir()
    media = trash_path(root / "trash", 1, entry.id, entry.suffix)
    if operation == "purge":
        assert await purge_trash(sessions, root / "trash", now=T0 + timedelta(days=8)) == 1
    else:
        await delete_trash_entry(sessions, root / "trash", entry.id)
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
    assert not media.exists()


async def awaitable_false(sessions, ident):
    async with sessions() as session:
        return await session.scalar(select(TrashEntry).where(TrashEntry.id == ident)) is None


@pytest.mark.asyncio
async def test_preserves_purge_failure(env, monkeypatch) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    monkeypatch.setattr("server.trash.shutil.rmtree", lambda _: (_ for _ in ()).throw(OSError("no")))
    await purge_trash(sessions, root / "trash", now=T0 + timedelta(days=8))
    assert await awaitable_false(sessions, entry.id) is False


@pytest.mark.asyncio
async def test_purge_batches_progress(env, monkeypatch, caplog) -> None:
    monkeypatch.setattr("server.trash._TRASH_BATCH_SIZE", 2)
    root, _, sessions = env
    trash = root / "trash"
    entries = await _store_batch(env, 7)
    async with sessions() as session:
        current = await session.scalar(select(TrashEntry).where(TrashEntry.id == entries[2].id))
        current.purge_at = T0 + timedelta(days=9)
        session.add(
            TrashRestoreJournal(
                id="j" * 32, entry_id=entries[1].id, target_path=entries[1].original_path, state="pending"
            )
        )
        await session.commit()
    failed_leaf = trash_path(trash, 1, entries[0].id, entries[0].suffix).parent
    failure = OSError("purge failed")

    def purge(leaf, root):
        if leaf == failed_leaf:
            raise failure
        _purge_leaf(leaf, root)

    monkeypatch.setattr("server.trash._purge_leaf", purge)
    assert await purge_trash(sessions, trash, now=T0 + timedelta(days=8)) == 4
    async with sessions() as session:
        assert set(await session.scalars(select(TrashEntry.id))) == {entry.id for entry in entries[:3]}
        assert await session.scalar(select(TrashRestoreJournal.id)) == "j" * 32
    assert all(trash_path(trash, 1, entry.id, entry.suffix).read_bytes() == b"payload" for entry in entries[:3])
    assert any(
        record.name == "server.trash"
        and getattr(record, "operation", None) == "purge"
        and getattr(record, "entry_id", None) == entries[0].id
        and record.exc_info
        and record.exc_info[1] is failure
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cleanup_recovers_purges(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    source = library / "second.mkv"
    source.write_bytes(b"expired")
    async with sessions() as session:
        orphan = await store_trash(
            session, trash, 1, source, library, 7, identifier="fedcba9876543210", clock=lambda: T0
        )
        session.add(
            TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=entry.original_path, state="pending")
        )
        await session.commit()
        await session.delete(orphan)
        await session.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return T0 + timedelta(days=8)

    monkeypatch.setattr("server.trash.datetime", Clock)
    assert await cleanup_trash(sessions, trash) is None
    assert (library / "file.mkv").read_bytes() == b"payload"
    assert not source.exists()
    assert not (trash / "1").exists()
    async with sessions() as session:
        assert not (await session.scalars(select(TrashEntry))).all()
        assert not (await session.scalars(select(TrashRestoreJournal))).all()


@pytest.mark.asyncio
async def test_cleanup_failure_rolls_back(env, monkeypatch) -> None:
    root, _, sessions, entry = await _store(env, monkeypatch)
    calls = 0

    async def fail_refresh(sessionmaker, trash):
        nonlocal calls
        calls += 1
        async with sessionmaker() as session:
            await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
            raise RuntimeError("refresh failed")

    monkeypatch.setattr("server.trash.refresh_trash_records", fail_refresh)
    with pytest.raises(RuntimeError):
        await cleanup_trash(sessions, root / "trash")
    assert calls == 1
    assert trash_path(root / "trash", 1, entry.id, entry.suffix).read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [1, 2, 3])
async def test_rejects_symlink_shards(env, monkeypatch, level: int) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    media = trash_path(trash, 1, entry.id, entry.suffix)
    shard = media.parent.parents[level - 1]
    outside = root / "outside"
    shard.rename(outside)
    shard.symlink_to(outside, target_is_directory=True)
    marker = outside / "marker"
    marker.write_bytes(b"untouched")
    assert _lock_leaf(media.parent) is None
    with pytest.raises(ValueError):
        _manifest(media.parent / "metadata.json", trash)
    with pytest.raises(ValueError):
        _purge_leaf(media.parent, trash)
    async with sessions() as session:
        assert await purge_trash(sessions, trash, now=T0 + timedelta(days=8)) == 0
        await refresh_trash_records(sessions, trash)
        with pytest.raises(ValueError):
            await delete_trash_entry(sessions, trash, entry.id)
        with pytest.raises(ValueError):
            await restore_trash(sessions, trash, entry.id)
        await session.commit()
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert marker.read_bytes() == b"untouched" and media.read_bytes() == b"payload"
    assert not list(outside.rglob(".restore.lock"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "before", "after"])
async def test_cancelled_restore_locks(env, monkeypatch, failure: str | None) -> None:
    root, library, sessions, first = await _store(env, monkeypatch)
    trash = root / "trash"
    destination = library / "file.mkv"
    destination.write_bytes(b"second")
    async with sessions() as session:
        second = await store_trash(session, trash, 1, destination, library, 7, identifier="fedcba9876543210")
        await session.commit()
    destination.write_bytes(b"old-bytes")
    media = trash_path(trash, 1, first.id, first.suffix)
    second_media = trash_path(trash, 1, second.id, second.suffix)
    started = asyncio.Event()
    completed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_replace = os.replace

    def blocked_replace(source, target, **kwargs):
        source_path = fd_path(source, kwargs.get("src_dir_fd"))
        target_path = fd_path(target, kwargs.get("dst_dir_fd"))
        if target_path != destination or not media.exists() or not source_path.samefile(media):
            return original_replace(source, target, **kwargs)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            if failure == "before":
                raise OSError("publication failed")
            original_replace(source, target, **kwargs)
            if failure == "after":
                raise OSError("publication failed")
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr("common.fs.effects.os.replace", blocked_replace)

    async def restoring():
        return await restore_trash(sessions, trash, first.id, replace_existing=True)

    task = asyncio.create_task(restoring())
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done()
            assert not completed.is_set()
            fd = _lock_leaf(media.parent)
            if fd is not None:
                _unlock_leaf(fd)
            assert fd is None
            with pytest.raises(ValueError):
                await restore_trash(sessions, trash, second.id, replace_existing=True)
            assert destination.read_bytes() == b"old-bytes"
            assert second_media.read_bytes() == b"second"
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)

    if failure is None:
        assert isinstance(outcomes[0], asyncio.CancelledError)
    else:
        assert isinstance(outcomes[0], OSError)
    async with sessions() as session:
        journal = await session.scalar(select(TrashRestoreJournal))
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == first.id)) is not None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == second.id)) is not None
        assert journal is not None and journal.state == "overwrite_pending"
        if failure == "before":
            assert media.read_bytes() == b"payload"
        else:
            assert (media.parent / "metadata.json").is_file()
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    assert media.read_bytes() == b"payload"
    existing_content = b"old-bytes" if failure == "before" else b"payload"
    assert destination.read_bytes() == existing_content
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: "0000000000000003")
    with pytest.raises(FileExistsError):
        await restore_trash(sessions, trash, second.id)
    await restore_trash(sessions, trash, second.id, replace_existing=True, move_to_trash=True)
    async with sessions() as session:
        retained = (await session.scalars(select(TrashEntry).where(TrashEntry.id == "0000000000000003"))).one()
    assert destination.read_bytes() == b"second"
    assert trash_path(trash, 1, retained.id, retained.suffix).read_bytes() == existing_content


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["retaining", "rollback", "recover", "recover_claim"])
async def test_restore_worker_cancellation(env, monkeypatch, phase: str) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    destination = library / "file.mkv"
    media = trash_path(trash, 1, entry.id, entry.suffix)
    started = asyncio.Event()
    completed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    recovering = phase.startswith("recover")
    displaced_id = "0000000000000003"
    existing_claim = destination.with_name(f".{displaced_id}.claim")
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)
    if recovering:
        async with sessions() as session:
            session.add(
                TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=str(destination), state="pending")
            )
            await session.commit()
        if phase == "recover_claim":
            media.rename(media.parent / f".{entry.id}.restore-claim")
        method = "publish"
    else:
        destination.write_bytes(b"old-bytes")
        original_link = os.link

        def cross_device(source, target, **kwargs):
            source_path = fd_path(source, kwargs.get("src_dir_fd"))
            target_path = fd_path(target, kwargs.get("dst_dir_fd"))
            if source_path == existing_claim and target_path.parent.name.endswith(".partial"):
                raise OSError(errno.EXDEV, "cross device")
            return original_link(source, target, **kwargs)

        monkeypatch.setattr("server.trash.os.link", cross_device)
        method = "copy_file" if phase == "retaining" else "move_file"
        if phase == "rollback":

            def failed_copy(*_args, **_kwargs):
                raise OSError("copy failed")

            monkeypatch.setattr(effects, "copy_file", failed_copy)
    original_operation = getattr(effects, method)

    def blocked_operation(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            return original_operation(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr(effects, method, blocked_operation)

    async def restoring():
        if recovering:
            return await cleanup_trash(sessions, trash)
        return await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)

    task = asyncio.create_task(restoring())
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done()
            assert not completed.is_set()
            fd = _lock_leaf(media.parent)
            if fd is not None:
                _unlock_leaf(fd)
            assert fd is None
            with pytest.raises(ValueError):
                async with library_mutation_lock(library, wait=False):
                    pytest.fail("restore worker released library locks")
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)

    assert isinstance(outcomes[0], asyncio.CancelledError)
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        journal = (await session.scalars(select(TrashRestoreJournal))).one()
        assert journal.state == "pending"
    assert (media.parent / "metadata.json").is_file()
    if phase == "retaining":
        assert media.read_bytes() == b"payload"
        displaced = trash_path(trash, 1, displaced_id, ".mkv")
        assert displaced.read_bytes() == b"old-bytes"
        assert not existing_claim.exists()
        assert not destination.exists()
        assert await recover_trash_restores(sessions, trash) == (1, 0)
        assert destination.read_bytes() == b"payload" and displaced.read_bytes() == b"old-bytes"
    elif phase == "rollback":
        assert media.read_bytes() == b"payload"
        assert destination.read_bytes() == b"old-bytes"
        assert not existing_claim.exists()
    elif phase == "recover_claim":
        assert destination.read_bytes() == b"payload"
        assert not media.exists()
        assert (media.parent / f".{entry.id}.restore-claim").read_bytes() == b"payload"
    else:
        assert destination.read_bytes() == b"payload"
        assert media.read_bytes() == b"payload"
    if recovering:
        assert await recover_trash_restores(sessions, trash) == (1, 0)
        assert not media.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["purge", "delete"])
async def test_cancelled_delete_locks(env, monkeypatch, operation: str) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    leaf = trash_path(trash, 1, entry.id, entry.suffix).parent
    started = asyncio.Event()
    completed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_purge = _purge_leaf

    def blocked_purge(path, trash_root):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            original_purge(path, trash_root)
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr("server.trash._purge_leaf", blocked_purge)

    async def deleting():
        if operation == "purge":
            await purge_trash(sessions, trash, now=T0 + timedelta(days=8))
        else:
            await delete_trash_entry(sessions, trash, entry.id)

    task = asyncio.create_task(deleting())
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done() and not completed.is_set()
            fd = _lock_leaf(leaf)
            if fd is not None:
                _unlock_leaf(fd)
            assert fd is None
            with pytest.raises(ValueError):
                async with library_mutation_lock(library, wait=False):
                    pytest.fail("delete worker released library locks")
            assert (leaf / "file.mkv").read_bytes() == b"payload"
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert not leaf.exists()
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    await refresh_trash_records(sessions, trash)
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None


@pytest.mark.asyncio
async def test_refresh_finalization_lock(env, monkeypatch) -> None:
    from server.finalization import recover

    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    identifier = await _pending_finalization(sessions, trash, library, entry)
    leaf = trash_path(trash, entry.library_id, entry.id, entry.suffix).parent
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_restore = effects.move_file

    def blocked_restore(claim, destination, **kwargs):
        original_restore(claim, destination, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)

    monkeypatch.setattr(effects, "move_file", blocked_restore)

    async def recovering():
        async with sessions() as session:
            journal = await session.get(FinalizationJournal, identifier)
            assert journal is not None
            await recover(session, journal, library_root=library)

    task = asyncio.create_task(recovering())
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with sessions() as session:
            changed = await refresh_trash_records(sessions, trash)
            assert (changed, (leaf / "metadata.json").is_file()) == (0, True)
            await session.rollback()
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert outcomes == [None]
    assert (library / "file.mkv").read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_restore_preserves_finalization(env, monkeypatch) -> None:
    import httpx

    from tests.helpers import api_client
    from tests.helpers import create_test_app as create_app
    from server.core.config import ServerConfig
    from server.finalization import recover_incomplete

    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    await _pending_finalization(sessions, trash, library, entry)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    app = create_app(sessions, config=ServerConfig(trash_dir=str(trash), workspace_dir=str(library)))
    async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/trash/{entry.id}/restore", json={})
    preserved = media.is_file()
    recovered = await recover_incomplete(sessions)
    assert (response.status_code, preserved, recovered) == (409, True, (1, 0))
    assert (library / "file.mkv").read_bytes() == b"payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "expunge", "expiry"])
async def test_relative_root_protection(env, monkeypatch, operation: str) -> None:
    import httpx

    from tests.helpers import api_client
    from tests.helpers import create_test_app as create_app
    from server.core.config import ServerConfig

    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    await _pending_finalization(sessions, trash, library, entry)
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    monkeypatch.chdir(root)
    if operation == "expiry":
        deleted = await purge_trash(sessions, Path("trash"), now=T0 + timedelta(days=8))
        assert (deleted, media.is_file()) == (0, True)
    else:
        app = create_app(sessions, config=ServerConfig(trash_dir="trash", workspace_dir=str(library)))
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/trash/{entry.id}" if operation == "delete" else "/api/trash")
        if operation == "delete":
            assert (response.status_code, media.is_file()) == (409, True)
        else:
            assert (response.json()["deleted"], media.is_file()) == (0, True)
            assert response.json()["failed"] == [entry.id]
    assert media.read_bytes() == b"payload"
    assert not (library / "file.mkv").exists()
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [False, True])
async def test_recovery_finalization_protection(env, monkeypatch, relative: bool) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    await _pending_finalization(sessions, trash, library, entry)
    async with sessions() as session:
        session.add(
            TrashRestoreJournal(id="j" * 32, entry_id=entry.id, target_path=entry.original_path, state="pending")
        )
        await session.commit()
    if relative:
        monkeypatch.chdir(root)
    configured = Path("trash") if relative else trash
    assert await recover_trash_restores(sessions, configured) == (0, 1)
    assert await refresh_trash_records(sessions, configured) == 0
    assert await purge_trash(sessions, configured, now=T0 + timedelta(days=8)) == 0
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
        assert await session.scalar(select(TrashRestoreJournal.id)) is not None
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    assert media.read_bytes() == b"payload"
    assert (media.parent / "metadata.json").is_file()
    assert not (library / "file.mkv").exists()


@pytest.mark.asyncio
async def test_refresh_worker_cancellation(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    media = trash_path(trash, entry.library_id, entry.id, entry.suffix)
    media.unlink()
    started = asyncio.Event()
    completed = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_purge = _purge_leaf

    def blocked_purge(leaf, trash_root):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            original_purge(leaf, trash_root)
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr("server.trash._purge_leaf", blocked_purge)

    async def refreshing():
        return await refresh_trash_records(sessions, trash)

    task = asyncio.create_task(refreshing())
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
                    pytest.fail("trash refresh released library locks")
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    async with library_mutation_lock(library, wait=False):
        assert not media.parent.exists()
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None
    assert await refresh_trash_records(sessions, trash) == 1
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None


@pytest.mark.asyncio
async def test_restore_owns_completion(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    destination = await restore_trash(sessions, root / "trash", entry.id)
    assert destination == library / "file.mkv"
    assert destination.read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
        assert await session.scalar(select(TrashRestoreJournal.id)) is None
    assert not trash_path(root / "trash", 1, entry.id, entry.suffix).parent.exists()


@pytest.mark.asyncio
async def test_recovery_owns_completion(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    journal_id = "j" * 32
    async with sessions() as session:
        session.add(
            TrashRestoreJournal(id=journal_id, entry_id=entry.id, target_path=entry.original_path, state="pending")
        )
        await session.commit()
    assert await recover_trash_restore(sessions, root / "trash", journal_id)
    assert (library / "file.mkv").read_bytes() == b"payload"
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is None
        assert await session.scalar(select(TrashRestoreJournal).where(TrashRestoreJournal.id == journal_id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "committed"])
async def test_recovery_batches_progress(env, monkeypatch, caplog, state: str) -> None:
    monkeypatch.setattr("server.trash._TRASH_BATCH_SIZE", 2)
    root, _, sessions = env
    trash = root / "trash"
    entries = await _store_batch(env, 6)
    journal_ids = [f"{index:032x}" for index in range(len(entries))]
    async with sessions() as session:
        for index, entry in enumerate(entries):
            session.add(
                TrashRestoreJournal(
                    id=journal_ids[index],
                    entry_id=entry.id,
                    target_path=entry.original_path + (".wrong" if index == 0 else ""),
                    state=state,
                )
            )
            if state == "committed" and index >= 2:
                Path(entry.original_path).write_bytes(b"payload")
                await session.delete(await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)))
        await session.commit()
    locked_leaf = trash_path(trash, 1, entries[1].id, entries[1].suffix).parent
    fd = _lock_leaf(locked_leaf, required=True)
    assert fd is not None
    try:
        assert await recover_trash_restores(sessions, trash) == (4, 1)
    finally:
        _unlock_leaf(fd)
    async with sessions() as session:
        assert set(await session.scalars(select(TrashRestoreJournal.id))) == set(journal_ids[:2])
        assert set(await session.scalars(select(TrashEntry.id))) == {entry.id for entry in entries[:2]}
    assert all(Path(entry.original_path).read_bytes() == b"payload" for entry in entries[2:])
    assert all(trash_path(trash, 1, entry.id, entry.suffix).read_bytes() == b"payload" for entry in entries[:2])
    assert any(
        record.name == "server.trash"
        and record.levelno == logging.ERROR
        and record.exc_info
        and isinstance(record.exc_info[1], ValueError)
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cleaned_displacement_protection(env, monkeypatch) -> None:
    root, library, sessions, entry = await _store(env, monkeypatch)
    trash = root / "trash"
    destination = library / "file.mkv"
    destination.write_bytes(b"old-bytes")
    displaced_id = "0000000000000003"
    monkeypatch.setattr("server.trash.secrets.token_hex", lambda _: displaced_id)

    def failed_cleanup(*_args):
        raise OSError("cleanup failed")

    with monkeypatch.context() as patch:
        patch.setattr("server.trash._purge_leaf", failed_cleanup)
        await restore_trash(sessions, trash, entry.id, replace_existing=True, move_to_trash=True)
    _purge_leaf(trash_path(trash, 1, entry.id, entry.suffix).parent, trash)
    displaced = trash_path(trash, 1, displaced_id, entry.suffix)
    displaced.write_bytes(b"bad")
    assert await recover_trash_restores(sessions, trash) == (0, 1)
    async with sessions() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == displaced_id)) is not None
        assert (await session.scalars(select(TrashRestoreJournal))).one().state == "committed"
    assert displaced.read_bytes() == b"bad"
    assert destination.read_bytes() == b"payload"
