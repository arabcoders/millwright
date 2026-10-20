from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.models import Library
from server.scanner.scan import ScanReport
from server.scanner.service import ScannerService
from server.scheduler.service import Scheduler
from server.staging import StagingService
from tests.helpers import database_context


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.ticks: asyncio.Queue[float] = asyncio.Queue()
        self.wake = asyncio.Event()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.ticks.put_nowait(seconds)
        await self.wake.wait()
        self.wake.clear()

    async def ready(self) -> None:
        assert await asyncio.wait_for(self.ticks.get(), 1) == 60

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wake.set()
        await self.ready()


@pytest.mark.asyncio
async def test_intervals_skip_backlog() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, sleep=clock.sleep)
    runs: asyncio.Queue[str] = asyncio.Queue()

    async def minute() -> None:
        runs.put_nowait("minute")

    async def hour() -> None:
        runs.put_nowait("hour")

    scheduler.add(minute)
    scheduler.add(hour, every=3600)
    scheduler.start()
    scheduler.start()
    try:
        await clock.ready()
        assert {await asyncio.wait_for(runs.get(), 1), await asyncio.wait_for(runs.get(), 1)} == {"minute", "hour"}
        await clock.advance(60)
        assert await asyncio.wait_for(runs.get(), 1) == "minute"
        assert runs.empty()
        await clock.advance(7200)
        assert {await asyncio.wait_for(runs.get(), 1), await asyncio.wait_for(runs.get(), 1)} == {"minute", "hour"}
        assert runs.empty()
    finally:
        await asyncio.wait_for(scheduler.aclose(), 1)


@pytest.mark.asyncio
async def test_slow_callback_isolation() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, sleep=clock.sleep)
    release, started = asyncio.Event(), asyncio.Event()
    runs: asyncio.Queue[str] = asyncio.Queue()
    slow_runs = 0

    async def slow() -> None:
        nonlocal slow_runs
        slow_runs += 1
        started.set()
        await release.wait()

    async def fast() -> None:
        runs.put_nowait("fast")

    scheduler.add(slow)
    scheduler.add(fast)
    scheduler.start()
    try:
        await clock.ready()
        await asyncio.wait_for(started.wait(), 1)
        assert await asyncio.wait_for(runs.get(), 1) == "fast"
        for _ in range(2):
            await clock.advance(60)
            assert await asyncio.wait_for(runs.get(), 1) == "fast"
            assert slow_runs == 1
    finally:
        release.set()
        await asyncio.wait_for(scheduler.aclose(), 1)


@pytest.mark.asyncio
async def test_failure_retries() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, sleep=clock.sleep)
    ran: asyncio.Queue[int] = asyncio.Queue()
    calls = 0

    async def failing() -> None:
        nonlocal calls
        calls += 1
        ran.put_nowait(calls)
        raise OSError("cleanup failed")

    scheduler.add(failing, name="auth")
    scheduler.start()
    try:
        await clock.ready()
        assert await asyncio.wait_for(ran.get(), 1) == 1
        await clock.advance(60)
        assert await asyncio.wait_for(ran.get(), 1) == 2
    finally:
        await asyncio.wait_for(scheduler.aclose(), 1)


@pytest.mark.asyncio
async def test_shutdown_finishes_callbacks() -> None:
    started, stopped, completed, release = (asyncio.Event() for _ in range(4))

    async def sleep(_seconds: float) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise

    scheduler = Scheduler(sleep=sleep)

    async def cleanup() -> None:
        started.set()
        await release.wait()
        completed.set()

    scheduler.add(cleanup)
    scheduler.start()
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(scheduler.aclose())
    try:
        await asyncio.wait_for(stopped.wait(), 1)
        for _ in range(2):
            closing.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 1)
            assert not closing.done() and not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        assert completed.is_set()
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        await asyncio.wait_for(scheduler.aclose(), 1)


@pytest.mark.asyncio
async def test_restart_runs_immediately() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, sleep=clock.sleep)
    runs: asyncio.Queue[None] = asyncio.Queue()

    async def cleanup() -> None:
        runs.put_nowait(None)

    scheduler.add(cleanup, every=3600)
    for _ in range(2):
        scheduler.start()
        try:
            await clock.ready()
            await asyncio.wait_for(runs.get(), 1)
            assert runs.empty()
        finally:
            await asyncio.wait_for(scheduler.aclose(), 1)


@pytest.mark.parametrize("every", [0, -1, float("inf"), float("nan")])
def test_rejects_invalid_interval(every: float) -> None:
    async def cleanup() -> None:
        pass

    with pytest.raises(ValueError):
        Scheduler().add(cleanup, every=every)


def test_rejects_duplicate_callback() -> None:
    async def cleanup() -> None:
        pass

    scheduler = Scheduler()
    scheduler.add(cleanup)
    with pytest.raises(ValueError):
        scheduler.add(cleanup)


@pytest.mark.asyncio
@pytest.mark.parametrize("scanner_first", [True, False])
async def test_scanner_cleanup_independence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scanner_first: bool
) -> None:
    clock = Clock()
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    entered, release = asyncio.Event(), asyncio.Event()
    checks: asyncio.Queue[None] = asyncio.Queue()
    cleanups: asyncio.Queue[None] = asyncio.Queue()
    scans = 0

    async def discover(*_args, **_kwargs) -> ScanReport:
        nonlocal scans
        scans += 1
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", discover)
    async with database_context("scanner-dispatch") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], scan_interval_s=65)
            session.add(library)
            await session.commit()
        scanner = ScannerService(
            factory,
            staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
            clock=lambda: epoch + timedelta(seconds=clock.now),
        )
        scheduler = Scheduler(clock=clock, sleep=clock.sleep)

        async def check_intervals() -> None:
            await scanner.check_intervals()
            checks.put_nowait(None)

        async def cleanup() -> None:
            cleanups.put_nowait(None)

        callbacks = [check_intervals, cleanup] if scanner_first else [cleanup, check_intervals]
        for callback in callbacks:
            scheduler.add(callback, every=60)
        scanner.set_paused(paused=True)
        scheduler.start()
        try:
            await clock.ready()
            await asyncio.wait_for(checks.get(), 1)
            await asyncio.wait_for(cleanups.get(), 1)
            await clock.advance(60)
            await asyncio.wait_for(checks.get(), 1)
            await asyncio.wait_for(cleanups.get(), 1)
            assert not scanner._scan_tasks
            await clock.advance(60)
            await asyncio.wait_for(checks.get(), 1)
            await asyncio.wait_for(cleanups.get(), 1)
            assert not scanner._scan_tasks
            scanner.set_paused(paused=False)
            assert not scanner._scan_tasks
            await clock.advance(60)
            await asyncio.wait_for(checks.get(), 1)
            await asyncio.wait_for(cleanups.get(), 1)
            await asyncio.wait_for(entered.wait(), 1)
            owner = scanner._scan_tasks[library.id]
            assert not owner.done()
            await clock.advance(60)
            await asyncio.wait_for(checks.get(), 1)
            await asyncio.wait_for(cleanups.get(), 1)
            assert scanner._scan_tasks[library.id] is owner and scans == 1
            await asyncio.wait_for(scheduler.aclose(), 1)
            assert not owner.done()
            release.set()
            await asyncio.wait_for(owner, 1)
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 2)
            await asyncio.wait_for(scanner.aclose(), 2)
