from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import event, func, select

from common.fs.identity import source_identity
from server.settings import ScannerConfig
from server.models import Library, MediaFile, ProbeRow, utcnow
from server.scanner import scan
from server.staging import StagingService
from tests.helpers import database_context

pytestmark = pytest.mark.usefixtures("without_fd")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["new", "unchanged", "changed", "stats"])
async def test_updates_one_batch(tmp_path: Path, state: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    paths = [root / name for name in ("first.mkv", "second.mkv", "third.mp4")]
    for path in paths:
        path.write_bytes(b"original")
    async with database_context("scan-batches") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as seed:
            library = Library(name="library", roots=[str(root)], mkv_stats=state == "stats")
            seed.add(library)
            await seed.flush()
            media = []
            if state != "new":
                for path in paths:
                    stat = path.stat()
                    row = MediaFile(
                        library_id=library.id,
                        path=str(path),
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        source_identity=source_identity(stat),
                        container=path.suffix[1:],
                        video_codec="h264",
                        medium="movie",
                        health_status="healthy",
                        transcode_status="transcoded",
                        transcode_decided=True,
                        last_scanned_at=utcnow(),
                    )
                    seed.add(row)
                    media.append(row)
                await seed.flush()
                for row in media:
                    probe = ProbeRow(
                        media_file_id=row.id,
                        raw_json=json.dumps({"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}),
                    )
                    seed.add(probe)
                    await seed.flush()
                    row.probe_id = probe.id
            await seed.commit()
        if state == "changed":
            for path in paths:
                path.write_bytes(b"replacement generation")
        statements: list[str] = []
        probe_reads: list[tuple[int, ...]] = []

        def executed(_conn, _cursor, sql, parameters, _context, _many) -> None:
            statements.append(sql)
            if sql.startswith("SELECT") and "FROM probes" in sql:
                probe_reads.append(parameters)

        engine = factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", executed)
        wakes = []
        try:
            async with factory() as session:
                current = await session.get(Library, library.id)
                assert current is not None

                async def wake(_ids: list[int]) -> None:
                    assert not session.in_transaction()
                    wakes.append(True)

                report = await scan.scan_library(
                    session, current, config=ScannerConfig(), staging=staging, on_files_pending=wake
                )
        finally:
            event.remove(engine, "before_cursor_execute", executed)
        # Preparation, catalog updates, and pruning each read the catalog once.
        assert sum(sql.startswith("SELECT") and "FROM media_files" in sql for sql in statements) == 3
        assert sum(sql.startswith("DELETE FROM probes") for sql in statements) == int(state == "changed")
        assert sum(sql.startswith("INSERT INTO media_files") for sql in statements) == int(state == "new")
        assert len(probe_reads) == int(state == "stats")
        if state == "stats":
            assert len(probe_reads[0]) == 2
        changed = 2 if state == "stats" else 3 if state in {"new", "changed"} else 0
        assert report.found == 3 and report.added + report.updated == changed
        assert report.unchanged == 3 - changed
        assert report.pruned == 0 and not report.errors
        assert len(wakes) == int(changed > 0)
        async with factory() as reader:
            rows = list((await reader.scalars(select(MediaFile).order_by(MediaFile.path))).all())
            assert len(rows) == 3
            assert await reader.scalar(select(func.count()).select_from(ProbeRow)) == (
                0 if state == "new" else 3 if state == "stats" else 3 - changed
            )
            for row in rows:
                assert row.last_seen_scan_id == 1
                pending = state in {"new", "changed"} or state == "stats" and row.container == "mkv"
                assert row.health_status == ("pending" if pending else "healthy")
                assert (row.probe_id is None) is pending
                assert row.source_identity == source_identity(Path(row.path).stat())
                if state != "new":
                    assert row.transcode_status == "transcoded" and row.transcode_decided


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["suspended", "roots", "scan"])
async def test_batch_rechecks_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"media")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    stat_files = scan._stat_files

    def inspect(paths, errors):
        files = stat_files(paths, errors)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return files

    monkeypatch.setattr(scan, "_stat_files", inspect)
    async with database_context("batch-library") as factory:
        async with factory() as seed:
            library = Library(name="library", roots=[str(root)])
            seed.add(library)
            await seed.commit()
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        wakes = []

        async def wake(_ids: list[int]) -> None:
            wakes.append(True)

        async def discover():
            async with factory() as session:
                current = await session.get(Library, library.id)
                assert current is not None
                return await scan.scan_library(
                    session,
                    current,
                    config=ScannerConfig(),
                    staging=staging,
                    on_files_pending=wake,
                )

        task = asyncio.create_task(discover())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with factory() as writer:
                current = await writer.get(Library, library.id)
                assert current is not None
                if change == "suspended":
                    current.suspended = True
                elif change == "scan":
                    from sqlalchemy import update

                    await writer.execute(
                        update(Library).where(Library.id == library.id).values(scan_id=2, updated_at=Library.updated_at)
                    )
                else:
                    current.roots = [str(tmp_path / "other")]
                await writer.commit()
            release.set()
            report = await asyncio.wait_for(task, 3)
            assert report.added == report.updated == report.pruned == 0
            assert not wakes
            async with factory() as reader:
                assert await reader.scalar(select(func.count()).select_from(MediaFile)) == 0
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_failed_batch_withholds_wake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"media")
    async with database_context("failed-batch") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as seed:
            library = Library(name="library", roots=[str(root)])
            seed.add(library)
            await seed.commit()
        wakes = []

        async def wake(_ids: list[int]) -> None:
            wakes.append(True)

        async with factory() as session:
            current = await session.get(Library, library.id)
            assert current is not None
            commit = session.commit
            commits = 0
            failure = RuntimeError("Batch commit failed.")

            async def fail_commit() -> None:
                nonlocal commits
                commits += 1
                if commits == 4:
                    raise failure
                await commit()

            monkeypatch.setattr(session, "commit", fail_commit)
            with pytest.raises(RuntimeError) as caught:
                await scan.scan_library(
                    session,
                    current,
                    config=ScannerConfig(),
                    staging=staging,
                    on_files_pending=wake,
                )
            assert caught.value is failure
        assert not wakes
        async with factory() as reader:
            assert await reader.scalar(select(func.count()).select_from(MediaFile)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_overlapping_roots_readable(tmp_path: Path, foreign: bool) -> None:
    root = tmp_path / "media"
    child = root / "child"
    child.mkdir(parents=True)
    path = child / "movie.mkv"
    path.write_bytes(b"original")
    async with database_context("overlapping-roots") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root), str(child)])
            session.add(library)
            if foreign:
                other = Library(name="other", roots=[str(child)])
                session.add(other)
                await session.flush()
                session.add(
                    MediaFile(
                        library_id=other.id,
                        path=str(path),
                        size=path.stat().st_size,
                        mtime_ns=path.stat().st_mtime_ns,
                        container="mkv",
                        video_codec="h264",
                        medium="movie",
                        last_scanned_at=utcnow(),
                    )
                )
            await session.commit()
            report = await scan.scan_library(session, library, config=ScannerConfig(), staging=staging)
            assert report.found == 2 and report.added == (not foreign)
            assert report.pruned == 0 and not report.errors
            assert await session.scalar(select(func.count()).select_from(MediaFile)) == 1


@pytest.mark.asyncio
async def test_duplicate_roots_update(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"media")
    async with database_context("duplicate-roots") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root), str(root)])
            session.add(library)
            await session.commit()
            report = await scan.scan_library(session, library, config=ScannerConfig(), staging=staging)
            assert report.found == 2 and report.added == 1
            assert await session.scalar(select(func.count()).select_from(MediaFile)) == 1


@pytest.mark.asyncio
async def test_foreign_owner_untouched(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    path = root / "movie.mkv"
    path.write_bytes(b"media")
    async with database_context("foreign-owner") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            owner = Library(name="owner", roots=[str(root)])
            scanner = Library(name="scanner", roots=[str(root)], mkv_stats=True)
            session.add_all([owner, scanner])
            await session.flush()
            stat = path.stat()
            row = MediaFile(
                library_id=owner.id,
                path=str(path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                source_identity=source_identity(stat),
                container="mkv",
                video_codec="h264",
                medium="movie",
                health_status="healthy",
                last_scanned_at=utcnow(),
            )
            session.add(row)
            await session.flush()
            probe = ProbeRow(media_file_id=row.id, raw_json="invalid foreign probe")
            session.add(probe)
            await session.flush()
            row.probe_id = probe.id
            await session.commit()
            report = await scan.scan_library(session, scanner, config=ScannerConfig(), staging=staging)
            assert report.found == 1 and report.added == report.updated == report.unchanged == 0
            await session.refresh(row)
            assert row.library_id == owner.id and row.probe_id == probe.id and row.health_status == "healthy"
