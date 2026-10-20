from __future__ import annotations

import threading
from pathlib import Path

import pytest
from common.fs.identity import source_identity
from sqlalchemy import event, select, update

from server.settings import ScannerConfig
from server.fs.source import MediaSnapshot
from server.models import Job, Library, MediaFile, ProbeRow, utcnow
from server.scanner import scan
from server.staging import StagingService
from tests.helpers import database_context

pytestmark = pytest.mark.usefixtures("without_fd")


@pytest.mark.asyncio
@pytest.mark.parametrize("observation", ["readable", "empty", "unreadable", "scoped"])
async def test_scan_marks_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    visible, missing, internal = [root / name for name in ("visible.mkv", "missing.mkv", ".internal.mkv")]
    internal.write_bytes(b"internal")
    if observation in {"readable", "scoped"}:
        visible.write_bytes(b"visible")
    if observation == "unreadable":

        def unavailable(roots, ignored, allowed, errors, unavailable, excluded, **kwargs):
            errors.append("Source storage is unavailable.")
            unavailable.add(root)
            return []

        monkeypatch.setattr(scan, "_wanted_files", unavailable)
    async with database_context("markers-presence") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.flush()
            rows = [
                MediaFile(
                    library_id=library.id,
                    path=str(path),
                    size=1,
                    mtime_ns=1,
                    container="mkv",
                    medium="video",
                    last_scanned_at=utcnow(),
                )
                for path in (visible, missing, internal)
            ]
            session.add_all(rows)
            await session.commit()
            await session.refresh(library)
            revision = library.updated_at

            report = await scan.scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=staging,
                target_paths={visible} if observation == "scoped" else None,
            )
            assert library.scan_id == 1 and library.updated_at == revision
            assert report.pruned == (2 if observation == "readable" else 0)
        async with factory() as session:
            current = list(await session.scalars(select(MediaFile).order_by(MediaFile.id)))
            assert len(current) == (1 if observation == "readable" else 3)
            assert current[0].last_seen_scan_id == int(observation in {"readable", "scoped"})
            assert all(row.last_seen_scan_id == 0 for row in current[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("skipped", ["stat", "owned", "cancelled", "probe_failed", "probe_failed_refresh", "probed"])
async def test_skipped_paths_marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skipped: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    observed = root / "observed.mkv"
    observed.write_bytes(b"observed")
    (root / "readable.mkv").write_bytes(b"readable")
    stat_files = scan._stat_files
    if skipped == "stat":

        def inspect(paths, errors):
            errors.append(f"{observed}: stat failed: unavailable")
            return stat_files([path for path in paths if path != observed], errors)

        monkeypatch.setattr(scan, "_stat_files", inspect)
    async with database_context("markers-skipped") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.flush()
            stat = observed.stat()
            media = MediaFile(
                library_id=library.id,
                path=str(observed),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                source_identity=(
                    {"size": 0} if skipped in {"stat", "owned", "probe_failed_refresh"} else source_identity(stat)
                ),
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
            session.add(media)
            await session.flush()
            if skipped == "owned":
                session.add(Job(library_id=library.id, media_file_id=media.id, status="running"))
            elif skipped == "cancelled":
                media.metadata_cancelled = True
            elif skipped in {"probe_failed", "probe_failed_refresh"}:
                media.health_status = "probe_failed"
            elif skipped == "probed":
                probe = ProbeRow(media_file_id=media.id, raw_json='{"streams": [], "format": {}}')
                session.add(probe)
                await session.flush()
                media.probe_id = probe.id
                media.video_codec = "h264"
            await session.commit()
            await session.refresh(media)
            await session.refresh(library)
            snapshot = MediaSnapshot.from_media(media)
            revision = library.updated_at
            scanned_at = utcnow()
            monkeypatch.setattr(scan, "utcnow", lambda: scanned_at)
            report = await scan.scan_library(session, library, config=ScannerConfig(), staging=staging)
            assert report.pruned == 0 and report.added == 1
            await session.refresh(media)
            assert media.last_seen_scan_id == library.scan_id == 1
            if skipped == "probe_failed_refresh":
                assert media.source_identity == source_identity(stat)
                assert media.updated_at != snapshot.updated_at
            else:
                assert MediaSnapshot.from_media(media) == snapshot
            assert library.updated_at == revision
            if skipped in {"cancelled", "probe_failed", "probe_failed_refresh", "probed"}:
                assert media.last_scanned_at == scanned_at


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discovery", "update"])
async def test_cancelled_retry_monotonic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    for name in ("first.mkv", "second.mkv"):
        (root / name).write_bytes(b"media")
    stopped = threading.Event()

    async def stop(_ids: list[int]) -> None:
        stopped.set()

    monkeypatch.setattr(scan, "COMMIT_BATCH", 1)
    wanted = scan._wanted_files

    def discover(*args, **kwargs):
        paths = wanted(*args, **kwargs)
        if phase == "discovery":
            stopped.set()
        return paths

    monkeypatch.setattr(scan, "_wanted_files", discover)
    async with database_context("markers-retry") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.flush()
            stale = MediaFile(
                library_id=library.id,
                path=str(root / "missing.mkv"),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
            session.add(stale)
            await session.commit()
            await session.refresh(library)
            revision = library.updated_at
            report = await scan.scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=staging,
                stopped=stopped,
                on_files_pending=stop,
            )
            assert report.cancelled and report.pruned == 0
        monkeypatch.setattr(scan, "_wanted_files", wanted)
        async with factory() as session:
            current = await session.get(Library, library.id)
            assert current is not None and current.scan_id == int(phase == "update")
            assert await session.get(MediaFile, stale.id) is not None
            report = await scan.scan_library(session, current, config=ScannerConfig(), staging=staging)
            assert not report.cancelled and report.pruned == 1
            assert current.scan_id == (2 if phase == "update" else 1)
            assert current.updated_at == revision
        async with factory() as session:
            current = await session.get(Library, library.id)
            assert current is not None
            previous = current.scan_id
            report = await scan.scan_library(session, current, config=ScannerConfig(), staging=staging)
            assert report.pruned == 0 and current.scan_id == previous + 1
            assert all(
                marker == current.scan_id for marker in await session.scalars(select(MediaFile.last_seen_scan_id))
            )


@pytest.mark.asyncio
async def test_materializes_only_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(scan, "COMMIT_BATCH", 2)
    async with database_context("markers-materialization") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)], scan_id=3)
            session.add(library)
            await session.flush()
            rows = [
                MediaFile(
                    library_id=library.id,
                    path=str(root / f"{index}.mkv"),
                    last_seen_scan_id=marker,
                    size=1,
                    mtime_ns=1,
                    container="mkv",
                    medium="video",
                    last_scanned_at=utcnow(),
                )
                for index, marker in enumerate((3, 1, 3, 0, 3, 2, 3))
            ]
            session.add_all(rows)
            await session.commit()
        materialized = []

        def loaded(_session, row):
            if isinstance(row, MediaFile):
                materialized.append((row.last_seen_scan_id, row.id))

        sync_class = factory.class_.sync_session_class
        event.listen(sync_class, "loaded_as_persistent", loaded)
        try:
            async with factory() as session:
                assert (
                    await scan._prune_missing(session, library, 3, staging=staging, unavailable=set(), excluded=()) == 3
                )
        finally:
            event.remove(sync_class, "loaded_as_persistent", loaded)
        assert materialized == [(0, rows[3].id), (1, rows[1].id), (2, rows[5].id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["marker", "run"])
async def test_sweep_rechecks_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    async with database_context("markers-check") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)], scan_id=1)
            session.add(library)
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(root / "missing.mkv"),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
            session.add(media)
            await session.commit()
            await session.refresh(media)
            await session.refresh(library)
            snapshot = MediaSnapshot.from_media(media)
            revision = library.updated_at
        prune = staging.prune_catalog

        async def intervening(snapshots, **kwargs):
            async with factory() as writer:
                if change == "marker":
                    await writer.execute(
                        update(MediaFile)
                        .where(MediaFile.id == media.id)
                        .values(last_seen_scan_id=1, updated_at=MediaFile.updated_at)
                    )
                else:
                    await writer.execute(
                        update(Library).where(Library.id == library.id).values(scan_id=2, updated_at=Library.updated_at)
                    )
                await writer.commit()
            return await prune(snapshots, **kwargs)

        monkeypatch.setattr(staging, "prune_catalog", intervening)
        async with factory() as session:
            assert await scan._prune_missing(session, library, 1, staging=staging, unavailable=set(), excluded=()) == 0
        async with factory() as session:
            current = await session.get(MediaFile, media.id)
            current_library = await session.get(Library, library.id)
            assert current is not None and MediaSnapshot.from_media(current) == snapshot
            assert current_library is not None and current_library.updated_at == revision
