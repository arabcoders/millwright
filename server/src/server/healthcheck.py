from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING

from common.enums import ExecutionLocation, HealthStatus, JobStatus, TranscodeStatus
from common.fs.identity import source_identity
from common.healthcheck import HealthcheckExitError, HealthcheckResult, _append_log, _decode_healthcheck, _drain
from sqlalchemy import select, update
from sqlalchemy.exc import InterfaceError, OperationalError

from server.events import EventBus, publish_job_status
from server.fs.library import apply_probe_metadata
from server.fs.source import (
    MediaSnapshot,
    authorized_library,
    catalog_generation,
    claim_catalog,
    lock_job_source,
)
from server.healthlogs import (
    DiagnosticLog,
    cleanup_healthcheck_log,
    create_healthcheck_log,
    healthcheck_log_path,
    snapshot_healthcheck_log,
)
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, NodeRow, utcnow
from server.processing_cutoff import processing_allowed
from server.queue.store import enqueue_healthcheck_job, finish, release_transcode_decision, requeue
from server.serialize import iso_utc
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ProgressCallback = Callable[[dict[str, object]], Awaitable[None]]

LOG = logging.getLogger("server.healthcheck")


class HealthcheckService:
    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        log_dir: Path,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        *,
        max_concurrency: int = 1,
        hwaccel: bool = True,
        events: EventBus | None = None,
        on_healthy: Callable[[], None] | None = None,
        activity_paused: Callable[[], bool] | None = None,
    ) -> None:
        if max_concurrency < 1:
            msg = "max_concurrency must be positive"
            raise ValueError(msg)
        self.db, self.log_dir = db, log_dir
        self.ffmpeg_bin, self.ffprobe_bin = ffmpeg_bin, ffprobe_bin
        self.hwaccel = hwaccel
        self.location = ExecutionLocation.LOCAL
        self._max_concurrency = max_concurrency
        self.events = events
        self._on_healthy = on_healthy
        self.on_finished: Callable[[], None] | None = None
        self.activity_paused = activity_paused
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._running: dict[int, tuple[int, asyncio.Task[None]]] = {}
        self._started: set[int] = set()
        self._cancel_requested: set[int] = set()
        self._unreaped: dict[int, str] = {}
        self._retry = False
        self._closed = False
        self._loop_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self.dispatch_lock = asyncio.Lock()

    def configure(self, *, max_concurrency: int, hwaccel: bool, location: ExecutionLocation) -> None:
        self._max_concurrency = max_concurrency
        self.hwaccel = hwaccel
        self.location = location
        self.wake()

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._closed = False
            self._close_task = None
            self._loop_task = asyncio.create_task(self._loop())
            self._tasks.add(self._loop_task)

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _drain(self._close_task)

    async def _close(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
        for job_id in tuple(self._running):
            self.cancel_job(job_id)
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()
        self._tasks.clear()
        self._loop_task = None

    def cancel_job(self, job_id: int) -> bool:
        entry = self._running.get(job_id)
        if entry is None:
            return False
        self._cancel_requested.add(entry[0])
        if entry[0] in self._started:
            entry[1].cancel()
        return True

    async def _loop(self) -> None:
        while not self._closed:
            if self._retry:
                self._retry = False
                await asyncio.sleep(1)
            self._wake.clear()
            if len(self._running) >= self._max_concurrency:
                await self._wake.wait()
                continue
            try:
                async with self.dispatch_lock:
                    await self._recover_jobs()
                    claimed = await self.claim_next()
                    if claimed is not None:
                        job_id, attempt_id = claimed
                        task = asyncio.create_task(self.run(job_id, attempt_id))
                        self._tasks.add(task)
                        self._running[job_id] = (attempt_id, task)
                        task.add_done_callback(self._tasks.discard)
                        task.add_done_callback(lambda finished, jid=job_id: self._finished(jid, finished))
            except OperationalError, InterfaceError:
                LOG.exception("Healthcheck database operation failed. Retrying in 1 second.")
                await asyncio.sleep(1)
                continue
            if claimed is None:
                await self._wake.wait()

    async def _recover_jobs(self) -> None:
        # A registered worker still owns its processes, source descriptors, and cleanup.
        after = 0
        while True:
            async with self.db() as session:
                ids = list(
                    await session.scalars(
                        select(Job.id)
                        .where(
                            Job.kind == "healthcheck",
                            Job.node_id.is_(None),
                            Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                            Job.delete_requested.is_(False),
                            Job.id.not_in(self._running),
                            Job.id > after,
                        )
                        .order_by(Job.id)
                        .limit(100)
                    )
                )
            if not ids:
                return
            after = ids[-1]
            for job_id in ids:
                async with self.db() as session:
                    job = await lock_job_source(session, job_id)
                    if job is None or job.delete_requested or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}:
                        continue
                    attempt = await session.scalar(
                        select(JobAttempt).where(JobAttempt.job_id == job_id, JobAttempt.attempt_number == job.attempts)
                    )
                    assert attempt is not None
                    attempt_id = attempt.id
                    if attempt_id in self._unreaped or attempt_id in self._cancel_requested:
                        await session.close()
                        await self._finish_attempt(
                            job_id,
                            attempt_id,
                            JobStatus.FAILED if attempt_id in self._unreaped else JobStatus.CANCELLED,
                            self._unreaped.get(attempt_id, "cancelled by operator"),
                            interrupted=attempt_id in self._unreaped,
                        )
                    else:
                        await snapshot_healthcheck_log(session, self.log_dir, attempt)
                        if attempt.log_path is None:
                            attempt.log_data = b"healthcheck: interrupted during database outage\n"
                        await requeue(session, job_id)
                        await session.commit()
                self._unreaped.pop(attempt_id, None)
                self._cancel_requested.discard(attempt_id)
                await self._cleanup_log(job_id, attempt_id)
                LOG.info("Recovered orphaned healthcheck job %s.", job_id)
                await self._publish_transition(job_id, "database recovery")

    async def drain_library(self, library_id: int) -> None:
        # The claim commit and task registration must finish together before the dispatch lock is released.
        async with self.dispatch_lock:
            async with self.db() as session:
                ids = list(
                    await session.scalars(select(Job.id).where(Job.library_id == library_id, Job.kind == "healthcheck"))
                )
            tasks = []
            for job_id in ids:
                if (entry := self._running.get(job_id)) is not None:
                    self.cancel_job(job_id)
                    tasks.append(entry[1])
        if tasks:
            results = await _drain(asyncio.gather(*tasks, return_exceptions=True))
            for result in results:
                if isinstance(result, Exception):
                    raise result

    def _finished(self, job_id: int, task: asyncio.Task[None]) -> None:
        current = self._running.get(job_id)
        if current is not None and current[1] is task:
            self._running.pop(job_id)
        if not task.cancelled() and (exc := task.exception()) is not None:
            LOG.error("Healthcheck worker failed for job %s.", job_id, exc_info=exc)
            if isinstance(exc, (OperationalError, InterfaceError)):
                self._retry = True
        self._wake.set()

        if self.on_finished is not None:
            self.on_finished()

    async def claim_next(
        self,
        *,
        node_id: int | None = None,
        exclude_jobs: Collection[int] = (),
        lease_expires_at: datetime | None = None,
    ) -> tuple[int, int] | None:
        if (node_id is None and self.location == ExecutionLocation.NODE) or (
            node_id is not None and self.location == ExecutionLocation.LOCAL
        ):
            return None
        if self.activity_paused is not None and self.activity_paused():
            return None
        async with self.db() as session:
            await _drain(asyncio.create_task(session.connection()))
            eligible = Job.library_id.in_(
                select(Library.id).where(Library.suspended.is_(False), Library.deleted.is_(False))
            )
            candidates = await session.scalars(
                select(Job.id)
                .where(
                    Job.kind == "healthcheck",
                    Job.status == JobStatus.QUEUED.value,
                    Job.id.not_in(exclude_jobs),
                    Job.delete_requested.is_(False),
                    eligible,
                )
                .order_by(Job.priority.desc(), Job.created_at, Job.id)
                .limit(100)
            )
            job = None
            for job_id in candidates:
                job_source = (
                    await session.execute(select(Job.library_id, Job.media_file_id).where(Job.id == job_id))
                ).one_or_none()
                if job_source is None:
                    continue
                library = await session.get(
                    Library,
                    job_source.library_id,
                    with_for_update={"read": True, "skip_locked": True},
                    populate_existing=True,
                )
                if library is None or library.suspended or library.deleted:
                    await session.rollback()
                    continue
                media = await session.get(
                    MediaFile, job_source.media_file_id, with_for_update={"skip_locked": True}, populate_existing=True
                )
                if media is None:
                    await session.rollback()
                    continue
                job = await session.get(Job, job_id, with_for_update={"skip_locked": True}, populate_existing=True)
                if job is not None and job.status == JobStatus.QUEUED and not job.delete_requested:
                    break
                await session.rollback()
                job = None
            if job is None:
                return None
            job_id = job.id
            attempt_number = await session.scalar(
                update(Job)
                .where(
                    Job.id == job_id, Job.status == JobStatus.QUEUED.value, Job.delete_requested.is_(False), eligible
                )
                .values(
                    status=JobStatus.CLAIMED.value,
                    attempts=Job.attempts + 1,
                    node_id=node_id,
                    lease_expires_at=lease_expires_at,
                )
                .returning(Job.attempts)
                .execution_options(synchronize_session=False)
            )
            if attempt_number is None:
                return None
            now = utcnow()
            node_name = (
                await session.scalar(select(NodeRow.display_name).where(NodeRow.id == node_id))
                if node_id is not None
                else "server"
            )
            attempt = JobAttempt(
                job_id=job_id,
                attempt_number=attempt_number,
                node_id=node_id,
                node_name=node_name,
                workload="healthcheck",
                status=JobStatus.CLAIMED.value,
                claimed_at=now,
            )
            session.add(attempt)
            await session.flush()
            session.add(JobEvent(job_id=job_id, attempt_id=attempt.id, type="claimed", payload={}, ts=now))
            claimed = (job_id, attempt.id)
            try:
                await session.commit()
            except asyncio.CancelledError:

                async def finish_claim() -> None:
                    await session.rollback()
                    await self._cancel_attempt(*claimed)

                await _drain(asyncio.create_task(finish_claim()))
                raise
            return claimed

    async def _publish_transition(self, job_id: int, operation: str) -> None:
        if self.events is None:
            return
        try:
            async with self.db() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                await publish_job_status(self.events, self.db, job.id)
        except Exception:
            LOG.exception("healthcheck event publication failed for job %s during %s", job_id, operation)

    async def run(self, job_id: int, attempt_id: int) -> None:
        self._started.add(attempt_id)
        cancelled = attempt_id in self._cancel_requested
        unavailable = False
        try:
            await _HealthcheckOperation(self).run(job_id, attempt_id, cancelled=cancelled)
        except OperationalError, InterfaceError:
            unavailable = True
            raise
        finally:
            self._started.discard(attempt_id)
            if not unavailable or self._closed:
                self._cancel_requested.discard(attempt_id)

    async def _cancel_attempt(self, job_id: int, attempt_id: int) -> None:
        if not self._closed:
            await self._finish_attempt(job_id, attempt_id, JobStatus.CANCELLED, "cancelled by operator")
            return
        async with self.db() as session:
            job = await lock_job_source(session, job_id)
            attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
            if (
                attempt is None
                or job is None
                or attempt.job_id != job_id
                or attempt.attempt_number != job.attempts
                or attempt.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
            ):
                return
            await snapshot_healthcheck_log(session, self.log_dir, attempt)
            if attempt.log_path is None:
                attempt.log_data = b"healthcheck: interrupted during shutdown\n"
            await requeue(session, job_id, now=utcnow())
            await session.commit()
        try:
            async with self.db() as session:
                await cleanup_healthcheck_log(session, self.log_dir, attempt_id)
                await session.commit()
        except Exception:
            LOG.exception("healthcheck attempt cleanup failed for job %s attempt %s", job_id, attempt_id)
        await self._publish_transition(job_id, "shutdown requeue")

    async def _finish_attempt(
        self, job_id: int, attempt_id: int, status: JobStatus, error: str, *, interrupted: bool = False
    ) -> None:
        async with self.db() as session:
            job = await lock_job_source(session, job_id)
            attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
            terminal = {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
            if (
                job is None
                or attempt is None
                or attempt.status in terminal
                or job.status in terminal
                or attempt.job_id != job_id
                or attempt.attempt_number != job.attempts
            ):
                return
            if attempt.log_path and attempt.log_data is None:
                suffix = f"healthcheck: {error}\n"
                if status is JobStatus.CANCELLED:
                    suffix = "healthcheck: cancelled by operator\nhealthcheck: result cancelled\n"
                try:
                    acquired = await session.scalar(
                        select(JobEvent).where(
                            JobEvent.attempt_id == attempt_id, JobEvent.type == "healthcheck_log_created"
                        )
                    )
                    if acquired is None:
                        raise ValueError("Healthcheck log creation record is missing.")  # noqa: TRY301
                    log = DiagnosticLog(path=healthcheck_log_path(self.log_dir, attempt.log_path))
                    await wait_workspace_io(lambda: _append_log(log, suffix, sync=True))
                    if not interrupted:
                        await snapshot_healthcheck_log(session, self.log_dir, attempt)
                except OSError, ValueError:
                    LOG.warning("healthcheck diagnostic log unavailable for job %s", job_id, exc_info=True)
                    if not interrupted:
                        await snapshot_healthcheck_log(session, self.log_dir, attempt)
            elif attempt.log_path is None:
                attempt.log_data = f"healthcheck: {error}\nhealthcheck: result {status.value}\n".encode()
            await finish(session, job_id, status, error=error, source_size=attempt.source_size)
            if interrupted:
                attempt.result = "interrupted"
                if attempt.log_path is not None:
                    session.add(
                        JobEvent(
                            job_id=job_id,
                            attempt_id=attempt_id,
                            type="healthcheck_log_unavailable",
                            payload={"log_path": attempt.log_path, "error": error},
                        )
                    )
                job.warning = "Healthcheck process exit is unconfirmed. Diagnostics retained."
            await session.commit()
        await self._cleanup_log(job_id, attempt_id)
        await self._publish_transition(job_id, "completion")

    async def _record_progress(self, job_id: int, attempt_id: int, progress: dict[str, object]) -> None:
        async with self.db() as session:
            updated = await session.scalar(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.kind == "healthcheck",
                    Job.status == JobStatus.RUNNING,
                    select(JobAttempt.id)
                    .where(
                        JobAttempt.id == attempt_id,
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt_number == Job.attempts,
                        JobAttempt.status == JobStatus.RUNNING,
                        JobAttempt.workload == "healthcheck",
                    )
                    .exists(),
                )
                .values(progress=progress)
                .returning(Job.updated_at)
            )
            if updated is None:
                return
            await session.commit()
        if self.events is not None:
            self.events.publish(
                "job.progress",
                {"job_id": job_id, "progress": progress, "updated_at": iso_utc(updated)},
                key=f"job.progress:{job_id}",
            )

    async def _cleanup_log(self, job_id: int, attempt_id: int) -> None:
        try:
            async with self.db() as session:
                await cleanup_healthcheck_log(session, self.log_dir, attempt_id)
                await session.commit()
        except Exception as exc:
            LOG.exception("healthcheck diagnostic cleanup failed for job %s", job_id)
            warning = f"healthcheck diagnostic cleanup failed: {exc}"
        else:
            return
        async with self.db() as session:
            job = await session.get(Job, job_id, with_for_update=True, populate_existing=True)
            if job is not None:
                job.warning = warning
                session.add(JobEvent(job_id=job_id, type="cleanup_failed", payload={"warning": warning}, ts=utcnow()))
                await session.commit()

    async def _create_log(self, session: AsyncSession, job_id: int, attempt: JobAttempt) -> DiagnosticLog:
        # Save the log path before creating the file so recovery can find it after a crash.
        attempt.log_path = f"{attempt.id}.log"
        await session.commit()
        log = await wait_workspace_io(lambda: create_healthcheck_log(self.log_dir, attempt.id))
        job = await lock_job_source(session, job_id)
        await session.get(JobAttempt, attempt.id, with_for_update=True, populate_existing=True)
        if job is None or job.status != JobStatus.CLAIMED or job.attempts != attempt.attempt_number:
            raise ValueError("Healthcheck attempt changed before the diagnostic log was created.")
        session.add(
            JobEvent(
                job_id=job_id,
                attempt_id=attempt.id,
                type="healthcheck_log_created",
                payload={"log_path": log.path.name},
            )
        )
        await session.commit()
        return log


class _HealthcheckOperation:
    def __init__(self, service: HealthcheckService) -> None:
        self.service = service
        self.hwaccel = service.hwaccel

    async def run(self, job_id: int, attempt_id: int, *, cancelled: bool) -> None:
        service = self.service
        try:
            if cancelled:
                await _drain(asyncio.create_task(service._cancel_attempt(job_id, attempt_id)))
            else:
                # Reporting must not block the dispatcher from filling other slots.
                await service._publish_transition(job_id, "claim")
                await self._run(job_id, attempt_id)
        except asyncio.CancelledError:
            await _drain(asyncio.create_task(service._cancel_attempt(job_id, attempt_id)))
            raise
        except HealthcheckExitError as exc:
            service._unreaped[attempt_id] = str(exc)
            await _drain(
                asyncio.create_task(
                    service._finish_attempt(job_id, attempt_id, JobStatus.FAILED, str(exc), interrupted=True)
                )
            )
            service._unreaped.pop(attempt_id, None)
        except OperationalError, InterfaceError:
            raise
        except Exception as exc:
            LOG.exception("healthcheck lifecycle failed for job %s", job_id)
            reason = f"healthcheck lifecycle failed ({type(exc).__name__}): {exc or type(exc).__name__}"
            await _drain(asyncio.create_task(service._finish_attempt(job_id, attempt_id, JobStatus.FAILED, reason)))
        if cancelled:
            raise asyncio.CancelledError

    async def _run(self, job_id: int, attempt_id: int) -> None:
        service = self.service
        async with service.db() as session:
            # Wait for the lock before cancellation can close the transaction.
            job = await _drain(asyncio.create_task(lock_job_source(session, job_id)))
            media = await _drain(asyncio.create_task(session.get(MediaFile, job.media_file_id))) if job else None
            attempt = await _drain(
                asyncio.create_task(session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True))
            )
            if (
                job is None
                or attempt is None
                or job.kind != "healthcheck"
                or job.status != JobStatus.CLAIMED
                or attempt.status != JobStatus.CLAIMED
                or attempt.job_id != job_id
                or attempt.attempt_number != job.attempts
            ):
                return
            if media is None:
                error = "healthcheck media lookup failed (MissingMedia): media file missing"
                await finish(session, job_id, JobStatus.FAILED, error=error)
                attempt.result = "interrupted"
                await session.commit()
                await service._publish_transition(job_id, "failed completion")
                return
            log = None
            try:
                log = await _drain(asyncio.create_task(service._create_log(session, job_id, attempt)))
                await session.refresh(job)
                await session.refresh(media)
                if job.status != JobStatus.CLAIMED or job.attempts != attempt.attempt_number:
                    return
                source_path = Path(media.path)
                snapshot = MediaSnapshot.from_media(media)
                library = await authorized_library(session, job.library_id, snapshot)
                if library is None:
                    raise ValueError("healthcheck source is outside the active library")  # noqa: TRY301
                library_version = library.updated_at
                await session.close()
                source_stat = await wait_workspace_io(source_path.stat)
                checked_source = source_identity(source_stat)
                source_size = checked_source["size"]
                # Reading source details must not hold catalog or job locks.
                current_job = await lock_job_source(session, job_id)
                current_attempt = await session.get(
                    JobAttempt, attempt_id, with_for_update=True, populate_existing=True
                )
                if current_job is None or current_attempt is None:
                    return
                job, attempt = current_job, current_attempt
                if (
                    job.status != JobStatus.CLAIMED
                    or job.attempts != attempt.attempt_number
                    or attempt.status != JobStatus.CLAIMED
                ):
                    return
                current_library = await session.scalar(
                    select(Library.id).where(
                        Library.id == library.id,
                        Library.updated_at == library_version,
                        Library.suspended.is_(False),
                        Library.deleted.is_(False),
                    )
                )
                if current_library is None or await claim_catalog(session, snapshot) is None:
                    raise ValueError("Healthcheck source changed while reading file details.")  # noqa: TRY301
            except OperationalError, InterfaceError:
                raise
            except Exception as exc:
                LOG.exception("healthcheck diagnostic/stat failed for job %s", job_id)
                current_job = await lock_job_source(session, job_id)
                current_attempt = await session.get(
                    JobAttempt, attempt_id, with_for_update=True, populate_existing=True
                )
                if current_job is None or current_attempt is None:
                    return
                job, attempt = current_job, current_attempt
                if (
                    job.status != JobStatus.CLAIMED
                    or job.attempts != attempt.attempt_number
                    or attempt.status != JobStatus.CLAIMED
                ):
                    return
                reason = f"healthcheck source stat failed ({type(exc).__name__}): {exc or type(exc).__name__}"
                await finish(session, job_id, JobStatus.FAILED, error=reason)
                if attempt.log_path is None:
                    attempt.log_data = reason.encode()
                else:
                    await snapshot_healthcheck_log(session, service.log_dir, attempt)
                await session.commit()
                if log is not None:
                    await service._cleanup_log(job_id, attempt_id)
                await service._publish_transition(job_id, "failed completion")
                return
            attempt_number = attempt.attempt_number
            indexed_size = media.size
            generation = catalog_generation(snapshot)
            started = attempt.started_at or utcnow()
            attempt.status, attempt.started_at, attempt.log_path, attempt.source_size = (
                JobStatus.RUNNING.value,
                started,
                log.path.name if log is not None else None,
                source_size,
            )
            job.status = JobStatus.RUNNING.value
            if job.started_at is None:
                job.started_at = started
            session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="started", payload={}, ts=started))
            await session.commit()
        await service._publish_transition(job_id, "running")
        try:

            async def progress(update: dict[str, object]) -> None:
                await service._record_progress(job_id, attempt_id, update)

            result = await _decode_healthcheck(
                str(source_path),
                service.ffmpeg_bin,
                service.ffprobe_bin,
                log,
                hwaccel=self.hwaccel,
                on_progress=progress,
            )
        except asyncio.CancelledError:
            raise
        except HealthcheckExitError:
            raise
        except OperationalError, InterfaceError:
            raise
        except Exception as exc:
            LOG.exception("healthcheck %s execution failed", job_id)
            reason = f"execution failed ({type(exc).__name__}): {exc or type(exc).__name__}"
            try:
                await wait_workspace_io(
                    lambda: _append_log(log, f"healthcheck: {reason}\nhealthcheck: result failed\n", sync=True)
                )
            except OSError:
                LOG.warning("healthcheck diagnostic log unavailable for job %s", job_id, exc_info=True)
            result = HealthcheckResult(healthy=False, error=reason, failed=True)
        log_data: bytes | None = None
        try:
            current_size = (await wait_workspace_io(source_path.stat)).st_size
        except OSError:
            current_size = None
        async with service.db() as session:
            # Shutdown must wait for the log snapshot's database connection to open.
            captured_job = await _drain(asyncio.create_task(lock_job_source(session, job_id)))
            attempt = await _drain(
                asyncio.create_task(session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True))
            )
            if captured_job is None or attempt is None or captured_job.attempts != attempt_number:
                return
            await snapshot_healthcheck_log(session, service.log_dir, attempt)
            log_data = attempt.log_data
            await session.commit()
            library = await authorized_library(session, job.library_id, snapshot)
        async with service.db() as session:
            job = await lock_job_source(session, job_id)
            attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
            if (
                job is None
                or attempt is None
                or job.status != JobStatus.RUNNING.value
                or job.attempts != attempt_number
                or attempt.status != JobStatus.RUNNING.value
                or attempt.job_id != job_id
                or attempt.attempt_number != job.attempts
            ):
                return
            unchanged = (
                current_size == source_size
                and library is not None
                and await session.scalar(
                    select(Library.id).where(
                        Library.id == library.id,
                        Library.updated_at == library.updated_at,
                        Library.suspended.is_(False),
                        Library.deleted.is_(False),
                    )
                )
                is not None
            )
            if unchanged:
                unchanged = await claim_catalog(session, snapshot) is not None
            if unchanged and result.probe is not None:
                assert library is not None
                media = await session.get(MediaFile, snapshot.id, populate_existing=True)
                assert media is not None
                await apply_probe_metadata(
                    session,
                    path=source_path,
                    stat=source_stat,
                    raw=result.probe,
                    row=media,
                    library=library,
                    purpose="healthcheck",
                )
            if unchanged and not result.failed:
                await session.execute(
                    update(MediaFile)
                    .where(MediaFile.id == snapshot.id)
                    .values(
                        health_status=HealthStatus.HEALTHY.value if result.healthy else HealthStatus.CORRUPT.value,
                        health_source={"path": str(source_path), **checked_source},
                    )
                )
                if result.healthy:
                    await release_transcode_decision(session, job, prior_health=snapshot.health_status)
            stale = not unchanged
            reevaluate = False
            if stale:
                result = HealthcheckResult(
                    healthy=False,
                    error="Source changed during healthcheck. Enqueue a new healthcheck.",
                    failed=True,
                )
                await session.execute(
                    update(MediaFile)
                    .where(generation)
                    .values(health_status=HealthStatus.PENDING.value, health_source=None)
                )
            status = JobStatus.FAILED if result.failed else JobStatus.SUCCEEDED
            health = (
                None
                if result.failed
                else {
                    "healthy": result.healthy,
                    "error_count": result.error_count,
                    "samples": result.samples or [],
                }
            )
            attempt.log_data = log_data
            await finish(session, job_id, status, error=result.error, source_size=attempt.source_size, health=health)
            if stale:
                media = await session.get(MediaFile, job.media_file_id)
                current_library = await session.get(Library, job.library_id)
                if (
                    media is not None
                    and current_library is not None
                    and not current_library.suspended
                    and processing_allowed(current_library, media)
                    and media.health_status == HealthStatus.PENDING
                    and media.probe_id is not None
                    and media.size != indexed_size
                ):
                    if current_library.flow_id is None or media.transcode_status == TranscodeStatus.TRANSCODED.value:
                        await enqueue_healthcheck_job(session, media.id, job.library_id)
                    else:
                        reevaluate = True
            await session.commit()
        await service._cleanup_log(job_id, attempt_id)
        await service._publish_transition(job_id, "terminal")
        if ((result.healthy and not result.failed) or reevaluate) and service._on_healthy is not None:
            try:
                service._on_healthy()
            except Exception:
                LOG.exception("healthcheck on_healthy callback failed for job %s", job_id)
