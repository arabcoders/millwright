from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from common.fs.identity import source_identity

from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.core.paths import healthcheck_dir
from server.models import FinalizationJournal, Job, JobAttempt, Library, MediaFile, ProbeRow, utcnow
from server.queue.store import create_library
from server.fs.library import LibraryFS, LibraryProbeError
from common.media.tools import ProbeError
from server.probing import ProbeService
from server.scanner import scan
from server.scanner.scan import scan_library
from tests.helpers import database_context
from tests.test_scanner import (
    FAKE_FFPROBE,
    HEVC_4K_PROBE,
    ScanEnv,
    fetch_row,
    run_scan,
    scan_env,  # noqa: F401
    write_media,
    finish_healthchecks,
)


@pytest.mark.asyncio
async def test_internal_sources_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_STATE_PATH", str(tmp_path / "state"))
    workspace, trash = tmp_path / "workspace", tmp_path / "trash"
    paths = [
        workspace / "copy.mkv",
        trash / "original.mkv",
        healthcheck_dir() / "misindexed.mkv",
        tmp_path / "workspace-library" / "movie.mkv",
    ]
    calls: list[Path] = []

    async def probe(_binary, source, **_kwargs):
        calls.append(source)
        return HEVC_4K_PROBE

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    async with database_context("internal-sources") as factory:
        create_app(factory, config=ServerConfig(workspace_dir=str(workspace), trash_dir=str(trash)))
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            rows = []
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"original")
                stat = path.stat()
                row = MediaFile(
                    library_id=library.id,
                    path=str(path),
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    source_identity=source_identity(stat),
                    container="mkv",
                    medium="video",
                    last_scanned_at=utcnow(),
                )
                session.add(row)
                rows.append(row)
            await session.commit()
        fs = LibraryFS(
            library.id, sessionmaker=factory, ffprobe_bin="probe", mkvpropedit_bin="unused", workspace_dir=workspace
        )
        for row in rows[:-1]:
            with pytest.raises(LibraryProbeError) as raised:
                await fs.refresh_current(row.id)
            assert raised.value.reason == "unauthorized"
        await fs.refresh_current(rows[-1].id)
        assert calls == [paths[-1]]
        assert all(path.read_bytes() == b"original" for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["library", "generation"])
async def test_prune_batch_checks(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    monkeypatch.setattr(scan, "COMMIT_BATCH", 1)
    paths = [scan_env.lib_dir / f"{index}.mkv" for index in range(3)]
    async with scan_env.factory() as session:
        rows = [
            MediaFile(
                library_id=scan_env.library_id,
                path=str(path),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
            for path in paths
        ]
        session.add_all(rows)
        await session.commit()
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.scan_id = 1
        await session.commit()
        revision = library.updated_at
        prune = scan_env.staging.prune_catalog
        calls = 0

        async def change_batch(snapshots, **kwargs):
            nonlocal calls
            calls += 1
            assert len(snapshots) == 1 and kwargs["library_updated_at"] == revision
            if calls == 2 and change == "generation":
                async with scan_env.factory() as writer:
                    current = await writer.get(MediaFile, rows[1].id)
                    assert current is not None
                    current.size = 2
                    await writer.commit()
            count = await prune(snapshots, **kwargs)
            if calls == 1 and change == "library":
                async with scan_env.factory() as writer:
                    current = await writer.get(Library, scan_env.library_id)
                    assert current is not None
                    current.name = "edited library"
                    current.updated_at = revision + timedelta(seconds=1)
                    await writer.commit()
            return count

        monkeypatch.setattr(scan_env.staging, "prune_catalog", change_batch)
        count = await scan._prune_missing(session, library, 1, staging=scan_env.staging, unavailable=set(), excluded=())
    assert count == (1 if change == "library" else 2)
    assert calls == (1 if change == "library" else 3)
    assert await fetch_row(scan_env, paths[0]) is None
    retained = await fetch_row(scan_env, paths[1])
    assert retained is not None and retained.size == (1 if change == "library" else 2)
    assert (await fetch_row(scan_env, paths[2]) is not None) == (change == "library")


@pytest.mark.asyncio
async def test_failed_refresh_invalidates(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    await run_scan(scan_env)
    await finish_healthchecks(scan_env)
    original = await fetch_row(scan_env, path)
    assert original is not None and original.probe_id is not None
    path.write_bytes(b"replacement")
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", path.name)

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        with pytest.raises(LibraryProbeError):
            await LibraryFS(
                scan_env.library_id,
                sessionmaker=scan_env.factory,
                ffprobe_bin=FAKE_FFPROBE,
                mkvpropedit_bin="mkvpropedit",
                workspace_dir=scan_env.lib_dir,
            ).refresh_current(original.id)

    async with scan_env.factory() as session:
        current = await session.get(MediaFile, original.id)
        assert current is not None and current.health_status == "probe_failed"
        assert current.probe_id is None
        assert await session.scalar(select(ProbeRow.id).where(ProbeRow.media_file_id == original.id)) is None
    assert path.read_bytes() == b"replacement"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["editor", "reprobe"])
async def test_changed_edit_invalidates(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    calls = 0

    async def probe(*_args, **_kwargs) -> dict:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ProbeError("post-edit probe failed")
        return HEVC_4K_PROBE

    async def edit(*_args, **_kwargs) -> None:
        with path.open("ab") as edited:
            edited.write(b"changed metadata")
        if failure == "editor":
            raise ProbeError("editor failed after changing media")

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    monkeypatch.setattr("server.fs.library.run_mkvpropedit", edit)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        media = (await session.scalars(select(MediaFile))).one()
        with pytest.raises(LibraryProbeError) as failure_error:
            await LibraryFS(
                scan_env.library_id,
                sessionmaker=scan_env.factory,
                ffprobe_bin="fake-probe",
                mkvpropedit_bin="fake-editor",
                workspace_dir=scan_env.lib_dir,
            ).refresh_current(media.id)
        cause = failure_error.value.__cause__
        assert isinstance(cause, ProbeError)

    async with scan_env.factory() as session:
        current = await session.get(MediaFile, media.id)
        assert current is not None
        assert current.probe_id is None
        assert current.health_status == "probe_failed"
        assert list(await session.scalars(select(Job))) == []


@pytest.mark.asyncio
async def test_removed_root_refused(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    allowed = scan_env.lib_dir / "new-root"
    allowed.mkdir()
    effects: list[Path] = []

    async def probe(_binary, source: Path, **_kwargs) -> dict:
        effects.append(source)
        return HEVC_4K_PROBE

    async def edit(_binary, source: Path, *, on_progress=None, **_kwargs) -> None:
        effects.append(source)
        with source.open("ab") as edited:
            edited.write(b"statistics")

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    monkeypatch.setattr("server.fs.library.run_mkvpropedit", edit)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        media = (await session.scalars(select(MediaFile))).one()
        library.roots = [str(allowed)]
        await session.commit()

    await finish_healthchecks(scan_env)
    service = ProbeService(scan_env.factory, "fake-probe", "fake-editor", workspace_dir=scan_env.lib_dir)
    await asyncio.wait_for(service._fill(), 2)
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 2)
    assert effects == []
    assert path.read_bytes() == b"x" * 2048


@pytest.mark.asyncio
async def test_changed_root_stops(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    allowed = scan_env.lib_dir / "new-root"
    allowed.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    edits: list[Path] = []

    async def probe(*_args, **_kwargs) -> dict:
        entered.set()
        await release.wait()
        return HEVC_4K_PROBE

    async def edit(_binary, source: Path, *, on_progress=None, **_kwargs) -> None:
        edits.append(source)
        with source.open("ab") as edited:
            edited.write(b"statistics")

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    monkeypatch.setattr("server.fs.library.run_mkvpropedit", edit)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        media = (await session.scalars(select(MediaFile))).one()

    service = ProbeService(scan_env.factory, "fake-probe", "fake-editor", workspace_dir=scan_env.lib_dir)
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task = service._tasks[media.id]
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            library.roots = [str(allowed)]
            await session.commit()
        await asyncio.wait_for(service.check_library_access(), 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert edits == []
        assert path.read_bytes() == b"x" * 2048
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_stale_failure_preserves(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        media = (await session.scalars(select(MediaFile))).one()
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*_args, **_kwargs) -> dict:
        entered.set()
        await release.wait()
        raise ProbeError("obsolete attempt failed")

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    service = ProbeService(scan_env.factory, "fake-probe", "fake-editor", workspace_dir=scan_env.lib_dir)
    await service._fill()
    task = next(iter(service._tasks.values()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        replacement = scan_env.lib_dir / "replacement"
        replacement.write_bytes(b"new generation")
        replacement.replace(path)
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        release.set()
        await asyncio.wait_for(task, 2)
        current = await fetch_row(scan_env, path)
        assert current is not None and current.size == len(b"new generation")
        assert current.health_status == "pending"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_scan_stat_cache(scan_env: ScanEnv, active: bool) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    await run_scan(scan_env)
    original = await fetch_row(scan_env, path)
    assert original is not None and original.probe_id is not None
    replacement = scan_env.lib_dir / "replacement"
    replacement.write_bytes(b"y" * original.size)
    os.utime(replacement, ns=(original.mtime_ns, original.mtime_ns))
    replacement.replace(path)

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        if not active:
            job = await session.scalar(select(Job).where(Job.media_file_id == original.id))
            assert job is not None
            job.status = "failed"
            await session.commit()
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)

    current = await fetch_row(scan_env, path)
    assert current is not None and current.probe_id == original.probe_id
    assert current.source_identity == (original.source_identity if active else source_identity(path.stat()))
    assert current.first_seen == original.first_seen
    assert current.health_status == "pending"
    assert path.read_bytes() == b"y" * original.size


@pytest.mark.asyncio
async def test_editor_preserves_recovery(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        job = (await session.scalars(select(Job).where(Job.kind == "healthcheck"))).one()
        job.kind = "transcode"
        job.status = "failed"
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
        session.add(attempt)
        await session.flush()
        session.add(
            FinalizationJournal(
                attempt_id=attempt.id,
                source_path=str(path),
                destination_path=str(path),
                partial_path=str(path.with_suffix(".partial")),
                trash_path="",
                state="prepared",
            )
        )
        await session.commit()

    async def edit(*_args, **_kwargs) -> None:
        with path.open("ab") as edited:
            edited.write(b"statistics")

    monkeypatch.setattr("server.fs.library.run_mkvpropedit", edit)
    await run_scan(scan_env, mkvpropedit_bin="fake-editor")
    assert path.read_bytes() == b"x" * 2048
    async with scan_env.factory() as session:
        assert (await session.scalars(select(FinalizationJournal))).one().state == "prepared"
