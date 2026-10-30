from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import math
import os
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import TYPE_CHECKING

from common.fs import effects
from common.fs.acquire import directory_fd, entry_stat
from common.process import drain
from common.workspace import open_workspace_lock
from sqlalchemy import or_, select

from server.models import FinalizationJournal, JobAttempt, TrashEntry, TrashRestoreJournal
from server.storage import storage_io, storage_scope
from server.workspace import FINISHED_FINALIZATION_STATES, recovery_jobs, wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql import Select

LOG = logging.getLogger(__name__)
_TRASH_BATCH_SIZE = 256


def _try_library_lock(root: Path, *, shared: bool = False, workspace: bool = False) -> int | None:
    if workspace:
        try:
            return open_workspace_lock(root, exclusive=not shared)
        except BlockingIOError:
            return None
    with directory_fd(root) as directory:
        fd = os.dup(directory)
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@asynccontextmanager
async def library_mutation_lock(root: Path, *, wait: bool, shared: bool = False, workspace: bool = False):
    async with storage_scope():
        deadline = asyncio.get_running_loop().time() + (30 if wait else 0)
        while True:
            acquiring = asyncio.create_task(storage_io(_try_library_lock, root, shared=shared, workspace=workspace))
            try:
                await asyncio.wait((acquiring,))
                fd = acquiring.result()
            except asyncio.CancelledError:
                fd = await drain(acquiring, propagate_cancel=False)
                if fd is not None:
                    await storage_io(_unlock_leaf, fd)
                raise
            if fd is not None:
                try:
                    # This descriptor lives in the same storage worker that runs
                    # the writes. API death cannot unlock an active writer.
                    yield
                finally:
                    await storage_io(_unlock_leaf, fd)
                return
            if not wait or asyncio.get_running_loop().time() >= deadline:
                raise ValueError("library finalization is active")
            await asyncio.sleep(0.05)


def _lock_leaf(leaf: Path, *, required: bool = False) -> int | None:
    try:
        _confined(leaf, leaf.parents[3])
    except ValueError:
        if required:
            raise ValueError("trash entry is unavailable") from None
        return None
    if not leaf.parent.is_dir() or leaf.is_symlink() or (leaf.exists() and not leaf.is_dir()):
        if required:
            raise ValueError("trash entry is unavailable")
        return None
    lock = leaf / ".restore.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError as exc:
        if required:
            raise ValueError("trash entry is unavailable") from exc
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _unlock_leaf(fd: int) -> None:
    os.close(fd)


def _valid_id(value: str) -> bool:
    return len(value) == 16 and all(char in "0123456789abcdef" for char in value)


def _suffix(value: str) -> bool:
    return len(value) >= 2 and len(value) <= 16 and value[0] == "." and value[1:].isalnum()


def trash_path(root: Path, library_id: int, identifier: str, suffix: str) -> Path:
    if library_id < 1 or not _valid_id(identifier) or not _suffix(suffix):
        msg = (
            "Trash path requires a positive library ID, a 16-character lowercase hexadecimal entry ID, "
            "and a 2-to-16-character suffix with alphanumeric characters after its leading period."
        )
        raise ValueError(msg)
    return root / str(library_id) / identifier[:2] / identifier[2:4] / identifier[4:] / f"file{suffix}"


def _regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir()


def _confined(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    if ".." in relative.parts or root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError("unsafe trash path")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("unsafe trash path")


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _restore_claim(claim: Path, source: Path, size: int) -> None:
    if claim.stat().st_size != size:
        raise ValueError("Original size changed before rollback.")
    effects.move_file(claim, source, overwrite=False)


def _store_original(claim: Path, staged: Path, media: Path, manifest: dict) -> None:
    effects.publish(claim, staged)
    partial = staged.parent
    with (partial / "metadata.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync(partial)
    # Reserve the leaf so an existing empty directory is not overwritten.
    media.parent.mkdir(mode=0o700)
    os.replace(partial, media.parent)
    _fsync(media.parent)
    _fsync(media.parent.parent)
    claim.unlink()


async def store_trash(
    session: AsyncSession,
    root: Path,
    library_id: int,
    source: Path,
    library_root: Path,
    retention_days: int,
    *,
    identifier: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TrashEntry:
    if library_id < 1 or retention_days < 1:
        raise ValueError("Library ID and trash retention days must be positive.")

    def inspect_source() -> tuple[Path, Path, Path, os.stat_result]:
        if not _regular(root) or not os.access(root, os.R_OK | os.W_OK):
            msg = f"Trash root is not an accessible directory: {root}"
            raise ValueError(msg)
        if source.is_symlink() or library_root.is_symlink():
            msg = f"Source and library root must not be symlinks: {source}, {library_root}"
            raise ValueError(msg)
        resolved_source = source.resolve(strict=True)
        resolved_root = library_root.resolve(strict=True)
        if not resolved_source.is_file():
            msg = f"Trash source is not a regular file: {resolved_source}"
            raise ValueError(msg)
        try:
            relative = resolved_source.relative_to(resolved_root)
        except ValueError as exc:
            msg = f"Trash source is outside library root: {resolved_source} (library root: {resolved_root})"
            raise ValueError(msg) from exc
        return resolved_source, resolved_root, relative, resolved_source.stat()

    source, library_root, relative, stat = await wait_workspace_io(inspect_source)
    suffix = source.suffix.lower()
    if not _suffix(suffix):
        msg = f"Trash source suffix must be 2 to 16 characters with alphanumeric characters after its leading period: {suffix!r}"
        raise ValueError(msg)
    now = clock()
    requested_identifier = identifier
    while True:
        identifier = identifier or secrets.token_hex(8)
        if not _valid_id(identifier):
            msg = f"Trash entry ID must be 16 lowercase hexadecimal characters: {identifier!r}"
            raise ValueError(msg)
        if await session.scalar(select(TrashEntry).where(TrashEntry.id == identifier)):
            if requested_identifier is not None:
                msg = f"Trash entry ID already exists: {identifier}"
                raise ValueError(msg)
            identifier = None
            continue
        media = trash_path(root, library_id, identifier, suffix)
        if await wait_workspace_io(lambda media=media: not media.parent.exists() or not media.exists()):
            break
        if requested_identifier is not None:
            msg = f"Trash destination already exists for entry ID {identifier}: {media}"
            raise ValueError(msg)
        identifier = None
    library = root / str(library_id)
    claim = source.with_name(f".{identifier}.claim")
    first = library / identifier[:2]
    second = first / identifier[2:4]
    partial = media.parent.with_name(media.parent.name + ".partial")
    staged = partial / media.name
    metadata = partial / "metadata.json"
    claimed = False
    cancelled = False

    def prepare_entry() -> None:
        if claim.exists() or claim.is_symlink():
            msg = f"Trash source claim already exists: {claim}"
            raise ValueError(msg)
        for directory in (library, first, second):
            if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
                msg = f"Trash path component is not a real directory: {directory}"
                raise ValueError(msg)
            directory.mkdir(exist_ok=True)
        partial.mkdir(mode=0o700)

    def claim_original() -> None:
        nonlocal claimed
        effects.claim(source, claim)
        claimed = True

    await wait_workspace_io(prepare_entry)
    try:
        await wait_workspace_io(claim_original)
        manifest = {
            "version": 1,
            "id": identifier,
            "library_id": library_id,
            "original_path": str(source),
            "library_root": str(library_root),
            "relative_path": str(relative),
            "stored_file": staged.name,
            "trashed_at": now.isoformat(),
            "purge_at": (now + timedelta(days=retention_days)).isoformat(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

        storing = asyncio.create_task(storage_io(_store_original, claim, staged, media, manifest))
        try:
            await asyncio.wait((storing,))
            storing.result()
        except asyncio.CancelledError:
            # Caller cancellation must obtain the actual operation result before
            # recording retention. A worker cancellation is not a successful copy.
            await drain(storing, propagate_cancel=False)
            cancelled = True
    except Exception:
        if await wait_workspace_io(media.parent.exists):
            # A failed rename or sync acknowledgement doesn't prove the
            # private leaf stayed put. Preserve both copies and the error.
            raise
        if claimed:
            try:
                await storage_io(_restore_claim, claim, source, stat.st_size)
            except Exception as rollback:
                raise RuntimeError("Trash rollback failed. Staged copy preserved.") from rollback

        def remove_partial() -> None:
            staged.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            partial.rmdir()

        await wait_workspace_io(remove_partial)
        raise
    entry = TrashEntry(
        id=identifier,
        library_id=library_id,
        original_path=str(source),
        library_root=str(library_root),
        suffix=suffix,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        trashed_at=now,
        purge_at=now + timedelta(days=retention_days),
    )
    session.add(entry)
    if cancelled:
        await drain(asyncio.create_task(session.commit()))
        raise asyncio.CancelledError
    return entry


def _manifest(meta: Path, root: Path, *, allow_missing_media: bool = False) -> tuple[dict, Path]:
    _confined(meta, root)
    if meta.is_symlink():
        msg = f"Trash metadata must not be a symlink: {meta}"
        raise ValueError(msg)
    data = json.loads(meta.read_text(encoding="utf-8"))
    identifier = data["id"]
    suffix = Path(data["stored_file"]).name.removeprefix("file")
    if (
        data["version"] != 1
        or not _valid_id(identifier)
        or not _suffix(suffix)
        or data["stored_file"] != f"file{suffix}"
    ):
        msg = f"Trash metadata has an invalid version, entry ID, or stored filename: {meta}"
        raise ValueError(msg)
    if data["library_id"] < 1 or trash_path(root, data["library_id"], identifier, suffix).parent != meta.parent:
        msg = f"Trash metadata library ID, entry ID, or path does not match its location: {meta}"
        raise ValueError(msg)
    for key in ("trashed_at", "purge_at"):
        parsed = datetime.fromisoformat(data[key])
        if parsed.tzinfo is None:
            msg = f"Trash metadata {key} must include a timezone: {meta}"
            raise ValueError(msg)
    media = meta.parent / data["stored_file"]
    media_stat = entry_stat(media)
    if media_stat is None:
        if not allow_missing_media:
            msg = f"Trash media file is missing: {media}"
            raise ValueError(msg)
    elif not S_ISREG(media_stat.st_mode) or media_stat.st_size != data["size"]:
        msg = f"Trash media file is not regular or its size does not match metadata: {media}"
        raise ValueError(msg)
    return data, media


def _check_trash_records(root: Path, rows: Sequence[tuple[str, int, str, str]], active: set[str]) -> list:
    row_results = []
    for row_id, library_id, suffix, library_root in rows:
        leaf = trash_path(root, library_id, row_id, suffix).parent
        if row_id in active:
            continue
        library_fd = fd = None
        try:
            try:
                library_fd = _try_library_lock(Path(library_root))
            except FileNotFoundError:
                pass
            else:
                if library_fd is None:
                    continue
            try:
                _confined(leaf, root)
            except ValueError:
                row_results.append((row_id, "invalid", None))
                continue
            if not leaf.exists():
                row_results.append((row_id, "missing", None))
                continue
            if leaf.is_symlink() or not leaf.is_dir():
                row_results.append((row_id, "invalid", None))
                continue
            fd = _lock_leaf(leaf)
            if fd is None or (leaf / f".{row_id}.restore-claim").exists():
                continue
            try:
                data, _ = _manifest(leaf / "metadata.json", root)
                if data["id"] != row_id:
                    msg = f"Trash metadata entry ID does not match row {row_id}: {leaf / 'metadata.json'}"
                    error = ValueError(msg)
                    raise error
                row_results.append((row_id, "valid", None))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                media = leaf / f"file{suffix}"
                if not media.exists() or media.is_symlink():
                    row_results.append((row_id, "missing", None))
                else:
                    row_results.append((row_id, "bad", exc))
        finally:
            if fd is not None:
                _unlock_leaf(fd)
            if library_fd is not None:
                _unlock_leaf(library_fd)
    return row_results


def _trash_directories(path: Path) -> Generator[Path]:
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                yield Path(entry.path)


def _trash_leaves(root: Path) -> Generator[Path]:
    for library in _trash_directories(root):
        for first in _trash_directories(library):
            for second in _trash_directories(first):
                for leaf in _trash_directories(second):
                    if not leaf.name.endswith(".partial"):
                        yield leaf


def _read_trash_manifests(root: Path, leaves: list[Path]) -> list:
    orphans = []
    for leaf in leaves:
        try:
            data, media = _manifest(leaf / "metadata.json", root)
            orphans.append((data, media))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            orphans.append((None, (leaf, exc)))
    return orphans


def _purge_leaf(leaf: Path, root: Path) -> None:
    _confined(leaf, root)
    leaf_stat = entry_stat(leaf)
    if leaf_stat is not None:
        if not S_ISDIR(leaf_stat.st_mode):
            raise ValueError("unsafe trash leaf")
        shutil.rmtree(leaf)
    for parent in (leaf.parent, leaf.parent.parent, leaf.parent.parent.parent):
        if parent != root and parent.is_dir() and not parent.is_symlink():
            with os.scandir(parent) as entries:
                empty = next(entries, None) is None
            if empty:
                parent.rmdir()


def _journal_leaf(root: Path, entry_id: str) -> Path | None:
    if not _valid_id(entry_id):
        return None
    with closing(_trash_directories(root)) as libraries:
        for library in libraries:
            leaf = library / entry_id[:2] / entry_id[2:4] / entry_id[4:]
            if not _regular(leaf):
                continue
            try:
                data, _ = _manifest(leaf / "metadata.json", root, allow_missing_media=True)
            except OSError, KeyError, TypeError, ValueError, json.JSONDecodeError:
                continue
            if data["id"] == entry_id:
                return leaf
    return None


async def refresh_trash_records(sessionmaker: async_sessionmaker[AsyncSession], root: Path) -> int:
    async with sessionmaker() as session:
        return await _refresh_trash_records(session, root)


async def _restore_entries(session: AsyncSession, entry_ids: list[str]) -> set[str]:
    return set(
        await session.scalars(
            select(TrashRestoreJournal.entry_id)
            .where(TrashRestoreJournal.entry_id.in_(entry_ids))
            .union(
                select(TrashRestoreJournal.displaced_entry_id).where(
                    TrashRestoreJournal.displaced_entry_id.in_(entry_ids)
                )
            )
        )
    )


async def _refresh_trash_records(session: AsyncSession, root: Path) -> int:
    if not _regular(root) or not os.access(root, os.R_OK | os.W_OK):
        LOG.error("trash root unavailable: %s", root)
        return 0
    changed = 0
    after = ""
    while True:
        rows = list(
            await session.execute(
                select(TrashEntry.id, TrashEntry.library_id, TrashEntry.suffix, TrashEntry.library_root)
                .where(TrashEntry.id > after)
                .order_by(TrashEntry.id)
                .limit(_TRASH_BATCH_SIZE)
            )
        )
        if not rows:
            break
        after = rows[-1][0]
        active = await _restore_entries(session, [row[0] for row in rows])
        paths = {
            row_id: str(trash_path(root, library_id, row_id, suffix).resolve())
            for row_id, library_id, suffix, _ in rows
        }
        protected = set(
            await session.scalars(
                _finalization_trash().where(FinalizationJournal.trash_path.in_(paths.values())).distinct()
            )
        )
        active.update(row_id for row_id, path in paths.items() if path in protected)
        row_results = await wait_workspace_io(lambda rows=rows, active=active: _check_trash_records(root, rows, active))
        for row_id, status, detail in row_results:
            if status == "missing":
                row = await session.scalar(select(TrashEntry).where(TrashEntry.id == row_id))
                if row is None:
                    continue
                try:
                    if await _delete_trash_entry(session, root, row, missing_only=True):
                        changed += 1
                except ValueError:
                    continue
            elif status == "invalid":
                LOG.error("invalid trash leaf for row %s", row_id)
            elif status == "bad":
                LOG.error("invalid trash metadata for row %s", row_id, extra={"error": str(detail)})

    # Filesystem batches only identify candidates. Recheck each candidate under its
    # locks before cataloging it, never treating a partial catalog as complete.
    with closing(_trash_leaves(root)) as leaves:
        while True:
            batch = await wait_workspace_io(lambda: list(islice(leaves, _TRASH_BATCH_SIZE)))
            if not batch:
                break
            orphans = await wait_workspace_io(lambda batch=batch: _read_trash_manifests(root, batch))
            entry_ids = [data["id"] for data, _ in orphans if data is not None]
            known = set(await session.scalars(select(TrashEntry.id).where(TrashEntry.id.in_(entry_ids))))
            active = await _restore_entries(session, entry_ids)
            paths = [str(media.resolve()) for data, media in orphans if data is not None]
            protected = set(
                await session.scalars(_finalization_trash().where(FinalizationJournal.trash_path.in_(paths)).distinct())
            )
            for data, detail in orphans:
                if data is None:
                    leaf, exc = detail
                    LOG.error("invalid trash entry %s", leaf, extra={"error": str(exc)})
                    continue
                if data["id"] in known or data["id"] in active or str(detail.resolve()) in protected:
                    continue
                if await _restore_trash_record(session, root, data, detail):
                    changed += 1
    return changed


async def _restore_trash_record(session: AsyncSession, root: Path, data: dict, media: Path) -> bool:
    lock_fd = library_fd = None
    try:
        lock_fd = _lock_leaf(media.parent, required=True)
        if lock_fd is None:
            return False
        try:
            library_fd = _try_library_lock(Path(data["library_root"]))
        except FileNotFoundError:
            pass
        else:
            if library_fd is None:
                return False
        historical_root = data["library_root"]
        data, media = _manifest(media.parent / "metadata.json", root)
        if data["library_root"] != historical_root:
            LOG.error("trash entry historical root changed for %s", data["id"])
            return False
        if await session.scalar(select(TrashEntry.id).where(TrashEntry.id == data["id"])) is not None:
            return False
        if (
            await session.scalar(
                select(TrashRestoreJournal.id)
                .where(
                    or_(
                        TrashRestoreJournal.entry_id == data["id"],
                        TrashRestoreJournal.displaced_entry_id == data["id"],
                    )
                )
                .limit(1)
            )
            is not None
            or await session.scalar(
                _finalization_trash().where(FinalizationJournal.trash_path == str(media.resolve())).limit(1)
            )
            is not None
        ):
            return False
        now = datetime.fromisoformat(data["trashed_at"])
        purge = datetime.fromisoformat(data["purge_at"])
        session.add(
            TrashEntry(
                id=data["id"],
                library_id=data["library_id"],
                original_path=data["original_path"],
                library_root=data["library_root"],
                suffix=media.suffix,
                size_bytes=data["size"],
                mtime_ns=data["mtime_ns"],
                trashed_at=now,
                purge_at=purge,
            )
        )
        await session.commit()
        return True
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("invalid trash entry %s", media.parent, extra={"error": str(exc)})
        return False
    finally:
        if library_fd is not None:
            _unlock_leaf(library_fd)
        if lock_fd is not None:
            _unlock_leaf(lock_fd)


async def purge_trash(
    sessionmaker: async_sessionmaker[AsyncSession], root: Path, *, now: datetime | None = None
) -> int:
    if not _regular(root) or not os.access(root, os.R_OK | os.W_OK):
        return 0
    cutoff = now or datetime.now(UTC)
    count = 0
    failed = 0
    after = ""
    while True:
        async with sessionmaker() as session:
            entry_ids = list(
                await session.scalars(
                    select(TrashEntry.id)
                    .where(TrashEntry.purge_at <= cutoff, TrashEntry.id > after)
                    .order_by(TrashEntry.id)
                    .limit(_TRASH_BATCH_SIZE)
                )
            )
        if not entry_ids:
            break
        after = entry_ids[-1]
        for entry_id in entry_ids:
            try:
                async with sessionmaker() as session:
                    row = await session.scalar(select(TrashEntry).where(TrashEntry.id == entry_id))
                    if row is not None and await _delete_trash_entry(session, root, row, expired_before=cutoff):
                        count += 1
            except ValueError:
                continue
            except Exception:
                failed += 1
                LOG.exception("trash purge failed", extra={"operation": "purge", "entry_id": entry_id})
    if count or failed:
        LOG.info(
            "Purged %d trash entries (%d failures)",
            count,
            failed,
            extra={"operation": "purge", "deleted_count": count, "failure_count": failed},
        )
    return count


def _finalization_trash() -> Select[str]:
    return select(FinalizationJournal.trash_path).where(
        or_(
            FinalizationJournal.state.not_in(FINISHED_FINALIZATION_STATES),
            FinalizationJournal.attempt_id.in_(select(JobAttempt.id).where(JobAttempt.job_id.in_(recovery_jobs()))),
        ),
    )


async def _mutation_blocker(session: AsyncSession, root: Path, entry: TrashEntry) -> str | None:
    # Hold the leaf and any existing library root: finalization does not own the leaf.
    active = await session.scalar(
        select(TrashRestoreJournal.id)
        .where(or_(TrashRestoreJournal.entry_id == entry.id, TrashRestoreJournal.displaced_entry_id == entry.id))
        .limit(1)
    )
    if active is not None:
        return "trash entry restore is already pending recovery"
    journal = await session.scalar(
        _finalization_trash()
        .where(
            FinalizationJournal.trash_path == str(trash_path(root, entry.library_id, entry.id, entry.suffix).resolve())
        )
        .limit(1)
    )
    if journal is not None:
        return "trash entry finalization is still pending recovery"
    return None


async def delete_trash_entry(sessionmaker: async_sessionmaker[AsyncSession], root: Path, entry_id: str) -> bool:
    async with sessionmaker() as session:
        entry = await session.scalar(select(TrashEntry).where(TrashEntry.id == entry_id))
        if entry is None:
            return False
        return await _delete_trash_entry(session, root, entry)


async def _delete_trash_entry(
    session: AsyncSession,
    root: Path,
    entry: TrashEntry,
    *,
    missing_only: bool = False,
    expired_before: datetime | None = None,
) -> bool:
    leaf = trash_path(root, entry.library_id, entry.id, entry.suffix).parent
    _confined(leaf, root)
    explicit = not missing_only and expired_before is None
    lock_fd = _lock_leaf(leaf, required=explicit)
    if lock_fd is None and (explicit or entry_stat(leaf) is not None):
        raise ValueError("trash entry is already locked")
    library_fd = None
    try:
        try:
            library_fd = _try_library_lock(Path(entry.library_root))
        except FileNotFoundError:
            pass
        else:
            if library_fd is None:
                raise ValueError("library finalization is active")
        current = await session.scalar(
            select(TrashEntry)
            .where(TrashEntry.id == entry.id)
            .with_for_update(of=TrashEntry)
            .execution_options(populate_existing=True)
        )
        if current is None:
            return False
        if expired_before is not None and current.purge_at.replace(tzinfo=UTC) > expired_before:
            return False
        blocker = await _mutation_blocker(session, root, entry)
        if blocker is not None:
            raise ValueError(blocker)
        if missing_only:
            media = leaf / f"file{entry.suffix}"
            if entry_stat(media) is not None or entry_stat(leaf / f".{entry.id}.restore-claim") is not None:
                return False
        await wait_workspace_io(lambda: _purge_leaf(leaf, root))
        await session.delete(entry)
        await session.commit()
        return True
    finally:
        if library_fd is not None:
            _unlock_leaf(library_fd)
        if lock_fd is not None:
            _unlock_leaf(lock_fd)


def _restore_destination(entry: TrashEntry, root: Path, *, allow_missing_media: bool = False) -> tuple[Path, Path]:
    library_root = Path(entry.library_root)
    destination = Path(entry.original_path)
    if destination.is_symlink() or library_root.is_symlink() or not library_root.is_dir():
        raise ValueError("unsafe restore path")
    library_root = library_root.resolve(strict=True)
    try:
        destination.relative_to(library_root)
    except ValueError as exc:
        raise ValueError("restore path is outside the library") from exc
    leaf = trash_path(root, entry.library_id, entry.id, entry.suffix).parent
    if leaf.is_symlink() or not leaf.is_dir():
        raise ValueError("trash entry is unavailable")
    data, media = _manifest(leaf / "metadata.json", root, allow_missing_media=allow_missing_media)
    if (
        data["id"] != entry.id
        or data["library_id"] != entry.library_id
        or data["original_path"] != entry.original_path
        or data["library_root"] != entry.library_root
    ):
        raise ValueError("trash metadata does not match entry")
    parent = destination.parent
    current = library_root
    for component in destination.relative_to(library_root).parts[:-1]:
        current /= component
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise ValueError("unsafe restore parent")
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise ValueError("unsafe restore parent")
    parent.mkdir(parents=True, exist_ok=True)
    return destination, media


def _sync_restore(destination: Path, library_root: Path) -> None:
    _confined(destination, library_root)
    destination_stat = entry_stat(destination)
    if destination_stat is None or not S_ISREG(destination_stat.st_mode):
        raise ValueError("unsafe restore destination")
    _fsync(destination)
    # Include every ancestor: a failed prior restore may already have created these directories.
    parent = destination.parent
    while True:
        _fsync(parent)
        if parent == library_root:
            break
        parent = parent.parent


def _publish_restore(
    media: Path, destination: Path, size: int, library_root: Path, *, overwrite: bool = False, sync: bool = True
) -> None:
    if media.stat().st_size != size:
        raise ValueError("Trash file size changed.")
    effects.publish(media, destination, overwrite=overwrite)
    if sync:
        _sync_restore(destination, library_root)
    else:
        _confined(destination, library_root)


async def restore_trash(
    sessionmaker: async_sessionmaker[AsyncSession],
    root: Path,
    entry_id: str,
    *,
    replace_existing: bool = False,
    move_to_trash: bool = False,
    retention_days: int | None = None,
    sync_to_disk: bool = True,
) -> Path:
    if move_to_trash and not replace_existing:
        raise ValueError("move_to_trash requires replace_existing")
    async with sessionmaker() as session:
        entry = await session.scalar(select(TrashEntry).where(TrashEntry.id == entry_id))
        if entry is None:
            raise LookupError("trash entry not found")
        return await _restore_entry(
            session,
            root,
            entry,
            replace_existing=replace_existing,
            move_to_trash=move_to_trash,
            retention_days=retention_days,
            sync_to_disk=sync_to_disk,
        )


async def _restore_entry(
    session: AsyncSession,
    root: Path,
    entry: TrashEntry,
    *,
    replace_existing: bool,
    move_to_trash: bool,
    retention_days: int | None,
    sync_to_disk: bool,
) -> Path:
    library_root = Path(entry.library_root)
    started = time.perf_counter()
    leaf = trash_path(root, entry.library_id, entry.id, entry.suffix).parent
    fd = _lock_leaf(leaf, required=True)
    if fd is None:
        raise ValueError("trash entry restore is already in progress")
    try:
        async with library_mutation_lock(library_root, wait=False):
            destination = await _restore_trash_locked(
                session,
                root,
                entry,
                library_root,
                replace_existing=replace_existing,
                move_to_trash=move_to_trash,
                retention_days=retention_days,
                sync_to_disk=sync_to_disk,
            )
        duration_ms = round((time.perf_counter() - started) * 1000)
        LOG.info(
            "Restored trash entry %s to %s in %dms",
            entry.id,
            destination,
            duration_ms,
            extra={
                "operation": "restore",
                "outcome": "succeeded",
                "entry_id": entry.id,
                "library_id": entry.library_id,
                "restored_path": str(destination),
                "duration_ms": duration_ms,
                "replace_existing": replace_existing,
                "move_to_trash": move_to_trash,
            },
        )
        return destination
    except Exception:
        LOG.exception(
            "Failed to restore trash entry %s from %s",
            entry.id,
            entry.original_path,
            extra={
                "operation": "restore",
                "outcome": "failed",
                "entry_id": entry.id,
                "library_id": entry.library_id,
                "original_path": entry.original_path,
            },
        )
        raise
    finally:
        _unlock_leaf(fd)


async def _restore_trash_locked(
    session: AsyncSession,
    root: Path,
    entry: TrashEntry,
    library_root: Path,
    *,
    replace_existing: bool,
    move_to_trash: bool,
    retention_days: int | None,
    sync_to_disk: bool,
) -> Path:
    current = await session.scalar(
        select(TrashEntry)
        .where(TrashEntry.id == entry.id)
        .with_for_update(of=TrashEntry)
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise LookupError("trash entry not found")
    if current.library_root != str(library_root):
        raise ValueError("trash entry historical root changed")
    blocker = await _mutation_blocker(session, root, entry)
    if blocker is not None:
        raise ValueError(blocker)
    destination, media = _restore_destination(entry, root)
    target_stat = entry_stat(destination)
    if target_stat is not None:
        if not replace_existing:
            raise FileExistsError(destination)
        if not S_ISREG(target_stat.st_mode):
            raise ValueError("existing restore path is unsafe")
        if move_to_trash and retention_days is None:
            retention_days = max(1, math.ceil((entry.purge_at - entry.trashed_at).total_seconds() / 86400))
    displaced_id = secrets.token_hex(8) if move_to_trash and target_stat is not None else None
    journal = TrashRestoreJournal(
        id=uuid.uuid4().hex,
        entry_id=entry.id,
        target_path=str(destination),
        displaced_entry_id=displaced_id,
        state="overwrite_pending" if target_stat is not None and displaced_id is None else "pending",
    )
    session.add(journal)
    await session.commit()

    if displaced_id is not None:
        assert retention_days is not None
        await store_trash(
            session,
            root,
            entry.library_id,
            destination,
            library_root,
            retention_days,
            identifier=displaced_id,
        )
        journal.state = "displaced"
        await session.commit()
    await storage_io(
        _publish_restore,
        media,
        destination,
        entry.size_bytes,
        library_root,
        overwrite=replace_existing,
        sync=sync_to_disk,
    )
    journal.state = "moved"
    await session.commit()
    await wait_workspace_io(media.unlink)
    # Commit the DB decision while the source leaf still exists. Cleanup is recoverable.
    await session.delete(entry)
    journal.state = "committed"
    await session.commit()
    try:
        _purge_leaf(media.parent, root)
    except Exception:
        LOG.exception("trash restore cleanup pending for %s", entry.id)
    else:
        await session.delete(journal)
        await session.commit()
    return destination


async def recover_trash_restores(sessionmaker: async_sessionmaker[AsyncSession], root: Path) -> tuple[int, int]:
    completed = failed = 0
    after = ""
    while True:
        async with sessionmaker() as session:
            journal_ids = list(
                await session.scalars(
                    select(TrashRestoreJournal.id)
                    .where(TrashRestoreJournal.id > after)
                    .order_by(TrashRestoreJournal.id)
                    .limit(_TRASH_BATCH_SIZE)
                )
            )
        if not journal_ids:
            break
        after = journal_ids[-1]
        for journal_id in journal_ids:
            try:
                if await recover_trash_restore(sessionmaker, root, journal_id):
                    completed += 1
            except Exception:
                failed += 1
                LOG.exception("trash restore recovery failed for %s", journal_id)
    if completed or failed:
        LOG.info(
            "Recovered trash restores: %d completed, %d failed",
            completed,
            failed,
            extra={"operation": "restore_recovery", "completed_count": completed, "failure_count": failed},
        )
    return completed, failed


async def recover_trash_restore(sessionmaker: async_sessionmaker[AsyncSession], root: Path, journal_id: str) -> bool:
    async with sessionmaker() as session:
        journal = await session.scalar(select(TrashRestoreJournal).where(TrashRestoreJournal.id == journal_id))
        if journal is None:
            return False
        entry = await session.scalar(select(TrashEntry).where(TrashEntry.id == journal.entry_id))
        if journal.state != "committed":
            if entry is None:
                raise ValueError("Restore entry is unavailable. Recovery files retained.")
            leaf = trash_path(root, entry.library_id, entry.id, entry.suffix).parent
            fd = _lock_leaf(leaf)
            if fd is None:
                return False
            try:
                async with library_mutation_lock(Path(entry.library_root), wait=False):
                    if (
                        await session.scalar(
                            select(TrashRestoreJournal)
                            .where(TrashRestoreJournal.id == journal_id)
                            .with_for_update(of=TrashRestoreJournal)
                            .execution_options(populate_existing=True)
                        )
                        is None
                    ):
                        return False
                    await _recover_restore(session, root, entry, journal)
                return True
            finally:
                _unlock_leaf(fd)
        leaf = (
            trash_path(root, entry.library_id, entry.id, entry.suffix).parent
            if entry is not None
            else _journal_leaf(root, journal.entry_id)
        )
        if leaf is None:
            if (
                await session.scalar(
                    select(TrashRestoreJournal)
                    .where(TrashRestoreJournal.id == journal_id)
                    .with_for_update(of=TrashRestoreJournal)
                    .execution_options(populate_existing=True)
                )
                is None
            ):
                return False
            if journal.displaced_entry_id is not None:
                displaced = await session.scalar(select(TrashEntry).where(TrashEntry.id == journal.displaced_entry_id))
                if displaced is None:
                    raise ValueError("Displaced original retention is incomplete. Recovery files retained.")
                await _verify_displaced(session, root, journal, displaced.library_id, displaced.library_root)
            await session.delete(journal)
            await session.commit()
            return True
        fd = _lock_leaf(leaf)
        if fd is None:
            return False
        try:
            data, media = _manifest(leaf / "metadata.json", root, allow_missing_media=True)
            destination = Path(journal.target_path)
            if str(destination) != data["original_path"] or data["id"] != journal.entry_id:
                raise ValueError("restore journal does not match metadata")
            library_root = Path(data["library_root"])
            _confined(destination, library_root)
            async with library_mutation_lock(library_root, wait=False):
                if (
                    await session.scalar(
                        select(TrashRestoreJournal)
                        .where(TrashRestoreJournal.id == journal_id)
                        .with_for_update(of=TrashRestoreJournal)
                        .execution_options(populate_existing=True)
                    )
                    is None
                ):
                    return False
                if (
                    await session.scalar(
                        _finalization_trash().where(FinalizationJournal.trash_path == str(media.resolve())).limit(1)
                    )
                    is not None
                ):
                    raise ValueError("trash entry finalization is still pending recovery")
                await _verify_displaced(session, root, journal, data["library_id"], data["library_root"])
                if destination.stat().st_size != data["size"]:
                    raise ValueError("Restored file size changed.")
                await storage_io(_sync_restore, destination, library_root)
                _purge_leaf(leaf, root)
                if entry is not None:
                    await session.delete(entry)
                await session.delete(journal)
                await session.commit()
            return True
        finally:
            _unlock_leaf(fd)


async def _verify_displaced(
    session: AsyncSession, root: Path, journal: TrashRestoreJournal, library_id: int, library_root: str
) -> None:
    if journal.displaced_entry_id is None:
        return
    displaced = await session.scalar(select(TrashEntry).where(TrashEntry.id == journal.displaced_entry_id))
    if displaced is None:
        raise ValueError("Displaced original retention is incomplete. Recovery files retained.")
    if (
        displaced.original_path != journal.target_path
        or displaced.library_id != library_id
        or displaced.library_root != library_root
    ):
        raise ValueError("displaced original does not match restore journal")
    _manifest(trash_path(root, displaced.library_id, displaced.id, displaced.suffix).parent / "metadata.json", root)


async def _recover_restore(session: AsyncSession, root: Path, entry: TrashEntry, journal: TrashRestoreJournal) -> None:
    historical_root = entry.library_root
    current = await session.scalar(
        select(TrashEntry)
        .where(TrashEntry.id == entry.id)
        .with_for_update(of=TrashEntry)
        .execution_options(populate_existing=True)
    )
    if current is None or current.library_root != historical_root:
        raise ValueError("Restore entry changed. Recovery files retained.")
    if journal.target_path != entry.original_path:
        raise ValueError("restore journal does not match entry")
    protected = await session.scalar(
        _finalization_trash()
        .where(
            FinalizationJournal.trash_path == str(trash_path(root, entry.library_id, entry.id, entry.suffix).resolve())
        )
        .limit(1)
    )
    if protected is not None:
        raise ValueError("trash entry finalization is still pending recovery")
    await _verify_displaced(session, root, journal, entry.library_id, entry.library_root)
    destination, media = _restore_destination(entry, root, allow_missing_media=True)
    claim = media.parent / f".{entry.id}.restore-claim"
    restore_source = media if media.exists() else claim
    if entry_stat(destination) is not None:
        # An existing same-size destination can still be the file we meant to replace.
        # Only a recorded publication permits discarding the retained original.
        if journal.state == "overwrite_pending":
            message = (
                f"Overwrite restore publication is unconfirmed at {destination}. "
                "Inspect both files before continuing. Original and recovery records retained."
            )
            raise ValueError(message)
        if destination.stat().st_size != entry.size_bytes:
            raise ValueError("Restored file size changed.")
        await storage_io(_sync_restore, destination, Path(entry.library_root))
    else:
        if restore_source.stat().st_size != entry.size_bytes:
            raise ValueError("Trash file size changed.")

        await storage_io(_publish_restore, restore_source, destination, entry.size_bytes, Path(entry.library_root))
    journal.state = "moved"
    await session.commit()
    await wait_workspace_io(lambda: restore_source.unlink(missing_ok=True))
    await session.delete(entry)
    journal.state = "committed"
    await session.commit()
    try:
        _purge_leaf(media.parent, root)
    except Exception:
        LOG.exception("trash restore cleanup pending for %s", journal.entry_id)
    else:
        await session.delete(journal)
        await session.commit()


async def cleanup_trash(sessionmaker: async_sessionmaker[AsyncSession], root: Path) -> None:
    await recover_trash_restores(sessionmaker, root)
    await refresh_trash_records(sessionmaker, root)
    await purge_trash(sessionmaker, root)
