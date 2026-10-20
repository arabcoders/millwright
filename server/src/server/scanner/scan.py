from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import TYPE_CHECKING

from common.enums import JobStatus, TranscodeStatus
from common.fs.identity import source_identity
from common.media.probe import probe_stream
from common.process import ChildNotReapedError
from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import load_only

from server.core.paths import healthcheck_dir
from server.fs.source import MediaSnapshot
from server.models import FinalizationJournal, Job, JobAttempt, Library, MediaFile, ProbeRow, utcnow
from server.processing_cutoff import processing_allowed
from server.queue.store import ACTIVE_STATUSES, cancel_pending, enqueue_healthcheck_job
from server.settings import ScannerConfig
from server.workspace import FINISHED_FINALIZATION_STATES, wait_workspace_io

if TYPE_CHECKING:
    import threading
    from collections.abc import Awaitable, Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from server.events import EventBus
    from server.staging import StagingService

COMMIT_BATCH = 100
_TEMP_SUFFIX = ".partial"

LOG = logging.getLogger(__name__)


@dataclass(kw_only=True, slots=True)
class ScanReport:
    found: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


async def scan_library(
    session: AsyncSession,
    library: Library,
    *,
    config: ScannerConfig,
    staging: StagingService,
    events: EventBus | None = None,
    trash_dir: Path | None = None,
    on_progress: Callable[[ScanReport], None] | None = None,
    on_files_pending: Callable[[list[int]], Awaitable[None]] | None = None,
    stopped: threading.Event | None = None,
    target_paths: set[Path] | None = None,
) -> ScanReport:
    report = ScanReport()
    if library.suspended:
        return report
    await session.commit()
    await session.refresh(library)
    library_updated_at = library.updated_at
    allowed = {c.lower() for c in config.containers}
    ignored = set(config.ignore_dirs)
    roots = [Path(root) for root in library.roots]
    excluded = tuple(
        path.resolve() for path in (staging.workspace_dir, healthcheck_dir(), trash_dir) if path is not None
    )
    unavailable: set[Path] = set()
    loop = asyncio.get_running_loop()

    def found(count: int) -> None:
        if on_progress is not None:
            loop.call_soon_threadsafe(on_progress, ScanReport(found=count, errors=list(report.errors)))

    scopes: list[tuple[Path, bool]] | None = None
    if target_paths is None:
        paths = await wait_workspace_io(
            lambda: _wanted_files(
                roots,
                ignored,
                allowed,
                report.errors,
                unavailable,
                excluded,
                on_found=found if on_progress is not None else None,
                stopped=stopped,
            )
        )
    else:
        scopes, paths = await wait_workspace_io(
            lambda: _scoped_files(
                roots,
                target_paths,
                ignored,
                allowed,
                report.errors,
                unavailable,
                excluded,
                stopped,
                on_found=found if on_progress is not None else None,
            )
        )
    report.found = len(paths)
    if stopped is not None and stopped.is_set():
        report.cancelled = True
        return report
    if on_progress is not None:
        on_progress(report)
    progress_at = time.monotonic()
    if stopped is not None and stopped.is_set():
        await session.rollback()
        report.cancelled = True
        return report
    scan_id = await session.scalar(
        update(Library)
        .where(
            Library.id == library.id,
            Library.updated_at == library_updated_at,
            Library.suspended.is_(False),
        )
        .values(scan_id=Library.scan_id + 1, updated_at=Library.updated_at)
        .returning(Library.scan_id)
    )
    if scan_id is None:
        await session.rollback()
        return report
    if stopped is not None and stopped.is_set():
        await session.rollback()
        report.cancelled = True
        return report
    await session.refresh(library)
    await session.commit()
    readable_roots: set[Path] = set()

    for offset in range(0, len(paths), COMMIT_BATCH):
        if stopped is not None and stopped.is_set():
            report.cancelled = True
            return report
        batch = paths[offset : offset + COMMIT_BATCH]
        expected = {
            row.path: MediaSnapshot.from_media(row)
            for row in (
                await session.scalars(
                    select(MediaFile)
                    .where(MediaFile.library_id == library.id, MediaFile.path.in_([str(path) for path in batch]))
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
        await session.commit()
        # Inspect slow storage before locking catalog rows for updates.
        files = await wait_workspace_io(lambda batch=batch: _stat_files(batch, report.errors))
        if stopped is not None and stopped.is_set():
            report.cancelled = True
            return report
        current_library = await session.scalar(
            select(Library)
            .where(Library.id == library.id)
            .with_for_update(read=True, of=Library)
            .execution_options(populate_existing=True)
        )
        if (
            current_library is None
            or library.suspended
            or library.roots != [str(root) for root in roots]
            or library.updated_at != library_updated_at
            or library.scan_id != scan_id
        ):
            await session.rollback()
            return report
        # Lock catalog IDs in one order before marker updates or source checks.
        current_rows = {
            row.path: row
            for row in (
                await session.scalars(
                    select(MediaFile)
                    .where(MediaFile.library_id == library.id, MediaFile.path.in_([str(path) for path in batch]))
                    .order_by(MediaFile.id)
                    .with_for_update(of=MediaFile)
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
        if stopped is not None and stopped.is_set():
            await session.rollback()
            report.cancelled = True
            return report
        # Discovery confirms presence even when source records cannot be updated.
        # Internal markers must not change source-generation timestamps.
        await session.execute(
            update(MediaFile)
            .where(
                MediaFile.library_id == library.id,
                MediaFile.path.in_([str(path) for path in batch]),
                MediaFile.last_seen_scan_id != scan_id,
            )
            .values(last_seen_scan_id=scan_id, updated_at=MediaFile.updated_at)
        )
        blocked: set[int] = set()
        # Statistics edits the source in place. Discovery must not record
        # temporary size changes as a new source while the writer is running.
        blocked.update(
            await session.scalars(
                select(Job.media_file_id).where(
                    Job.media_file_id.in_([row.id for row in current_rows.values()]),
                    Job.kind == "mkv_stats",
                    Job.status == "running",
                )
            )
        )
        probe_ids = {
            row.id
            for path, stat in files
            if library.mkv_stats
            and path.suffix.lower() == ".mkv"
            and (row := current_rows.get(str(path))) is not None
            and row.library_id == library.id
            and MediaSnapshot.from_media(row) == expected.get(str(path))
            and row.size == stat.st_size
        }
        refresh_ids = {
            row.id
            for row in current_rows.values()
            if row.probe_id is None and row.health_status in {"healthy", "corrupt"}
        }
        active_probe_ids = (
            set(
                await session.scalars(
                    select(Job.media_file_id).where(
                        Job.media_file_id.in_(refresh_ids),
                        Job.kind == "probe",
                        Job.status.in_(ACTIVE_STATUSES),
                    )
                )
            )
            if refresh_ids
            else set()
        )
        stored_probes = (
            {
                probe.media_file_id: json.loads(probe.raw_json)
                for probe in (
                    await session.scalars(
                        select(ProbeRow)
                        .where(ProbeRow.media_file_id.in_(probe_ids))
                        .execution_options(populate_existing=True)
                    )
                ).all()
            }
            if probe_ids
            else {}
        )
        new_paths = [str(path) for path, _stat in files if str(path) not in current_rows]
        # Publication owns its new path before its source catalog row moves.
        # Do not turn that output into a second catalog generation.
        publication_paths = (
            set(
                await session.scalars(
                    select(FinalizationJournal.destination_path)
                    .join(JobAttempt, FinalizationJournal.attempt_id == JobAttempt.id)
                    .join(Job, JobAttempt.job_id == Job.id)
                    .where(
                        FinalizationJournal.destination_path.in_(new_paths),
                        or_(
                            FinalizationJournal.state.not_in(FINISHED_FINALIZATION_STATES),
                            Job.status.in_([JobStatus.VERIFYING, JobStatus.FINALIZING]),
                        ),
                    )
                )
            )
            if new_paths
            else set()
        )
        pending = False
        invalidated: list[int] = []
        priority_files: list[MediaFile] = []
        new_rows: list[dict] = []
        scanned_ids: list[int] = []
        processed_paths: set[Path] = set()
        for path, stat in files:
            readable_roots.update(root for root in roots if path.is_relative_to(root))
            if path in processed_paths:
                continue
            processed_paths.add(path)
            row = current_rows.get(str(path))
            if row is not None and (row.library_id != library.id or row.id in blocked):
                continue
            current = MediaSnapshot.from_media(row) if row is not None else None
            if current != expected.get(str(path)):
                # A committed catalog change supersedes this batch's filesystem observation.
                continue
            identity = source_identity(stat)
            if row is None:
                if str(path) in publication_paths:
                    continue
                new_rows.append(
                    {
                        "library_id": library.id,
                        "path": str(path),
                        "container": path.suffix.lstrip(".").lower(),
                        "last_seen_scan_id": scan_id,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "source_identity": identity,
                        "medium": "other",
                        "metadata_priority": 1,
                        "first_seen": utcnow(),
                        "last_scanned_at": utcnow(),
                    }
                )
                continue
            unchanged = row.size == stat.st_size
            stored_raw = stored_probes.get(row.id)
            needs_stats = unchanged and stored_raw is not None and _lacks_stream_bitrates(stored_raw)
            if unchanged and row.metadata_cancelled:
                scanned_ids.append(row.id)
                report.unchanged += 1
                continue
            if (
                unchanged
                and row.probe_id is None
                and (
                    row.health_status == "pending"
                    or (row.health_status in {"healthy", "corrupt"} and row.id in active_probe_ids)
                )
            ):
                # A rescan must not replace the catalog snapshot in use by a
                # basic probe or in-place statistics writer for this generation.
                report.unchanged += 1
                pending = True
                if row.metadata_priority:
                    priority_files.append(row)
                continue
            if unchanged and row.health_status == "probe_failed":
                # Discovery isn't a retry request for a finished, unchanged failure.
                row.source_identity = identity
                scanned_ids.append(row.id)
                report.unchanged += 1
                continue
            if (
                unchanged
                and row.probe_id is not None
                and not needs_stats
                and row.video_codec != ""
                and row.health_status != "probe_failed"
            ):
                if row.source_identity != identity:
                    current_health_source = {"path": row.path, **identity}
                    # Current health results stay valid. Disabled checks also permit
                    # bound cached probe health under the stat policy.
                    healthy = row.health_status == "healthy" and (
                        row.health_source == current_health_source
                        or (
                            not library.healthcheck_enabled
                            and row.source_identity is not None
                            and row.health_source == {"path": row.path, **row.source_identity}
                        )
                    )
                    if healthy:
                        row.health_source = current_health_source
                    else:
                        row.health_source = None
                        if row.health_status == "healthy":
                            row.health_status = "pending"
                    row.source_identity = identity
                    if (
                        library.healthcheck_enabled
                        and not healthy
                        and processing_allowed(library, row)
                        and (library.flow_id is None or row.transcode_status == TranscodeStatus.TRANSCODED.value)
                    ):
                        row.health_status = "pending"
                        await enqueue_healthcheck_job(session, row.id, library.id)
                scanned_ids.append(row.id)
                report.unchanged += 1
            else:
                row.probe_id = None
                row.health_status = "pending"
                pending = True
                row.health_source = None
                if not unchanged:
                    row.first_seen = utcnow()
                    row.metadata_cancelled = False
                    row.metadata_priority = 1
                    if row.transcode_status != TranscodeStatus.TRANSCODED.value:
                        row.transcode_status = TranscodeStatus.PENDING.value
                        row.transcode_decided = False
                row.size = stat.st_size
                row.mtime_ns = stat.st_mtime_ns
                row.source_identity = identity
                row.last_scanned_at = utcnow()
                report.updated += 1
                if not needs_stats:
                    invalidated.append(row.id)
                if row.metadata_priority:
                    priority_files.append(row)
        if scanned_ids:
            # Unchanged scans must not invalidate an in-flight healthcheck.
            # Metadata updates still change the catalog snapshot on autoflush.
            await session.execute(
                update(MediaFile)
                .where(MediaFile.id.in_(scanned_ids))
                .values(last_scanned_at=utcnow(), updated_at=MediaFile.updated_at)
            )
        if new_rows:
            inserted = list(
                await session.scalars(
                    insert(MediaFile)
                    .values(new_rows)
                    .on_conflict_do_nothing(index_elements=[MediaFile.path])
                    .returning(MediaFile)
                )
            )
            report.added += len(inserted)
            priority_files.extend(inserted)
            pending = pending or bool(inserted)
        if invalidated:
            # Clear every catalog FK before deleting its old probe rows.
            await session.flush()
            await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id.in_(invalidated)))
        await session.commit()
        if pending and on_files_pending is not None:
            await on_files_pending([row.id for row in priority_files])
        now = time.monotonic()
        if on_progress is not None and now - progress_at >= 1:
            progress_at = now
            on_progress(report)

    if stopped is not None and stopped.is_set():
        report.cancelled = True
        return report
    for root in roots:
        if scopes is not None and not any(path == root and subtree for path, subtree in scopes):
            continue
        if root.name.startswith(".") or any(root.resolve().is_relative_to(path) for path in excluded):
            continue
        if root not in unavailable and root not in readable_roots:
            unavailable.add(root)
            report.errors.append(
                f"{root}: No readable media found. Missing-file pruning withheld. "
                "Check storage availability before removing indexed files manually."
            )
    if stopped is not None and stopped.is_set():
        report.cancelled = True
        return report
    # A failed scoped observation cannot prove removals, even if another target
    # supplied readable files. Full scans retain their per-subtree storage guards.
    if scopes is None or not report.errors:
        report.pruned = await _prune_missing(
            session,
            library,
            scan_id,
            staging=staging,
            config=config,
            unavailable=unavailable,
            excluded=excluded,
            scopes=scopes,
            stopped=stopped,
        )
    if stopped is not None and stopped.is_set():
        report.cancelled = True
        return report
    await session.commit()
    if events is not None:
        # Discovery updates use a summary. Pruning publishes committed removal IDs.
        events.publish(
            "file.status",
            {
                "action": "scanned",
                "library_id": library.id,
                "added": report.added,
                "updated": report.updated,
                "pruned": report.pruned,
            },
            key=f"file.status:{library.id}",
        )
    return report


def _stat_files(paths: list[Path], errors: list[str]) -> list[tuple[Path, os.stat_result]]:
    files = []
    for path in paths:
        if path.is_symlink():
            continue
        try:
            files.append((path, path.stat()))
        except OSError as exc:
            errors.append(f"{path}: stat failed: {exc}")
    return files


def _lacks_stream_bitrates(raw: dict) -> bool:
    streams = [probe_stream(stream) for stream in raw.get("streams") or []]
    video = [s for s in streams if s.codec_type == "video" and not s.disposition_attached_pic]
    audio = [s for s in streams if s.codec_type == "audio"]
    video_missing = any(not s.bit_rate for s in video)
    audio_missing = bool(audio) and all(not s.bit_rate for s in audio)
    return video_missing or audio_missing


async def _prune_missing(
    session: AsyncSession,
    library: Library,
    scan_id: int,
    *,
    staging: StagingService,
    unavailable: set[Path],
    excluded: tuple[Path, ...],
    config: ScannerConfig | None = None,
    scopes: list[tuple[Path, bool]] | None = None,
    stopped: threading.Event | None = None,
) -> int:
    pruned = 0
    filters = config if config is not None else ScannerConfig()
    allowed = {container.lower() for container in filters.containers}
    ignored = set(filters.ignore_dirs)
    library_updated_at = library.updated_at
    # Literal prefix matching must not depend on database collation or LIKE wildcards.
    conditions = [MediaFile.library_id == library.id]
    if scopes is not None:
        conditions = []
        for path, subtree in scopes:
            value = str(path)
            conditions.append(MediaFile.path == value)
            if not subtree:
                continue
            prefix = value.rstrip(os.sep) + os.sep
            conditions.append(func.starts_with(MediaFile.path, prefix))
    for condition in conditions:
        last_path = ""
        last_id = 0
        last_seen_scan_id = -1
        while True:
            if stopped is not None and stopped.is_set():
                return pruned
            current_source = (
                select(Library.id)
                .where(
                    Library.id == library.id,
                    Library.updated_at == library_updated_at,
                    Library.suspended.is_(False),
                    Library.scan_id == scan_id,
                )
                .with_for_update(read=True, of=Library)
            )
            current = await session.scalar(current_source)
            if current is None:
                await session.commit()
                return pruned
            statement = (
                select(MediaFile)
                .options(
                    load_only(
                        MediaFile.id,
                        MediaFile.library_id,
                        MediaFile.last_seen_scan_id,
                        MediaFile.path,
                        MediaFile.size,
                        MediaFile.mtime_ns,
                        MediaFile.source_identity,
                        MediaFile.created_at,
                        MediaFile.first_seen,
                        MediaFile.updated_at,
                        MediaFile.probe_id,
                        MediaFile.health_status,
                        raiseload=True,
                    )
                )
                .where(condition, MediaFile.last_seen_scan_id < scan_id)
            )
            if scopes is None:
                statement = statement.where(
                    tuple_(MediaFile.last_seen_scan_id, MediaFile.id) > (last_seen_scan_id, last_id)
                ).order_by(MediaFile.last_seen_scan_id, MediaFile.id)
            else:
                # Scope first through the path index.
                statement = statement.where(MediaFile.path > last_path).order_by(MediaFile.path)
            rows = (await session.scalars(statement.limit(COMMIT_BATCH))).all()
            await session.commit()
            if not rows:
                break
            last_path = rows[-1].path
            last_id = rows[-1].id
            last_seen_scan_id = rows[-1].last_seen_scan_id
            count = len(rows)
            if scopes is not None:
                rows = [row for row in rows if row.library_id == library.id]
            excluded_ids = await wait_workspace_io(
                lambda rows=rows: {
                    row.id for row in rows if any(Path(row.path).resolve().is_relative_to(path) for path in excluded)
                }
            )
            if excluded_ids:
                # Catalog rows needed for recovery must survive without creating new automatic work.
                if await session.scalar(current_source) is None:
                    await session.rollback()
                    return pruned
                if stopped is not None and stopped.is_set():
                    await session.rollback()
                    return pruned
                await session.execute(
                    select(MediaFile.id)
                    .where(MediaFile.id.in_(excluded_ids))
                    .order_by(MediaFile.id)
                    .with_for_update(of=MediaFile)
                )
                await session.execute(
                    update(MediaFile).where(MediaFile.id.in_(excluded_ids)).values(transcode_decided=True)
                )
                last_job_id = 0
                while True:
                    pending = (
                        await session.scalars(
                            select(Job.id)
                            .where(
                                Job.media_file_id.in_(excluded_ids),
                                Job.status.in_([JobStatus.QUEUED, JobStatus.STAGING, JobStatus.HOLD]),
                                Job.id > last_job_id,
                            )
                            .order_by(Job.id)
                            .limit(COMMIT_BATCH)
                        )
                    ).all()
                    if not pending:
                        break
                    last_job_id = pending[-1]
                    for job_id in pending:
                        await cancel_pending(session, job_id)
            await session.flush()
            # Discovery filters do not prove that previously indexed files disappeared.
            # Internal storage exclusions still remove accidental catalog entries.
            snapshots = [
                MediaSnapshot.from_media(row)
                for row in rows
                if row.id in excluded_ids
                or (
                    not any(Path(row.path).is_relative_to(path) for path in unavailable)
                    and Path(row.path).suffix.lstrip(".").lower() in allowed
                    and not any(
                        part in ignored
                        for root in library.roots
                        if Path(row.path).is_relative_to(root)
                        for part in Path(row.path).relative_to(root).parts[:-1]
                    )
                )
            ]
            await session.commit()
            # Each batch owns fresh row locks through graph deletion. Cancellation
            # rolls back that batch, but its count must retain earlier committed removals.
            if snapshots:
                pruned += await staging.prune_catalog(
                    snapshots,
                    library_updated_at=library_updated_at,
                    scan_id=scan_id,
                    unavailable=unavailable,
                    excluded=excluded,
                    stopped=stopped,
                )
            if count < COMMIT_BATCH:
                break
    return pruned


def merge_targets(paths: set[Path], *, roots: list[Path] | None = None) -> set[Path]:
    normalized = {path.absolute() for path in paths}
    if roots is not None:
        # An unauthorized ancestor must not swallow an authorized child target.
        normalized = {path for path in normalized if sum(path.is_relative_to(root) for root in roots) == 1}
    ordered = sorted(normalized, key=lambda path: (len(path.parts), str(path)))
    merged: set[Path] = set()
    for path in ordered:
        if not any(path.is_relative_to(parent) for parent in merged):
            merged.add(path)
    return merged


def _scoped_files(
    roots: list[Path],
    targets: set[Path],
    ignored: set[str],
    allowed: set[str],
    errors: list[str],
    unavailable: set[Path],
    excluded: tuple[Path, ...],
    stopped: threading.Event | None,
    *,
    on_found: Callable[[int], None] | None = None,
) -> tuple[list[tuple[Path, bool]], list[Path]]:
    scopes: list[tuple[Path, bool]] = []
    files: set[Path] = set()
    for path in sorted(merge_targets(targets, roots=roots)):
        if stopped is not None and stopped.is_set():
            break
        matching_roots = [root for root in roots if path.is_relative_to(root)]
        if len(matching_roots) != 1:
            continue
        root = matching_roots[0]
        parts = path.relative_to(root).parts
        if root.name.startswith(".") or any(
            part.startswith(".") or part in ignored or part.endswith(_TEMP_SUFFIX) for part in parts
        ):
            continue
        try:
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                continue
            if any(path.resolve().is_relative_to(directory) for directory in excluded):
                continue
            # Verify configured storage without walking it. Missing child targets
            # are deletions only when their surviving ancestors are readable.
            with os.scandir(root) as entries:
                next(entries, None)
            for parent in reversed(path.parents):
                if parent != root and parent.is_relative_to(root):
                    try:
                        with os.scandir(parent) as entries:
                            next(entries, None)
                    except FileNotFoundError:
                        break
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                scopes.append((path, True))
                continue
            if S_ISDIR(mode):
                scopes.append((path, True))
                files.update(
                    _wanted_files(
                        [path],
                        ignored,
                        allowed,
                        errors,
                        unavailable,
                        excluded,
                        stopped=stopped,
                        on_found=(lambda count: on_found(len(files) + count)) if on_found is not None else None,
                    )
                )
            elif S_ISREG(mode) and path.suffix.lstrip(".").lower() in allowed:
                scopes.append((path, False))
                files.add(path)
        except OSError as exc:
            errors.append(f"{path}: scoped discovery failed: {exc}")
            unavailable.add(root)
    return scopes, sorted(files)


def _wanted_files(
    roots: list[Path],
    ignored: set[str],
    allowed: set[str],
    errors: list[str],
    unavailable: set[Path],
    excluded: tuple[Path, ...],
    *,
    on_found: Callable[[int], None] | None = None,
    stopped: threading.Event | None = None,
) -> list[Path]:
    wanted: list[Path] = []
    progress_at = float("-inf")
    fd = shutil.which("fd") or shutil.which("fdfind")
    for root in roots:
        if stopped is not None and stopped.is_set():
            return wanted
        if (
            root.name.startswith(".")
            or root.is_symlink()
            or any(root.resolve().is_relative_to(path) for path in excluded)
        ):
            continue

        if fd is not None:
            offset = len(wanted)
            for path in _fd_files(fd, root, ignored, allowed, errors, unavailable, excluded, stopped):
                wanted.append(path)
                now = time.monotonic()
                if on_found is not None and now - progress_at >= 1:
                    progress_at = now
                    on_found(len(wanted))
            # fd emits parallel results in no fixed order. Preserve root files first and
            # sorted depth-first directories for catalog and batch ordering.
            wanted[offset:] = sorted(wanted[offset:], key=lambda path: (path.relative_to(root).parts[:-1], path.name))
            continue

        # A broken root must not hide the others. os.walk calls onerror for
        # unreadable or missing directories.
        def onerror(exc: OSError, root: Path = root) -> None:
            errors.append(f"{root}: {exc}")
            unavailable.add(Path(exc.filename) if exc.filename is not None else root)

        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            if stopped is not None and stopped.is_set():
                return wanted
            directory = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".")
                and name not in ignored
                and not (directory / name).is_symlink()
                and not any((directory / name).resolve().is_relative_to(path) for path in excluded)
            )
            for name in sorted(filenames):
                if stopped is not None and stopped.is_set():
                    return wanted
                if name.startswith(".") or name.endswith(_TEMP_SUFFIX):
                    continue
                if Path(name).suffix.lstrip(".").lower() not in allowed:
                    continue
                path = Path(dirpath) / name
                if not path.is_symlink():
                    wanted.append(path)
                    now = time.monotonic()
                    if on_found is not None and now - progress_at >= 1:
                        progress_at = now
                        on_found(len(wanted))
    return wanted


def _literal_glob(value: str) -> str:
    return re.sub(r"([\\*?\[\]{}])", r"\\\1", value)


def _fd_files(
    fd: str,
    root: Path,
    ignored: set[str],
    allowed: set[str],
    errors: list[str],
    unavailable: set[Path],
    excluded: tuple[Path, ...],
    stopped: threading.Event | None,
) -> Iterator[Path]:
    argv = [
        fd,
        "--no-ignore",
        "--hidden",
        "--exclude",
        ".*",
        "--no-follow",
        "--type",
        "file",
        "--print0",
        "--color",
        "never",
        "--show-errors",
        "--ignore-case",
    ]
    # A trailing slash excludes directories only. Names are literal policy,
    # not user-supplied glob patterns. Anchored paths exclude only that subtree.
    for name in sorted(ignored):
        if name and os.sep not in name:
            argv.extend(("--exclude", "**/" + _literal_glob(name) + "/"))
    resolved_root = root.resolve()
    for directory in excluded:
        if directory.is_relative_to(resolved_root):
            argv.extend(("--exclude", "/" + _literal_glob(directory.relative_to(resolved_root).as_posix()) + "/"))
    # fd's extension filter omits non-UTF-8 filenames. Match the suffix as bytes.
    pattern = r"(?-u)\.(?:" + "|".join(re.escape(extension) for extension in sorted(allowed)) + ")$"
    argv.extend(("--", pattern, "."))
    try:
        proc = subprocess.Popen(
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        errors.append(f"{root}: fd discovery failed: {exc}")
        unavailable.add(root)
        return

    pending = b""
    diagnostics = bytearray()
    try:
        assert proc.stdout is not None
        assert proc.stderr is not None
        with selectors.DefaultSelector() as reader:
            reader.register(proc.stdout, selectors.EVENT_READ)
            reader.register(proc.stderr, selectors.EVENT_READ)
            while reader.get_map() or proc.poll() is None:
                if stopped is not None and stopped.is_set():
                    return
                for key, _events in reader.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        reader.unregister(key.fileobj)
                    elif key.fileobj is proc.stderr:
                        diagnostics.extend(chunk)
                    else:
                        pending += chunk
                        names = pending.split(b"\0")
                        pending = names.pop()
                        for name in names:
                            if stopped is not None and stopped.is_set():
                                return
                            relative = Path(os.fsdecode(name))
                            if (
                                any(part.startswith(".") for part in relative.parts)
                                or any(part in ignored for part in relative.parts[:-1])
                                or relative.name.endswith(_TEMP_SUFFIX)
                                or relative.suffix.lstrip(".").lower() not in allowed
                            ):
                                continue
                            path = root / relative
                            if not path.is_symlink():
                                yield path
        code = proc.wait()
        # fd can report unreadable subtrees on stderr with a successful exit.
        # Partial discovery cannot establish absence anywhere in this root.
        if code != 0 or diagnostics or pending:
            detail = os.fsdecode(bytes(diagnostics)).rstrip()
            if pending:
                detail += "\nIncomplete NUL-delimited fd output."
            errors.append(f"{root}: fd discovery failed (exit {code}): {detail}")
            unavailable.add(root)
    except OSError as exc:
        errors.append(f"{root}: fd discovery failed: {exc}. {os.fsdecode(bytes(diagnostics)).rstrip()}")
        unavailable.add(root)
    finally:
        # Force-stop like common.process's cancellation cleanup. communicate
        # drains both pipes and reaps the child without additional reader threads.
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            detail = os.fsdecode(bytes(diagnostics) + (exc.stderr or b"")).rstrip()
            message = f"{root}: fd cleanup did not finish after stopping. {detail}".rstrip()
            raise ChildNotReapedError(message) from exc
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
