from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from common.capabilities import resolve_mkv_tools
from common.enums import ExecutionLocation, JobStatus
from common.process import drain
from sqlalchemy import and_, exists, func, literal, select, union, update
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import aliased

from server.core.db import _complete
from server.events import publish_file_status, publish_job_status
from server.fs.library import LibraryFS, LibraryProbeError
from server.fs.source import check_source_root, lock_job_source
from server.metadata_jobs import METADATA_JOB_KINDS, cancel_metadata, enqueue_metadata_job, metadata_source
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, NodeRow, ProbeRow, utcnow
from server.queue.store import (
    QUERY_BATCH,
    ActiveJobError,
    finish,
    release_expired_holds,
    requeue,
)
from server.serialize import iso_utc
from server.workspace import recovery_jobs

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.events import EventBus

LOG = logging.getLogger(__name__)


class ProbeService:
    """Execute saved metadata jobs with separate probe and statistics limits."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        ffprobe_bin: str | Path,
        mkvpropedit_bin: str | Path,
        events: EventBus | None = None,
        healthcheck_wake: Callable[[], None] | None = None,
        max_concurrency: int = 4,
        *,
        workspace_dir: Path,
        on_healthy: Callable[[], None] | None = None,
        scanner_paused: Callable[[], bool] | None = None,
        max_statistics: int = 1,
        busy_jobs: Collection[int] = (),
    ) -> None:
        if max_concurrency < 1 or max_statistics < 1:
            raise ValueError("Metadata concurrency limits must be positive.")
        self.sessionmaker = sessionmaker
        self.ffprobe_bin = ffprobe_bin
        self.mkvpropedit_bin = mkvpropedit_bin
        self.statistics_available = resolve_mkv_tools(mkvpropedit=str(mkvpropedit_bin)).mkvpropedit is not None
        self.events = events
        self.healthcheck_wake = healthcheck_wake
        self.on_healthy = on_healthy
        self.scanner_paused = scanner_paused
        self.max_concurrency = max_concurrency
        self.max_statistics = max_statistics
        self.statistics_location = ExecutionLocation.LOCAL
        # Publication exposes pending metadata before its inline refresh finishes.
        # Do not start a competing probe while the previous task is still running.
        # Saved artifact markers alone must not block recovery after the task exits.
        self.busy_jobs = busy_jobs
        if events is not None:
            events.probe_slots = max_concurrency
            events.mkv_stats_slots = max_statistics
        self.workspace_dir = workspace_dir
        self.dispatch_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._task_libraries: dict[int, int] = {}
        self._task_kinds: dict[int, str] = {}
        self._cancelled: set[int] = set()
        self._interrupted: set[int] = set()
        self._progress_at = float("-inf")
        self._closed = False
        self.failed = False
        self._close_task: asyncio.Task[None] | None = None
        self._cooldown_until: datetime | None = None

    def configure(self, *, max_concurrency: int, max_statistics: int, statistics_location: ExecutionLocation) -> None:
        self.max_concurrency = max_concurrency
        self.max_statistics = max_statistics
        self.statistics_location = statistics_location
        if self.events is not None:
            self.events.probe_slots = max_concurrency
            self.events.mkv_stats_slots = (
                max_statistics if self.statistics_available and statistics_location != ExecutionLocation.NODE else 0
            )
        self.wake()

    def _paused(self) -> bool:
        return self.scanner_paused is not None and self.scanner_paused()

    async def start(self) -> None:
        if self._close_task is not None and not self._close_task.done():
            raise RuntimeError("Probe service shutdown is still active.")
        self._close_task = None
        self._closed = False
        self.failed = False
        if self.events is not None:
            self.events.publish("probing.state", {"failed": False})
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run())
            self._loop_task.add_done_callback(self._loop_finished)
        self.wake()

    def _loop_finished(self, task: asyncio.Task[None]) -> None:
        if self._closed:
            return
        self.failed = True
        if self.events is not None:
            self.events.publish("probing.state", {"failed": True})
        exc = None if task.cancelled() else task.exception()
        LOG.error("Probe service stopped unexpectedly.", exc_info=exc)

    def wake(self) -> None:
        if not self._closed:
            self._wake.set()

    async def files_pending(self, identifiers: list[int]) -> None:
        # Discovery calls this after each committed, bounded stat batch. Old
        # reprobe backlog stays in catalog rows until a lane has a free slot.
        try:
            await _complete(self._queue_metadata(identifiers))
        finally:
            self.wake()

    async def _queue_metadata(self, identifiers: list[int]) -> None:
        jobs = []
        async with self.sessionmaker() as session:
            await session.execute(
                select(Library.id)
                .where(Library.id.in_(select(MediaFile.library_id).where(MediaFile.id.in_(identifiers))))
                .order_by(Library.id)
                .with_for_update(read=True)
            )
            rows = (
                await session.execute(
                    select(MediaFile, Library)
                    .join(Library)
                    .where(
                        MediaFile.id.in_(identifiers),
                        MediaFile.probe_id.is_(None),
                        MediaFile.health_status == "pending",
                        MediaFile.metadata_cancelled.is_(False),
                        Library.suspended.is_(False),
                        Library.deleted.is_(False),
                        MediaFile.id.not_in(
                            select(Job.media_file_id).where(Job.id.in_(self.busy_jobs), Job.media_file_id.is_not(None))
                        ),
                    )
                    .order_by(MediaFile.id)
                    .with_for_update(of=MediaFile, skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
            for row, library in rows:
                # A completion may have committed while this row's lock was awaited.
                if (
                    self.busy_jobs
                    and await session.scalar(
                        select(Job.id).where(Job.media_file_id == row.id, Job.id.in_(self.busy_jobs)).limit(1)
                    )
                    is not None
                ):
                    continue
                try:
                    stored = await session.scalar(select(ProbeRow.id).where(ProbeRow.media_file_id == row.id))
                    job = await enqueue_metadata_job(
                        session, row, library, kind="mkv_stats" if stored else "probe", priority=1
                    )
                    jobs.append(job.id)
                except ActiveJobError:
                    continue
            await session.commit()
        for job_id in jobs:
            await self._publish(job_id)

    async def cancel_job(self, job_id: int) -> bool:
        return await drain(asyncio.create_task(self._cancel_job(job_id)))

    async def _cancel_job(self, job_id: int) -> bool:
        async with self.dispatch_lock:
            async with self.sessionmaker() as session:
                job = await lock_job_source(session, job_id)
                if (
                    job is None
                    or job.kind not in {"probe", "mkv_stats"}
                    or job.status != "running"
                    or job.node_id is not None
                ):
                    return False
                attempt_id = await session.scalar(
                    select(JobAttempt.id).where(JobAttempt.job_id == job_id, JobAttempt.attempt_number == job.attempts)
                )
                assert attempt_id is not None
                session.add(
                    JobEvent(job_id=job_id, attempt_id=attempt_id, type="metadata_cancel_requested", payload={})
                )
                await session.commit()
            task = self._tasks.get(job_id)
            if task is None or task.done():
                # An exited worker has released its tools and source descriptors.
                await self._finish_attempt(job_id, attempt_id, cancelled=True)
                await self._publish(job_id)
                self.wake()
            else:
                self._cancelled.add(job_id)
                # A started worker checks the marker before its first await.
                if task.get_name() == "metadata-running":
                    task.cancel()
        return True

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await drain(self._close_task)

    async def _close(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        async with self.dispatch_lock:
            self._interrupted.update(self._tasks)
            tasks = list(self._tasks.values())
            for task in tasks:
                if task.get_name() == "metadata-running":
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._loop_task = None

    async def _run(self) -> None:
        while not self._closed:
            self._wake.clear()
            try:
                await self._fill()
            except OperationalError, InterfaceError:
                self.failed = True
                if self.events is not None:
                    self.events.publish("probing.state", {"failed": True})
                LOG.exception("Metadata database operation failed. Retrying in 1 second.")
                await asyncio.sleep(1)
                continue
            if self.failed:
                self.failed = False
                if self.events is not None:
                    self.events.publish("probing.state", {"failed": False})
                LOG.info("Metadata database operations recovered.")
            timeout = (
                max(0, (self._cooldown_until - utcnow()).total_seconds()) if self._cooldown_until is not None else None
            )
            try:
                async with asyncio.timeout(timeout):
                    await self._wake.wait()
            except TimeoutError:
                pass

    async def _fill(self) -> None:
        async with self.dispatch_lock:
            if self._closed:
                return
            await self._recover_jobs()
            if self._paused() or self._closed:
                await self._refresh_progress()
                return
            async with self.sessionmaker() as session:
                await release_expired_holds(session, utcnow(), kinds={"mkv_stats"})
                await session.execute(
                    update(Job)
                    .where(
                        Job.kind == "mkv_stats",
                        Job.status == "hold",
                        Job.library_id.in_(select(Library.id).where(Library.mkv_stats.is_(False))),
                    )
                    .values(status="queued", hold_until=None)
                )
                self._cooldown_until = await session.scalar(
                    select(func.min(Job.hold_until)).where(Job.kind == "mkv_stats", Job.status == "hold")
                )
                await session.commit()
            for kind, limit in (("probe", self.max_concurrency), ("mkv_stats", self.max_statistics)):
                slots = limit - sum(value == kind for value in self._task_kinds.values())
                for _ in range(slots):
                    if self._paused() or self._closed:
                        break
                    if not await _complete(self._start_job(kind)):
                        break
            await self._refresh_progress()

    async def _recover_jobs(self) -> None:
        # Only workers that have exited may be requeued. In-flight filesystem
        # cleanup keeps its task registered even if the database is unavailable.
        after = 0
        while True:
            async with self.sessionmaker() as session:
                identifiers = list(
                    await session.scalars(
                        select(Job.id)
                        .where(
                            Job.kind.in_(["probe", "mkv_stats"]),
                            Job.node_id.is_(None),
                            Job.status == "running",
                            Job.delete_requested.is_(False),
                            Job.id.not_in(self._tasks),
                            Job.id > after,
                        )
                        .order_by(Job.id)
                        .limit(QUERY_BATCH)
                    )
                )
                if not identifiers:
                    return
                after = identifiers[-1]
                for job_id in identifiers:
                    job = await requeue(session, job_id)
                    if job is not None and job.status == "failed":
                        row = await session.get(MediaFile, job.media_file_id, populate_existing=True)
                        expected = await session.scalar(
                            select(JobEvent.payload)
                            .where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
                            .order_by(JobEvent.id.desc())
                            .limit(1)
                        )
                        if row is not None and row.library_id == job.library_id and metadata_source(row) == expected:
                            # Exhaustion belongs to this source generation, not just
                            # its job. Otherwise queueing creates a fresh retry budget.
                            row.health_status = "probe_failed"
                            row.health_source = None
                    await session.commit()
                    if job is not None:
                        LOG.info("Recovered orphaned metadata job %s as %s.", job_id, job.status)
                        await self._publish(job_id)

    async def _start_job(self, kind: str) -> bool:
        claimed = await self._claim(kind)
        if claimed is None:
            return False
        job_id, attempt_id, library_id = claimed
        self._task_libraries[job_id] = library_id
        self._task_kinds[job_id] = kind
        if self._closed:
            self._interrupted.add(job_id)
        task = asyncio.create_task(self._execute(job_id, attempt_id), name="metadata-started")
        self._tasks[job_id] = task
        task.add_done_callback(lambda done, jid=job_id: self._finished(jid, done))
        return True

    async def _claim(
        self,
        kind: str,
        *,
        node_id: int | None = None,
        exclude_jobs: Collection[int] = (),
        lease_expires_at: datetime | None = None,
    ) -> tuple[int, int, int] | None:
        if kind == "mkv_stats" and (
            (node_id is None and self.statistics_location == ExecutionLocation.NODE)
            or (node_id is not None and self.statistics_location == ExecutionLocation.LOCAL)
        ):
            return None
        if (
            kind == "mkv_stats"
            and node_id is None
            and self.statistics_location == ExecutionLocation.NODE_LOCAL
            and not self.statistics_available
        ):
            return None
        async with self.sessionmaker() as session:
            busy_job = aliased(Job)
            busy_files = select(busy_job.media_file_id).where(
                busy_job.id.in_(self.busy_jobs), busy_job.media_file_id.is_not(None)
            )
            eligible = Job.library_id.in_(
                select(Library.id).where(Library.suspended.is_(False), Library.deleted.is_(False))
            )
            queued = select(Job).where(
                Job.kind == kind,
                Job.id.not_in(exclude_jobs),
                Job.status == "queued",
                Job.delete_requested.is_(False),
                Job.media_file_id.not_in(busy_files),
                eligible,
            )
            deadline = MediaFile.first_seen + literal(timedelta(seconds=1)) * Library.cooldown_s
            cooling = and_(
                MediaFile.first_seen.is_not(None),
                Library.mkv_stats.is_(True),
                MediaFile.container == "mkv",
                Library.cooldown_s > 0,
                deadline > utcnow(),
            )
            if kind == "mkv_stats":
                queued = queued.join(MediaFile, MediaFile.id == Job.media_file_id).join(
                    Library, Library.id == Job.library_id
                )
                # Enqueue-time holds can be stale after cooldown or first_seen changes.
                # Filter before claiming so cooling jobs do not consume attempts or lane capacity.
                earliest = await session.scalar(queued.with_only_columns(func.min(deadline)).where(cooling))
                if earliest is not None and (self._cooldown_until is None or earliest < self._cooldown_until):
                    self._cooldown_until = earliest
                queued = queued.where(~cooling)
            job = None
            for job_id in await session.scalars(
                queued.with_only_columns(Job.id)
                .order_by(Job.priority.desc(), Job.created_at.desc(), Job.id.desc())
                .limit(QUERY_BATCH)
            ):
                candidate = await lock_job_source(session, job_id, skip_locked=True)
                if candidate is None:
                    continue
                library = await session.get(Library, candidate.library_id)
                row = await session.get(MediaFile, candidate.media_file_id)
                if (
                    library is None
                    or row is None
                    or library.suspended
                    or library.deleted
                    or candidate.status != "queued"
                    or candidate.delete_requested
                ):
                    continue
                if (
                    self.busy_jobs
                    and await session.scalar(
                        select(Job.id).where(Job.media_file_id == row.id, Job.id.in_(self.busy_jobs)).limit(1)
                    )
                    is not None
                ):
                    continue
                if kind == "mkv_stats" and library.mkv_stats and row.container == "mkv":
                    until = row.first_seen + timedelta(seconds=library.cooldown_s)
                    if until > utcnow():
                        if self._cooldown_until is None or until < self._cooldown_until:
                            self._cooldown_until = until
                        continue
                job = candidate
                break
            if job is None:
                # Old catalogs remain saved pending rows. Create only the
                # next runnable item, not a catalog-sized queue ahead of new files.
                stored = exists(select(ProbeRow.id).where(ProbeRow.media_file_id == MediaFile.id))
                active = exists(
                    select(Job.id).where(
                        Job.media_file_id == MediaFile.id,
                        Job.status.in_(
                            ["pending", "hold", "queued", "staging", "claimed", "running", "verifying", "finalizing"]
                        ),
                        ~((Job.status == JobStatus.PENDING) & Job.waiting_metadata.is_(True)),
                    )
                )
                candidates = (
                    select(MediaFile)
                    .join(Library)
                    .where(
                        MediaFile.probe_id.is_(None),
                        MediaFile.health_status == "pending",
                        MediaFile.metadata_cancelled.is_(False),
                        Library.suspended.is_(False),
                        Library.deleted.is_(False),
                        ~active,
                        MediaFile.id.not_in(busy_files),
                        ~exists(
                            recovery_jobs(include_artifact_checks=False, scoped=True).where(
                                Job.media_file_id == MediaFile.id
                            )
                        ),
                        stored if kind == "mkv_stats" else ~stored,
                    )
                )
                if kind == "mkv_stats":
                    earliest = await session.scalar(candidates.with_only_columns(func.min(deadline)).where(cooling))
                    # Only the earliest pending deadline is needed to wake
                    # the bounded dispatch loop.
                    if earliest is not None and (self._cooldown_until is None or earliest < self._cooldown_until):
                        self._cooldown_until = earliest
                    candidates = candidates.where(~cooling)
                identifier = (
                    await session.execute(
                        candidates.with_only_columns(MediaFile.id, MediaFile.library_id)
                        .order_by(MediaFile.metadata_priority.desc(), MediaFile.id.desc())
                        .limit(1)
                    )
                ).one_or_none()
                if identifier is None:
                    return None
                library = await session.get(
                    Library, identifier.library_id, populate_existing=True, with_for_update={"read": True}
                )
                row = await session.scalar(
                    candidates.where(MediaFile.id == identifier.id)
                    .with_for_update(of=MediaFile, skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if row is None:
                    return None
                if (
                    self.busy_jobs
                    and await session.scalar(
                        select(Job.id).where(Job.media_file_id == row.id, Job.id.in_(self.busy_jobs)).limit(1)
                    )
                    is not None
                ):
                    return None
                assert library is not None
                try:
                    check_source_root(session, row, library)
                    job = await enqueue_metadata_job(
                        session,
                        row,
                        library,
                        kind="probe" if kind == "probe" else "mkv_stats",
                        priority=row.metadata_priority,
                    )
                except ValueError, ActiveJobError:
                    row.health_status = "probe_failed"
                    await session.commit()
                    self.wake()
                    return None
                if job.status == "hold":
                    assert job.hold_until is not None
                    if self._cooldown_until is None or job.hold_until < self._cooldown_until:
                        self._cooldown_until = job.hold_until
                    await session.commit()
                    return None
            number = await session.scalar(
                update(Job)
                .where(Job.id == job.id, Job.status == "queued", Job.delete_requested.is_(False), eligible)
                .values(
                    status="running" if node_id is None else "claimed",
                    attempts=Job.attempts + 1,
                    started_at=utcnow() if node_id is None else Job.started_at,
                    progress=None,
                    node_id=node_id,
                    lease_expires_at=lease_expires_at,
                )
                .returning(Job.attempts)
            )
            if number is None:
                return None
            node_name = (
                await session.scalar(select(NodeRow.display_name).where(NodeRow.id == node_id))
                if node_id is not None
                else "server"
            )
            attempt = JobAttempt(
                job_id=job.id,
                attempt_number=number,
                node_id=node_id,
                node_name=node_name,
                workload=kind,
                status="running" if node_id is None else "claimed",
                claimed_at=utcnow(),
                started_at=utcnow() if node_id is None else None,
                log_data=b"",
            )
            session.add(attempt)
            await session.flush()
            session.add(
                JobEvent(
                    job_id=job.id, attempt_id=attempt.id, type="started" if node_id is None else "claimed", payload={}
                )
            )
            await session.commit()
            return job.id, attempt.id, job.library_id

    async def _execute(self, job_id: int, attempt_id: int) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.set_name("metadata-running")
        started = time.perf_counter()
        result = None
        failure: LibraryProbeError | None = None
        try:
            if job_id in self._cancelled or job_id in self._interrupted:
                raise asyncio.CancelledError  # noqa: TRY301 -- Finish a cancelled attempt before executing tools.
            async with self.sessionmaker() as session:
                job = await session.get(Job, job_id)
                assert job is not None
            await self._publish(job_id)
            fs = LibraryFS(
                job.library_id,
                sessionmaker=self.sessionmaker,
                ffprobe_bin=self.ffprobe_bin,
                mkvpropedit_bin=self.mkvpropedit_bin,
                workspace_dir=self.workspace_dir,
                stats_allowed=lambda: not self._paused(),
                on_tool=lambda payload: self._tool(job_id, attempt_id, payload),
            )
            result = await fs.run_job(job_id, attempt_id)
        except asyncio.CancelledError:
            await drain(asyncio.create_task(self._finish_attempt(job_id, attempt_id, cancelled=True)))
        except LibraryProbeError as exc:
            failure = exc
            await drain(asyncio.create_task(self._finish_attempt(job_id, attempt_id, failure=exc)))
            LOG.error("Metadata job %s failed", job_id, extra={"error": str(exc)})
        except Exception as exc:
            failure = LibraryProbeError(
                str(exc) or type(exc).__name__, reason="failed", cleanup="unresolved", media_id=None
            )
            await drain(asyncio.create_task(self._finish_attempt(job_id, attempt_id, failure=failure)))
        finally:
            await drain(asyncio.create_task(self._publish(job_id)))
        if result is not None:
            async with self.sessionmaker() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                completed = job.status == "succeeded"
                done = completed and result.media.probe_id is not None
                followup = await session.scalar(
                    select(Job.id)
                    .where(
                        Job.media_file_id == job.media_file_id,
                        Job.kind.in_(["healthcheck", "mkv_stats"]),
                        Job.status.in_(["queued", "hold"]),
                    )
                    .limit(1)
                )
            if self.events is not None and completed:
                samples = self.events.probe_samples if job.kind == "probe" else self.events.mkv_stats_samples
                samples.append((self.events.clock(), time.perf_counter() - started))
            if done:
                self._processed(job.library_id)
            if followup is not None:
                await self._publish(followup)
            if self.events is not None:
                await publish_file_status(self.events, self.sessionmaker, job.media_file_id, action="probed", bulk=True)
        elif failure is not None:
            self._processed(self._task_libraries[job_id], failed=True)
            if self.events is not None:
                async with self.sessionmaker() as session:
                    media_id = await session.scalar(select(Job.media_file_id).where(Job.id == job_id))
                if media_id is not None:
                    await publish_file_status(
                        self.events, self.sessionmaker, media_id, action="probe_error", error=str(failure), bulk=True
                    )

    async def _finish_attempt(
        self, job_id: int, attempt_id: int, *, cancelled: bool = False, failure: LibraryProbeError | None = None
    ) -> None:
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id, attempt_id=attempt_id)
            if job is None:
                return
            library = await session.get(Library, job.library_id, populate_existing=True)
            row = await session.get(MediaFile, job.media_file_id, populate_existing=True)
            attempt = await session.get(JobAttempt, attempt_id, populate_existing=True, with_for_update=True)
            if job is None or attempt is None or attempt.attempt_number != job.attempts or job.status != "running":
                return
            requested = await session.scalar(
                select(JobEvent.id)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.attempt_id == attempt_id,
                    JobEvent.type == "metadata_cancel_requested",
                )
                .limit(1)
            )
            cancelled |= requested is not None
            interrupted = cancelled and job_id not in self._cancelled and requested is None
            interrupted |= (
                failure is not None
                and failure.reason == "unauthorized"
                and (self._paused() or library is None or library.suspended)
            )
            if interrupted:
                await requeue(session, job_id, max_attempts=job.attempts + 1)
                expected = await session.scalar(
                    select(JobEvent.payload)
                    .where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
                    .order_by(JobEvent.id.desc())
                    .limit(1)
                )
                if row is not None and metadata_source(row) == expected:
                    row.health_status = "pending"
                message = "Metadata work interrupted and requeued."
                detail = {"result": "interrupted", "reason": message}
            else:
                if cancelled:
                    await cancel_metadata(session, job)
                detail = {"result": "cancelled" if cancelled else "failed"}
                message = f"Metadata work {detail['result']}."
                if failure is not None:
                    detail.update(
                        tool=failure.tool,
                        exit_code=failure.exit_code,
                        stdout=failure.stdout,
                        stderr=failure.stderr,
                        reason=failure.reason,
                        cleanup=failure.cleanup,
                        diagnostics=list(failure.diagnostics),
                        error=str(failure),
                    )
                await finish(
                    session,
                    job_id,
                    JobStatus.CANCELLED if cancelled else JobStatus.FAILED,
                    error="Cancelled by operator." if cancelled else str(failure),
                    source_size=attempt.source_size,
                )
            lines = [message]
            if not interrupted and failure is not None:
                lines.append(str(failure))
                if failure.tool is not None:
                    lines.append(f"Tool: {failure.tool}.")
                if failure.exit_code is not None:
                    lines.append(f"Exit code: {failure.exit_code}.")
                lines.append(f"Source cleanup: {failure.cleanup} ({failure.reason}).")
                lines.extend(failure.diagnostics)
                lines.extend(
                    f"{name}:\n{output}" for name in ("stdout", "stderr") if (output := getattr(failure, name))
                )
            attempt.log_data = (attempt.log_data or b"") + ("\n".join(lines) + "\n").encode()
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="metadata_result", payload=detail))
            await session.commit()

    async def _tool(self, job_id: int, attempt_id: int, payload: dict) -> None:
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id, attempt_id=attempt_id)
            if job is None:
                return
            row = await session.get(MediaFile, job.media_file_id, populate_existing=True)
            attempt = await session.get(JobAttempt, attempt_id, populate_existing=True, with_for_update=True)
            if (
                job is None
                or attempt is None
                or job.status != "running"
                or attempt.status != "running"
                or attempt.attempt_number != job.attempts
            ):
                return
            expected = await session.scalar(
                select(JobEvent.payload)
                .where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            if row is None or metadata_source(row) != expected:
                return
            previous = job.progress or {}
            started = previous.get("started_at") if previous.get("tool") == payload["tool"] else iso_utc(utcnow())
            progress = {
                "tool": payload["tool"],
                "percent": payload.get("percent"),
                "stage": payload["stage"],
                "started_at": started,
            }
            job.progress = progress
            if "argv" in payload:
                attempt.argv = payload["argv"]
                lines = [f"{payload['tool']} {payload['stage']} for {row.path}."]
                if "exit_code" in payload:
                    lines.append(f"Exit code: {payload['exit_code']}.")
                lines.extend(f"{name}:\n{output}" for name in ("stdout", "stderr") if (output := payload.get(name)))
                attempt.log_data = (attempt.log_data or b"") + ("\n".join(lines) + "\n").encode()
                session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="metadata_tool", payload=payload))
            await session.flush()
            stamp = job.updated_at
            await session.commit()
            library_id, media_id, path = job.library_id, row.id, row.path
        if self.events is not None:
            self.events.publish("job.progress", {"job_id": job_id, "progress": progress, "updated_at": iso_utc(stamp)})
            active = self.events.library_snapshot(library_id)["probing"]["active"]
            item = {
                "job_id": job_id,
                "media_file_id": media_id,
                "path": path,
                "tool": progress["tool"],
                "started_at": started,
                "percent": progress["percent"],
            }
            self.events.update_library_progress(
                library_id,
                probing={
                    "status": "scanning",
                    "active": [*[entry for entry in active if entry["job_id"] != job_id], item],
                },
            )

    async def check_library_access(self) -> None:
        async with self.dispatch_lock:
            async with self.sessionmaker() as session:
                allowed = set()
                rows = (
                    await session.execute(
                        select(Job.id, MediaFile, Library)
                        .join(MediaFile, MediaFile.id == Job.media_file_id)
                        .join(Library, Library.id == Job.library_id)
                        .where(Job.id.in_(self._tasks), Library.suspended.is_(False), Library.deleted.is_(False))
                    )
                ).all()
                for job_id, row, library in rows:
                    try:
                        check_source_root(session, row, library)
                    except ValueError:
                        continue
                    allowed.add(job_id)
            tasks = []
            for job_id in self._tasks:
                if job_id not in allowed:
                    self._interrupted.add(job_id)
                    task = self._tasks[job_id]
                    if task.get_name() == "metadata-running":
                        task.cancel()
                    tasks.append(task)
        if tasks:
            await drain(asyncio.gather(*tasks, return_exceptions=True))
        self.wake()

    async def _publish(self, job_id: int) -> None:
        if self.events is not None:
            await publish_job_status(self.events, self.sessionmaker, job_id, bulk=True)

    def _finished(self, job_id: int, task: asyncio.Task[None]) -> None:
        self._tasks.pop(job_id, None)
        library_id = self._task_libraries.pop(job_id, None)
        self._task_kinds.pop(job_id, None)
        self._cancelled.discard(job_id)
        self._interrupted.discard(job_id)
        if self.events is not None and library_id is not None:
            state = self.events.library_snapshot(library_id)["probing"]
            self.events.update_library_progress(
                library_id,
                probing={
                    "active": [entry for entry in state["active"] if entry["job_id"] != job_id],
                    "status": self._pending_status(library_id)
                    if state["remaining"]
                    else ("failed" if state["error_count"] else "completed"),
                },
            )
        self.wake()
        # Completion may have committed even if its status publication failed.
        # Wake saved follow-up requests without depending on another database read.
        if self.healthcheck_wake is not None:
            self.healthcheck_wake()
        if self.on_healthy is not None:
            self.on_healthy()
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Metadata job %s completion failed.", job_id, exc_info=task.exception())

    async def _remaining(self) -> dict[int, int]:
        async with self.sessionmaker() as session:
            pending = select(MediaFile.id, MediaFile.library_id).where(
                MediaFile.probe_id.is_(None),
                MediaFile.health_status == "pending",
                MediaFile.metadata_cancelled.is_(False),
            )
            active = (
                select(MediaFile.id, MediaFile.library_id)
                .join(Job, Job.media_file_id == MediaFile.id)
                .where(
                    Job.kind.in_(METADATA_JOB_KINDS),
                    Job.status.in_(["pending", "hold", "queued", "running", "claimed"]),
                )
            )
            remaining = union(pending, active).subquery()
            rows = await session.execute(
                select(Library.id, func.count(remaining.c.id))
                .outerjoin(remaining, remaining.c.library_id == Library.id)
                .group_by(Library.id)
                .order_by(Library.id)
            )
            return dict(rows.tuples().all())

    def _pending_status(self, library_id: int) -> str:
        if library_id in self._task_libraries.values():
            return "scanning"
        return "paused" if self._paused() else "queued"

    async def progress_snapshot(self) -> list[dict]:
        if self.events is None:
            return []
        remaining = await _complete(self._remaining())
        result = []
        for library_id, count in remaining.items():
            state = self.events.library_snapshot(library_id)
            state["probing"]["remaining"] = count
            if count:
                state["probing"]["status"] = self._pending_status(library_id)
            elif library_id not in self._task_libraries.values() and state["probing"]["status"] in {
                "queued",
                "scanning",
                "paused",
            }:
                state["probing"]["status"] = "failed" if state["probing"]["error_count"] else "completed"
            result.append(state)
        return result

    async def _refresh_progress(self) -> None:
        if self.events is None or self.events.clock() - self._progress_at < 1:
            return
        self._progress_at = self.events.clock()
        for library_id, count in (await _complete(self._remaining())).items():
            previous = self.events.library_snapshot(library_id)["probing"]
            update = {"remaining": count}
            if count:
                update["status"] = self._pending_status(library_id)
                if previous["remaining"] == 0:
                    update.update(processed=0, error_count=0)
            elif previous["status"] in {"queued", "scanning", "paused"}:
                update["status"] = "failed" if previous["error_count"] else "completed"
            if any(previous.get(key) != value for key, value in update.items()):
                self.events.update_library_progress(library_id, probing=update)

    def _processed(self, library_id: int, *, failed: bool = False) -> None:
        if self.events is not None:
            state = self.events.library_snapshot(library_id)["probing"]
            self.events.update_library_progress(
                library_id,
                probing={
                    "processed": state["processed"] + 1,
                    "remaining": max(0, state["remaining"] - 1),
                    "error_count": state["error_count"] + int(failed),
                },
            )
