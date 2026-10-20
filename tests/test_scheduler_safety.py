from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from server.models import Library
from server.scanner.scan import ScanReport
from server.scanner.service import ScannerService
from server.scheduler.schedule import active, parse_schedule
from server.staging import StagingService
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_startup_close_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts = 0

    async def startup() -> None:
        nonlocal starts
        starts += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await asyncio.wait_for(release.wait(), 3)

    async with database_context("scanner-startup-close") as factory:
        scanner = ScannerService(factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1))
        monkeypatch.setattr(scanner, "_scan_on_start", startup)
        scanner.start()
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            owner = scanner._startup_task
            assert owner is not None
            closing = asyncio.create_task(scanner.aclose())
            await asyncio.wait_for(draining.wait(), 1)
            scanner.start()
            assert starts == 1 and not scanner.running
            assert not closing.done() and not owner.done()
            release.set()
            await asyncio.wait_for(closing, 1)
            assert owner.cancelled()
            entered.clear()
            scanner.start()
            await asyncio.wait_for(entered.wait(), 1)
            assert starts == 2 and scanner.running
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 2)
            await asyncio.wait_for(scanner.aclose(), 2)


@pytest.mark.asyncio
async def test_shutdown_drains_debounce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def sleep(_seconds: float) -> None:
        return

    async def scan(*_args, **_kwargs) -> ScanReport:
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", scan)
    async with database_context("scheduler-shutdown") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.commit()
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), sleep=sleep
        )
        scheduler._running = True
        scheduler.library_changed(library.id, {tmp_path / "movie.mkv"})
        task = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task = scheduler._scan_tasks[library.id]
            await asyncio.wait_for(scheduler.aclose(), 3)
            assert task.done()
            assert task.cancelled()
        finally:
            release.set()
            if task is not None:
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_watch_keeps_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    debouncing = asyncio.Event()
    calls = 0

    async def sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            debouncing.set()
            await asyncio.wait_for(release.wait(), 3)

    async def scan(*_args, **_kwargs) -> ScanReport:
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), 3)
        finally:
            cancelled.set()
            await asyncio.wait_for(release.wait(), 3)
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", scan)
    async with database_context("scheduler-replacement") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.commit()
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), sleep=sleep
        )
        scheduler._running = True
        scheduler.library_changed(library.id, {tmp_path / "movie.mkv"})
        tasks = []
        try:
            await asyncio.wait_for(entered.wait(), 3)
            first = scheduler._scan_tasks[library.id]
            tasks.append(first)
            scheduler.library_changed(library.id, {tmp_path / "movie.mkv"})
            assert scheduler._scan_tasks[library.id] is first
            assert not cancelled.is_set() and not debouncing.is_set()
            closing = asyncio.create_task(scheduler.aclose())
            tasks.append(closing)
            await asyncio.wait_for(cancelled.wait(), 2)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 2)
            assert first.done()
            assert first.cancelled()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
            await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_close_repeated_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_started, third_started = asyncio.Event(), asyncio.Event()

    async def sleep(_seconds: float) -> None:
        return

    async def scan(*_args, **_kwargs) -> ScanReport:
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), 3)
        finally:
            cleanup.set()
            await asyncio.wait_for(release.wait(), 3)
        return ScanReport()

    monkeypatch.setattr("server.scanner.service.scan_library", scan)
    async with database_context("scheduler-close-cancellation") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.commit()
        scheduler = ScannerService(
            factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1), sleep=sleep
        )
        scheduler._running = True
        scheduler.library_changed(library.id, {tmp_path / "movie.mkv"})
        worker = None
        closing: list[asyncio.Task[None]] = []

        async def close_signalled(started: asyncio.Event) -> None:
            started.set()
            await scheduler.aclose()

        try:
            await asyncio.wait_for(entered.wait(), 3)
            worker = scheduler._scan_tasks[library.id]
            first = asyncio.create_task(scheduler.aclose())
            closing.append(first)
            await asyncio.wait_for(cleanup.wait(), 3)
            first.cancel()
            second = asyncio.create_task(close_signalled(second_started))
            closing.append(second)
            await asyncio.wait_for(second_started.wait(), 3)
            first.cancel()
            third = asyncio.create_task(close_signalled(third_started))
            closing.append(third)
            await asyncio.wait_for(third_started.wait(), 3)
            assert first.cancelling() == 2
            assert not any(task.done() for task in closing)
            assert not worker.done()
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(*closing, return_exceptions=True), 3)
            assert isinstance(outcomes[0], asyncio.CancelledError)
            assert outcomes[1:] == [None, None]
            assert worker.done() and worker.cancelled()
            assert not scheduler.running
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*(task for task in [worker, *closing] if task is not None), return_exceptions=True), 3
            )
            await asyncio.wait_for(scheduler.aclose(), 3)


@pytest.mark.asyncio
async def test_close_drains_watchers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def watch(_library: Library) -> None:
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), 3)
        finally:
            cleanup.set()
            await asyncio.wait_for(release.wait(), 3)

    async with database_context("scheduler-watcher-update-close") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)], watch=True)
            session.add(library)
            await session.commit()
        scheduler = ScannerService(factory, staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1))
        monkeypatch.setattr(scheduler, "_watch_library", watch)
        scheduler._running = True
        await scheduler.update_watchers()
        worker = scheduler._watch_tasks[library.id]
        tasks = [worker]

        async def disable_watch() -> None:
            async with scheduler.library_mutation(), factory() as session:
                current = await session.get(Library, library.id)
                assert current is not None
                current.watch = False
                await session.commit()

        try:
            await asyncio.wait_for(entered.wait(), 3)
            updating_watchers = asyncio.create_task(disable_watch())
            tasks.append(updating_watchers)
            await asyncio.wait_for(cleanup.wait(), 3)
            await asyncio.wait_for(scheduler.aclose(), 3)
            assert worker.done()
            assert worker.cancelled()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
            await asyncio.wait_for(scheduler.aclose(), 3)


def test_dst_windows() -> None:
    timezone = ZoneInfo("America/New_York")
    cells = [[False] * 24 for _ in range(7)]
    cells[6][1] = True
    raw = {"type": "grid", "cells": cells}
    schedule = parse_schedule(raw)
    assert active(schedule, datetime(2026, 11, 1, 5, 30, tzinfo=UTC), timezone)
    assert active(schedule, datetime(2026, 11, 1, 6, 30, tzinfo=UTC), timezone)
    assert not active(schedule, datetime(2026, 11, 1, 7, 30, tzinfo=UTC), timezone)
    assert active(schedule, datetime(2026, 3, 8, 6, 30, tzinfo=UTC), timezone)
    cells[6][1], cells[6][2] = False, True
    raw = {"type": "grid", "cells": cells}
    schedule = parse_schedule(raw)
    assert not active(schedule, datetime(2026, 3, 8, 6, 30, tzinfo=UTC), timezone)
    assert not active(schedule, datetime(2026, 3, 8, 7, 30, tzinfo=UTC), timezone)
