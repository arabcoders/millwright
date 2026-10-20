from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select

from server.event_rows import ACTIVE_FILE_JOB_STATUSES, job_row, media_file_row
from server.metrics import EVENTS_SENT
from server.models import Job, JobAttempt, Library, MediaFile
from server.serialize import iso_utc

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_PENDING_KEYS = 256
MAX_BATCH_PENDING = 512
MAX_BATCH_SIZE = 100
EVENT_BATCH_INTERVAL = 0.5


@dataclass(kw_only=True, slots=True)
class Event:
    type: str
    payload: dict
    key: str


async def publish_job_status(
    bus: EventBus, sessionmaker: async_sessionmaker[AsyncSession], job_id: int, *, bulk: bool = False
) -> None:
    async with sessionmaker() as session:
        await publish_job_status_session(bus, session, job_id, bulk=bulk)


async def publish_job_status_session(bus: EventBus, session: AsyncSession, job_id: int, *, bulk: bool = False) -> None:
    job = await session.get(Job, job_id)
    if job is None:
        return
    file = await session.get(MediaFile, job.media_file_id) if job.media_file_id is not None else None
    attempt = (
        await session.execute(
            select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_number.desc()).limit(1)
        )
    ).scalar_one_or_none()
    bus.publish(
        "job.status",
        {
            "job_id": job.id,
            "status": job.status,
            "priority": job.priority,
            "error": job.error,
            "warning": job.warning,
            "finished_at": iso_utc(job.finished_at),
            "job": job_row(job, file, attempt),
        },
        key=f"job.status:{job.id}",
        bulk=bulk,
    )
    if file is not None:
        await publish_file_status_session(bus, session, file.id, bulk=bulk)


async def publish_file_status(
    bus: EventBus,
    sessionmaker: async_sessionmaker[AsyncSession],
    file_id: int,
    *,
    action: str = "updated",
    bulk: bool = False,
    **extra: str,
) -> None:
    async with sessionmaker() as session:
        await publish_file_status_session(bus, session, file_id, action=action, bulk=bulk, **extra)


async def publish_file_status_session(
    bus: EventBus, session: AsyncSession, file_id: int, *, action: str = "updated", bulk: bool = False, **extra: str
) -> None:
    file = await session.get(MediaFile, file_id)
    if file is None:
        return
    active = await session.scalar(
        select(Job)
        .where(Job.media_file_id == file_id, Job.status.in_(ACTIVE_FILE_JOB_STATUSES))
        .order_by(Job.id.desc())
        .limit(1)
    )
    library = await session.get(Library, file.library_id)
    bus.publish(
        "file.status",
        {
            "action": action,
            "library_id": file.library_id,
            "media_file_id": file.id,
            "file": media_file_row(file, active, library=library),
            **extra,
        },
        key=f"file.status:file:{file.id}",
        bulk=bulk,
    )


class Subscription:
    """One /api/events client. Keyed events coalesce to the latest. Overflow drops the oldest."""

    def __init__(self, max_pending: int = MAX_PENDING_KEYS) -> None:
        self._pending: dict[str, Event] = {}
        self._order: deque[str] = deque()
        self._max = max_pending
        self._ready = asyncio.Event()
        self.closed = asyncio.Event()
        self._resync = False

    def offer(self, event: Event) -> None:
        if event.key not in self._pending:
            self._order.append(event.key)
            while len(self._order) > self._max:
                self._pending.pop(self._order.popleft(), None)
                self._resync = True
        self._pending[event.key] = event
        self._ready.set()

    def close(self) -> None:
        self.closed.set()
        self._ready.set()

    async def next(self) -> Event | None:
        while True:
            self._ready.clear()
            if self._resync:
                self._resync = False
                return Event(type="events.resync", payload={"reason": "overflow"}, key="events.resync")
            while self._order:
                event = self._pending.pop(self._order.popleft(), None)
                if event is not None:
                    return event
            if self.closed.is_set():
                return None
            await self._ready.wait()

    @property
    def backlog(self) -> int:
        return len(self._order)


class EventBus:
    """Send each event key at most once per interval, then flush its latest staged update."""

    def __init__(self, *, min_interval_s: float = 1.0, clock: Callable[[], float] | None = None) -> None:
        self.min_interval_s = min_interval_s
        self.healthcheck_slots = 0
        self.probe_slots = 0
        self.mkv_stats_slots = 0
        self.healthchecks_on_nodes = False
        self.mkv_stats_on_nodes = False
        self.scanner_paused = False
        self.probe_samples: deque[tuple[float, float]] = deque(maxlen=100)
        self.mkv_stats_samples: deque[tuple[float, float]] = deque(maxlen=100)
        self.timezone = ZoneInfo("UTC")
        self.clock = clock if clock is not None else self._loop_time
        self._subs: set[Subscription] = set()
        self._last: dict[str, float] = {}
        self._staged: dict[str, Event] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._closed = False
        self._bulk: dict[str, Event] = {}
        self._bulk_timer: asyncio.TimerHandle | None = None
        self._bulk_overflow = False
        self._batch_number = 0
        self._terminal_jobs: dict[int, float] = {}
        self._latest_progress: dict[int, Event] = {}
        self._removed_files: dict[int, None] = {}
        self.library_progress: dict[int, dict] = {}

    def library_snapshot(self, library_id: int) -> dict:
        state: dict | None = self.library_progress.get(library_id)
        if state is None:
            state = {
                "library_id": library_id,
                "scanner": {"paused": False, "next_run_at": None, "discovery_pending": False},
                "discovery": {
                    "status": "idle",
                    "found": 0,
                    "added": 0,
                    "updated": 0,
                    "error_count": 0,
                    "error": None,
                    "reason": None,
                    "latest_result": None,
                    "finished_at": None,
                },
                "probing": {"status": "idle", "processed": 0, "remaining": 0, "error_count": 0, "active": []},
            }
        return {
            "library_id": library_id,
            "scanner": {**state["scanner"], "paused": self.scanner_paused},
            "discovery": dict(state["discovery"]),
            "probing": dict(state["probing"]),
        }

    def update_library_progress(
        self,
        library_id: int,
        *,
        discovery: dict | None = None,
        probing: dict | None = None,
        scanner: dict | None = None,
    ) -> None:
        state = self.library_snapshot(library_id)
        if scanner is not None:
            state["scanner"].update(scanner)
        state["scanner"]["paused"] = self.scanner_paused
        if discovery is not None:
            state["discovery"].update(discovery)
        if probing is not None:
            state["probing"].update(probing)
        self.library_progress[library_id] = state
        self.publish("library.progress", state, key=f"library.progress:{library_id}")

    @staticmethod
    def _loop_time() -> float:
        return asyncio.get_running_loop().time()

    @property
    def probe_mean_s(self) -> float | None:
        cutoff = self.clock() - 86400
        samples = [duration for stamp, duration in self.probe_samples if stamp >= cutoff and duration > 0]
        return sum(samples) / len(samples) if samples else None

    @property
    def mkv_stats_mean_s(self) -> float | None:
        cutoff = self.clock() - 86400
        samples = [duration for stamp, duration in self.mkv_stats_samples if stamp >= cutoff and duration > 0]
        return sum(samples) / len(samples) if samples else None

    def publish(self, type_: str, payload: dict, key: str | None = None, *, bulk: bool = False) -> None:
        if self._closed:
            return
        EVENTS_SENT.labels(type_).inc()
        file_id = payload.get("media_file_id")
        if type_ == "file.status" and isinstance(file_id, int):
            # Catalog IDs are never reused. An in-flight database read can finish after removal.
            if payload.get("action") in {"pruned", "deleted"}:
                self._removed_files[file_id] = None
                if len(self._removed_files) > MAX_BATCH_PENDING:
                    self._removed_files.pop(next(iter(self._removed_files)))
            elif file_id in self._removed_files:
                return
        if not self._subs:
            return
        coalesce = type_ if key is None else key
        if key is None and type_ in {"job.status", "job.progress"}:
            coalesce = f"{type_}:{payload['job_id']}"
        event = Event(type=type_, payload=payload, key=coalesce)
        if type_ == "job.deleted":
            job_id = payload.get("job_id")
            if isinstance(job_id, int):
                self._bulk.pop(f"job.status:{job_id}", None)
                self._bulk.pop(f"job.progress:{job_id}", None)
                self._remember_terminal(job_id)
            self._send(coalesce, event)
            return
        batch = type_ in {"job.status", "job.progress"} and isinstance(payload.get("job_id"), int)
        batch = batch or (type_ == "file.status" and isinstance(payload.get("media_file_id"), int))
        batch = batch or (bulk and type_ in {"job.status", "file.status"})
        if batch:
            job_id = payload.get("job_id")
            terminal = type_ == "job.status" and payload.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
                "skipped",
            }
            if isinstance(job_id, int) and type_ == "job.status" and not terminal:
                self._terminal_jobs.pop(job_id, None)
                if payload.get("status") == "queued":
                    self._latest_progress.pop(job_id, None)
            if isinstance(job_id, int) and terminal:
                self._latest_progress.pop(job_id, None)
            if type_ == "job.progress" and payload["job_id"] in self._terminal_jobs:
                return
            stamp = _updated_at(payload)
            pending = self._bulk.get(coalesce)
            if pending is not None and stamp is not None:
                previous = _updated_at(pending.payload)
                if previous is not None and stamp < previous:
                    return
            if isinstance(job_id, int) and type_ == "job.progress":
                status = self._bulk.get(f"job.status:{job_id}")
                status_stamp = _updated_at(status.payload) if status is not None else None
                if status_stamp is not None and stamp is not None and stamp < status_stamp:
                    return
                if status is not None and stamp is not None and (status_stamp is None or stamp > status_stamp):
                    merged = dict(status.payload)
                    if "progress" in payload:
                        merged["progress"] = payload["progress"]
                    merged["updated_at"] = payload.get("updated_at", merged.get("updated_at"))
                    if isinstance(merged.get("job"), dict):
                        merged["job"] = dict(merged["job"])
                        merged["job"]["updated_at"] = merged["updated_at"]
                        if "progress" in payload:
                            merged["job"]["progress"] = payload["progress"]
                    self._bulk[status.key] = Event(type="job.status", payload=merged, key=status.key)
                    self._bulk.pop(coalesce, None)
                    self._latest_progress[job_id] = event
                    if len(self._latest_progress) > MAX_BATCH_PENDING:
                        self._latest_progress.pop(next(iter(self._latest_progress)))
                    return
            if isinstance(job_id, int) and type_ == "job.status":
                progress = self._bulk.get(f"job.progress:{job_id}")
                progress = progress or self._latest_progress.get(job_id)
                progress_stamp = _updated_at(progress.payload) if progress is not None else None
                if progress is not None and stamp is not None and progress_stamp is not None and progress_stamp > stamp:
                    merged = dict(payload)
                    merged["updated_at"] = progress.payload.get("updated_at", merged.get("updated_at"))
                    if "progress" in progress.payload:
                        merged["progress"] = progress.payload["progress"]
                    if isinstance(merged.get("job"), dict):
                        merged["job"] = dict(merged["job"])
                        merged["job"]["updated_at"] = merged["updated_at"]
                        if "progress" in progress.payload:
                            merged["job"]["progress"] = progress.payload["progress"]
                    event = Event(type="job.status", payload=merged, key=coalesce)
                    self._bulk.pop(f"job.progress:{job_id}", None)
            self._bulk[coalesce] = event
            if isinstance(job_id, int) and type_ == "job.progress":
                self._latest_progress[job_id] = event
                if len(self._latest_progress) > MAX_BATCH_PENDING:
                    self._latest_progress.pop(next(iter(self._latest_progress)))
            if len(self._bulk) > MAX_BATCH_PENDING:
                self._bulk.clear()
                self._bulk_overflow = True
                return
            if self._bulk_timer is None:
                self._bulk_timer = asyncio.get_running_loop().call_later(EVENT_BATCH_INTERVAL, self.flush_bulk)
            return
        self._bulk.pop(coalesce, None)
        now = self.clock()
        last = self._last.get(coalesce)
        if last is not None and now - last < self.min_interval_s:
            self._staged[coalesce] = event
            if coalesce not in self._timers:
                delay = self.min_interval_s - (now - last)
                self._timers[coalesce] = asyncio.get_running_loop().call_later(delay, self._flush, coalesce)
            return
        self._send(coalesce, event)

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        sub.close()

    def staged_count(self) -> int:
        return len(self._staged)

    async def aclose(self) -> None:
        self._closed = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._staged.clear()
        if self._bulk_timer is not None:
            self._bulk_timer.cancel()
            self._bulk_timer = None
        self._bulk.clear()
        self._bulk_overflow = False
        self._terminal_jobs.clear()
        self._latest_progress.clear()
        self._removed_files.clear()
        for sub in list(self._subs):
            sub.close()

    def _flush(self, key: str) -> None:
        self._timers.pop(key, None)
        event = self._staged.pop(key, None)
        if event is not None:
            self._send(key, event)

    def _send(self, key: str, event: Event) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        # A superseded staged event drops here: the fresh event already carries its state.
        self._staged.pop(key, None)
        self._last[key] = self.clock()
        if len(self._last) > 512:
            cutoff = self.clock() - self.min_interval_s
            self._last = {name: stamp for name, stamp in self._last.items() if stamp > cutoff}
        for sub in self._subs:
            sub.offer(event)

    def _remember_terminal(self, job_id: int) -> None:
        self._terminal_jobs[job_id] = self.clock()
        if len(self._terminal_jobs) > MAX_BATCH_PENDING:
            self._terminal_jobs.pop(next(iter(self._terminal_jobs)))

    def flush_bulk(self) -> None:
        if self._bulk_timer is not None:
            self._bulk_timer.cancel()
        self._bulk_timer = None
        if self._closed:
            return
        if self._bulk_overflow:
            self._bulk_overflow = False
            for sub in self._subs:
                sub._resync = True
                sub._ready.set()
        if not self._bulk:
            return
        events = list(self._bulk.values())
        terminal_ids = {
            event.payload.get("job_id")
            for event in events
            if event.type == "job.status"
            and event.payload.get("status") in {"succeeded", "failed", "cancelled", "skipped"}
        }
        for job_id in terminal_ids:
            if isinstance(job_id, int):
                self._bulk.pop(f"job.progress:{job_id}", None)
        events = [
            event
            for event in events
            if not (event.type == "job.progress" and event.payload.get("job_id") in terminal_ids)
        ]
        selected = events[:MAX_BATCH_SIZE]
        selected.sort(key=lambda event: (event.payload.get("job_id", -1), event.type == "job.progress"))
        for event in selected:
            if event.type == "job.status" and event.payload.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
                "skipped",
            }:
                job_id = event.payload.get("job_id")
                if isinstance(job_id, int):
                    self._remember_terminal(job_id)
                    self._bulk.pop(f"job.progress:{job_id}", None)
        for event in selected:
            self._bulk.pop(event.key, None)
        self._batch_number += 1
        batch = Event(
            type="events.batch",
            payload={"events": [{"type": e.type, "payload": e.payload} for e in selected]},
            key=f"events.batch:{self._batch_number}",
        )
        for sub in self._subs:
            sub.offer(batch)
        if self._bulk:
            self._bulk_timer = asyncio.get_running_loop().call_later(EVENT_BATCH_INTERVAL, self.flush_bulk)


def _updated_at(payload: dict) -> datetime | None:
    for value in (payload.get("updated_at"), *(payload.get(name, {}).get("updated_at") for name in ("job", "file"))):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
    return None
