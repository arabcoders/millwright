from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.enums import JobStatus, TranscodeStatus
from common.fs.identity import source_identity
from tests.helpers import create_test_app as create_app
from server.api.auth import AuthStack
from server.core.auth import PendingStates, WsTickets
from server.core.config import ServerConfig
from server.settings import ScannerConfig
from server.events import EventBus
from server.healthcheck import HealthcheckService
from server.healthlogs import DiagnosticLog
from server.models import (
    FinalizationJournal,
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NodeRow,
    ProbeRow,
    SessionRow,
    TrashEntry,
    utcnow,
)
from server.nodehub.hub import probe_for_file
from server.queue.enqueue import FlowEnqueueService, _candidate_statement
from server.queue.store import ActiveJobError, claim
from server.scanner import scan
from server.scanner.normalize import normalize, resolution_bucket
from common.media.tools import ProbeError
from server.fs.library import LibraryFS
from server.fs.library import LibraryProbeError
from server.probing import ProbeService
from server.plugins.host import PluginHost
from server.scanner.scan import ScanReport, scan_library
from server.staging import StagingService
from server.trash import store_trash, trash_path
from server.workspace import cleanup_workspace
from tests.helpers import database_context, fd_path, graph_flow, make_test_disk_path, make_test_temp_dir

FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"
FAKE_MKVPROPEDIT = Path(__file__).parent / "fakes" / "mkvpropedit.py"
pytestmark = pytest.mark.usefixtures("without_fd")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
async def test_scan_releases_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, scoped: bool
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "first.mkv").write_bytes(b"media")
    second = root / "second.mkv"
    second.write_bytes(b"media")
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_stat = Path.stat
    progress: list[tuple[int, int, int]] = []

    def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == second and follow_symlinks:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("Scan test did not release filesystem work.")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    async with database_context("scan-writer") as factory:
        async with factory() as seed:
            library = Library(name="library", roots=[str(root)])
            seed.add(library)
            await seed.commit()
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        stack = AuthStack(config=ServerConfig(), sessionmaker=factory, ws_tickets=WsTickets(), pending=PendingStates())

        async def inspect() -> ScanReport:
            async with factory() as session:
                current = await session.get(Library, library.id)
                assert current is not None
                return await scan_library(
                    session,
                    current,
                    config=ScannerConfig(),
                    staging=staging,
                    on_progress=lambda report: progress.append((report.found, report.added, report.updated)),
                    target_paths={root} if scoped else None,
                )

        task = asyncio.create_task(inspect())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert progress[-1] == (2, 0, 0)
            await asyncio.wait_for(stack.cleanup(), 2)
            if cancel:
                task.cancel()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            else:
                report = await asyncio.wait_for(task, 3)
                assert report.found == report.added == 2
            async with factory() as reader:
                assert await reader.scalar(select(func.count()).select_from(MediaFile)) == (0 if cancel else 2)
                assert await reader.scalar(select(func.count()).select_from(SessionRow)) == 0
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
async def test_scan_observation_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, scoped: bool
) -> None:
    from common.fs.identity import source_identity
    from server.scanner import scan as scan_module

    root = tmp_path / "media"
    root.mkdir()
    path = root / "movie.mkv"
    path.write_bytes(b"old")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    stat_files = scan_module._stat_files

    def stale_stat(paths, errors):
        result = stat_files(paths, errors)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return result

    async with database_context("scan-generation") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            if existing:
                await session.flush()
                session.add(
                    MediaFile(
                        library_id=library.id,
                        path=str(path),
                        size=3,
                        mtime_ns=path.stat().st_mtime_ns,
                        source_identity=source_identity(path.stat()),
                        container="mkv",
                        medium="other",
                        last_scanned_at=utcnow(),
                    )
                )
            await session.commit()
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)

        async def inspect() -> ScanReport:
            async with factory() as session:
                current = await session.get(Library, library.id)
                assert current is not None
                return await scan_library(
                    session, current, config=ScannerConfig(), staging=staging, target_paths={path} if scoped else None
                )

        monkeypatch.setattr(scan_module, "_stat_files", stale_stat)
        task = asyncio.create_task(inspect())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            path.write_bytes(b"replacement generation")
            identity = source_identity(path.stat())
            async with factory() as writer:
                current = await writer.scalar(select(MediaFile).where(MediaFile.path == str(path)))
                if current is None:
                    current = MediaFile(
                        library_id=library.id, path=str(path), container="mkv", medium="other", last_scanned_at=utcnow()
                    )
                    writer.add(current)
                current.size, current.mtime_ns, current.source_identity = (
                    identity["size"],
                    path.stat().st_mtime_ns,
                    identity,
                )
                current.health_status = "healthy"
                await writer.commit()
            release.set()
            report = await asyncio.wait_for(task, 3)
            assert report.updated == report.added == 0
            async with factory() as reader:
                current = await reader.scalar(select(MediaFile).where(MediaFile.path == str(path)))
                assert current is not None and current.source_identity == identity
                assert current.last_seen_scan_id == 1
                assert current.size == len(b"replacement generation") and current.health_status == "healthy"
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


HEVC_4K_PROBE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "hevc",
            "codec_type": "video",
            "profile": "Main 10",
            "pix_fmt": "yuv420p10le",
            "width": 3840,
            "height": 2160,
            "disposition": {"default": 1},
            "tags": {"language": "und"},
        },
        {
            "index": 1,
            "codec_name": "truehd",
            "codec_type": "audio",
            "channels": 8,
            "bit_rate": "4000000",
            "disposition": {"default": 1},
            "tags": {"language": "eng", "title": "Surround 7.1"},
        },
        {
            "index": 2,
            "codec_name": "subrip",
            "codec_type": "subtitle",
            "disposition": {"forced": 1},
            "tags": {"language": "eng"},
        },
    ],
    "format": {"duration": "3000.5", "size": "20000000000", "bit_rate": "50000000"},
}

H264_NO_BITRATE_PROBE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "profile": "High",
            "pix_fmt": "yuv420p",
            "width": 1920,
            "height": 1080,
            "disposition": {},
            "tags": {},
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "channels": 2,
            "disposition": {},
            "tags": {"language": "eng"},
        },
    ],
    "format": {"duration": "100.0", "size": "2048"},
}


@dataclass
class ScanEnv:
    factory: async_sessionmaker[AsyncSession]
    library_id: int
    lib_dir: Path
    probe_log: Path
    config: ScannerConfig
    staging: StagingService


@pytest_asyncio.fixture
async def scan_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ScanEnv]:
    lib_dir = make_test_temp_dir("scanner-lib")
    probe_log = make_test_disk_path(f"probe-log-{uuid.uuid4().hex}.txt")
    monkeypatch.setenv("FAKE_FFPROBE_LOG", str(probe_log))
    async with database_context("scanner") as factory:
        async with factory() as session:
            library = Library(name="lib", roots=[str(lib_dir)], healthcheck_enabled=True)
            session.add(library)
            await session.commit()
            library_id = library.id
        config = ScannerConfig(ignore_dirs=["Extras"])
        yield ScanEnv(
            factory=factory,
            library_id=library_id,
            lib_dir=lib_dir,
            probe_log=probe_log,
            config=config,
            staging=StagingService(factory, make_test_temp_dir("scanner-workspace"), min_free_gb=1),
        )


class Tick:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def write_media(directory: Path, name: str, sidecar: dict | None = None) -> Path:
    path = directory / name
    path.write_bytes(b"x" * 2048)
    if sidecar is not None:
        Path(f"{path}.probe.json").write_text(json_sidecar(sidecar), encoding="utf-8")
    return path


def json_sidecar(sidecar: dict) -> str:
    return json.dumps(sidecar)


async def finish_healthchecks(env: ScanEnv) -> None:
    async with env.factory() as session:
        jobs = list(await session.scalars(select(Job).where(Job.kind == "healthcheck")))
        for job in jobs:
            job.status = JobStatus.SUCCEEDED.value
        await session.commit()


def with_video_codec(sidecar: dict, codec: str) -> dict:
    changed = json.loads(json.dumps(sidecar))
    changed["streams"][0] = dict(changed["streams"][0], codec_name=codec)
    return changed


def build_tree(lib_dir: Path) -> dict[str, Path]:
    movies = lib_dir / "movies"
    movies.mkdir()
    extras = lib_dir / "Extras"
    extras.mkdir()
    (extras / "sample.mkv").write_bytes(b"x")
    (lib_dir / "notes.txt").write_bytes(b"x")
    (lib_dir / "junk.partial").write_bytes(b"x")
    return {
        "a": write_media(movies, "movie_a.mkv", HEVC_4K_PROBE),
        "b": write_media(movies, "movie_b.mp4", H264_NO_BITRATE_PROBE),
        "c": write_media(movies, "show_s1e1.mkv"),
    }


async def run_scan(
    env: ScanEnv,
    config: ScannerConfig | None = None,
    mkvpropedit_bin: str | Path = "mkvpropedit",
    workspace_dir: Path | None = None,
    trash_dir: Path | None = None,
    target_paths: set[Path] | None = None,
    probe_pending: bool = True,
) -> ScanReport:
    async with env.factory() as session:
        library = await session.get(Library, env.library_id)
        assert library is not None
        report = await scan_library(
            session,
            library,
            config=config or env.config,
            staging=env.staging if workspace_dir is None else StagingService(env.factory, workspace_dir, min_free_gb=1),
            trash_dir=trash_dir,
            target_paths=target_paths,
        )
        await session.commit()
        pending = list(
            await session.execute(
                select(MediaFile.id, MediaFile.path).where(
                    MediaFile.library_id == library.id,
                    MediaFile.probe_id.is_(None),
                    MediaFile.health_status == "pending",
                )
            )
        )
        for media_id, path in pending if probe_pending else []:
            try:
                from tests.helpers import probe_media

                result = await probe_media(
                    LibraryFS(
                        library.id,
                        sessionmaker=env.factory,
                        ffprobe_bin=FAKE_FFPROBE,
                        mkvpropedit_bin=mkvpropedit_bin,
                        workspace_dir=workspace_dir or Path("."),
                    ),
                    media_id,
                )
            except (LibraryProbeError, ActiveJobError) as exc:
                report.errors.append(f"{path}: {exc}")
            else:
                report.errors.extend(f"{result.media.path}: {message}" for message in result.diagnostics)
        return report


async def fetch_row(env: ScanEnv, path: Path) -> MediaFile | None:
    async with env.factory() as session:
        return (await session.execute(select(MediaFile).where(MediaFile.path == str(path)))).scalar_one_or_none()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["target.mkv", "movie.TdarrCacheFile-cache.mkv"])
async def test_scoped_file_changes(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    target = write_media(scan_env.lib_dir, name)
    sibling = write_media(scan_env.lib_dir, "sibling.mkv")
    await run_scan(scan_env)
    retained = await fetch_row(scan_env, sibling)
    assert retained is not None
    walks: list[Path] = []
    walk = os.walk

    def record(root, **kwargs):
        walks.append(Path(root))
        return walk(root, **kwargs)

    monkeypatch.setattr(os, "walk", record)
    target.write_bytes(b"changed media")
    report = await run_scan(scan_env, target_paths={target})
    assert (report.found, report.updated, report.unchanged, report.pruned) == (1, 1, 0, 0)
    added = write_media(scan_env.lib_dir, "added.mkv")
    report = await run_scan(scan_env, target_paths={added})
    assert (report.found, report.added, report.pruned) == (1, 1, 0)
    target.unlink()
    report = await run_scan(scan_env, target_paths={target})
    assert (report.found, report.pruned, report.errors) == (0, 1, [])
    assert await fetch_row(scan_env, target) is None
    current = await fetch_row(scan_env, sibling)
    assert current is not None
    assert (current.last_scanned_at, current.probe_id, current.first_seen) == (
        retained.last_scanned_at,
        retained.probe_id,
        retained.first_seen,
    )
    assert walks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [False, True])
async def test_outside_scope_ancestor(scan_env: ScanEnv, service: bool) -> None:
    from server.scanner.service import ScannerService

    target = write_media(scan_env.lib_dir, "movie.mkv")
    scopes = {scan_env.lib_dir.parent, target}
    if service:
        scanner = ScannerService(scan_env.factory, staging=scan_env.staging)
        try:
            scanner.queue_scan(scan_env.library_id, "scoped", target_paths=scopes)
            report = await asyncio.wait_for(scanner._scan_tasks[scan_env.library_id], 2)
        finally:
            await asyncio.wait_for(scanner.aclose(), 3)
    else:
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            report = await scan_library(
                session, library, config=scan_env.config, staging=scan_env.staging, target_paths=scopes
            )
    assert report is not None and report.found == report.added == 1 and not report.errors
    assert await fetch_row(scan_env, target) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["add", "rename", "delete", "empty"])
async def test_scoped_directory_changes(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    root = scan_env.lib_dir
    branch, sibling = root / "branch.directory", root / "sibling"
    branch.mkdir()
    sibling.mkdir()
    target = write_media(branch, "movie.mkv")
    other = write_media(sibling, "other.mkv")
    await run_scan(scan_env)
    retained = await fetch_row(scan_env, other)
    assert retained is not None
    new = root / "new"
    if change == "add":
        new.mkdir()
        write_media(new, "movie.mkv")
        targets = {new, new / "movie.mkv"}
    elif change == "rename":
        branch.rename(new)
        targets = {branch, new, new / "movie.mkv"}
    else:
        target.unlink()
        if change == "delete":
            branch.rmdir()
        targets = {branch}
    walks: list[Path] = []
    walk = os.walk

    def record(directory, **kwargs):
        walks.append(Path(directory))
        return walk(directory, **kwargs)

    monkeypatch.setattr(os, "walk", record)
    report = await run_scan(scan_env, target_paths=targets)
    assert not report.errors
    assert (report.found, report.added, report.pruned) == (
        int(change in {"add", "rename"}),
        int(change in {"add", "rename"}),
        int(change != "add"),
    )
    assert walks == ([new] if change in {"add", "rename"} else [branch] if change == "empty" else [])
    current = await fetch_row(scan_env, other)
    assert current is not None and current.last_scanned_at == retained.last_scanned_at
    assert (await fetch_row(scan_env, target) is not None) == (change == "add")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["root", "child", "deleted", "file"])
async def test_scoped_empty_storage(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    root = scan_env.lib_dir
    child = root / "branch"
    child.mkdir()
    media = write_media(child, "movie.mkv")
    await run_scan(scan_env)
    retained = await fetch_row(scan_env, media)
    assert retained is not None
    media.unlink()
    if scope in {"root", "deleted"}:
        child.rmdir()
    walks: list[Path] = []
    walk = os.walk

    def record(directory, **kwargs):
        walks.append(Path(directory))
        return walk(directory, **kwargs)

    monkeypatch.setattr(os, "walk", record)
    target = root if scope == "root" else media if scope == "file" else child
    report = await run_scan(scan_env, target_paths={target})
    assert report.found == 0 and report.pruned == int(scope != "root")
    assert walks == ([root] if scope == "root" else [child] if scope == "child" else [])
    current = await fetch_row(scan_env, media)
    if scope == "root":
        assert len(report.errors) == 1
        assert current is not None and current.probe_id == retained.probe_id
    else:
        assert report.errors == [] and current is None


@pytest.mark.asyncio
async def test_scope_sql_boundaries(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.dialects import postgresql

    paths = []
    for name in ("Show_%\\x", "Show_AXx", "Show_%\\x-extra", "show_%\\x"):
        directory = scan_env.lib_dir / name
        directory.mkdir()
        paths.append(write_media(directory, "movie.mkv"))
    await run_scan(scan_env)
    unrelated = paths[0].parent / "unrelated.mkv"
    async with scan_env.factory() as session:
        library = Library(name="other", roots=[str(scan_env.lib_dir / "unrelated")])
        session.add(library)
        await session.flush()
        session.add(
            MediaFile(
                library_id=library.id,
                path=str(unrelated),
                size=3,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
        )
        await session.commit()
    for path in paths:
        path.unlink()
    execute = scan_env.factory.class_.execute
    queries: list[str] = []

    async def capture(session, statement, *args, **kwargs):
        sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        if statement.is_select and "ORDER BY media_files.path" in sql:
            queries.append(sql)
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(scan_env.factory.class_, "execute", capture)
    report = await run_scan(scan_env, target_paths={paths[0].parent})
    assert report.pruned == 1 and not report.errors
    assert await fetch_row(scan_env, paths[0]) is None
    assert all([await fetch_row(scan_env, path) is not None for path in paths[1:]])
    assert await fetch_row(scan_env, unrelated) is not None
    assert len(queries) == 2


@pytest.mark.asyncio
async def test_scope_prune_batches(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    branch = scan_env.lib_dir / "branch"
    branch.mkdir()
    monkeypatch.setattr(scan, "COMMIT_BATCH", 1)
    async with scan_env.factory() as session:
        other = Library(name="other", roots=[str(scan_env.lib_dir / "other")])
        session.add(other)
        await session.flush()
        paths = [branch / name for name in ("a.mkv", "b.mkv", "c.mkv", "nested/d.mkv")]
        for index, path in enumerate(paths):
            session.add(
                MediaFile(
                    library_id=scan_env.library_id if index % 2 else other.id,
                    path=str(path),
                    size=3,
                    mtime_ns=1,
                    container="mkv",
                    medium="video",
                    last_scanned_at=utcnow(),
                )
            )
        await session.commit()
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        report = await scan_library(
            session, library, config=scan_env.config, staging=scan_env.staging, target_paths={branch, branch / "nested"}
        )
    assert report.pruned == 2 and not report.errors
    assert await fetch_row(scan_env, paths[0]) is not None
    assert await fetch_row(scan_env, paths[2]) is not None
    assert await fetch_row(scan_env, paths[1]) is None
    assert await fetch_row(scan_env, paths[3]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("excluded_only", [False, True])
async def test_catalog_batches(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, scoped: bool, excluded_only: bool):
    monkeypatch.setattr(scan, "COMMIT_BATCH", 2)
    branch = scan_env.lib_dir / "branch"
    branch.mkdir()
    paths = [branch / f"{index}.mkv" for index in range(5)]
    seen_path = branch / "seen.mkv"
    unavailable_path = branch / "offline" / "missing.mkv"
    async with scan_env.factory() as session:
        session.add_all(
            MediaFile(
                library_id=scan_env.library_id,
                path=str(path),
                last_seen_scan_id=int(path == seen_path),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=utcnow(),
            )
            for path in [*paths, seen_path, unavailable_path]
        )
        await session.commit()
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        batches = []
        library.scan_id = 1
        await session.commit()
        prune = scan_env.staging.prune_catalog

        async def capture(snapshots, **kwargs):
            batches.append([row.path for row in snapshots])
            return await prune(snapshots, **kwargs)

        monkeypatch.setattr(scan_env.staging, "prune_catalog", capture)
        count = await scan._prune_missing(
            session,
            library,
            1,
            staging=scan_env.staging,
            unavailable={branch if excluded_only else unavailable_path.parent},
            excluded=tuple(paths) if excluded_only else (),
            scopes=[(branch, True)] if scoped else None,
        )
    assert count == 5
    assert sorted(path for batch in batches for path in batch) == sorted(map(str, paths))
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert all([await fetch_row(scan_env, path) is None for path in paths])
    assert await fetch_row(scan_env, seen_path) is not None
    assert await fetch_row(scan_env, unavailable_path) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "root", "subtree", "stat"])
async def test_scoped_storage_failure(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    root = scan_env.lib_dir
    branch = root / "branch"
    branch.mkdir()
    target = write_media(branch, "movie.mkv")
    await run_scan(scan_env)
    retained = await fetch_row(scan_env, target)
    assert retained is not None
    target.unlink()
    scandir, path_stat = os.scandir, Path.stat
    saved = root.with_name(root.name + "-saved")
    if failure == "missing":
        root.rename(saved)

    def unavailable(directory):
        path = fd_path("", directory) if isinstance(directory, int) else Path(directory)
        if path == (branch if failure == "subtree" else root):
            raise PermissionError(13, "unavailable", str(path))
        return scandir(directory)

    def broken_stat(path, *, follow_symlinks=True):
        if path == target and follow_symlinks:
            raise OSError(5, "I/O failure", str(path))
        return path_stat(path, follow_symlinks=follow_symlinks)

    try:
        with monkeypatch.context() as patch:
            if failure in {"root", "subtree"}:
                patch.setattr(os, "scandir", unavailable)
            elif failure == "stat":
                patch.setattr(Path, "stat", broken_stat)
            async with scan_env.factory() as session:
                library = await session.get(Library, scan_env.library_id)
                assert library is not None
                report = await scan_library(
                    session, library, config=scan_env.config, staging=scan_env.staging, target_paths={target}
                )
        assert report.pruned == 0 and report.errors
        current = await fetch_row(scan_env, target)
        assert current is not None and current.probe_id == retained.probe_id
    finally:
        if failure == "missing":
            saved.rename(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["outside", "hidden", "ignored", "temp", "symlink", "excluded", "text"])
async def test_rejects_invalid_scopes(scan_env: ScanEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str):
    root = scan_env.lib_dir
    target = root / "target.mkv"
    config = scan_env.config
    if invalid == "outside":
        target = tmp_path / "outside.mkv"
    elif invalid == "hidden":
        target = root / ".hidden" / "movie.mkv"
    elif invalid == "ignored":
        target = root / "Extras" / "movie.mkv"
    elif invalid == "temp":
        target = root / "movie.mkv.partial"
        config = ScannerConfig(containers=["partial"])
    elif invalid == "symlink":
        (root / "linked").symlink_to(tmp_path, target_is_directory=True)
        target = root / "linked" / "movie.mkv"
    elif invalid == "excluded":
        scan_env.staging.workspace_dir = root / "workspace"
        target = root / "workspace" / "movie.mkv"
    elif invalid == "text":
        target = root / "readme.txt"
        target.write_text("not media")
    async with scan_env.factory() as session:
        row = MediaFile(
            library_id=scan_env.library_id,
            path=str(target),
            size=3,
            mtime_ns=1,
            container="mkv",
            medium="video",
            last_scanned_at=utcnow(),
        )
        session.add(row)
        await session.commit()
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        walks: list[Path] = []

        def walk(path, **_kwargs):
            walks.append(Path(path))
            return iter(())

        monkeypatch.setattr(os, "walk", walk)
        report = await scan_library(session, library, config=config, staging=scan_env.staging, target_paths={target})
        assert (report.found, report.pruned, report.errors, walks) == (0, 0, [], [])
        assert await session.get(MediaFile, row.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("pruning", [False, True])
async def test_scoped_cancel_preserves(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, pruning: bool) -> None:
    from server import staging as staging_module

    branch = scan_env.lib_dir / "branch"
    branch.mkdir()
    target = write_media(branch, "movie.mkv")
    await run_scan(scan_env)
    target.unlink()
    stopped = threading.Event()
    original = staging_module._catalog_prunable if pruning else scan._wanted_files

    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped.set()
        return result

    monkeypatch.setattr(
        staging_module if pruning else scan, "_catalog_prunable" if pruning else "_wanted_files", cancel
    )
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        report = await scan_library(
            session, library, config=scan_env.config, staging=scan_env.staging, target_paths={branch}, stopped=stopped
        )
    assert report.cancelled and report.pruned == 0
    assert await fetch_row(scan_env, target) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_discovery_health_policy(scan_env: ScanEnv, enabled: bool) -> None:
    path = write_media(scan_env.lib_dir, "fresh.mkv", HEVC_4K_PROBE)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.healthcheck_enabled = enabled
        await session.commit()
    report = await run_scan(scan_env)
    assert report.added == 1 and not report.errors
    async with scan_env.factory() as session:
        row = (await session.scalars(select(MediaFile))).one()
        assert row.probe_id is not None and row.video_codec == "hevc"
        assert row.health_status == ("pending" if enabled else "healthy")
        assert row.health_source == (None if enabled else {"path": str(path), **source_identity(path.stat())})
        jobs = list(await session.scalars(select(Job).where(Job.kind == "healthcheck")))
        assert len(jobs) == int(enabled)
        if enabled:
            assert jobs[0].kind == "healthcheck" and jobs[0].status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_interval", [0, 0.5], ids=["immediate", "coalesced"])
async def test_probe_publishes_healthcheck(
    scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, batch_interval: float
) -> None:
    # Intermediate updates can flush before the follow-up job is published.
    monkeypatch.setattr("server.events.EVENT_BATCH_INTERVAL", batch_interval)
    media = write_media(scan_env.lib_dir, "queued.mkv", HEVC_4K_PROBE)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        await session.commit()
        row = (await session.execute(select(MediaFile).where(MediaFile.path == str(media)))).scalar_one()
        assert row is not None

    bus = EventBus()
    subscription = bus.subscribe()
    service = ProbeService(
        scan_env.factory,
        FAKE_FFPROBE,
        FAKE_MKVPROPEDIT,
        events=bus,
        workspace_dir=scan_env.staging.workspace_dir,
    )
    try:
        await service._fill()
        await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        async with scan_env.factory() as session:
            healthcheck = await session.scalar(
                select(Job).where(Job.media_file_id == row.id, Job.kind == "healthcheck")
            )
            assert healthcheck is not None and healthcheck.status == JobStatus.QUEUED
        bus.flush_bulk()
        payload = None
        async with asyncio.timeout(2):
            while payload is None:
                event = await subscription.next()
                assert event is not None
                if event.type == "events.batch":
                    payload = next(
                        (
                            inner["payload"]
                            for inner in event.payload["events"]
                            if inner["type"] == "job.status" and inner["payload"]["job_id"] == healthcheck.id
                        ),
                        None,
                    )
        assert payload["status"] == JobStatus.QUEUED.value
        assert payload["job"]["kind"] == "healthcheck"
        assert payload["job"]["media_file_id"] == row.id
    finally:
        try:
            await asyncio.wait_for(service.close(), 3)
        finally:
            bus.unsubscribe(subscription)
            await bus.aclose()


def touch(path: Path, mtime_ns: int, size: int) -> None:
    path.write_bytes(b"y" * size)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_resolution_buckets() -> None:
    assert resolution_bucket(None, 1080) is None
    assert resolution_bucket(1920, None) is None
    assert resolution_bucket(99, 99) is None
    assert resolution_bucket(100, 100) == "480p"
    assert resolution_bucket(720, 576) == "576p"
    assert resolution_bucket(1280, 720) == "720p"
    assert resolution_bucket(1920, 1080) == "1080p"
    assert resolution_bucket(3840, 2160) == "4KUHD"
    assert resolution_bucket(4500, 2376) == "DCI4K"
    assert resolution_bucket(7680, 4320) == "8KUHD"
    assert resolution_bucket(9000, 6000) == "other"


@pytest.mark.asyncio
async def test_scans_tree_counts(scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    report = await run_scan(scan_env)

    assert (report.found, report.added, report.updated, report.unchanged, report.pruned) == (3, 3, 0, 0, 0)
    assert report.errors == []

    async with scan_env.factory() as session:
        all_paths = (await session.execute(select(MediaFile.path))).scalars().all()
        assert len(all_paths) == 3
        assert all("Extras" not in p and not p.endswith(".txt") and not p.endswith(".partial") for p in all_paths)
        jobs = (await session.execute(select(Job).where(Job.kind == "healthcheck"))).scalars().all()
        assert len(jobs) == 3
        assert {job.kind for job in jobs} == {"healthcheck"}
        assert {job.media_file_id for job in jobs} == {
            row_id for row_id in (await session.execute(select(MediaFile.id))).scalars()
        }

        a = (await session.execute(select(MediaFile).where(MediaFile.path == str(paths["a"])))).scalar_one()
        assert a.container == "mkv"
        assert a.video_codec == "hevc"
        assert (a.width, a.height) == (3840, 2160)
        assert a.resolution_bucket == "4KUHD"
        assert a.medium == "video"
        assert a.duration_s == 3000.5
        assert a.bit_rate == 50_000_000
        assert a.transcode_status == "pending"
        assert a.health_status == "pending"
        assert a.probe_id is not None

        b = (await session.execute(select(MediaFile).where(MediaFile.path == str(paths["b"])))).scalar_one()
        assert b.container == "mp4"
        assert b.resolution_bucket == "1080p"
        assert b.bit_rate == 164  # estimated from 2048 bytes * 8 / 100.0s

        c = (await session.execute(select(MediaFile).where(MediaFile.path == str(paths["c"])))).scalar_one()
        assert c.video_codec == "h264"
        assert c.resolution_bucket == "1080p"
        assert c.duration_s == 600.0
        assert c.bit_rate == 1_333_333

        probe = await session.get(ProbeRow, a.probe_id)
        assert probe is not None
        assert probe.media_file_id == a.id
        raw = json.loads(probe.raw_json)
        assert raw["streams"][0]["codec_name"] == "hevc"
        assert raw["format"]["duration"] == "3000.5"
        assert probe.created_at is not None
        rebuilt = await probe_for_file(session, a)
        assert rebuilt is not None and rebuilt.raw == HEVC_4K_PROBE
        assert rebuilt.video[0].profile == "Main 10"
        assert rebuilt.video[0].pix_fmt == "yuv420p10le"

        default_probe = await session.get(ProbeRow, c.probe_id)
        assert default_probe is not None
        assert json.loads(default_probe.raw_json)["format"]["bit_rate"] == "1333333"


@pytest.mark.asyncio
async def test_skips_hidden_media(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    visible = write_media(scan_env.lib_dir, "movie.mkv")
    hidden = scan_env.lib_dir / ".hidden"
    nested = hidden / "nested"
    nested.mkdir(parents=True)
    hidden_files = [
        write_media(scan_env.lib_dir, ".movie.mkv"),
        write_media(scan_env.lib_dir, ".7.part"),
        write_media(nested, "movie.mkv"),
    ]
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.roots = [str(scan_env.lib_dir), str(hidden)]
        await session.commit()
    visited: list[Path] = []
    original_walk = os.walk

    def walk(*args, **kwargs):
        for entry in original_walk(*args, **kwargs):
            visited.append(Path(entry[0]))
            yield entry

    monkeypatch.setattr("server.scanner.scan.os.walk", walk)
    report = await run_scan(scan_env, config=ScannerConfig(containers=["mkv", "part"]))
    assert report.found == report.added == 1 and not report.errors
    assert visited == [scan_env.lib_dir]
    async with scan_env.factory() as session:
        assert list(await session.scalars(select(MediaFile.path))) == [str(visible)]
        jobs = list(await session.scalars(select(Job).where(Job.kind == "healthcheck")))
        assert len(jobs) == 1 and jobs[0].kind == "healthcheck"
    assert all(path.exists() for path in hidden_files)


@pytest.mark.asyncio
async def test_skips_symlinked_media(scan_env: ScanEnv) -> None:
    external = make_test_temp_dir("scanner-external") / "outside.mkv"
    write_media(external.parent, external.name, HEVC_4K_PROBE)
    link = scan_env.lib_dir / "outside.mkv"
    link.symlink_to(external)

    report = await run_scan(scan_env)

    assert report.found == 0
    assert await fetch_row(scan_env, link) is None
    assert not scan_env.probe_log.exists()


def test_normalize_streams() -> None:
    path = Path("/lib/movie.mkv")
    stat = os.stat_result((0, 1, 2, 3, 4, 5, 20_480, 0, 0, 0))

    norm = normalize(path, stat, HEVC_4K_PROBE)

    assert norm.probe.size_bytes == 20_480
    assert norm.probe.duration_s == 3000.5
    assert norm.probe.file_path == str(path)
    video, audio, subtitle = norm.probe.streams
    assert (video.index, video.codec, video.codec_type) == (0, "hevc", "video")
    assert video.ten_bit is True
    assert video.language == "und"
    assert (audio.language, audio.channels, audio.bit_rate) == ("eng", 8, 4_000_000)
    assert audio.ten_bit is None
    assert audio.disposition_default is True
    assert subtitle.disposition_forced is True
    assert subtitle.is_descriptive is False
    assert (norm.container, norm.video_codec, norm.medium) == ("mkv", "hevc", "video")
    assert (norm.width, norm.height, norm.resolution_bucket) == (3840, 2160, "4KUHD")


def test_normalize_audio_only() -> None:
    path = Path("/lib/album.mp3")
    stat = os.stat_result((0, 1, 2, 3, 4, 5, 1_000, 0, 0, 0))

    raw = {
        "streams": [
            {"index": 0, "codec_name": "mp3", "codec_type": "audio", "channels": 2, "tags": {"language": "DEU"}}
        ],
        "format": {"duration": "0"},  # zero duration: no bit-rate estimate possible
    }

    norm = normalize(path, stat, raw)

    assert norm.medium == "audio"
    assert norm.video_codec is None
    assert norm.resolution_bucket is None
    assert norm.duration_s == 0.0
    assert norm.bit_rate is None
    assert norm.probe.streams[0].language == "deu"


@pytest.mark.asyncio
@pytest.mark.parametrize("codec", [None, "hevc"])
async def test_rescan_preserves_failure(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, codec: str | None) -> None:
    broken = write_media(scan_env.lib_dir, "broken.mkv", HEVC_4K_PROBE)
    healthy = write_media(scan_env.lib_dir, "healthy.mkv", HEVC_4K_PROBE)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.healthcheck_enabled = False
        await session.commit()
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", broken.name)
    initial = await run_scan(scan_env)
    assert initial.added == 2 and len(initial.errors) == 1
    failed = await fetch_row(scan_env, broken)
    valid = await fetch_row(scan_env, healthy)
    assert failed is not None and failed.health_status == "probe_failed"
    assert valid is not None and valid.probe_id is not None
    first_seen, identity = failed.first_seen, failed.source_identity
    async with scan_env.factory() as session:
        row = await session.get(MediaFile, failed.id)
        assert row is not None
        row.video_codec = codec
        row.transcode_status = TranscodeStatus.TRANSCODED.value
        row.transcode_decided = True
        await session.commit()

    for remains_broken in (True, False):
        monkeypatch.setenv("FAKE_FFPROBE_FAIL", broken.name if remains_broken else "")
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            report = await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        assert (report.found, report.added, report.updated, report.unchanged) == (2, 0, 0, 2)
        queued = await fetch_row(scan_env, broken)
        assert queued is not None and queued.health_status == "probe_failed" and queued.probe_id is None
        assert queued.first_seen == first_seen and queued.source_identity == identity
        assert queued.transcode_status == TranscodeStatus.TRANSCODED.value and queued.transcode_decided
        unchanged = await fetch_row(scan_env, healthy)
        assert unchanged is not None and unchanged.probe_id == valid.probe_id

        service = ProbeService(
            scan_env.factory,
            FAKE_FFPROBE,
            FAKE_MKVPROPEDIT,
            workspace_dir=scan_env.staging.workspace_dir,
        )
        try:
            await service._fill()
            assert not service._tasks
        finally:
            await asyncio.wait_for(service.close(), 3)
    assert scan_env.probe_log.read_text(encoding="utf-8").splitlines().count(str(broken)) == 1
    assert scan_env.probe_log.read_text(encoding="utf-8").splitlines().count(str(healthy)) == 1

    broken.write_bytes(b"replacement")
    report = await run_scan(scan_env)
    assert (report.updated, report.unchanged) == (1, 1)
    retried = await fetch_row(scan_env, broken)
    assert retried is not None and retried.health_status == "healthy" and retried.probe_id is not None
    assert scan_env.probe_log.read_text(encoding="utf-8").splitlines().count(str(broken)) == 2


@pytest.mark.asyncio
async def test_repairs_empty_codec(scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    row = await fetch_row(scan_env, paths["a"])
    assert row is not None
    async with scan_env.factory() as session:
        stored = await session.get(MediaFile, row.id)
        assert stored is not None
        stored.video_codec = ""
        await session.commit()

    first = scan_env.probe_log.read_text(encoding="utf-8").splitlines()
    report = await run_scan(scan_env)

    assert (report.updated, report.unchanged) == (1, 2)
    repaired = await fetch_row(scan_env, paths["a"])
    assert repaired is not None and repaired.video_codec == "hevc"
    assert len(scan_env.probe_log.read_text(encoding="utf-8").splitlines()) == len(first) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TranscodeStatus.NOT_NEEDED, TranscodeStatus.TRANSCODED])
async def test_updates_changed_file(scan_env: ScanEnv, status: TranscodeStatus) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)

    async with scan_env.factory() as session:
        media = await session.scalar(select(MediaFile).where(MediaFile.path == str(paths["b"])))
        assert media is not None
        media.transcode_status = status.value
        media.transcode_decided = True
        await session.commit()

    Path(f"{paths['b']}.probe.json").write_text(
        json_sidecar(with_video_codec(H264_NO_BITRATE_PROBE, "hevc")), encoding="utf-8"
    )
    touch(paths["b"], mtime_ns=1_700_000_000_000_000_000, size=4096)

    report = await run_scan(scan_env)

    assert (report.added, report.updated, report.unchanged) == (0, 1, 2)
    row = await fetch_row(scan_env, paths["b"])
    assert row is not None
    assert row.video_codec == "hevc"
    assert row.size == 4096
    assert row.mtime_ns == 1_700_000_000_000_000_000
    assert row.probe_id is not None
    assert row.transcode_status == (
        status.value if status == TranscodeStatus.TRANSCODED else TranscodeStatus.PENDING.value
    )
    assert row.transcode_decided == (status == TranscodeStatus.TRANSCODED)
    async with scan_env.factory() as session:
        probe = await session.get(ProbeRow, row.probe_id)
        assert probe is not None
        assert '"hevc"' in probe.raw_json
        assert probe.media_file_id == row.id


@pytest.mark.asyncio
async def test_prunes_vanished_file(scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)

    async with scan_env.factory() as session:
        job = (
            await session.execute(
                select(Job)
                .join(MediaFile, Job.media_file_id == MediaFile.id)
                .where(MediaFile.path == str(paths["c"]), Job.kind == "healthcheck")
            )
        ).scalar_one()
        job.status = JobStatus.FAILED.value
        job.kind = "transcode"
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
        session.add(attempt)
        await session.flush()
        session.add_all(
            [
                JobEvent(job_id=job.id, type="failed"),
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path="a",
                    destination_path="d",
                    partial_path="b",
                    trash_path="c",
                    state="rolled_back",
                ),
            ]
        )
        await session.commit()
    workspace = scan_env.lib_dir.parent / "workspace"
    (workspace / "jobs" / str(job.id)).mkdir(parents=True)

    paths["c"].unlink()
    report = await run_scan(scan_env, workspace_dir=workspace)

    assert (report.found, report.pruned) == (2, 1)
    async with scan_env.factory() as session:
        remaining = (await session.execute(select(MediaFile.path))).scalars().all()
        assert set(remaining) == {str(paths["a"]), str(paths["b"])}
        probe_count = (await session.execute(select(func.count()).select_from(ProbeRow))).scalar_one()
        assert probe_count == 2
        job_count = (
            await session.execute(select(func.count()).select_from(Job).where(Job.kind == "healthcheck"))
        ).scalar_one()
        assert job_count == 2
        assert (
            await session.execute(select(JobAttempt).where(JobAttempt.job_id == job.id))
        ).scalar_one_or_none() is None
        assert (await session.execute(select(JobEvent).where(JobEvent.job_id == job.id))).scalar_one_or_none() is None
        assert (await session.execute(select(FinalizationJournal))).scalar_one_or_none() is None
    assert not (workspace / "jobs" / str(job.id)).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
async def test_prune_removal_events(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, rollback: bool, scoped: bool):
    target = write_media(scan_env.lib_dir, "target.mp4")
    write_media(scan_env.lib_dir, "companion.mkv")
    await run_scan(scan_env)
    media = await fetch_row(scan_env, target)
    assert media is not None
    async with scan_env.factory() as session:
        job_ids = list(await session.scalars(select(Job.id).where(Job.media_file_id == media.id)))
    target.unlink()

    bus = EventBus(min_interval_s=0)
    subscription = bus.subscribe()
    staging = StagingService(scan_env.factory, scan_env.staging.workspace_dir, min_free_gb=1, events=bus)
    session_class = scan_env.factory.class_
    execute, commit, publish = session_class.execute, session_class.commit, bus.publish
    deleting: set[AsyncSession] = set()
    removals = []
    job_removals = []
    committed = False
    failure = RuntimeError("Pruning commit failed.")

    async def track_delete(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if statement.is_delete and statement.table.name == "media_files":
            deleting.add(session)
        return result

    async def commit_delete(session):
        nonlocal committed
        if session in deleting:
            assert not removals and not job_removals
            if rollback:
                raise failure
        await commit(session)
        if session in deleting:
            committed = True
            # A pending row update must coalesce into its committed removal.
            publish(
                "file.status",
                {"action": "updated", "media_file_id": media.id, "library_id": scan_env.library_id},
                key=f"file.status:file:{media.id}",
            )

    def capture(type_, payload, key=None, *, bulk=False):
        if type_ == "job.deleted":
            assert committed
            job_removals.append(payload["job_id"])
        if type_ == "file.status" and payload["action"] == "pruned":
            assert committed
            removals.append((payload, key))
        publish(type_, payload, key, bulk=bulk)

    monkeypatch.setattr(session_class, "execute", track_delete)
    monkeypatch.setattr(session_class, "commit", commit_delete)
    monkeypatch.setattr(bus, "publish", capture)
    try:
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            with pytest.raises(RuntimeError) if rollback else nullcontext() as raised:
                report = await scan_library(
                    session,
                    library,
                    config=scan_env.config,
                    staging=staging,
                    events=bus,
                    target_paths={target} if scoped else None,
                )
                assert report.pruned == 1
            if rollback:
                assert raised is not None and raised.value is failure
        bus.flush_bulk()
        async with scan_env.factory() as session:
            assert (await session.get(MediaFile, media.id) is not None) == rollback
            assert (await session.get(ProbeRow, media.probe_id) is not None) == rollback
        if rollback:
            assert removals == job_removals == [] and subscription.backlog == 0
        else:
            payload = {"action": "pruned", "media_file_id": media.id, "library_id": scan_env.library_id}
            assert removals == [(payload, f"file.status:file:{media.id}")]
            assert job_removals == job_ids
            events = []
            while subscription.backlog:
                events.append(await asyncio.wait_for(subscription.next(), 1))
            summary = next(event for event in events if event is not None and event.type == "file.status")
            assert summary.payload["pruned"] == 1
            event = next(event for event in events if event is not None and event.type == "events.batch")
            assert event.payload["events"] == [{"type": "file.status", "payload": payload}]
        assert not target.exists()
    finally:
        bus.unsubscribe(subscription)
        await bus.aclose()


@pytest.mark.asyncio
async def test_retains_filtered_file(scan_env: ScanEnv) -> None:
    filtered = write_media(scan_env.lib_dir, "filtered.mp4")
    companion = write_media(scan_env.lib_dir, "companion.mkv")
    assert (await run_scan(scan_env)).added == 2
    retained = await fetch_row(scan_env, companion)
    assert retained is not None
    async with scan_env.factory() as session:
        media = (await session.scalars(select(MediaFile).where(MediaFile.path == str(filtered)))).one()
        job = (await session.scalars(select(Job).where(Job.media_file_id == media.id, Job.kind == "healthcheck"))).one()
        job.status = JobStatus.FAILED.value
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status=JobStatus.FAILED.value)
        session.add(attempt)
        await session.flush()
        session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type="failed"))
        await session.commit()

    report = await run_scan(scan_env, config=ScannerConfig(containers=["mkv"]))

    assert filtered.read_bytes() == companion.read_bytes() == b"x" * 2048
    assert (report.found, report.pruned) == (1, 0)
    assert report.errors == []
    assert set(await all_paths(scan_env)) == {str(filtered), str(companion)}
    async with scan_env.factory() as session:
        assert await session.get(MediaFile, media.id) is not None
        assert await session.get(ProbeRow, media.probe_id) is not None
        assert await session.get(Job, job.id) is not None
        assert await session.get(JobAttempt, attempt.id) is not None
        assert list(await session.scalars(select(JobEvent).where(JobEvent.job_id == job.id)))
        current = await session.get(MediaFile, retained.id)
        assert current is not None and current.probe_id == retained.probe_id
        assert await session.get(ProbeRow, current.probe_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "original_moved", "discard_prepared"])
@pytest.mark.parametrize("scoped", [False, True])
async def test_preserves_recovery_scan(scan_env: ScanEnv, state: str, scoped: bool) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    partial = paths["c"].with_suffix(".mkv.partial")
    partial.write_bytes(b"verified output")
    trash = make_test_temp_dir("scanner-recovery-trash") / "original.mkv"
    paths["c"].rename(trash)
    async with scan_env.factory() as session:
        job = (
            await session.scalars(
                select(Job)
                .join(MediaFile, Job.media_file_id == MediaFile.id)
                .where(MediaFile.path == str(paths["c"]), Job.kind == "healthcheck")
            )
        ).one()
        job.status = JobStatus.FAILED.value
        job.kind = "transcode"
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            status="failed",
            result="failed",
        )
        session.add(attempt)
        await session.flush()
        session.add(
            FinalizationJournal(
                attempt_id=attempt.id,
                source_path=str(paths["c"]),
                destination_path=str(paths["c"]),
                partial_path=str(partial),
                trash_path=str(trash),
                state=state,
            )
        )
        await session.commit()
    workspace = make_test_temp_dir("scanner-recovery-workspace")
    marker = workspace / "jobs" / str(job.id) / "attempts" / "1" / "output.mkv"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"artifact")
    assert (
        await run_scan(scan_env, workspace_dir=workspace, target_paths={paths["c"]} if scoped else None)
    ).pruned == 0
    await cleanup_workspace(scan_env.factory, workspace)
    assert not (workspace / "jobs" / str(job.id)).exists()
    assert partial.read_bytes() == b"verified output"
    assert trash.read_bytes() == b"x" * 2048
    assert await fetch_row(scan_env, paths["c"]) is not None
    async with scan_env.factory() as session:
        assert await session.get(Job, job.id) is not None
        assert await session.get(JobAttempt, attempt.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_preserves_active_file(scan_env: ScanEnv, scoped: bool) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        job = (
            await session.execute(
                select(Job)
                .join(MediaFile, Job.media_file_id == MediaFile.id)
                .where(MediaFile.path == str(paths["c"]), Job.kind == "healthcheck")
            )
        ).scalar_one()
        job.status = JobStatus.RUNNING.value
        await session.commit()

    paths["c"].unlink()
    report = await run_scan(scan_env, target_paths={paths["c"]} if scoped else None)

    assert report.pruned == 0
    assert await fetch_row(scan_env, paths["c"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_preserves_staging_worker(scan_env: ScanEnv, scoped: bool) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        job = (
            await session.scalars(
                select(Job)
                .join(MediaFile, Job.media_file_id == MediaFile.id)
                .where(MediaFile.path == str(paths["c"]), Job.kind == "healthcheck")
            )
        ).one()
        job.kind = "transcode"
        job.status = JobStatus.CANCELLED.value
        await session.commit()
    marker = scan_env.staging.workspace_dir / "jobs" / str(job.id) / "source.mkv"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"copy in progress")
    paths["c"].unlink()
    scan_env.staging.active_jobs.add(job.id)
    try:
        assert (await run_scan(scan_env, target_paths={paths["c"]} if scoped else None)).pruned == 0
        assert marker.read_bytes() == b"copy in progress"
        assert await fetch_row(scan_env, paths["c"]) is not None
        async with scan_env.factory() as session:
            saved = await session.get(Job, job.id)
            assert saved is not None and saved.status == JobStatus.CANCELLED.value
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None and await session.get(ProbeRow, media.probe_id) is not None
    finally:
        scan_env.staging.active_jobs.discard(job.id)
    assert (await run_scan(scan_env, target_paths={paths["c"]} if scoped else None)).pruned == 1
    assert not marker.exists()


@pytest.mark.asyncio
async def test_claim_during_prune(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        job = (
            await session.scalars(
                select(Job)
                .join(MediaFile, Job.media_file_id == MediaFile.id)
                .where(MediaFile.path == str(paths["c"]), Job.kind == "healthcheck")
            )
        ).one()
        job.kind = "transcode"
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        media.transcode_decided = True
        node = NodeRow(name="node", display_name="node", token_hash="token")
        session.add(node)
        await session.commit()
    workspace = make_test_temp_dir("scanner-claim-workspace")
    marker = workspace / "jobs" / str(job.id) / "source.mkv"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"source")
    paths["c"].unlink()
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        execute = session.execute
        library.scan_id += 1
        for path in (paths["a"], paths["b"]):
            observed = await session.scalar(select(MediaFile).where(MediaFile.path == str(path)))
            assert observed is not None
            observed.last_seen_scan_id = library.scan_id
        await session.commit()
        selected = False

        async def claim_selected() -> None:
            async with scan_env.factory() as writer:
                assert await claim(writer, job.id, node.id) is not None
                await writer.commit()

        async def select_then_claim(statement, *args, **kwargs):
            nonlocal selected
            result = await execute(statement, *args, **kwargs)
            if not selected and statement.is_select and statement.column_descriptions[0]["entity"] is MediaFile:
                selected = True
                await asyncio.wait_for(claim_selected(), 3)
            return result

        monkeypatch.setattr(session, "execute", select_then_claim)
        count = await asyncio.wait_for(
            scan._prune_missing(
                session,
                library,
                library.scan_id,
                staging=StagingService(scan_env.factory, workspace, min_free_gb=1),
                unavailable=set(),
                excluded=(),
            ),
            3,
        )
        await session.commit()
        assert selected and count == 0
    assert marker.read_bytes() == b"source"
    async with scan_env.factory() as session:
        saved = await session.get(Job, job.id)
        assert saved is not None and saved.status == JobStatus.CLAIMED.value
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None and await session.get(ProbeRow, media.probe_id) is not None
        assert (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id))).one().job_id == job.id
        assert (await session.scalars(select(JobEvent).where(JobEvent.job_id == job.id))).all()


@pytest.mark.asyncio
async def test_prune_blocks_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server import workspace as workspace_ops

    entered, attempted = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    remove = workspace_ops._remove_job

    def blocked_remove(root: Path, job_id: int) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        remove(root, job_id)

    monkeypatch.setattr("server.staging._remove_job", blocked_remove)
    async with database_context("scanner-prune-writer") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        (tmp_path / "library").mkdir()
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path / "library")], scan_id=1)
            node = NodeRow(name="node", display_name="node", token_hash="token")
            session.add_all([library, node])
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(tmp_path / "library" / "missing.mkv"),
                size=2048,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            session.add(media)
            await session.flush()
            job = Job(library_id=library.id, media_file_id=media.id, kind="transcode", status="queued")
            session.add(job)
            await session.commit()
        marker = staging.workspace_dir / "jobs" / str(job.id) / "source.mkv"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"source")

        async def prune() -> int:
            async with factory() as session:
                count = await scan._prune_missing(session, library, 1, staging=staging, unavailable=set(), excluded=())
                await session.commit()
                return count

        async def claim_selected() -> Job | None:
            async with factory() as session:
                attempted.set()
                result = await claim(session, job.id, node.id)
                await session.commit()
                return result

        pruning = asyncio.create_task(prune())
        tasks = [pruning]
        try:
            await asyncio.wait_for(entered.wait(), 3)
            claimant = asyncio.create_task(claim_selected())
            tasks.append(claimant)
            await asyncio.wait_for(attempted.wait(), 3)
            assert not claimant.done()
            assert marker.read_bytes() == b"source"
            release.set()
            count, claimed = await asyncio.wait_for(asyncio.gather(pruning, claimant), 3)
            assert count == 1 and claimed is None
            assert not marker.exists()
            async with factory() as session:
                assert await session.get(MediaFile, media.id) is None
                assert await session.get(Job, job.id) is None
                assert list(await session.scalars(select(JobAttempt))) == []
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_failed_probe_row(monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)

    touch(paths["a"], mtime_ns=1_700_000_000_000_000_001, size=8192)
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", "movie_a.mkv")

    report = await run_scan(scan_env)

    assert (report.updated, report.unchanged) == (1, 2)
    assert len(report.errors) == 1
    assert "movie_a.mkv" in report.errors[0]
    row = await fetch_row(scan_env, paths["a"])
    assert row is not None
    assert row.video_codec == "hevc"
    assert row.health_status == "probe_failed"
    async with scan_env.factory() as session:
        probe_count = (await session.execute(select(func.count()).select_from(ProbeRow))).scalar_one()
        assert probe_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["BPS", "BPS-eng"])
@pytest.mark.parametrize("artwork", ["disposition", "mime"])
async def test_existing_stats_retained(
    monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv, tag: str, artwork: str
) -> None:
    prop_log = scan_env.lib_dir / "editor.log"
    monkeypatch.setenv("FAKE_MKVPROPEDIT_LOG", str(prop_log))
    raw = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "tags": {tag: "2077485"}},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {tag: "347691"}},
            {
                "index": 2,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": int(artwork == "disposition")},
                "tags": {"FILENAME": "cover.jpg", "MIMETYPE": "image/jpeg", "BPS": "0"} if artwork == "mime" else {},
            },
        ],
        "format": {"duration": "1342.55"},
    }
    movie = write_media(scan_env.lib_dir, "existing.mkv", raw)
    await run_scan(scan_env)
    await finish_healthchecks(scan_env)
    existing = await fetch_row(scan_env, movie)
    assert existing is not None and existing.probe_id is not None
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await session.commit()

    report = await run_scan(scan_env, mkvpropedit_bin=str(FAKE_MKVPROPEDIT))
    assert (report.updated, report.unchanged) == (0, 1)
    retained = await fetch_row(scan_env, movie)
    assert retained is not None and retained.probe_id == existing.probe_id
    result = await LibraryFS(
        scan_env.library_id,
        sessionmaker=scan_env.factory,
        ffprobe_bin=FAKE_FFPROBE,
        mkvpropedit_bin=FAKE_MKVPROPEDIT,
        workspace_dir=scan_env.staging.workspace_dir,
    ).refresh_current(existing.id)
    assert result.probe.real_video[0].bit_rate == 2077485
    assert result.probe.audio[0].bit_rate == 347691
    assert not prop_log.exists()
    assert scan_env.probe_log.read_text().splitlines() == [str(movie), str(movie)]


@pytest.mark.asyncio
async def test_mkv_stats_reprobes(monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv) -> None:
    prop_log = make_test_disk_path(f"mkvpropedit-log-{uuid.uuid4().hex}.txt")
    monkeypatch.setenv("FAKE_MKVPROPEDIT_LOG", str(prop_log))
    # The fake tool mutates the sidecar probe, so files without one cannot
    # show the materialized bitrates and would re-materialize every scan.
    movies = scan_env.lib_dir / "movies"
    movies.mkdir()
    paths = {
        "a": write_media(movies, "movie_a.mkv", HEVC_4K_PROBE),
        "b": write_media(movies, "movie_b.mp4", H264_NO_BITRATE_PROBE),
    }
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await session.commit()

    report = await run_scan(scan_env, mkvpropedit_bin=str(FAKE_MKVPROPEDIT))

    assert report.errors == []
    assert prop_log.read_text(encoding="utf-8").splitlines() == [str(paths["a"])]
    probed = scan_env.probe_log.read_text(encoding="utf-8").splitlines()
    assert probed.count(str(paths["a"])) == 2
    assert probed.count(str(paths["b"])) == 1

    row = await fetch_row(scan_env, paths["a"])
    assert row is not None
    async with scan_env.factory() as session:
        probe = await session.get(ProbeRow, row.probe_id)
        assert probe is not None
        raw = json.loads(probe.raw_json)
    assert raw["streams"][0]["tags"]["BPS"] == "5280000"

    report = await run_scan(scan_env, mkvpropedit_bin=str(FAKE_MKVPROPEDIT))

    assert (report.added, report.updated, report.unchanged) == (0, 0, 2)
    assert prop_log.read_text(encoding="utf-8").splitlines() == [str(paths["a"])]
    probed = scan_env.probe_log.read_text(encoding="utf-8").splitlines()
    assert probed.count(str(paths["a"])) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["local", "node", "node_local"])
async def test_rescan_materializes_stats(monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv, location: str) -> None:
    from server.kv import KeyValueStore

    await KeyValueStore(scan_env.factory).put("operational_settings", {"mkv_stats_location": location})
    prop_log = make_test_disk_path(f"mkvpropedit-log-{uuid.uuid4().hex}.txt")
    monkeypatch.setenv("FAKE_MKVPROPEDIT_LOG", str(prop_log))
    movie = write_media(scan_env.lib_dir, "movie_a.mkv", HEVC_4K_PROBE)
    published_mtime = movie.stat().st_mtime_ns
    from server.fs import library as library_fs

    original = library_fs.run_mkvpropedit

    async def materialize(binary, path, *, on_progress=None, **_kwargs):
        await original(binary, path, on_progress=on_progress)
        path.write_bytes(path.read_bytes() + b"stats")

    monkeypatch.setattr(library_fs, "run_mkvpropedit", materialize)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await session.commit()

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        await scan_library(session, library, config=scan_env.config, staging=scan_env.staging)
        await session.commit()

    existing = await fetch_row(scan_env, movie)
    assert existing is not None
    row = await LibraryFS(
        scan_env.library_id,
        sessionmaker=scan_env.factory,
        ffprobe_bin=FAKE_FFPROBE,
        mkvpropedit_bin=FAKE_MKVPROPEDIT,
        workspace_dir=Path("."),
    ).refresh_current(existing.id)

    if location != "local":
        assert not prop_log.exists()
        assert movie.stat().st_size == existing.size
        assert row.media.probe_id is None and row.media.health_status == "pending"
        async with scan_env.factory() as session:
            stored = await session.scalar(select(ProbeRow).where(ProbeRow.media_file_id == existing.id))
            assert stored is not None
        return
    assert prop_log.read_text(encoding="utf-8").splitlines() == [str(movie)]
    probed = scan_env.probe_log.read_text(encoding="utf-8").splitlines()
    assert probed.count(str(movie)) == 2
    assert row is not None
    assert row.media.size == movie.stat().st_size
    assert row.media.mtime_ns == movie.stat().st_mtime_ns
    assert row.media.mtime_ns == published_mtime
    async with scan_env.factory() as session:
        probe = await session.get(ProbeRow, row.media.probe_id)
        assert probe is not None
        raw = json.loads(probe.raw_json)
    assert raw["streams"][0]["tags"]["BPS"] == "5280000"


@pytest.mark.asyncio
async def test_mkv_stats_off(monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv) -> None:
    prop_log = make_test_disk_path(f"mkvpropedit-log-{uuid.uuid4().hex}.txt")
    monkeypatch.setenv("FAKE_MKVPROPEDIT_LOG", str(prop_log))
    paths = build_tree(scan_env.lib_dir)

    report = await run_scan(scan_env, mkvpropedit_bin=str(FAKE_MKVPROPEDIT))

    assert report.errors == []
    assert not prop_log.exists()
    probed = scan_env.probe_log.read_text(encoding="utf-8").splitlines()
    assert probed.count(str(paths["a"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("ordinary", [False, True], ids=["rescan", "probe"])
async def test_stats_preserve_dates(
    monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv, failed: bool, ordinary: bool
) -> None:
    movie = write_media(scan_env.lib_dir, "dated.mkv", HEVC_4K_PROBE)
    await run_scan(scan_env)
    await finish_healthchecks(scan_env)
    original_bytes = movie.read_bytes()
    dates = (1_600_000_000_000_000_111, 1_600_000_000_000_000_222)
    os.utime(movie, ns=dates)

    async def materialize(binary: str | Path, path: Path, *, on_progress=None, **_kwargs) -> None:
        os.utime(path, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
        if failed:
            raise ProbeError("partially edited")

    from server.fs import library as library_fs

    monkeypatch.setattr(library_fs, "run_mkvpropedit", materialize)
    outcome = pytest.raises(LibraryProbeError) if failed else nullcontext()
    with outcome as error:
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            library.mkv_stats = True
            await session.commit()
            if ordinary:
                existing = (await session.execute(select(MediaFile).where(MediaFile.path == str(movie)))).scalar_one()
                from tests.helpers import probe_media

                row = await probe_media(
                    LibraryFS(
                        scan_env.library_id,
                        sessionmaker=scan_env.factory,
                        ffprobe_bin=FAKE_FFPROBE,
                        mkvpropedit_bin="mkvpropedit",
                        workspace_dir=Path("."),
                    ),
                    existing.id,
                )
            else:
                existing = await fetch_row(scan_env, movie)
                assert existing is not None
                row = await LibraryFS(
                    scan_env.library_id,
                    sessionmaker=scan_env.factory,
                    ffprobe_bin=FAKE_FFPROBE,
                    mkvpropedit_bin="mkvpropedit",
                    workspace_dir=Path("."),
                ).refresh_current(existing.id)
            assert row is not None
            assert row.media.mtime_ns == dates[1]
    if failed:
        assert error is not None
        assert error.value.cleanup == "completed"
        async with scan_env.factory() as session:
            persisted = (await session.execute(select(MediaFile).where(MediaFile.path == str(movie)))).scalar_one()
        assert persisted.mtime_ns == dates[1]
    stat = movie.stat()
    assert (stat.st_atime_ns, stat.st_mtime_ns) == dates
    assert movie.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_mkv_stats_failure(monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv) -> None:
    prop_log = make_test_disk_path(f"mkvpropedit-log-{uuid.uuid4().hex}.txt")
    monkeypatch.setenv("FAKE_MKVPROPEDIT_LOG", str(prop_log))
    paths = build_tree(scan_env.lib_dir)
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.mkv_stats = True
        await session.commit()

    missing_bin = scan_env.lib_dir / "no-such-mkvpropedit"
    report = await run_scan(scan_env, mkvpropedit_bin=str(missing_bin))

    # Per-file errors leave the rest of the scan running.
    assert len(report.errors) == 2
    assert all(str(missing_bin) in message for message in report.errors), report.errors
    assert report.added == 3
    row = await fetch_row(scan_env, paths["a"])
    assert row is not None
    assert row.health_status == "probe_failed" and row.probe_id is None
    probed = scan_env.probe_log.read_text(encoding="utf-8").splitlines()
    assert probed.count(str(paths["a"])) == 1


@pytest.mark.asyncio
async def test_rescan_single_file(scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    await finish_healthchecks(scan_env)

    changed = with_video_codec(H264_NO_BITRATE_PROBE, "hevc")
    Path(f"{paths['b']}.probe.json").write_text(json_sidecar(changed), encoding="utf-8")
    touch(paths["b"], mtime_ns=1_700_000_000_000_000_002, size=512)

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        existing = await fetch_row(scan_env, paths["b"])
        assert existing is not None
        row = await LibraryFS(
            scan_env.library_id,
            sessionmaker=scan_env.factory,
            ffprobe_bin=FAKE_FFPROBE,
            mkvpropedit_bin="mkvpropedit",
            workspace_dir=Path("."),
        ).refresh_current(existing.id)
        assert row is not None
        assert row.probe.video[0].codec == "hevc"
        assert row.media.size == 512

        with pytest.raises(LibraryProbeError) as missing:
            await LibraryFS(
                scan_env.library_id,
                sessionmaker=scan_env.factory,
                ffprobe_bin=FAKE_FFPROBE,
                mkvpropedit_bin="mkvpropedit",
                workspace_dir=Path("."),
            ).refresh_current(-1)
        assert missing.value.reason == "missing"

    reread = await fetch_row(scan_env, paths["b"])
    assert reread is not None
    assert reread.video_codec == "hevc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "change"),
    [
        ("healthy", "none"),
        ("corrupt", "none"),
        ("probe_failed", "none"),
        ("healthy", "size"),
        ("corrupt", "size"),
        ("healthy", "mtime"),
        ("corrupt", "mtime"),
    ],
)
async def test_rescan_health_state(scan_env: ScanEnv, health: str, change: str) -> None:
    movie = write_media(scan_env.lib_dir, "movie.mkv", HEVC_4K_PROBE)
    await run_scan(scan_env)
    await finish_healthchecks(scan_env)
    original = await fetch_row(scan_env, movie)
    assert original is not None
    async with scan_env.factory() as session:
        row = await session.get(MediaFile, original.id)
        assert row is not None
        row.health_status = health
        await session.commit()

    if change == "size":
        touch(movie, mtime_ns=original.mtime_ns, size=4096)
    elif change == "mtime":
        os.utime(movie, ns=(original.mtime_ns + 1, original.mtime_ns + 1))

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        row = await LibraryFS(
            scan_env.library_id,
            sessionmaker=scan_env.factory,
            ffprobe_bin=FAKE_FFPROBE,
            mkvpropedit_bin="mkvpropedit",
            workspace_dir=Path("."),
        ).refresh_current(original.id)
        assert row is not None
        assert row.media.health_status == (health if change != "size" and health != "probe_failed" else "pending")
        assert row.media.probe_id is not None

    saved = await fetch_row(scan_env, movie)
    assert saved is not None and saved.health_status == row.media.health_status
    assert scan_env.probe_log.read_text().splitlines() == [str(movie), str(movie)]


async def all_paths(env: ScanEnv) -> list[str]:
    async with env.factory() as session:
        return list((await session.execute(select(MediaFile.path))).scalars())


@pytest.mark.asyncio
async def test_scans_multiple_roots(scan_env: ScanEnv) -> None:
    second = make_test_temp_dir("scanner-lib-2")
    build_tree(scan_env.lib_dir)
    b = write_media(second, "movie_b2.mkv", H264_NO_BITRATE_PROBE)

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.roots = [*library.roots, str(second)]
        await session.commit()

    report = await run_scan(scan_env)

    assert (report.found, report.added, report.pruned) == (4, 4, 0)
    row = await fetch_row(scan_env, b)
    assert row is not None and row.container == "mkv"
    stored = await all_paths(scan_env)
    assert [p for p in stored if p.startswith(str(second))] == [str(b)]


@pytest.mark.asyncio
async def test_isolates_broken_roots(scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    missing = scan_env.lib_dir / "does-not-exist"

    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.roots = [*library.roots, str(missing)]
        await session.commit()

    report = await run_scan(scan_env)

    assert report.found == 3
    assert len(report.errors) == 1
    assert str(missing) in report.errors[0]
    row = await fetch_row(scan_env, paths["a"])
    assert row is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "not_directory", "unreadable", "subtree", "empty"])
async def test_preserves_unavailable_history(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    subtree = scan_env.lib_dir / "movies"
    subtree.mkdir()
    path = write_media(subtree, "retained.mkv")
    vanished = write_media(scan_env.lib_dir, "vanished.mkv")
    if failure == "subtree":
        write_media(scan_env.lib_dir, "available.mkv")
    second = make_test_temp_dir("scan-second-root")
    second_missing = write_media(second, "vanished.mkv")
    if failure == "empty":
        write_media(second, "available.mkv")
        async with scan_env.factory() as session:
            library = await session.get(Library, scan_env.library_id)
            assert library is not None
            library.roots = [*library.roots, str(second)]
            await session.commit()
    await run_scan(scan_env)
    row = await fetch_row(scan_env, path)
    assert row is not None
    async with scan_env.factory() as session:
        job = (await session.scalars(select(Job).where(Job.media_file_id == row.id, Job.kind == "healthcheck"))).one()
        job.kind = "transcode"
        job.status = JobStatus.SUCCEEDED.value
        session.add(JobEvent(job_id=job.id, type="succeeded"))
        session.add(JobAttempt(job_id=job.id, attempt_number=1, status=JobStatus.SUCCEEDED.value))
        await session.commit()
        job_id = job.id
    workspace = make_test_temp_dir("scan-workspace")
    job_dir = workspace / "jobs" / str(job_id)
    job_dir.mkdir(parents=True)
    vanished.unlink()
    second_missing.unlink()
    probe_log = scan_env.probe_log.read_text(encoding="utf-8")
    root = scan_env.lib_dir
    saved = root.with_name(f"{root.name}-saved")
    if failure in {"missing", "not_directory", "empty"}:
        root.rename(saved)
        if failure == "not_directory":
            root.write_bytes(b"not a directory")
        elif failure == "empty":
            root.mkdir()
    scandir = os.scandir

    def unavailable(directory):
        path = fd_path("", directory) if isinstance(directory, int) else Path(directory)
        if path == (subtree if failure == "subtree" else root):
            raise PermissionError(13, "unavailable", str(directory))
        return scandir(directory)

    try:
        with monkeypatch.context() as patch:
            if failure in {"unreadable", "subtree"}:
                patch.setattr(os, "scandir", unavailable)
            report = await run_scan(scan_env, workspace_dir=workspace)
        assert len(report.errors) == 1
        assert report.pruned == (1 if failure in {"subtree", "empty"} else 0)
        if failure == "empty":
            assert await fetch_row(scan_env, second_missing) is None
        retained = await fetch_row(scan_env, path)
        assert retained is not None and retained.id == row.id and retained.probe_id == row.probe_id
        assert job_dir.is_dir()
        async with scan_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.status == JobStatus.SUCCEEDED.value
            assert (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            assert (await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))).all()
    finally:
        if failure in {"missing", "not_directory", "empty"}:
            if failure == "not_directory":
                root.unlink()
            elif failure == "empty":
                root.rmdir()
            saved.rename(root)
    await run_scan(scan_env, workspace_dir=workspace)
    retained = await fetch_row(scan_env, path)
    assert retained is not None and retained.id == row.id
    assert scan_env.probe_log.read_text(encoding="utf-8") == probe_log
    async with scan_env.factory() as session:
        assert (
            await session.scalars(select(Job).where(Job.media_file_id == row.id, Job.kind == "transcode"))
        ).one().id == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["healthcheck", "transcode"])
async def test_changed_healthcheck_generation(scan_env: ScanEnv, kind: str) -> None:
    path = write_media(scan_env.lib_dir, "growing.mkv")
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        job = (await session.scalars(select(Job).where(Job.kind == "healthcheck"))).one()
        job.kind = kind
        job.status = JobStatus.SUCCEEDED.value
        old_id = job.id
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        media.health_status = "healthy"
        media.transcode_status = "transcoded" if kind == "transcode" else "pending"
        media.transcode_decided = kind == "transcode"
        flow = Flow(name="eligibility", definition=json.dumps(graph_flow(("transcode.remux", {"container": "mp4"}))))
        session.add(flow)
        await session.flush()
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.flow_id = flow.id
        await session.commit()
        media_id = media.id
    report = await run_scan(scan_env)
    assert report.unchanged == 1
    touch(path, mtime_ns=1_700_000_000_000_000_000, size=4096)
    report = await run_scan(scan_env)
    assert report.updated == 1
    if kind == "healthcheck":
        async with scan_env.factory() as session:
            assert list(await session.scalars(select(Job.id).where(Job.kind == "healthcheck"))) == [old_id]
        host = PluginHost([Path(__file__).resolve().parents[1] / "plugins"])
        queue = FlowEnqueueService(scan_env.factory, host)
        try:
            assert await queue._pass() == 1
        finally:
            await queue.close()
            await host.aclose()
    async with scan_env.factory() as session:
        jobs = (
            await session.scalars(select(Job).where(Job.kind.in_(["healthcheck", "transcode"])).order_by(Job.id))
        ).all()
        assert len(jobs) == 2 and jobs[0].id == old_id and jobs[0].status == JobStatus.SUCCEEDED.value
        assert jobs[1].kind == "healthcheck" and jobs[1].status == JobStatus.QUEUED.value
    workspace = make_test_temp_dir("generation-health")
    service = HealthcheckService(
        scan_env.factory,
        workspace,
        str(Path(__file__).parent / "fakes" / "ffmpeg.py"),
        str(FAKE_FFPROBE),
    )
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 3)
    report = await run_scan(scan_env)
    assert report.unchanged == 1
    async with scan_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.health_status == "healthy"
        assert media.transcode_status == ("transcoded" if kind == "transcode" else "pending")
        assert media.transcode_decided == (kind == "transcode")
        assert len((await session.scalars(select(Job).where(Job.kind.in_(["healthcheck", "transcode"])))).all()) == 2
        candidates = (await session.scalars(_candidate_statement(10))).all()
        assert [row.id for row in candidates] == ([media_id] if kind == "healthcheck" else [])


@pytest.mark.asyncio
async def test_preserves_active_healthcheck(scan_env: ScanEnv) -> None:
    path = write_media(scan_env.lib_dir, "growing.mkv")
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        job = (await session.scalars(select(Job).where(Job.kind == "healthcheck"))).one()
        job_id = job.id
    touch(path, mtime_ns=1_700_000_000_000_000_000, size=4096)
    report = await run_scan(scan_env, probe_pending=False)
    assert report.updated == 1
    async with scan_env.factory() as session:
        job = (await session.scalars(select(Job).where(Job.kind == "healthcheck"))).one()
        assert job.id == job_id and job.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_healthcheck_source_changes(scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.healthcheck import HealthcheckResult

    path = write_media(scan_env.lib_dir, "growing.mkv")
    await run_scan(scan_env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(
        source: str, ffmpeg_bin: str, ffprobe_bin: str, log_path: DiagnosticLog, *, hwaccel: object, on_progress: object
    ) -> HealthcheckResult:
        assert Path(source).read_bytes() == b"x" * 2048
        entered.set()
        await release.wait()
        log_path.path.write_text("healthcheck: result succeeded\n", encoding="utf-8")
        return HealthcheckResult(healthy=True, samples=[])

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
    workspace = make_test_temp_dir("health-generation")
    service = HealthcheckService(scan_env.factory, workspace, "unused", "unused")
    claimed = await service.claim_next()
    assert claimed is not None
    task = asyncio.create_task(service.run(*claimed))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        replacement = path.with_name("replacement.mkv")
        replacement.write_bytes(b"new generation" * 300)
        replacement.replace(path)
        assert (await run_scan(scan_env, probe_pending=False)).updated == 1
        pending = await fetch_row(scan_env, path)
        assert pending is not None and pending.health_status == "pending"
        release.set()
        await asyncio.wait_for(task, 2)
        assert (await run_scan(scan_env)).unchanged == 1
        current = await fetch_row(scan_env, path)
        assert current is not None and current.size == len(b"new generation" * 300)
        assert current.health_status == "pending", "old generation's result certified the replacement"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_config_filters_files(scan_env: ScanEnv) -> None:
    build_tree(scan_env.lib_dir)

    report = await run_scan(scan_env, ScannerConfig(ignore_dirs=["Extras"], containers=["mkv"]))

    assert report.found == 2
    stored = await all_paths(scan_env)
    assert all(p.endswith(".mkv") for p in stored)
    assert all("Extras" not in p for p in stored)

    app = create_app(scan_env.factory)
    await app.state.scanner.scan_now(scan_env.library_id)
    stored = await all_paths(scan_env)
    assert any("Extras" in path for path in stored)
    await app.state.settings.update({"scanner": {"containers": ["mp4"], "ignore_dirs": ["Extras"]}})
    narrowed = await app.state.scanner.scan_now(scan_env.library_id)
    assert narrowed is not None and narrowed.found > 0
    assert set(stored) <= set(await all_paths(scan_env))


@pytest.mark.asyncio
async def test_excludes_internal_storage(scan_env: ScanEnv) -> None:
    trash = scan_env.lib_dir / "retained"
    workspace = scan_env.lib_dir / "workspace"
    trash.mkdir()
    workspace.mkdir()
    source = write_media(scan_env.lib_dir, "movie.mkv")
    async with scan_env.factory() as session:
        entry = await store_trash(session, trash, scan_env.library_id, source, scan_env.lib_dir, 30)
        await session.commit()
    source = write_media(scan_env.lib_dir, "movie.mkv")
    staged_source = write_media(workspace, "source.mkv")
    ordinary = scan_env.lib_dir / "movies" / "workspace"
    ordinary.mkdir(parents=True)
    movie = write_media(ordinary, "movie.mkv")
    app = create_app(
        scan_env.factory,
        config=ServerConfig(workspace_dir=str(workspace / ".." / "workspace"), trash_dir=str(trash)),
    )
    for _ in range(2):
        report = await app.state.scanner.scan_now(scan_env.library_id)
        assert report is not None and report.found == 2 and report.errors == []
    assert set(await all_paths(scan_env)) == {str(source), str(movie)}
    assert staged_source.read_bytes() == b"x" * 2048
    assert trash_path(trash, scan_env.library_id, entry.id, entry.suffix).read_bytes() == b"x" * 2048
    async with scan_env.factory() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["trash", "workspace", "healthchecks"])
@pytest.mark.parametrize("nested", [False, True])
async def test_excludes_nested_roots(
    scan_env: ScanEnv, monkeypatch: pytest.MonkeyPatch, storage: str, nested: bool
) -> None:
    if storage == "healthchecks":
        monkeypatch.setenv("MW_STATE_PATH", str(scan_env.lib_dir))
    excluded = scan_env.lib_dir / storage
    root = excluded / "nested" if nested else excluded
    root.mkdir(parents=True)
    media = write_media(root, "file.mkv")
    async with scan_env.factory() as session:
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.roots = [str(root)]
        await session.commit()
    report = await run_scan(
        scan_env,
        workspace_dir=excluded if storage == "workspace" else None,
        trash_dir=excluded if storage == "trash" else None,
    )
    assert report.found == 0 and report.errors == []
    assert await all_paths(scan_env) == []
    assert media.read_bytes() == b"x" * 2048


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["none", "interrupted", "journal"])
async def test_excludes_indexed_storage(scan_env: ScanEnv, record_type: str) -> None:
    trash = scan_env.lib_dir / "retained"
    trash.mkdir()
    media = write_media(trash, "file.mkv")
    await run_scan(scan_env)
    async with scan_env.factory() as session:
        row = (await session.scalars(select(MediaFile))).one()
        row.health_status = "healthy"
        flow = Flow(name="eligibility", definition="{}")
        session.add(flow)
        await session.flush()
        library = await session.get(Library, scan_env.library_id)
        assert library is not None
        library.flow_id = flow.id
        job = (await session.scalars(select(Job).where(Job.kind == "healthcheck"))).one()
        job.kind = "transcode"
        if record_type != "none":
            attempt = JobAttempt(
                job_id=job.id,
                attempt_number=1,
                status="failed",
                result="interrupted" if record_type == "interrupted" else "failed",
            )
            session.add(attempt)
            await session.flush()
            if record_type == "journal":
                session.add(
                    FinalizationJournal(
                        attempt_id=attempt.id,
                        source_path=str(media),
                        destination_path="destination",
                        partial_path="partial",
                        trash_path="trash",
                        state="prepared",
                    )
                )
        await session.commit()
        row_id, job_id = row.id, job.id
    workspace = scan_env.lib_dir / "workspace"
    marker = workspace / "jobs" / str(job_id) / "attempts" / "1" / "output.mkv"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"output")
    report = await run_scan(scan_env, workspace_dir=workspace, trash_dir=trash)
    assert report.found == 0 and report.pruned == (0 if record_type == "journal" else 1)
    assert media.read_bytes() == b"x" * 2048
    async with scan_env.factory() as session:
        assert (await session.scalars(_candidate_statement(10))).all() == []
        row = await session.get(MediaFile, row_id)
        job = await session.get(Job, job_id)
        if record_type != "journal":
            assert row is None and job is None and not marker.exists()
        else:
            assert row is not None and row.transcode_decided
            assert job is not None and job.status == JobStatus.CANCELLED
            assert (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).one()
            if record_type == "journal":
                assert (await session.scalars(select(FinalizationJournal))).one().state == "prepared"
            assert marker.read_bytes() == b"output"


@pytest.mark.asyncio
async def test_first_seen_change(monkeypatch: pytest.MonkeyPatch, scan_env: ScanEnv) -> None:
    tick = Tick()
    monkeypatch.setattr(scan, "utcnow", tick)
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)
    first = await fetch_row(scan_env, paths["a"])
    assert first is not None and first.first_seen == tick.now

    tick.advance(120)
    touch(paths["a"], mtime_ns=1_700_000_000_000_000_000, size=4096)
    await run_scan(scan_env)
    changed = await fetch_row(scan_env, paths["a"])
    assert changed is not None and changed.first_seen == tick.now

    tick.advance(120)
    await run_scan(scan_env)
    unchanged = await fetch_row(scan_env, paths["a"])
    assert unchanged is not None and unchanged.first_seen == changed.first_seen
