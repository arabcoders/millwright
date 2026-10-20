from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.fs.identity import source_identity
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.settings import ScannerConfig
from server.events import EventBus
from server.fs import library as library_fs
from server.fs.library import LibraryFS, LibraryProbeError
from server.models import Library, MediaFile, utcnow
from server.probing import ProbeService
from server.scanner import scan
from server.scanner.scan import ScanReport
from server.scanner.service import ScannerService
from server.staging import StagingService
from tests.helpers import database_context

pytestmark = pytest.mark.usefixtures("without_fd")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_probe_during_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    root = tmp_path / "media"
    root.mkdir()
    for name in ("first.mkv", "second.mkv"):
        (root / name).write_bytes(b"media")
    blocked, probed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    stat_files = scan._stat_files
    batches = 0

    def inspect(paths, errors):
        nonlocal batches
        batches += 1
        if batches == 2:
            loop.call_soon_threadsafe(blocked.set)
            if not release.wait(5):
                raise TimeoutError("Second batch was not released.")
        return stat_files(paths, errors)

    async def probe(*_args, **_kwargs) -> dict:
        probed.set()
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}

    monkeypatch.setattr(scan, "COMMIT_BATCH", 1)
    monkeypatch.setattr(scan, "_stat_files", inspect)
    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    async with database_context("scan-batch-probe") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.commit()
        app = create_app(
            factory,
            config=ServerConfig(workspace_dir=str(tmp_path / "workspace"), trash_dir=str(tmp_path / "trash")),
            ffprobe_bin="fake-probe",
            mkvpropedit_bin="fake-editor",
        )
        try:
            await app.state.probes.start()
            app.state.scanner.queue_scan(library.id, "manual")
            owner = app.state.scanner._scan_tasks[library.id]
            await asyncio.wait_for(blocked.wait(), 2)
            await asyncio.wait_for(probed.wait(), 2)
            await asyncio.wait_for(asyncio.gather(*app.state.probes._tasks.values()), 2)
            assert not owner.done()
            assert app.state.events.library_snapshot(library.id)["discovery"]["status"] == "scanning"
            async with factory() as session:
                row = await session.scalar(select(MediaFile).where(MediaFile.library_id == library.id))
                assert row is not None and row.path == str(root / "first.mkv")
                assert row.probe_id is not None and row.health_status == "healthy" and row.video_codec == "h264"
            if cancel:
                assert app.state.scanner.cancel_scan(library.id) == "cancelling"
            release.set()
            report = await asyncio.wait_for(owner, 3)
            assert report is not None and report.cancelled is cancel
            assert report.added == (1 if cancel else 2)
        finally:
            release.set()
            await asyncio.wait_for(app.state.scanner.aclose(), 3)
            await asyncio.wait_for(app.state.probes.close(), 3)
            await app.state.events.aclose()


@pytest.mark.asyncio
async def test_triggers_share_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, followup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reports = 0

    async def discover(*_args, on_progress, **_kwargs) -> ScanReport:
        nonlocal reports
        reports += 1
        report = ScanReport(found=7)
        on_progress(report)
        if reports == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        else:
            followup.set()
        return report

    async def debounce(_seconds: float) -> None:
        return

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("scanner-coordination") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), events=bus, sleep=debounce
        )
        try:
            assert scheduler.queue_scan(library.id, "watch", target_paths={tmp_path / "movie.mkv"}) == "queued"
            first = scheduler._scan_tasks[library.id]
            assert scheduler.queue_scan(library.id, "manual") == "already_queued"
            await asyncio.wait_for(entered.wait(), 2)
            before = bus.library_snapshot(library.id)
            for reason in ("manual", "scan-on-start", "interval"):
                assert scheduler.queue_scan(library.id, reason) == "already_running"
            for _ in range(3):
                scheduler.library_changed(library.id, {tmp_path / "movie.mkv"})
            assert scheduler._scan_tasks[library.id] is first
            assert bus.library_snapshot(library.id) == before
            release.set()
            await asyncio.wait_for(first, 2)
            await asyncio.wait_for(followup.wait(), 2)
            await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
            assert reports == 2
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_interval_completion_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entered, gate = asyncio.Event(), asyncio.Event()
    gate.set()

    async def discover(*_args, **_kwargs) -> ScanReport:
        nonlocal now
        entered.set()
        await asyncio.wait_for(gate.wait(), 3)
        now += timedelta(seconds=20)
        if outcome == "failed":
            raise OSError("Unavailable storage.")
        return ScanReport(cancelled=outcome == "cancelled")

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("scanner-clock") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=3600)
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            events=bus,
            clock=lambda: now,
        )
        try:
            await scheduler.check_intervals()
            await scheduler.scan_now(library.id)
            assert scheduler._due[library.id] == now + timedelta(hours=1)
            assert bus.library_snapshot(library.id)["discovery"]["latest_result"] == outcome
            await scheduler.check_intervals()
            assert not scheduler._scan_tasks
            scheduler.set_paused(paused=True)
            now += timedelta(days=5)
            await scheduler.check_intervals()
            assert not scheduler._scan_tasks
            scheduler.set_paused(paused=False)
            gate.clear()
            entered.clear()
            await scheduler.check_intervals()
            task = scheduler._scan_tasks[library.id]
            await asyncio.wait_for(entered.wait(), 2)
            await scheduler.check_intervals()
            assert scheduler._scan_tasks[library.id] is task
            gate.set()
            await asyncio.wait_for(task, 2)
            await scheduler.check_intervals()
            assert not scheduler._scan_tasks
        finally:
            gate.set()
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_scoped_deadline_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    async def discover(*_args, target_paths, **_kwargs) -> ScanReport:
        nonlocal now
        if target_paths is not None:
            now += timedelta(seconds=20)
            if outcome == "failed":
                raise OSError("Unavailable storage.")
            return ScanReport(cancelled=outcome == "cancelled")
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("scoped-deadline") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=3600, watch=True)
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            events=bus,
            clock=lambda: now,
        )
        try:
            await scheduler.check_intervals()
            await scheduler.scan_now(library.id)
            completed, due = scheduler._full_completed[library.id], scheduler._due[library.id]
            now += timedelta(minutes=5)
            scheduler.queue_scan(library.id, "watch", target_paths={tmp_path / "movie.mkv"})
            await asyncio.wait_for(scheduler._scan_tasks[library.id], 2)
            assert scheduler._full_completed[library.id] == completed
            assert scheduler._due[library.id] == due
            state = bus.library_snapshot(library.id)
            assert state["discovery"]["latest_result"] == outcome
            assert datetime.fromisoformat(state["discovery"]["finished_at"]) == now
            assert datetime.fromisoformat(state["scanner"]["next_run_at"]) == due
            async with factory() as session:
                current = await session.get(Library, library.id)
                assert current is not None
                current.scan_interval_s = 7200
                await session.commit()
            await scheduler.check_intervals()
            assert scheduler._due[library.id] == completed + timedelta(hours=2)
        finally:
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "paused", "shutdown"])
async def test_scoped_cancel_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    waiting = asyncio.Event()
    calls = 0

    class ResumeEvent(asyncio.Event):
        async def wait(self) -> Literal[True]:
            waiting.set()
            return await super().wait()

    async def discover(*_args, **_kwargs) -> ScanReport:
        nonlocal calls
        calls += 1
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("scoped-cancel-deadline") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=3600)
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            events=bus,
            clock=lambda: now,
        )
        try:
            await scheduler.check_intervals()
            await scheduler.scan_now(library.id)
            completed, due = scheduler._full_completed[library.id], scheduler._due[library.id]
            now += timedelta(minutes=5)
            if phase == "paused":
                scheduler._resumed = ResumeEvent()
                scheduler.set_paused(paused=True)
            scheduler.queue_scan(library.id, "watch", target_paths={tmp_path / "movie.mkv"})
            task = scheduler._scan_tasks[library.id]
            if phase == "paused":
                await asyncio.wait_for(waiting.wait(), 2)
            if phase == "shutdown":
                async with asyncio.timeout(3):
                    await scheduler.aclose()
            else:
                assert scheduler.cancel_scan(library.id) == "cancelling"
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert calls == 1 and scheduler._full_completed[library.id] == completed
            assert scheduler._due[library.id] == due
            state = bus.library_snapshot(library.id)
            assert state["discovery"]["latest_result"] == "cancelled"
            assert datetime.fromisoformat(state["discovery"]["finished_at"]) == now
            assert datetime.fromisoformat(state["scanner"]["next_run_at"]) == due
        finally:
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
async def test_watch_keeps_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    watch_entered, watch_release = asyncio.Event(), asyncio.Event()
    full_entered, full_release, followed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[set[Path] | None] = []
    active, after = tmp_path / "active.mkv", tmp_path / "after.mkv"

    async def discover(*_args, target_paths, **_kwargs) -> ScanReport:
        calls.append(target_paths)
        if target_paths is None:
            full_entered.set()
            await asyncio.wait_for(full_release.wait(), 3)
        elif active in target_paths:
            watch_entered.set()
            await asyncio.wait_for(watch_release.wait(), 3)
        elif after in target_paths:
            followed.set()
        return ScanReport()

    async def debounce(_seconds: float) -> None:
        return

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("watch-full-interval") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=60, watch=True)
            session.add(library)
            await session.commit()
        scheduler = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            clock=lambda: now,
            sleep=debounce,
        )
        try:
            await scheduler.check_intervals()
            due = scheduler._due[library.id]
            for minute in range(1, 5):
                now += timedelta(seconds=10)
                scheduler.queue_scan(library.id, "watch", target_paths={tmp_path / f"movie-{minute}.mkv"})
                await asyncio.wait_for(scheduler._scan_tasks[library.id], 2)
                await scheduler.check_intervals()
                assert scheduler._due[library.id] == due
            now += timedelta(seconds=10)
            scheduler.queue_scan(library.id, "watch", target_paths={active})
            watch = scheduler._scan_tasks[library.id]
            await asyncio.wait_for(watch_entered.wait(), 2)
            now = due
            scheduler.library_changed(library.id, {tmp_path / "pending.mkv"})
            await scheduler.check_intervals()
            scheduler.library_changed(library.id, {tmp_path / "late.mkv"})
            watch_release.set()
            await asyncio.wait_for(watch, 2)
            await asyncio.wait_for(full_entered.wait(), 2)
            full = scheduler._scan_tasks[library.id]
            assert calls == [{tmp_path / f"movie-{minute}.mkv"} for minute in range(1, 5)] + [{active}, None]
            assert scheduler._due[library.id] == due and library.id not in scheduler._full_completed
            scheduler.library_changed(library.id, {after})
            now += timedelta(seconds=10)
            full_release.set()
            await asyncio.wait_for(full, 2)
            await asyncio.wait_for(followed.wait(), 2)
            await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
            assert calls[-1] == {after}
            assert scheduler._full_completed[library.id] == now
            assert scheduler._due[library.id] == now + timedelta(seconds=60)
            assert not scheduler._pending and not scheduler._dirty
        finally:
            watch_release.set()
            full_release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_pause_defers_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    visited: list[int] = []

    class ResumeEvent(asyncio.Event):
        async def wait(self) -> Literal[True]:
            waiting.set()
            return await super().wait()

    async def discover(_session, library, **_kwargs) -> ScanReport:
        visited.append(library.id)
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("scanner-pause") as factory:
        async with factory() as session:
            first = Library(name="first", roots=[str(tmp_path)])
            second = Library(name="second", roots=[str(tmp_path)])
            third = Library(name="third", roots=[str(tmp_path)])
            session.add_all([first, second, third])
            await session.commit()
        scheduler = ScannerService(factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1))
        scheduler._resumed = ResumeEvent()
        try:
            scheduler.queue_scan(first.id, "manual")
            first_task = scheduler._scan_tasks[first.id]
            await asyncio.wait_for(entered.wait(), 2)
            scheduler.queue_scan(second.id, "manual")
            second_task = scheduler._scan_tasks[second.id]
            scheduler.queue_scan(third.id, "manual")
            third_task = scheduler._scan_tasks[third.id]
            scheduler.set_paused(paused=True)
            await asyncio.wait_for(waiting.wait(), 2)
            release.set()
            await asyncio.wait_for(first_task, 2)
            assert visited == [first.id] and not second_task.done()
            scheduler.cancel_scan(second.id)
            await asyncio.wait_for(asyncio.gather(second_task, return_exceptions=True), 2)
            assert not third_task.done() and scheduler.paused
            async with factory() as session:
                library = await session.get(Library, first.id)
                assert library is not None and not library.suspended
            scheduler.set_paused(paused=False)
            await asyncio.wait_for(third_task, 2)
            assert visited == [first.id, third.id]
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_lookup_failure_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    get = AsyncSession.get

    async def unavailable(session, entity, *args, **kwargs):
        if entity is Library:
            raise OSError("Unavailable catalog.")
        return await get(session, entity, *args, **kwargs)

    async with database_context("scanner-lookup") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=3600)
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            events=bus,
            clock=lambda: now,
        )
        try:
            await scheduler.check_intervals()
            now += timedelta(hours=1)
            monkeypatch.setattr(AsyncSession, "get", unavailable)
            await scheduler.check_intervals()
            await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
            assert bus.library_snapshot(library.id)["discovery"]["latest_result"] == "failed"
            assert scheduler._due[library.id] == now + timedelta(hours=1)
            await scheduler.check_intervals()
            assert not scheduler._scan_tasks
        finally:
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("before_start", [False, True])
async def test_cancel_queued_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before_start: bool) -> None:
    entered, waiting, followup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    wanted_files = scan._wanted_files
    calls = 0
    now = datetime(2026, 1, 1, tzinfo=UTC)

    class ScanLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            if self.locked():
                waiting.set()
            return await super().acquire()

    def blocked_walk(*args, **kwargs) -> list[Path]:
        nonlocal calls
        calls += 1
        if calls == 1:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("Traversal was not released.")
        else:
            loop.call_soon_threadsafe(followup.set)
        return wanted_files(*args, **kwargs)

    async def debounce(_seconds: float) -> None:
        return

    monkeypatch.setattr(scan, "_wanted_files", blocked_walk)
    async with database_context("scanner-queued-cancel") as factory:
        async with factory() as session:
            first = Library(name="first", roots=[str(tmp_path)])
            second = Library(name="second", roots=[str(tmp_path)], scan_interval_s=3600)
            session.add_all([first, second])
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            events=bus,
            clock=lambda: now,
            sleep=debounce,
        )
        scheduler._scan_lock = ScanLock()
        try:
            await scheduler.check_intervals()
            scheduler.queue_scan(first.id, "manual")
            first_task = scheduler._scan_tasks[first.id]
            await asyncio.wait_for(entered.wait(), 2)
            scheduler.library_changed(first.id, {tmp_path})
            scheduler.queue_scan(second.id, "manual")
            second_task = scheduler._scan_tasks[second.id]
            if not before_start:
                await asyncio.wait_for(waiting.wait(), 2)
            now += timedelta(minutes=5)
            assert scheduler.cancel_scan(second.id) == "cancelling"
            await asyncio.wait_for(asyncio.gather(second_task, return_exceptions=True), 2)
            state = bus.library_snapshot(second.id)
            assert state["discovery"]["status"] == state["discovery"]["latest_result"] == "cancelled"
            assert not state["scanner"]["discovery_pending"]
            assert scheduler._due[second.id] == now + timedelta(hours=1)
            assert datetime.fromisoformat(state["scanner"]["next_run_at"]) == scheduler._due[second.id]
            assert scheduler.cancel_scan(second.id) == "not_running"
            assert second.id not in scheduler._scan_tasks
            assert scheduler._scan_tasks[first.id] is first_task and not first_task.done()
            assert scheduler._scan_lock.locked() and calls == 1
            assert not scheduler._stops[first.id].is_set()
            release.set()
            report = await asyncio.wait_for(first_task, 2)
            assert report is not None and not report.cancelled
            await asyncio.wait_for(followup.wait(), 2)
            await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
            assert calls == 2
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_cancel_drains_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scoped: bool) -> None:
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    wanted_files = scan._wanted_files

    def blocked_walk(*args, **kwargs) -> list[Path]:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise TimeoutError("Traversal was not released.")
        return wanted_files(*args, **kwargs)

    monkeypatch.setattr(scan, "_wanted_files", blocked_walk)
    async with database_context("scanner-cancel") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=3600)
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), events=bus
        )
        try:
            scheduler.queue_scan(
                library.id, "watch" if scoped else "manual", target_paths={tmp_path} if scoped else None
            )
            task = scheduler._scan_tasks[library.id]
            await asyncio.wait_for(entered.wait(), 2)
            assert scheduler.cancel_scan(library.id) == "cancelling"
            scheduler.library_changed(library.id, {tmp_path / "movie.mkv"})
            assert not task.done()
            state = bus.library_snapshot(library.id)
            assert state["discovery"]["status"] == "cancelling"
            assert state["scanner"]["discovery_pending"]
            release.set()
            report = await asyncio.wait_for(task, 3)
            assert report is not None and report.cancelled and report.pruned == 0
            state = bus.library_snapshot(library.id)
            assert state["discovery"]["latest_result"] == "cancelled"
            assert state["discovery"]["error_count"] == 0
            assert not state["scanner"]["discovery_pending"]
            assert not scheduler._debounce_tasks
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
async def test_cancel_keeps_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("first.mkv", "second.mkv"):
        (tmp_path / name).write_bytes(b"media")
    stopped = threading.Event()
    stat_files = scan._stat_files
    calls = 0

    def stop_second(paths, errors):
        nonlocal calls
        calls += 1
        files = stat_files(paths, errors)
        if calls == 2:
            stopped.set()
        return files

    monkeypatch.setattr(scan, "COMMIT_BATCH", 1)
    monkeypatch.setattr(scan, "_stat_files", stop_second)
    async with database_context("scanner-batches") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            missing = MediaFile(
                library_id=library.id,
                path=str(tmp_path / "missing.mkv"),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="other",
                last_scanned_at=utcnow(),
            )
            session.add(missing)
            await session.commit()
            report = await scan.scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
                stopped=stopped,
            )
            assert report.cancelled and report.added == 1 and report.pruned == 0
        async with factory() as session:
            rows = (await session.scalars(select(MediaFile).order_by(MediaFile.path))).all()
            assert [Path(row.path).name for row in rows] == ["first.mkv", "missing.mkv"]
            assert all(row.probe_id is None and row.health_status == "pending" for row in rows)


@pytest.mark.asyncio
async def test_api_scanner_snapshot(tmp_path: Path) -> None:
    async with database_context("scanner-api") as factory:
        app = create_app(factory)
        try:
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                assert (await client.get("/api/libraries/scanner")).json() == {"paused": False, "failed": False}
                assert (await client.get("/api/libraries/progress")).json() == {
                    "scanner": {"paused": False, "failed": False},
                    "probing": {"failed": False},
                    "libraries": [],
                }
                assert (await client.post("/api/libraries/scanner/pause")).json() == {"paused": True, "failed": False}
                assert (await client.get("/api/libraries/progress")).json() == {
                    "scanner": {"paused": True, "failed": False},
                    "probing": {"failed": False},
                    "libraries": [],
                }
                response = await client.post("/api/libraries", json={"name": "library", "roots": [str(tmp_path)]})
                library = response.json()
                assert library["scan_interval_s"] is None
                identifier = library["id"]
                suspended = (
                    await client.post(
                        "/api/libraries", json={"name": "suspended", "roots": [str(tmp_path)], "suspended": True}
                    )
                ).json()
                initial = (await client.get("/api/libraries/progress")).json()
                assert all(row["scanner"]["paused"] for row in initial["libraries"])
                app.state.events.update_library_progress(identifier, probing={"remaining": 1})
                assert app.state.events.library_progress[identifier]["scanner"]["paused"]
                for action, paused in (("pause", True), ("resume", False)):
                    async with app.state.scanner._scan_lock:
                        response = await asyncio.wait_for(client.post(f"/api/libraries/scanner/{action}"), 2)
                    assert response.status_code == 200 and response.json()["paused"] is paused
                    snapshot = (await client.get("/api/libraries/progress")).json()
                    assert snapshot["scanner"]["paused"] is paused
                    assert all(row["scanner"]["paused"] is paused for row in snapshot["libraries"])
                    assert (await client.get(f"/api/libraries/{identifier}")).json() == library
                    assert (await client.get(f"/api/libraries/{suspended['id']}")).json() == suspended
                    if paused:
                        assert (await client.post(f"/api/libraries/{identifier}/scan")).status_code == 409
                await client.post("/api/libraries/scanner/pause")
                await client.patch(f"/api/libraries/{suspended['id']}", json={"suspended": False})
                assert (await client.get("/api/libraries/scanner")).json()["paused"]
                assert (await client.post(f"/api/libraries/{suspended['id']}/scan")).status_code == 409
                response = await client.post(f"/api/libraries/{identifier}/scan/cancel")
                assert response.json()["status"] == "not_running"
                for interval, status in ((3600, 200), (None, 200), (0, 422), (-1, 422)):
                    response = await client.patch(f"/api/libraries/{identifier}", json={"scan_interval_s": interval})
                    assert response.status_code == status
                    if status == 200:
                        assert response.json()["scan_interval_s"] == interval
                response = await client.patch(f"/api/libraries/{identifier}", json={"schedule": {"type": "unknown"}})
                assert response.status_code == 422
        finally:
            await app.state.events.aclose()


@pytest.mark.asyncio
async def test_restart_retains_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        entered.set()
        await asyncio.wait_for(release.wait(), 3)

    async with database_context("scanner-restart") as factory:
        async with factory() as session:
            library = Library(name="suspended", roots=[str(tmp_path)], suspended=True)
            session.add(library)
            await session.commit()
        bus = EventBus()
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), events=bus
        )
        monkeypatch.setattr(scheduler, "_startup", run)
        scheduler.set_paused(paused=True)
        scheduler.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert scheduler.paused
            state = bus.library_snapshot(1)
            assert state["scanner"]["paused"] and state["discovery"]["status"] == "idle"
            scheduler.set_paused(paused=True)
            await asyncio.wait_for(scheduler.aclose(), 3)
            entered.clear()
            scheduler.start()
            await asyncio.wait_for(entered.wait(), 2)
            assert scheduler.paused and bus.scanner_paused
            async with factory() as session:
                stored = await session.get(Library, library.id)
                assert stored is not None and stored.suspended
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)
            await bus.aclose()


@pytest.mark.asyncio
async def test_background_pause_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def probe(*_args, **_kwargs) -> dict:
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    async with database_context("scanner-probes") as factory:
        async with factory() as session:
            libraries = [Library(name=name, roots=[str(tmp_path / name)]) for name in ("first", "second")]
            session.add_all(libraries)
            await session.commit()
            for library in libraries:
                root = Path(library.roots[0])
                root.mkdir()
                (root / "movie.mkv").write_bytes(b"media")
                await scan.scan_library(
                    session,
                    library,
                    config=ScannerConfig(),
                    staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
                )
        bus = EventBus(clock=lambda: 0)
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), events=bus
        )
        service = ProbeService(
            factory,
            "fake-probe",
            "fake-editor",
            workspace_dir=tmp_path / "workspace",
            events=bus,
            max_concurrency=1,
            scanner_paused=lambda: scheduler.paused,
        )
        batches = 0
        pending = service._claim

        async def pending_batch(kind: str):
            nonlocal batches
            batches += 1
            return await pending(kind)

        monkeypatch.setattr(service, "_claim", pending_batch)
        try:
            await service._fill()
            await asyncio.wait_for(entered.wait(), 2)
            scheduler.set_paused(paused=True)
            assert (await service.progress_snapshot())[1]["probing"]["status"] == "scanning"
            release.set()
            await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
            previous_batches = batches
            await service._fill()
            assert batches == previous_batches
            state = (await service.progress_snapshot())[0]
            assert state["probing"]["status"] == "paused" and state["probing"]["remaining"] == 1
            assert state["scanner"]["paused"]
            assert calls == 1
            scheduler.set_paused(paused=False)
            await service._fill()
            await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
            assert batches > previous_batches
            assert calls == 2 and all(row["probing"]["remaining"] == 0 for row in await service.progress_snapshot())
        finally:
            release.set()
            await asyncio.wait_for(service.close(), 3)
            await bus.aclose()


@pytest.mark.asyncio
async def test_stats_pause_purpose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"media")
    edits = 0

    async def probe(*_args, **_kwargs) -> dict:
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}

    async def edit(*_args, **_kwargs) -> None:
        nonlocal edits
        edits += 1

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    async with database_context("scanner-stats") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], mkv_stats=True)
            session.add(library)
            await session.commit()
            await scan.scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            )
            media_id = await session.scalar(select(MediaFile.id))
            assert media_id is not None
        scheduler = ScannerService(factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1))
        scheduler.set_paused(paused=True)
        fs = LibraryFS(
            library.id,
            sessionmaker=factory,
            ffprobe_bin="fake-probe",
            mkvpropedit_bin="fake-editor",
            workspace_dir=tmp_path / "workspace",
            stats_allowed=lambda: not scheduler.paused,
        )
        with pytest.raises(LibraryProbeError) as refusal:
            from tests.helpers import probe_media

            await probe_media(fs, media_id)
        assert refusal.value.cleanup == "refused" and edits == 0
        async with factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.health_status == "pending" and media.probe_id is None
            assert media.source_identity == source_identity(source.stat())
        result = await fs.refresh_current(media_id)
        assert edits == 1 and result.media.health_status == "healthy"


@pytest.mark.asyncio
async def test_resume_during_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    completed, release, probed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paused, hold = True, False

    async def probe(*_args, **_kwargs) -> dict:
        probed.set()
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    async with database_context("scanner-resume") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.commit()
            (tmp_path / "movie.mkv").write_bytes(b"media")
            await scan.scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            )
        service = ProbeService(
            factory,
            "fake-probe",
            "fake-editor",
            workspace_dir=tmp_path / "workspace",
            scanner_paused=lambda: paused,
        )
        refresh = service._refresh_progress

        async def refresh_progress() -> None:
            nonlocal hold
            await refresh()
            if hold:
                hold = False
                completed.set()
                await asyncio.wait_for(release.wait(), 3)

        monkeypatch.setattr(service, "_refresh_progress", refresh_progress)
        try:
            await service.start()
            hold = True
            await asyncio.wait_for(completed.wait(), 2)
            paused = False
            service.wake()
            release.set()
            await asyncio.wait_for(probed.wait(), 2)
            await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 2)
        finally:
            release.set()
            await asyncio.wait_for(service.close(), 3)
