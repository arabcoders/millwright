from __future__ import annotations

import asyncio
import logging
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from common.process import drain
from sqlalchemy import select
from watchfiles import Change
from watchfiles._rust_notify import RustNotify

from server.core.paths import healthcheck_dir
from server.models import Library, utcnow
from server.scanner.scan import _TEMP_SUFFIX, ScanReport, merge_targets, scan_library
from server.serialize import iso_utc
from server.settings import ScannerConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.events import EventBus
    from server.staging import StagingService

LOG = logging.getLogger(__name__)


class ScannerService:
    """Run library scans one at a time and track scan requests and folder watchers."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        staging: StagingService,
        scanner: ScannerConfig | None = None,
        debounce_s: float = 30.0,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        events: EventBus | None = None,
        trash_dir: Path | None = None,
        on_scan_complete: Callable[[ScanReport], None] | None = None,
        on_files_pending: Callable[[list[int]], Awaitable[None]] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.staging = staging
        self.scanner = scanner if scanner is not None else ScannerConfig()
        self.trash_dir = trash_dir.resolve() if trash_dir is not None else None
        self.on_scan_complete = on_scan_complete
        self.on_files_pending = on_files_pending
        self.debounce_s = debounce_s
        self.events = events
        self._clock = clock
        self._sleep = sleep
        self._scan_lock = asyncio.Lock()
        self._watch_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._startup_task: asyncio.Task[None] | None = None
        self._watch_tasks: dict[int, asyncio.Task] = {}
        self._watch_configs: dict[int, tuple[str, tuple[str, ...]]] = {}
        self._debounce_tasks: dict[int, asyncio.Task] = {}
        self._retiring_tasks: set[asyncio.Task] = set()
        self._scan_tasks: dict[int, asyncio.Task[ScanReport | None]] = {}
        self._stops: dict[int, threading.Event] = {}
        self._active: set[int] = set()
        self._waiting: set[int] = set()
        self._dirty: set[int] = set()
        self._pending: dict[int, tuple[str, set[Path] | None]] = {}
        self._requests: dict[int, tuple[str, set[Path] | None]] = {}
        self.paused = False
        self.failed = False
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._full_completed: dict[int, datetime] = {}
        self._due: dict[int, datetime] = {}
        self._intervals: dict[int, int] = {}
        self._running = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running or (self._close_task is not None and not self._close_task.done()):
            return
        self._close_task = None
        self._running = True
        self.failed = False
        self._stop.clear()
        self.set_paused(paused=self.paused)
        self._startup_task = asyncio.create_task(self._startup(), name="scanner-startup")

    async def aclose(self) -> None:
        if self._close_task is None:
            if not self._running and not self._scan_tasks and not self._debounce_tasks and not self._pending:
                return
            self._running = False
            self._stop.set()
            tasks = [
                self._startup_task,
                *self._watch_tasks.values(),
                *self._debounce_tasks.values(),
                *self._retiring_tasks,
                *self._scan_tasks.values(),
            ]
            self._startup_task = None
            self._watch_tasks.clear()
            self._watch_configs.clear()
            self._debounce_tasks.clear()
            self._retiring_tasks.clear()
            for stopped in self._stops.values():
                stopped.set()
            for task in tasks:
                if task is not None:
                    task.cancel()
            self._close_task = asyncio.create_task(self._close(tasks))
        closing = self._close_task
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self, tasks: list[asyncio.Task | None]) -> None:
        # Wait for blocking filesystem calls to finish before releasing their locks.
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        for library_id in list(self._scan_tasks):
            self._scan_progress(library_id, "cancelled")
            self._finish_discovery(library_id, self._intervals.get(library_id))
        self._scan_tasks.clear()
        self._stops.clear()
        self._active.clear()
        self._dirty.clear()
        self._pending.clear()
        self._requests.clear()

    async def _startup(self) -> None:
        try:
            await self._scan_on_start()
            await self.update_watchers()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            if self.events is not None:
                self.events.publish("scanner.state", {"paused": self.paused, "failed": True})
            LOG.exception("Scanner startup failed")

    async def _libraries(self, *, watch: bool | None = None) -> list[Library]:
        async with self.sessionmaker() as session:
            stmt = select(Library).where(Library.suspended.is_(False)).order_by(Library.id)
            if watch is True:
                stmt = stmt.where(Library.watch.is_(True))
            elif watch is False:
                stmt = stmt.where(Library.watch.is_(False))
            return list((await session.execute(stmt)).scalars())

    async def _scan_on_start(self) -> None:
        libraries = [library for library in await self._libraries(watch=None) if library.scan_on_start]
        for library in libraries:
            self.queue_scan(library.id, "scan-on-start")

    async def check_intervals(self) -> None:
        if self._stop.is_set():
            return
        libraries = await self._libraries()
        if self._stop.is_set():
            return
        now = self._clock()
        intervals = {
            library.id: library.scan_interval_s for library in libraries if library.scan_interval_s is not None
        }
        for identifier in self._due.keys() - intervals.keys():
            self._due.pop(identifier, None)
            if self.events is not None:
                self.events.update_library_progress(identifier, scanner={"next_run_at": None})
        for identifier, interval in intervals.items():
            if self._intervals.get(identifier) != interval or identifier not in self._due:
                self._due[identifier] = self._full_completed.get(identifier, now) + timedelta(seconds=interval)
            if self._due[identifier] <= now and not self.paused:
                self.queue_scan(identifier, "interval")
            if self.events is not None:
                self.events.update_library_progress(
                    identifier,
                    scanner={"next_run_at": None if identifier in self._scan_tasks else iso_utc(self._due[identifier])},
                )
        self._intervals = intervals

    async def update_watchers(self) -> None:
        async with self._watch_lock:
            libraries = await self._libraries(watch=True) if self._running else []
            configs = {library.id: (library.name, tuple(library.roots)) for library in libraries}
            changed = {
                identifier for identifier, config in self._watch_configs.items() if configs.get(identifier) != config
            }
            retired = changed | (self._watch_configs.keys() - configs.keys())
            self._watch_configs = configs
            self._dirty.difference_update(retired)
            for identifier in retired:
                self._pending.pop(identifier, None)
                request = self._requests.get(identifier)
                if request is not None and request[0] == "watch" and identifier not in self._active:
                    self.cancel_scan(identifier)
            tasks = []
            for identifier, task in list(self._watch_tasks.items()):
                if identifier in changed or identifier not in configs or task.done():
                    tasks.append(self._watch_tasks.pop(identifier))
            tasks.extend(
                self._debounce_tasks.pop(identifier)
                for identifier in list(self._debounce_tasks)
                if identifier in changed or identifier not in configs
            )
            for task in tasks:
                self._retiring_tasks.add(task)
                task.add_done_callback(self._retiring_tasks.discard)
                task.cancel()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            self._watch_configs = configs if self._running else {}
            if self._running:
                for library in libraries:
                    if library.id in self._watch_tasks:
                        continue
                    self._watch_tasks[library.id] = asyncio.create_task(
                        self._watch_library(library), name=f"watch-{library.name}"
                    )

    @asynccontextmanager
    async def library_mutation(self) -> AsyncGenerator[None]:
        async with self._scan_lock:
            try:
                yield
            finally:
                await self.update_watchers()

    async def _scan_library(self, library: Library) -> ScanReport | None:
        started = time.perf_counter()
        try:
            return await self._execute_scan(library, started)
        except asyncio.CancelledError:
            self._scan_progress(library.id, "cancelled")
            raise
        except Exception as exc:
            self._scan_progress(library.id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._finish_discovery(library.id, library.scan_interval_s)

    def _finish_discovery(self, library_id: int, interval: int | None) -> None:
        finished = self._clock()
        # Local Watch traffic must not postpone the full scan deadline.
        if self._requests[library_id][1] is None:
            self._full_completed[library_id] = finished
            if interval is not None:
                self._due[library_id] = finished + timedelta(seconds=interval)
            else:
                self._due.pop(library_id, None)
        if self.events is not None:
            state = self.events.library_snapshot(library_id)["discovery"]
            self.events.update_library_progress(
                library_id,
                discovery={"latest_result": state["status"], "finished_at": iso_utc(finished)},
                scanner={"next_run_at": iso_utc(self._due.get(library_id)), "discovery_pending": False},
            )
        self._stops.pop(library_id, None)

    def _scan_progress(
        self, library_id: int, status: str, report: ScanReport | None = None, *, error: str | None = None
    ) -> None:
        if self.events is None:
            return
        progress: dict = {"status": status}
        if report is not None:
            progress.update(
                found=report.found,
                added=report.added,
                updated=report.updated,
                error_count=len(report.errors),
                error=report.errors[0] if report.errors else None,
            )
        elif error is not None:
            progress["error"] = error
            progress["error_count"] = self.events.library_snapshot(library_id)["discovery"]["error_count"] + 1
        self.events.update_library_progress(library_id, discovery=progress)

    async def _execute_scan(self, library: Library, started: float) -> ScanReport | None:
        async with self._wait_for_scan_slot(library.id):
            stopped = self._stops[library.id]
            if stopped.is_set():
                self._scan_progress(library.id, "cancelled")
                return ScanReport(cancelled=True)
            reason, target_paths = self._requests[library.id]
            self._active.add(library.id)
            async with self.sessionmaker() as session:
                current = await session.get(Library, library.id)
                if current is None or current.suspended:
                    self._scan_progress(library.id, "failed", error="Library no longer authorizes discovery.")
                    return None
                library.scan_interval_s = current.scan_interval_s
                library = current
                if target_paths is not None:
                    target_paths = merge_targets(target_paths, roots=[Path(root) for root in library.roots])
                    self._requests[library.id] = (reason, target_paths)
                if stopped.is_set():
                    self._scan_progress(library.id, "cancelled")
                    return ScanReport(cancelled=True)
                self._scan_progress(library.id, "scanning", ScanReport())
                if self.events is not None:
                    self.events.update_library_progress(library.id, discovery={"reason": reason})
                scope = "full" if target_paths is None else "scoped"
                paths = library.roots if target_paths is None else sorted(str(path) for path in target_paths)
                LOG.info(
                    "Scanning library %s (%s, paths %s), reason %s",
                    library.id,
                    scope,
                    paths,
                    reason,
                    extra={
                        "operation": "library_scan_start",
                        "reason": reason,
                        "library_id": library.id,
                        "scope": scope,
                        "paths": paths,
                    },
                )
                report = await scan_library(
                    session,
                    library,
                    config=self.scanner,
                    staging=self.staging,
                    events=self.events,
                    trash_dir=self.trash_dir,
                    on_progress=lambda report: self._scan_progress(
                        library.id, "cancelling" if stopped.is_set() else "scanning", report
                    ),
                    on_files_pending=self.on_files_pending,
                    stopped=stopped,
                    target_paths=target_paths,
                )
                await session.commit()
            self._scan_progress(
                library.id, "cancelled" if report.cancelled else "failed" if report.errors else "completed", report
            )
            LOG.info(
                "Scanned library %s (%s, paths %s): found %s files, %s changed, %s errors in %.2fs",
                library.id,
                scope,
                paths,
                report.found,
                report.added + report.updated,
                len(report.errors),
                time.perf_counter() - started,
                extra={
                    "operation": "library_scan",
                    "reason": reason,
                    "scope": scope,
                    "target_paths": None if target_paths is None else sorted(str(path) for path in target_paths),
                    "library_id": library.id,
                    "found": report.found,
                    "changed": report.added + report.updated,
                    "pruned": report.pruned,
                    "error_count": len(report.errors),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
        return report

    @asynccontextmanager
    async def _wait_for_scan_slot(self, library_id: int) -> AsyncGenerator[None]:
        stopped = self._stops[library_id]
        if stopped.is_set():
            yield
            return
        self._waiting.add(library_id)
        try:
            while True:
                while self.paused and not stopped.is_set():
                    await self._resumed.wait()
                await self._scan_lock.acquire()
                if not self.paused or stopped.is_set():
                    break
                self._scan_lock.release()
        finally:
            self._waiting.discard(library_id)
        try:
            yield
        finally:
            self._scan_lock.release()

    async def scan_now(self, library_id: int) -> ScanReport | None:
        self.queue_scan(library_id, "manual")
        task = self._scan_tasks.get(library_id)
        return await asyncio.shield(task) if task is not None else None

    def set_paused(self, *, paused: bool) -> None:
        self.paused = paused
        if paused:
            self._resumed.clear()
        else:
            self._resumed.set()
            for library_id in self._dirty - self._scan_tasks.keys():
                self._dirty.discard(library_id)
                reason, paths = self._pending.pop(library_id)
                if paths is None:
                    self.queue_scan(library_id, reason)
                else:
                    self.library_changed(library_id, paths)
        if self.events is not None:
            self.events.scanner_paused = paused
            self.events.publish("scanner.state", {"paused": paused, "failed": self.failed})
            for library_id in self.events.library_progress:
                self.events.update_library_progress(library_id, scanner={"paused": paused})
                if library_id not in self._active:
                    state = self.events.library_snapshot(library_id)["discovery"]
                    self._scan_progress(
                        library_id,
                        "paused"
                        if paused
                        else "queued"
                        if library_id in self._scan_tasks
                        else state["latest_result"] or "idle",
                    )

    def cancel_scan(self, library_id: int) -> str:
        stopped = self._stops.get(library_id)
        if stopped is None:
            return "not_running"
        stopped.set()
        self._dirty.discard(library_id)
        self._pending.pop(library_id, None)
        self._scan_progress(library_id, "cancelling")
        # Only scan-slot waits are interruptible. Tasks that have not started must enter
        # their cleanup, and running scans must wait for filesystem work to finish.
        if library_id in self._waiting:
            self._scan_tasks[library_id].cancel()
        return "cancelling"

    async def drain_library(self, library_id: int) -> None:
        from common.process import drain

        self.cancel_scan(library_id)
        task = self._scan_tasks.get(library_id)
        if task is not None:
            await drain(asyncio.gather(task, return_exceptions=True))
        # The saved suspension stops discovery. This lock also
        # retires its watcher and debounce workers before catalog removal.
        async with self.library_mutation():
            pass

    def queue_scan(self, library_id: int, reason: str, *, target_paths: set[Path] | None = None) -> str:
        if self._stop.is_set():
            return "stopped"
        if target_paths is not None:
            target_paths = {path.absolute() for path in target_paths}
            if not target_paths:
                return "empty"
        previous = self._debounce_tasks.pop(library_id, None)
        if previous is not None and previous is not asyncio.current_task():
            self._retiring_tasks.add(previous)
            previous.add_done_callback(self._retiring_tasks.discard)
            previous.cancel()
        if library_id in self._scan_tasks:
            stopped = self._stops[library_id]
            if not stopped.is_set():
                if library_id in self._active:
                    if (
                        target_paths is not None
                        or self._requests[library_id][1] is not None
                        or library_id in self._dirty
                    ):
                        self._merge_pending(library_id, reason, target_paths)
                        self._dirty.add(library_id)
                else:
                    old_reason, old_paths = self._requests[library_id]
                    if target_paths is None:
                        self._requests[library_id] = (reason, None)
                    elif old_paths is None:
                        self._requests[library_id] = (old_reason, None)
                    else:
                        self._requests[library_id] = (old_reason, old_paths | target_paths)
            return "already_running" if library_id in self._active else "already_queued"
        pending = self._pending.pop(library_id, None)
        if pending is not None and target_paths is not None:
            reason, old_paths = pending
            target_paths = None if old_paths is None else old_paths | target_paths
        self._dirty.discard(library_id)
        self._requests[library_id] = (reason, target_paths)
        self._stops[library_id] = threading.Event()
        self._scan_tasks[library_id] = asyncio.create_task(
            self._requested_scan(library_id, reason), name=f"scan-{library_id}"
        )
        self._scan_progress(library_id, "paused" if self.paused else "queued", ScanReport())
        if self.events is not None:
            self.events.update_library_progress(
                library_id, discovery={"reason": reason}, scanner={"next_run_at": None, "discovery_pending": True}
            )
        return "queued"

    async def _requested_scan(self, library_id: int, reason: str) -> ScanReport | None:
        library_name = str(library_id)
        library: Library | None = None
        try:
            async with self.sessionmaker() as session:
                library = await session.get(Library, library_id)
            if library is None:
                self._scan_progress(library_id, "failed", error="Library no longer authorizes discovery.")
                return None
            library_name = library.name
            report = await self._scan_library(library)
            reason = self._requests[library_id][0]
            if report is None:
                return None
            if self.on_scan_complete is not None:
                self.on_scan_complete(report)
            if self.events is not None:
                self.events.publish(
                    "library.scan",
                    {
                        "status": "cancelled" if report.cancelled else "succeeded",
                        "reason": reason,
                        "library_id": library_id,
                        "library_name": library_name,
                        "found": report.found,
                        "added": report.added,
                        "updated": report.updated,
                        "unchanged": report.unchanged,
                        "pruned": report.pruned,
                        "error_count": len(report.errors),
                        "error_samples": report.errors[:20],
                    },
                    key=f"library.scan:{library_id}",
                )
            return report
        except asyncio.CancelledError:
            if library is None:
                self._scan_progress(library_id, "cancelled")
            raise
        except Exception as exc:
            reason = self._requests[library_id][0]
            LOG.exception("scan failed for library %s", library_id)
            if library is None:
                self._scan_progress(library_id, "failed", error=f"{type(exc).__name__}: {exc}")
            if self.events is not None:
                self.events.publish(
                    "library.scan",
                    {
                        "status": "failed",
                        "reason": reason,
                        "library_id": library_id,
                        "library_name": library_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    key=f"library.scan:{library_id}",
                )
            return None
        finally:
            if library is None:
                self._finish_discovery(library_id, self._intervals.get(library_id))
            self._active.discard(library_id)
            self._scan_tasks.pop(library_id, None)
            self._stops.pop(library_id, None)
            self._requests.pop(library_id, None)
            if self.events is not None:
                self.events.update_library_progress(library_id, scanner={"discovery_pending": False})
                if self.paused:
                    self._scan_progress(library_id, "paused")
            if library_id in self._dirty:
                self._dirty.discard(library_id)
                if not self._stop.is_set():
                    reason, paths = self._pending.pop(library_id)
                    if paths is None:
                        self.queue_scan(library_id, reason)
                    else:
                        self.library_changed(library_id, paths)

    def _merge_pending(self, library_id: int, reason: str, paths: set[Path] | None) -> None:
        old = self._pending.get(library_id)
        if paths is None or old is None:
            self._pending[library_id] = (reason, paths)
        elif old[1] is not None:
            self._pending[library_id] = (old[0], old[1] | paths)

    def library_changed(self, library_id: int, target_paths: set[Path]) -> None:
        if self._stop.is_set() or not target_paths:
            return
        target_paths = {path.absolute() for path in target_paths}
        if library_id in self._scan_tasks:
            self.queue_scan(library_id, "watch", target_paths=target_paths)
            return
        self._merge_pending(library_id, "watch", target_paths)
        if self.paused:
            self._dirty.add(library_id)
            return
        previous = self._debounce_tasks.pop(library_id, None)
        if previous is not None:
            self._retiring_tasks.add(previous)
            previous.add_done_callback(self._retiring_tasks.discard)
            previous.cancel()
        self._debounce_tasks[library_id] = asyncio.create_task(self._debounced_scan(library_id))

    async def _debounced_scan(self, library_id: int) -> None:
        try:
            await self._sleep(self.debounce_s)
            if self.paused:
                self._dirty.add(library_id)
            else:
                reason, paths = self._pending.pop(library_id)
                self.queue_scan(library_id, reason, target_paths=paths)
        finally:
            # A displaced scan must not retire its replacement's registry slot.
            if self._debounce_tasks.get(library_id) is asyncio.current_task():
                self._debounce_tasks.pop(library_id, None)

    def _watch_changes(
        self, library: Library, stopped: threading.Event, changed: Callable[[set[Path]], object]
    ) -> None:
        excluded = tuple(
            path.resolve()
            for path in (self.staging.workspace_dir, healthcheck_dir(), self.trash_dir)
            if path is not None
        )
        roots = []
        for value in library.roots:
            root = Path(value).absolute()
            if root.name.startswith(".") or root.is_symlink():
                continue
            root = root.resolve()
            if any(root.is_relative_to(path) for path in excluded):
                continue
            if not stat.S_ISDIR(root.stat().st_mode):
                message = f"Watch root is not a directory: {root}"
                raise NotADirectoryError(message)
            roots.append(root)
        if not roots or stopped.is_set():
            return

        def wanted(change: int, value: str) -> bool:
            path = Path(value)
            root = next((root for root in roots if path.is_relative_to(root)), None)
            if root is None:
                return False
            parts = path.relative_to(root).parts
            if any(part.startswith(".") or part in ignored or part.endswith(_TEMP_SUFFIX) for part in parts):
                return False
            if any(path.resolve().is_relative_to(directory) for directory in excluded):
                return False
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
                return False
            # Parent metadata changes accompany file creation. They are not subtree
            # triggers. Added and deleted directories still rescan their contents.
            if path.is_dir():
                return change != Change.modified
            # Deleted directories cannot be statted. Keep unknown subtree targets.
            return change == Change.deleted or path.suffix.lstrip(".").lower() in allowed

        with RustNotify(
            [str(root) for root in roots],
            debug=False,
            force_polling=False,
            poll_delay_ms=300,
            recursive=True,
            ignore_permission_denied=False,
        ) as watcher:
            # watchfiles falls back on ENOSYS even with force_polling=False. Its public
            # generators hide the selected backend, so reject that fallback here.
            if not repr(watcher).startswith("RustNotify(Recommended("):
                raise RuntimeError("Native filesystem notifications are unavailable. Polling fallback was refused.")
            while not stopped.is_set():
                changes = watcher.watch(debounce_ms=100, step_ms=50, timeout_ms=1000, stop_event=stopped)
                if changes == "stop":
                    return
                if changes == "signal":
                    raise RuntimeError("Native filesystem watcher received an unexpected signal.")
                if isinstance(changes, set):
                    config = self.scanner
                    ignored = set(config.ignore_dirs)
                    allowed = {container.lower() for container in config.containers}
                    paths = {Path(path) for change, path in changes if wanted(change, path)}
                    if paths:
                        changed(merge_targets(paths))

    async def _watch_library(self, library: Library) -> None:
        stopped = threading.Event()
        loop = asyncio.get_running_loop()

        def changed(paths: set[Path]) -> None:
            if not stopped.is_set() and not self._stop.is_set():
                if self._running and self._watch_configs.get(library.id) != (library.name, tuple(library.roots)):
                    return
                self.library_changed(library.id, paths)

        # Long-lived watchers must not occupy the shared filesystem executor used by scans.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"watch-{library.id}") as executor:
            worker = asyncio.wrap_future(
                executor.submit(
                    self._watch_changes, library, stopped, lambda paths: loop.call_soon_threadsafe(changed, paths)
                )
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception(
                    "Native Watch failed for library %s (%s), roots %s", library.name, library.id, library.roots
                )
            finally:
                stopped.set()
                await drain(asyncio.gather(worker, return_exceptions=True))
