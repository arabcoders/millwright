from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from common.process import drain
from sqlalchemy import delete, exists, or_, select, update

from server.models import FinalizationJournal, Job, JobAttempt, Library, MediaFile, ProbeRow
from server.queue.store import ACTIVE_STATUSES, cancel_pending
from server.retention import delete_job_graph
from server.workspace import FINISHED_FINALIZATION_STATES, post_action_jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.events import EventBus
    from server.healthcheck import HealthcheckService
    from server.jobs import JobService
    from server.nodehub.hub import NodeHub
    from server.probing import ProbeService
    from server.scanner.service import ScannerService
    from server.staging import StagingService

LOG = logging.getLogger(__name__)
PENDING = ("hold", "staging", "queued")


class LibraryDeletionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        scanner: ScannerService,
        probes: ProbeService,
        hub: NodeHub,
        healthchecks: HealthcheckService,
        staging: StagingService,
        events: EventBus | None,
        reset_lock: asyncio.Lock,
        jobs: JobService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.scanner = scanner
        self.probes = probes
        self.hub = hub
        self.healthchecks = healthchecks
        self.staging = staging
        self.events = events
        self.reset_lock = reset_lock
        self.jobs = jobs
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._closed = True
        self._closing: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closing is not None:
            await drain(self._closing)
        self._closing = None
        self._closed = False
        async with self.sessionmaker() as session:
            # Reset restarts this service while it already owns reset_lock.
            libraries = list(
                await session.scalars(
                    select(Library).where(Library.deleted.is_(True)).order_by(Library.id).with_for_update(of=Library)
                )
            )
            for library in libraries:
                library.suspended = True
                library.delete_error = None
            await session.commit()
        for library in libraries:
            self._publish(library, "deleting")
            self._schedule(library.id)

    async def request(self, library_id: int) -> dict:
        # A disconnected request must not leave a committed mark without its worker.
        return await drain(asyncio.create_task(self._request(library_id)))

    async def _request(self, library_id: int) -> dict:
        async with self.reset_lock, self.sessionmaker() as session:
            if self._closed:
                raise ValueError("Library deletion is unavailable while services are stopped.")
            library = await session.get(Library, library_id, populate_existing=True, with_for_update=True)
            if library is None:
                raise FileNotFoundError("Library not found.")
            library.deleted = True
            library.suspended = True
            library.delete_error = None
            await session.commit()
            self._publish(library, "deleting")
            self._schedule(library_id)
        return {"library_id": library_id, "status": "deleting"}

    def _schedule(self, library_id: int) -> None:
        current = self._tasks.get(library_id)
        if self._closed or (current is not None and not current.done()):
            return
        task = asyncio.create_task(self._delete(library_id), name=f"library-deletion:{library_id}")
        self._tasks[library_id] = task
        task.add_done_callback(lambda finished: self._finished(library_id, finished))

    def _finished(self, library_id: int, task: asyncio.Task[None]) -> None:
        if self._tasks.get(library_id) is task:
            self._tasks.pop(library_id)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Library %s deletion cleanup failed", library_id, exc_info=task.exception())

    async def close(self) -> None:
        self._closed = True
        if self._closing is None:
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
            self._closing = asyncio.create_task(self._close(tasks))
        await drain(self._closing)

    async def _close(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _delete(self, library_id: int) -> None:
        try:
            operations = (
                self.scanner.drain_library(library_id),
                self.jobs.drain_library(library_id),
                self.probes.check_library_access(),
                self.staging.wait_idle(),
                self.healthchecks.drain_library(library_id),
                self.hub.drain_library(library_id),
            )
            results = await drain(asyncio.gather(*operations, return_exceptions=True))
            for result in results:
                if isinstance(result, BaseException):
                    raise result  # noqa: TRY301 -- Persist drain failures through the outer handler.
            # Staging takes its local lock before database locks. Waiting for unrelated copies
            # must never hold the reset lock that blocks new mutations.
            async with self.staging._stage_lock, self.reset_lock, self.sessionmaker() as session:
                if self._closed:
                    return
                library = await session.get(Library, library_id, populate_existing=True, with_for_update=True)
                if library is None or not library.deleted:
                    return
                await session.execute(
                    select(MediaFile.id)
                    .where(MediaFile.library_id == library_id)
                    .order_by(MediaFile.id)
                    .with_for_update(of=MediaFile)
                )
                await session.execute(
                    select(Job.id).where(Job.library_id == library_id).order_by(Job.id).with_for_update(of=Job)
                )
                active = await session.scalar(
                    select(Job.id)
                    .where(
                        Job.library_id == library_id,
                        Job.status.in_([status.value for status in ACTIVE_STATUSES]),
                        Job.status.not_in(PENDING),
                    )
                    .limit(1)
                )
                journals = (
                    select(JobAttempt.job_id)
                    .join(FinalizationJournal, FinalizationJournal.attempt_id == JobAttempt.id)
                    .where(FinalizationJournal.state.not_in(FINISHED_FINALIZATION_STATES))
                )
                recovery = await session.scalar(
                    select(Job.id)
                    .where(Job.library_id == library_id, or_(Job.id.in_(journals), Job.id.in_(post_action_jobs())))
                    .limit(1)
                )
                if active is not None or recovery is not None:
                    error = f"Job {active if active is not None else recovery} still owns work or publication recovery. Library rows were retained."
                    raise ValueError(error)  # noqa: TRY301 -- Persist failed eligibility without removing rows.
                # Classify only after discovery, probes, staging, and completion
                # handlers have drained. Retried jobs retain their attempt history.
                rows = list(
                    await session.execute(
                        select(
                            Job.id,
                            Job.attempts,
                            exists(select(JobAttempt.id).where(JobAttempt.job_id == Job.id)).label("has_attempt"),
                        )
                        .where(Job.library_id == library_id, Job.status.in_(PENDING))
                        .order_by(Job.id)
                    )
                )
                removed = []
                for job_id, attempts, has_attempt in rows:
                    if attempts or has_attempt:
                        await cancel_pending(session, job_id)
                    else:
                        await delete_job_graph(session, job_id)
                        removed.append(job_id)
                media_ids = select(MediaFile.id).where(MediaFile.library_id == library_id)
                await session.execute(update(MediaFile).where(MediaFile.library_id == library_id).values(probe_id=None))
                await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id.in_(media_ids)))
                deleted_files = list(
                    await session.scalars(
                        delete(MediaFile).where(MediaFile.library_id == library_id).returning(MediaFile.id)
                    )
                )
                await session.delete(library)
                await session.commit()
            if self.events is not None:
                self.events.library_progress.pop(library_id, None)
                for job_id in removed:
                    self.events.publish("job.deleted", {"job_id": job_id}, key=f"job.deleted:{job_id}")
                for file_id in deleted_files:
                    self.events.publish(
                        "file.status",
                        {"action": "deleted", "media_file_id": file_id, "library_id": library_id},
                        key=f"file.status:file:{file_id}",
                    )
            self._publish(library, "deleted")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            LOG.exception("Library %s deletion failed", library_id, extra={"error": error})
            async with self.reset_lock, self.sessionmaker() as session:
                library = await session.get(Library, library_id, populate_existing=True, with_for_update=True)
                if library is not None and library.deleted:
                    library.delete_error = error
                    library.suspended = True
                    await session.commit()
                    self._publish(library, "failed")

    def _publish(self, library: Library, status: str) -> None:
        if self.events is not None:
            self.events.publish(
                "library.deletion",
                {
                    "library_id": library.id,
                    "library_name": library.name,
                    "status": status,
                    "error": library.delete_error,
                },
                key=f"library.deletion:{library.id}",
            )
