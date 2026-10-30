from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from queue import Queue
from typing import Literal

import pytest
import pytest_asyncio
from server.settings import ScannerConfig
from server.events import EventBus
from server.models import Library, utcnow
from server.plugins.host import PluginHost
from server.scanner import service as scheduler_service
from server.scanner.service import ScannerService
from server.scanner.scan import ScanReport
from server.staging import StagingService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.helpers import database_context, make_test_temp_dir
from watchfiles import Change


class CapturingEvents(EventBus):
    def __init__(self, captured: list[dict]) -> None:
        super().__init__()
        self.captured = captured

    def publish(self, type_: str, payload: dict, key: str | None = None, *, bulk: bool = False) -> None:
        if type_ == "library.scan":
            self.captured.append(payload)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("scheduler") as factory:
        yield factory


@pytest.fixture
def staging(factory: async_sessionmaker[AsyncSession]) -> StagingService:
    return StagingService(factory, make_test_temp_dir("scheduler-workspace"), min_free_gb=1)


@pytest.fixture
def scan_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_scan(
        session: AsyncSession,
        library: Library,
        *,
        config: ScannerConfig,
        staging: StagingService,
        events: EventBus | None = None,
        trash_dir: Path | None = None,
        on_progress: Callable[[ScanReport], None] | None = None,
        on_files_pending: Callable[[], None] | None = None,
        stopped: threading.Event | None = None,
        target_paths: set[Path] | None = None,
    ) -> ScanReport:
        calls.append(library.name)
        return ScanReport(found=2, added=1, updated=1)

    monkeypatch.setattr(scheduler_service, "scan_library", fake_scan)
    return calls


async def add_library(
    factory: async_sessionmaker[AsyncSession],
    lib_dir: Path,
    name: str,
    *,
    watch: bool = False,
    scan_on_start: bool = False,
) -> int:
    async with factory() as session:
        library = Library(name=name, roots=[str(lib_dir)], watch=watch, scan_on_start=scan_on_start)
        session.add(library)
        await session.commit()
        return library.id


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_discovery_progress_snapshot(
    factory, staging, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    identifier = await add_library(factory, tmp_path, "progress")
    entered, release = asyncio.Event(), asyncio.Event()
    bus = EventBus(min_interval_s=0)
    subscription = bus.subscribe()

    async def scan(_session, _library, *, on_progress, **_kwargs) -> ScanReport:
        report = ScanReport(found=7, added=2, updated=1)
        on_progress(report)
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        if failed:
            raise RuntimeError("Discovery failed.")
        return report

    monkeypatch.setattr(scheduler_service, "scan_library", scan)
    scheduler = ScannerService(factory, staging=staging, events=bus)
    try:
        assert scheduler.queue_scan(identifier, "manual") == "queued"
        task = scheduler._scan_tasks[identifier]
        assert bus.library_snapshot(identifier)["discovery"]["status"] == "queued"
        await asyncio.wait_for(entered.wait(), 2)
        state = bus.library_snapshot(identifier)
        assert state["discovery"] == {
            "status": "scanning",
            "found": 7,
            "added": 2,
            "updated": 1,
            "error_count": 0,
            "error": None,
            "reason": "manual",
            "latest_result": None,
            "finished_at": None,
        }
        event = await asyncio.wait_for(subscription.next(), 2)
        assert event is not None and event.type == "library.progress" and event.payload == state
        release.set()
        await asyncio.wait_for(task, 3)
        state = bus.library_snapshot(identifier)["discovery"]
        assert state["status"] == ("failed" if failed else "completed")
        assert state["found"] == 7 and state["added"] == 2 and state["updated"] == 1
        if failed:
            assert "Discovery failed." in state["error"]
        else:
            assert state["error"] is None
    finally:
        release.set()
        tasks = list(scheduler._scan_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        bus.unsubscribe(subscription)
        await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["roots", "suspend", "remove", "disable"])
async def test_preserves_pending_watch(factory, staging, monkeypatch, scan_log, change: str) -> None:
    first = await add_library(factory, make_test_temp_dir("watch-first"), "first", watch=True)
    second = await add_library(factory, make_test_temp_dir("watch-second"), "second", watch=True)
    watching = {first: asyncio.Event(), second: asyncio.Event()}
    release, pending_started = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    closed: list[list[str]] = []

    class Watcher:
        def __init__(self, paths, **_kwargs):
            self.paths = paths

        def __repr__(self):
            return "RustNotify(Recommended(test))"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(self.paths)

        def watch(self, *, stop_event, **_kwargs):
            identifier = second if self.paths == [str(second_root)] else first
            loop.call_soon_threadsafe(watching[identifier].set)
            assert stop_event.wait(3), "Native watcher was not stopped."
            return "stop"

    async with factory() as session:
        library = await session.get(Library, second)
        assert library is not None
        second_root = Path(library.roots[0])

    async def sleep(_seconds: float) -> None:
        pending_started.set()
        await asyncio.wait_for(release.wait(), 3)

    scheduler = ScannerService(factory, staging=staging, sleep=sleep)
    monkeypatch.setattr(scheduler_service, "RustNotify", Watcher)
    scheduler._running = True
    try:
        await scheduler.update_watchers()
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in watching.values())), 2)
        original = scheduler._watch_tasks[first]
        unaffected = scheduler._watch_tasks[second]
        scheduler.library_changed(second, {second_root / "movie.mkv"})
        await asyncio.wait_for(pending_started.wait(), 2)
        pending = scheduler._debounce_tasks[second]
        watching[first].clear()
        async with scheduler.library_mutation(), factory() as session:
            library = await session.get(Library, first)
            assert library is not None
            if change == "roots":
                library.roots = [str(make_test_temp_dir("watch-edited"))]
            elif change == "suspend":
                library.suspended = True
            elif change == "disable":
                library.watch = False
            else:
                await session.delete(library)
            await session.commit()
        assert original.cancelled()
        assert len(closed) == 1
        assert scheduler._watch_tasks[second] is unaffected and not unaffected.done()
        assert scheduler._debounce_tasks[second] is pending and not pending.done()
        if change == "roots":
            await asyncio.wait_for(watching[first].wait(), 2)
            assert scheduler._watch_tasks[first] is not original
        else:
            assert first not in scheduler._watch_tasks
        release.set()
        await asyncio.wait_for(pending, 2)
        await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
        assert scan_log == ["second"]
    finally:
        release.set()
        await asyncio.wait_for(scheduler.aclose(), 3)
    assert len(closed) == (3 if change == "roots" else 2)


@pytest.mark.asyncio
async def test_manual_event_reason(factory, staging, scan_log) -> None:
    library_id = await add_library(factory, make_test_temp_dir("manual-event"), "manual-library")
    events: list[dict] = []
    wakes: list[ScanReport] = []

    bus = CapturingEvents(events)
    scheduler = ScannerService(factory, staging=staging, events=bus, on_scan_complete=wakes.append)
    report = await scheduler.scan_now(library_id)

    assert report is not None and wakes == [report]
    event = next(event for event in events if "library_name" in event)
    assert event["reason"] == "manual"
    assert event["library_name"] == "manual-library"


@pytest.mark.asyncio
async def test_manual_failure_library(factory, staging, monkeypatch) -> None:
    library_id = await add_library(factory, make_test_temp_dir("manual-failure"), "failed-library")
    events: list[dict] = []

    async def fail_scan(*_args, **_kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(scheduler_service, "scan_library", fail_scan)
    bus = CapturingEvents(events)
    scheduler = ScannerService(factory, staging=staging, events=bus)
    await scheduler.scan_now(library_id)

    assert {key: value for key, value in events[0].items() if key != "error"} == {
        "status": "failed",
        "reason": "manual",
        "library_id": library_id,
        "library_name": "failed-library",
    }


@pytest.mark.asyncio
async def test_scan_start_flagged(
    factory: async_sessionmaker[AsyncSession], staging: StagingService, scan_log: list[str]
) -> None:
    flagged = make_test_temp_dir("sched-flagged")
    plain = make_test_temp_dir("sched-plain")
    await add_library(factory, flagged, "flagged", scan_on_start=True)
    await add_library(factory, plain, "plain")

    scheduler = ScannerService(factory, staging=staging)
    await scheduler._scan_on_start()
    await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)

    assert scan_log == ["flagged"]


@pytest.mark.asyncio
@pytest.mark.parametrize("watch", [False, True])
async def test_interval_per_library(
    factory: async_sessionmaker[AsyncSession], staging: StagingService, scan_log: list[str], watch: bool
) -> None:
    lib_dir = make_test_temp_dir("sched-interval")
    identifier = await add_library(factory, lib_dir, "interval", watch=watch)
    await add_library(factory, lib_dir, "off")
    async with factory() as session:
        library = await session.get(Library, identifier)
        assert library is not None
        library.scan_interval_s = 1800
        await session.commit()
    now = utcnow()
    scheduler = ScannerService(factory, staging=staging, clock=lambda: now)
    await scheduler.check_intervals()
    assert not scan_log
    now += timedelta(seconds=1800)
    await scheduler.check_intervals()
    await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
    assert scan_log == ["interval"]
    assert scheduler._due[identifier] == now + timedelta(seconds=1800)
    await scheduler.check_intervals()
    assert scan_log == ["interval"]


@pytest.mark.asyncio
async def test_debounce_collapses_burst(
    factory: async_sessionmaker[AsyncSession], staging: StagingService, scan_log: list[str]
) -> None:
    lib_dir = make_test_temp_dir("sched-debounce")
    library_id = await add_library(factory, lib_dir, "debounced")

    scheduler = ScannerService(factory, staging=staging, debounce_s=0.05)
    release = asyncio.Event()

    async def controlled_sleep(_: float) -> None:
        await release.wait()

    scheduler._sleep = controlled_sleep
    for _ in range(3):
        scheduler.library_changed(library_id, {lib_dir / "movie.mkv"})
    release.set()
    await asyncio.wait_for(asyncio.gather(*scheduler._debounce_tasks.values()), timeout=2)
    await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), timeout=2)
    assert scan_log == ["debounced"]


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_watch_scopes_merge(factory, staging, tmp_path, monkeypatch, paused: bool) -> None:
    identifier = await add_library(factory, tmp_path, "scoped")
    debounce, entered = asyncio.Event(), asyncio.Event()
    calls: list[set[Path] | None] = []

    async def sleep(_seconds):
        entered.set()
        await asyncio.wait_for(debounce.wait(), 3)

    async def discover(_session, library, *, target_paths, **_kwargs):
        assert library.roots == [str(tmp_path)]
        calls.append(target_paths)
        return ScanReport()

    monkeypatch.setattr(scheduler_service, "scan_library", discover)
    scheduler = ScannerService(factory, staging=staging, sleep=sleep)
    scheduler.set_paused(paused=paused)
    branch, other = tmp_path / "branch", tmp_path / "other.mkv"
    try:
        scheduler.library_changed(identifier, {branch / "movie.mkv"})
        scheduler.library_changed(identifier, {branch, other})
        if paused:
            assert identifier in scheduler._dirty and not scheduler._debounce_tasks
            scheduler.set_paused(paused=False)
        await asyncio.wait_for(entered.wait(), 2)
        debounce.set()
        await asyncio.wait_for(asyncio.gather(*scheduler._debounce_tasks.values()), 2)
        await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
        assert calls == [{branch, other}]
        assert not scheduler._dirty and not scheduler._pending and not scheduler._requests
    finally:
        debounce.set()
        await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
async def test_queued_scope_supersession(factory, staging, tmp_path, monkeypatch, full: bool) -> None:
    identifier = await add_library(factory, tmp_path, "queued")
    calls: list[set[Path] | None] = []

    async def discover(_session, _library, *, target_paths, **_kwargs):
        calls.append(target_paths)
        return ScanReport()

    monkeypatch.setattr(scheduler_service, "scan_library", discover)
    scheduler = ScannerService(factory, staging=staging)
    scheduler.set_paused(paused=True)
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    try:
        scheduler.queue_scan(identifier, "watch", target_paths={first})
        task = scheduler._scan_tasks[identifier]
        scheduler.library_changed(identifier, {second})
        if full:
            assert scheduler.queue_scan(identifier, "manual") == "already_queued"
        scheduler.set_paused(paused=False)
        await asyncio.wait_for(task, 2)
        assert calls == ([None] if full else [{first, second}])
        assert not scheduler._pending and not scheduler._dirty
    finally:
        await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_scan_scope_logging(factory, staging, tmp_path, scan_log, caplog, scoped: bool) -> None:
    identifier = await add_library(factory, tmp_path, "logging")
    scheduler = ScannerService(factory, staging=staging)
    media = tmp_path / "movie.mkv"
    with caplog.at_level(logging.DEBUG, logger=scheduler_service.__name__):
        scheduler.queue_scan(identifier, "watch" if scoped else "manual", target_paths={media} if scoped else None)
        await asyncio.wait_for(scheduler._scan_tasks[identifier], 2)
    records = [record for record in caplog.records if record.name == scheduler_service.__name__]
    assert len(records) == 2 and scan_log == ["logging"]
    assert all(record.__dict__["scope"] == ("scoped" if scoped else "full") for record in records)
    started = next(record for record in records if getattr(record, "operation", None) == "library_scan_start")
    assert getattr(started, "paths") == [str(media if scoped else tmp_path)]


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("paused", [False, True])
async def test_active_scope_followup(factory, staging, tmp_path, monkeypatch, full: bool, paused: bool) -> None:
    identifier = await add_library(factory, tmp_path, "active")
    entered, release, followed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[set[Path] | None] = []

    async def discover(_session, _library, *, target_paths, **_kwargs):
        calls.append(target_paths)
        if len(calls) == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        else:
            followed.set()
        return ScanReport()

    async def sleep(_seconds):
        return

    monkeypatch.setattr(scheduler_service, "scan_library", discover)
    scheduler = ScannerService(factory, staging=staging, sleep=sleep)
    first, second = tmp_path / "first.mkv", tmp_path / "branch"
    try:
        scheduler.queue_scan(identifier, "watch", target_paths={first})
        task = scheduler._scan_tasks[identifier]
        await asyncio.wait_for(entered.wait(), 2)
        scheduler.set_paused(paused=paused)
        scheduler.library_changed(identifier, {second / "movie.mkv"})
        scheduler.library_changed(identifier, {second})
        if full:
            scheduler.queue_scan(identifier, "interval")
            scheduler.library_changed(identifier, {tmp_path / "late.mkv"})
        release.set()
        await asyncio.wait_for(task, 2)
        scheduler.set_paused(paused=False)
        await asyncio.wait_for(followed.wait(), 2)
        await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
        assert calls == [{first}, None if full else {second}]
        assert not scheduler._pending and not scheduler._dirty
    finally:
        release.set()
        await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_debounce_full_supersedes(factory, staging, tmp_path, monkeypatch) -> None:
    identifier = await add_library(factory, tmp_path, "superseded")
    entered = asyncio.Event()
    calls: list[set[Path] | None] = []

    async def sleep(_seconds):
        entered.set()
        await asyncio.wait_for(asyncio.Event().wait(), 3)

    async def discover(_session, _library, *, target_paths, **_kwargs):
        calls.append(target_paths)
        return ScanReport()

    monkeypatch.setattr(scheduler_service, "scan_library", discover)
    scheduler = ScannerService(factory, staging=staging, sleep=sleep)
    try:
        scheduler.library_changed(identifier, {tmp_path / "movie.mkv"})
        await asyncio.wait_for(entered.wait(), 2)
        debounce = scheduler._debounce_tasks[identifier]
        scheduler.queue_scan(identifier, "manual")
        await asyncio.wait_for(asyncio.gather(debounce, return_exceptions=True), 2)
        await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values()), 2)
        assert debounce.cancelled() and calls == [None]
        assert not scheduler._pending and not scheduler._debounce_tasks
    finally:
        await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_scope_queue_revalidates(factory, staging, tmp_path) -> None:
    from server.models import MediaFile
    from sqlalchemy import select

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    media = old / "movie.mkv"
    media.write_bytes(b"media")
    identifier = await add_library(factory, old, "edited")
    scheduler = ScannerService(factory, staging=staging)
    scheduler.set_paused(paused=True)
    try:
        scheduler.queue_scan(identifier, "watch", target_paths={media})
        task = scheduler._scan_tasks[identifier]
        async with factory() as session:
            library = await session.get(Library, identifier)
            assert library is not None
            library.roots = [str(new)]
            await session.commit()
        scheduler.set_paused(paused=False)
        report = await asyncio.wait_for(task, 2)
        assert report is not None and report.found == report.pruned == 0
        async with factory() as session:
            assert list(await session.scalars(select(MediaFile))) == []
    finally:
        await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["paused", "debounce", "queued"])
@pytest.mark.parametrize("change", ["roots", "suspend", "remove"])
async def test_pending_scopes_retire(factory, staging, tmp_path, monkeypatch, phase: str, change: str) -> None:
    identifier = await add_library(factory, tmp_path, "retired", watch=True)
    debouncing = asyncio.Event()

    async def watch(_library):
        await asyncio.wait_for(asyncio.Event().wait(), 3)

    async def sleep(_seconds):
        debouncing.set()
        await asyncio.wait_for(asyncio.Event().wait(), 3)

    scheduler = ScannerService(factory, staging=staging, sleep=sleep)
    monkeypatch.setattr(scheduler, "_watch_library", watch)
    scheduler._running = True
    try:
        await scheduler.update_watchers()
        scheduler.set_paused(paused=phase != "debounce")
        if phase == "queued":
            scheduler.queue_scan(identifier, "watch", target_paths={tmp_path / "movie.mkv"})
        else:
            scheduler.library_changed(identifier, {tmp_path / "movie.mkv"})
        if phase == "debounce":
            await asyncio.wait_for(debouncing.wait(), 2)
        async with scheduler.library_mutation(), factory() as session:
            library = await session.get(Library, identifier)
            assert library is not None
            if change == "roots":
                library.roots = [str(tmp_path / "new")]
            elif change == "suspend":
                library.suspended = True
            else:
                await session.delete(library)
            await session.commit()
        await asyncio.wait_for(asyncio.gather(*scheduler._scan_tasks.values(), return_exceptions=True), 2)
        assert identifier not in scheduler._pending
        assert identifier not in scheduler._requests
        assert identifier not in scheduler._dirty
        assert identifier not in scheduler._debounce_tasks
    finally:
        await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_scans_run_serialized(
    factory: async_sessionmaker[AsyncSession], staging: StagingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib_a = make_test_temp_dir("sched-ser-a")
    lib_b = make_test_temp_dir("sched-ser-b")
    id_a = await add_library(factory, lib_a, "ser-a")
    id_b = await add_library(factory, lib_b, "ser-b")

    active = peak = 0
    entered, waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ScanLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            if self.locked():
                waiting.set()
            return await super().acquire()

    async def gated_scan(_session: AsyncSession, _library: Library, **_kwargs: object) -> ScanReport:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=3)
        finally:
            active -= 1
        return ScanReport()

    monkeypatch.setattr(scheduler_service, "scan_library", gated_scan)
    scheduler = ScannerService(factory, staging=staging)
    scheduler._scan_lock = ScanLock()

    tasks = [asyncio.create_task(scheduler.scan_now(id_a))]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        tasks.append(asyncio.create_task(scheduler.scan_now(id_b)))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert active == 1 and not tasks[1].done()
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=2) == [ScanReport(), ScanReport()]
        assert peak == 1
    finally:
        release.set()
        await asyncio.wait_for(scheduler.aclose(), timeout=3)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_watcher_change(
    factory: async_sessionmaker[AsyncSession],
    staging: StagingService,
    scan_log: list[str],
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    lib_dir = make_test_temp_dir("sched-watch")
    identifier = await add_library(factory, lib_dir, "watched", watch=True)

    scan_complete = asyncio.Event()
    watching, debounce, debounce_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    changes: Queue[set[tuple[int, str]] | str] = Queue()
    loop_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    closed = threading.Event()

    async def sleep(seconds: float) -> None:
        assert seconds == 30
        debounce_started.set()
        await asyncio.wait_for(debounce.wait(), 3)

    scheduler = ScannerService(factory, staging=staging, sleep=sleep, on_scan_complete=lambda _: scan_complete.set())

    class Watcher:
        def __init__(self, paths, **kwargs):
            assert paths == [str(lib_dir)]
            assert kwargs == {
                "debug": False,
                "force_polling": False,
                "poll_delay_ms": 300,
                "recursive": True,
                "ignore_permission_denied": False,
            }
            assert threading.get_ident() != loop_thread

        def __repr__(self):
            return "RustNotify(Recommended(test))"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.set()

        def watch(self, *, stop_event, timeout_ms, step_ms, **_kwargs):
            assert timeout_ms == 1000 and step_ms == 50
            loop.call_soon_threadsafe(watching.set)
            return changes.get(timeout=3)

    monkeypatch.setenv("WATCHFILES_FORCE_POLLING", "true")
    monkeypatch.setenv("WATCHFILES_IGNORE_PERMISSION_DENIED", "true")
    monkeypatch.setattr(scheduler_service, "RustNotify", Watcher)
    scheduler.start()
    try:
        await asyncio.wait_for(watching.wait(), 2)
        assert not scan_log and not scheduler._debounce_tasks

        scheduler.set_paused(paused=paused)
        watching.clear()
        changes.put({(Change.modified, str(lib_dir / "movie.avi"))})
        await asyncio.wait_for(watching.wait(), 2)
        if paused:
            assert identifier in scheduler._dirty and not scheduler._debounce_tasks
            scheduler.set_paused(paused=False)
        await asyncio.wait_for(debounce_started.wait(), 2)
        pending = scheduler._debounce_tasks[identifier]

        assert scheduler._debounce_tasks[identifier] is pending and not scan_log

        debounce.set()
        await asyncio.wait_for(scan_complete.wait(), timeout=2)
        assert scan_log == ["watched"]
    finally:
        changes.put("stop")
        await asyncio.wait_for(scheduler.aclose(), 3)
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Linux inotify notifications only")
async def test_native_notification(factory, staging, tmp_path, monkeypatch) -> None:
    scheduler = ScannerService(factory, staging=staging)
    library = Library(id=1, name="native", roots=[str(tmp_path)])
    ready, changed = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    native = scheduler_service.RustNotify
    representations: list[str] = []
    threads: list[threading.Thread] = []
    identifiers: list[int] = []

    def watch(*args, **kwargs):
        watcher = native(*args, **kwargs)
        representations.append(repr(watcher))
        threads.append(threading.current_thread())
        loop.call_soon_threadsafe(ready.set)
        return watcher

    def library_changed(identifier: int, paths: set[Path]) -> None:
        assert tmp_path / "movie.mkv" in paths
        identifiers.append(identifier)
        changed.set()

    monkeypatch.setattr(scheduler_service, "RustNotify", watch)
    monkeypatch.setattr(scheduler, "library_changed", library_changed)
    watching = asyncio.create_task(scheduler._watch_library(library))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert len(representations) == 1
        assert representations[0].startswith("RustNotify(Recommended(")
        assert "INotifyWatcher" in representations[0]
        (tmp_path / "movie.mkv").write_bytes(b"fake media")
        await asyncio.wait_for(changed.wait(), 2)
        assert identifiers and set(identifiers) == {library.id}
    finally:
        watching.cancel()
        await asyncio.wait_for(asyncio.gather(watching, return_exceptions=True), 3)
    assert watching.cancelled() and len(threads) == 1 and not threads[0].is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["startup", "runtime", "polling"])
async def test_native_watch_failure(factory, staging, tmp_path, monkeypatch, caplog, failure: str) -> None:
    library = Library(name="watched", roots=[str(tmp_path)])
    scheduler = ScannerService(factory, staging=staging)
    calls: list[str] = []
    startup_error = OSError("Native watcher initialization failed.")
    runtime_error = OSError("Native watcher event stream failed.")

    class Watcher:
        def __init__(self, _paths, **_kwargs):
            calls.append("start")
            if failure == "startup":
                raise startup_error

        def __repr__(self):
            return "RustNotify(Poll(test))" if failure == "polling" else "RustNotify(Recommended(test))"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            calls.append("close")

        def watch(self, **_kwargs):
            calls.append("watch")
            raise runtime_error

    monkeypatch.setattr(scheduler_service, "RustNotify", Watcher)
    await asyncio.wait_for(scheduler._watch_library(library), 2)

    assert (
        calls
        == {
            "startup": ["start"],
            "runtime": ["start", "watch", "close"],
            "polling": ["start", "close"],
        }[failure]
    )
    records = [record for record in caplog.records if record.name == scheduler_service.__name__]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    record = records[0]
    assert library.name in record.getMessage() and str(tmp_path) in record.getMessage()
    assert record.exc_info is not None
    if failure == "polling":
        assert isinstance(record.exc_info[1], RuntimeError)
    else:
        assert record.exc_info[1] is (startup_error if failure == "startup" else runtime_error)


@pytest.mark.asyncio
async def test_watch_exclusions(factory, staging, tmp_path, monkeypatch) -> None:
    trash = tmp_path / "trash"
    diagnostics = tmp_path / "healthchecks"
    workspace = tmp_path / "workspace"
    staging.workspace_dir = workspace
    monkeypatch.setattr(scheduler_service, "healthcheck_dir", lambda: diagnostics)
    scheduler = ScannerService(
        factory,
        staging=staging,
        trash_dir=trash,
        scanner=ScannerConfig(containers=["mkv", "mp4"], ignore_dirs=["sample"]),
    )
    hidden = tmp_path / ".hidden"
    linked = tmp_path / "linked"
    hidden.mkdir()
    linked.symlink_to(hidden, target_is_directory=True)
    library = Library(name="watched", roots=[str(path) for path in (tmp_path, hidden, linked, workspace, trash)])
    changes = [
        {(Change.modified, str(tmp_path / value))}
        for value in (
            ".hidden/movie.mkv",
            "linked/movie.mkv",
            "sample/movie.mkv",
            "workspace/movie.mkv",
            "trash/movie.mkv",
            "healthchecks/movie.mkv",
            "movie.mkv.partial",
            "readme.txt",
            "movie.mkv",
            "removed.directory",
        )
    ]
    changes[-1] = {(Change.deleted, str(tmp_path / "removed.directory"))}
    directory = tmp_path / "added"
    directory.mkdir()
    changes.extend(
        [
            {(Change.modified, str(tmp_path)), (Change.modified, str(directory))},
            {
                (Change.added, str(directory)),
                (Change.added, str(directory / "movie.mkv")),
                (Change.modified, str(tmp_path)),
            },
            {(Change.deleted, str(tmp_path / "readme.txt"))},
            {(Change.added, str(tmp_path / "fresh.mkv")), (Change.modified, str(tmp_path))},
            {(Change.deleted, str(tmp_path / "gone.mkv"))},
            {(Change.deleted, str(tmp_path / "old")), (Change.added, str(directory))},
        ]
    )
    notifications: list[set[Path]] = []
    filter_changes = {
        (Change.modified, str(tmp_path / name))
        for name in ("movie.mov", "movie.mp4", "sample/movie.mkv", "trailers/movie.mkv")
    }
    settings_changes = [
        (None, filter_changes),
        (ScannerConfig(containers=["mkv", "mov"], ignore_dirs=["trailers"]), filter_changes),
    ]

    class Watcher:
        def __init__(self, paths, **_kwargs):
            assert paths == [str(tmp_path)]

        def __repr__(self):
            return "RustNotify(Recommended(test))"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def watch(self, **_kwargs):
            if changes:
                return changes.pop(0)
            if settings_changes:
                config, batch = settings_changes.pop(0)
                if config is not None:
                    scheduler.scanner = config
                return batch
            return "stop"

    monkeypatch.setattr(scheduler_service, "RustNotify", Watcher)
    await asyncio.wait_for(
        asyncio.to_thread(scheduler._watch_changes, library, threading.Event(), notifications.append), 2
    )
    assert notifications == [
        {tmp_path / "movie.mkv"},
        {tmp_path / "removed.directory"},
        {directory},
        {tmp_path / "readme.txt"},
        {tmp_path / "fresh.mkv"},
        {tmp_path / "gone.mkv"},
        {tmp_path / "old", directory},
        {tmp_path / "movie.mp4", tmp_path / "trailers/movie.mkv"},
        {tmp_path / "movie.mov", tmp_path / "sample/movie.mkv"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "file"])
async def test_watch_root_error(factory, staging, tmp_path, caplog, invalid: str) -> None:
    path = tmp_path / invalid
    if invalid == "file":
        path.write_text("not a directory")
    scheduler = ScannerService(factory, staging=staging)

    await asyncio.wait_for(scheduler._watch_library(Library(name="invalid", roots=[str(path)])), 2)

    records = [
        record
        for record in caplog.records
        if record.name == scheduler_service.__name__ and record.levelno == logging.ERROR
    ]
    assert len(records) == 1 and records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], FileNotFoundError if invalid == "missing" else NotADirectoryError)
    assert str(path) in records[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_native_shutdown_drains(factory, staging, tmp_path, monkeypatch, cancel: bool) -> None:
    identifier = await add_library(factory, tmp_path, "shutdown", watch=True)
    watching, closing = asyncio.Event(), asyncio.Event()
    release, closed = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    threads: list[threading.Thread] = []

    class Watcher:
        def __init__(self, _paths, **_kwargs):
            threads.append(threading.current_thread())
            assert threads[-1].name.startswith(f"watch-{identifier}")

        def __repr__(self):
            return "RustNotify(Recommended(test))"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            loop.call_soon_threadsafe(closing.set)
            assert release.wait(3), "Native watcher cleanup was not released."
            closed.set()

        def watch(self, *, stop_event, **_kwargs):
            loop.call_soon_threadsafe(watching.set)
            assert stop_event.wait(3), "Native watcher was not stopped."
            return "stop"

    monkeypatch.setattr(scheduler_service, "RustNotify", Watcher)
    scheduler = ScannerService(factory, staging=staging)
    scheduler.start()
    shutdown = None
    try:
        await asyncio.wait_for(watching.wait(), 2)
        watcher = scheduler._watch_tasks[identifier]
        shutdown = asyncio.create_task(scheduler.aclose())
        await asyncio.wait_for(closing.wait(), 2)
        assert not watcher.done() and not shutdown.done() and not closed.is_set()
        if cancel:
            watcher.cancel()
            shutdown.cancel()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(shutdown, 2)
        else:
            await asyncio.wait_for(shutdown, 2)
        assert watcher.cancelled() and closed.is_set()
        assert len(threads) == 1 and not threads[0].is_alive()
        assert not scheduler._watch_tasks and not scheduler._retiring_tasks
    finally:
        release.set()
        await asyncio.wait_for(scheduler.aclose(), 3)
        if shutdown is not None:
            await asyncio.wait_for(asyncio.gather(shutdown, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_lifecycle_idempotent(
    factory: async_sessionmaker[AsyncSession], staging: StagingService, scan_log: list[str]
) -> None:
    scheduler = ScannerService(factory, staging=staging)
    scheduler.start()
    scheduler.start()
    assert scheduler._startup_task is not None
    await scheduler.aclose()
    await scheduler.aclose()

    assert scheduler.running is False
    assert scheduler._watch_tasks == {}
    assert scheduler._debounce_tasks == {}
    assert scan_log == []


@pytest.mark.asyncio
async def test_close_before_queue(factory, staging, scan_log, tmp_path: Path) -> None:
    identifier = await add_library(factory, tmp_path, "queued")
    bus = EventBus()
    scheduler = ScannerService(factory, staging=staging, events=bus)
    try:
        scheduler.queue_scan(identifier, "manual")
        await asyncio.wait_for(scheduler.aclose(), 2)
        assert not scheduler._scan_tasks and not scheduler._stops
        state = bus.library_snapshot(identifier)
        assert state["discovery"]["latest_result"] == "cancelled"
        assert not state["scanner"]["discovery_pending"]
        scheduler.start()
        report = await scheduler.scan_now(identifier)
        assert report is not None and scan_log == ["queued"]
    finally:
        await asyncio.wait_for(scheduler.aclose(), 3)
        await bus.aclose()
