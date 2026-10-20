from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from common.enums import JobStatus
from common.process import drain
from sqlalchemy import Integer, any_, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import InterfaceError, OperationalError

from server.events import publish_file_status, publish_job_status
from server.flows.evaluation import evaluate_file
from server.fs.library import LibraryFS, ProbeResult
from server.fs.source import MediaSnapshot, authorized_library, lock_job_source
from server.models import Flow, Job, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.nodehub.hub import stored_probe
from server.plugins.migrations import migrate_execution
from server.plugins.store_migrations import log_migrations, migrate_job
from server.queue.store import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    StaleJobError,
    apply_decision,
    capture_source,
    current_request,
    finish,
    record_request_outcome,
    request_file_job,
    restore_retry_result,
    retry_job,
)
from server.retention import DELETABLE, delete_eligible_job
from server.serialize import iso_utc
from server.workspace import post_action_jobs, recovery_jobs

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.events import EventBus
    from server.healthcheck import HealthcheckService
    from server.nodehub.hub import NodeHub
    from server.plugins.host import PluginHost
    from server.plugins.migrations import AppliedMigration
    from server.probing import ProbeService
    from server.staging import StagingService

LOG = logging.getLogger(__name__)
REQUEST_BATCH = 100


class JobRequestError(ValueError):
    def __init__(self, detail: str, *, status: int = 409) -> None:
        super().__init__(detail)
        self.status = status


class JobService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        staging: StagingService,
        healthchecks: HealthcheckService,
        probes: ProbeService,
        hub: NodeHub,
        host: PluginHost,
        events: EventBus,
        ffprobe_bin: str | Path,
        mkvpropedit_bin: str | Path,
        workspace_dir: Path,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.staging = staging
        self.healthchecks = healthchecks
        self.probes = probes
        self.hub = hub
        self.host = host
        self.events = events
        self.ffprobe_bin = ffprobe_bin
        self.mkvpropedit_bin = mkvpropedit_bin
        self.workspace_dir = workspace_dir
        self._closed = False
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._pending_results: dict[int, tuple[int, str, str]] = {}
        self._draining: set[int] = set()
        self._closing: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._closing is not None:
            await drain(self._closing)
        self._closing = None
        self._closed = False
        self._draining.clear()
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._run(), name="job-requests")
            self._loop.add_done_callback(self._loop_finished)
        self._wake.set()

    def _loop_finished(self, task: asyncio.Task[None]) -> None:
        if not self._closed:
            self._closed = True
            exc = None if task.cancelled() else task.exception()
            LOG.error("Job request processing stopped unexpectedly", exc_info=exc)

    async def files(
        self,
        ids: list[int],
        *,
        kind: Literal["transcode", "healthcheck"],
        refresh: bool = False,
        selected_flow: tuple[int, int] | None = None,
    ) -> dict:
        return await drain(
            asyncio.create_task(self._files(ids, kind=kind, refresh=refresh, selected_flow=selected_flow))
        )

    async def _files(self, ids: list[int], *, kind: str, refresh: bool, selected_flow: tuple[int, int] | None) -> dict:
        accepted, failed = [], []
        jobs = []
        async with self.sessionmaker() as session:
            if self._closed:
                raise JobRequestError("Job service is stopped.")
            identities = list(
                await session.execute(select(MediaFile.library_id, MediaFile.id).where(MediaFile.id.in_(ids)))
            )
            for library_id in sorted({row.library_id for row in identities}):
                await session.get(Library, library_id, with_for_update={"read": True}, populate_existing=True)
            for file_id in sorted(row.id for row in identities):
                await session.get(MediaFile, file_id, with_for_update=True, populate_existing=True)
            for file_id in dict.fromkeys(ids):
                media = await session.get(MediaFile, file_id)
                if media is None:
                    failed.append({"file_id": file_id, "status": 404, "detail": "file not found"})
                    continue
                try:
                    library = await self._eligible(session, media, kind=kind)
                    flow_snapshot = None
                    if kind == "transcode":
                        if selected_flow is not None:
                            flow = await session.get(Flow, selected_flow[0], with_for_update={"read": True})
                            if flow is None:
                                raise JobRequestError("Flow not found.", status=404)  # noqa: TRY301 -- Keep per-file rejection in the batch.
                            if flow.version != selected_flow[1]:
                                raise JobRequestError("Flow changed. Reload it before transcoding.")  # noqa: TRY301 -- Keep per-file rejection in the batch.
                            flow_snapshot = {
                                "id": flow.id,
                                "name": flow.name,
                                "version": flow.version,
                                "definition": json.loads(flow.definition),
                            }
                        elif library.flow_id is None:
                            raise JobRequestError("library has no flow", status=422)  # noqa: TRY301 -- Keep per-file rejection in the batch.
                        elif await session.get(Flow, library.flow_id) is None:
                            raise JobRequestError("library flow not found", status=422)  # noqa: TRY301 -- Keep per-file rejection in the batch.
                except JobRequestError as exc:
                    failed.append({"file_id": file_id, "status": exc.status, "detail": str(exc)})
                    continue
                job = await request_file_job(session, media, kind=kind, refresh=refresh, flow_snapshot=flow_snapshot)
                await session.flush()
                accepted.append({"file_id": file_id, "job_id": job.id, "kind": kind, "status": job.status})
                jobs.append(job)
            await session.commit()
        self._announce(jobs)
        if kind == "healthcheck" and accepted:
            self.healthchecks.wake()
        return {"accepted": accepted, "failed": failed}

    async def retry(self, ids: list[int]) -> dict:
        return await drain(asyncio.create_task(self._retry(ids)))

    async def _retry(self, ids: list[int]) -> dict:
        accepted, failed = [], []
        jobs = []
        applied: list[AppliedMigration] = []
        manifests = [info.manifest for info in self.host.plugins()]
        async with self.sessionmaker() as session:
            if self._closed:
                raise JobRequestError("Job service is stopped.")
            identities = list(
                await session.execute(
                    select(Job.library_id, Job.media_file_id).where(Job.id == any_(literal(ids, ARRAY(Integer))))
                )
            )
            for library_id in sorted({row.library_id for row in identities}):
                await session.get(Library, library_id, with_for_update={"read": True}, populate_existing=True)
            for file_id in sorted({row.media_file_id for row in identities}):
                await session.get(MediaFile, file_id, with_for_update=True, populate_existing=True)
            for job_id in sorted(set(ids)):
                await session.get(Job, job_id, with_for_update=True, populate_existing=True)
            for job_id in dict.fromkeys(ids):
                job = await session.get(Job, job_id)
                if job is None:
                    failed.append({"id": job_id, "detail": "job not found"})
                    continue
                if job.delete_requested:
                    failed.append({"id": job_id, "detail": "Job deletion is already requested."})
                    continue
                if job.status not in TERMINAL_STATUSES or job.status == JobStatus.SKIPPED:
                    detail = (
                        "skipped jobs cannot be retried"
                        if job.status == JobStatus.SKIPPED
                        else "only finished jobs can be retried"
                    )
                    failed.append({"id": job_id, "detail": detail})
                    continue
                media = await session.get(MediaFile, job.media_file_id)
                if media is None or media.library_id != job.library_id:
                    failed.append({"id": job_id, "detail": "Source is missing."})
                    continue
                try:
                    await self._eligible(session, media, kind=job.kind, retry=job)
                    changes: list[AppliedMigration] = []
                    migrate_job(job, manifests, applied=changes)
                    requested = await session.scalar(
                        select(JobEvent)
                        .where(JobEvent.job_id == job_id, JobEvent.type == "requested")
                        .order_by(JobEvent.id.desc())
                        .limit(1)
                    )
                    selection = (
                        migrate_execution(
                            {"flow_snapshot": requested.payload["flow_snapshot"]},
                            manifests,
                            applied=changes,
                            record=f"job {job_id} request",
                        )
                        if requested is not None and "flow_snapshot" in requested.payload
                        else {}
                    )
                except ValueError as exc:
                    failed.append({"id": job_id, "detail": str(exc)})
                    continue
                applied.extend(changes)
                previous = {
                    key: getattr(job, key)
                    for key in ("status", "error", "warning", "stderr_tail", "progress", "node_id")
                }
                previous.update(
                    {
                        key: iso_utc(getattr(job, key))
                        for key in ("started_at", "finished_at", "lease_expires_at", "hold_until")
                    }
                )
                session.add(
                    JobEvent(
                        job_id=job_id,
                        type="requested",
                        payload={"operation": "retry", "previous": previous, **selection},
                    )
                )
                job.status = JobStatus.PENDING
                job.error = job.warning = job.stderr_tail = job.progress = None
                job.node_id = job.started_at = job.finished_at = job.lease_expires_at = job.hold_until = None
                await session.flush()
                accepted.append(job_id)
                jobs.append(job)
            await session.commit()
        log_migrations(applied)
        self._announce(jobs)
        return {"accepted": accepted, "failed": failed}

    async def delete(self, ids: list[int]) -> dict:
        return await drain(asyncio.create_task(self._request_delete(ids)))

    async def _request_delete(self, ids: list[int]) -> dict:
        ids = list(dict.fromkeys(ids))
        accepted, failed = [], []
        async with self.sessionmaker() as session:
            if self._closed:
                raise JobRequestError("Job service is stopped.")
            rows = {
                job_id: (status, requested, library_id)
                for job_id, status, requested, library_id in await session.execute(
                    select(Job.id, Job.status, Job.delete_requested, Job.library_id)
                    .where(Job.id == any_(literal(ids, ARRAY(Integer))))
                    .order_by(Job.id)
                    .with_for_update()
                )
            }
            for job_id in ids:
                if job_id not in rows:
                    failed.append({"id": job_id, "detail": "job not found"})
                elif rows[job_id][1]:
                    failed.append({"id": job_id, "detail": "Job deletion is already requested."})
                elif rows[job_id][2] in self._draining:
                    failed.append({"id": job_id, "detail": "Library deletion is in progress."})
                elif rows[job_id][0] not in DELETABLE:
                    failed.append({"id": job_id, "detail": "Active jobs cannot be deleted."})
                else:
                    accepted.append(job_id)
            if accepted:
                await session.execute(update(Job).where(Job.id.in_(accepted)).values(delete_requested=True))
                await session.execute(
                    insert(JobEvent), [{"job_id": job_id, "type": "delete_requested"} for job_id in accepted]
                )
            await session.commit()
        self._wake.set()
        return {"accepted": accepted, "failed": failed}

    async def _delete(self, job_id: int) -> None:
        # Commit and announcement finish together even if shutdown cancels this worker.
        await drain(asyncio.create_task(self._delete_job(job_id)))

    async def _delete_job(self, job_id: int) -> None:
        try:
            async with self.sessionmaker() as session:
                job = await lock_job_source(session, job_id)
                if job is None or not job.delete_requested:
                    return
                request_id = await session.scalar(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id, JobEvent.type == "delete_requested")
                    .order_by(JobEvent.id.desc())
                    .limit(1)
                )
                file_id = job.media_file_id
                if not await delete_eligible_job(session, job_id):
                    raise JobRequestError(  # noqa: TRY301 -- Persist the worker's rejection after rollback.
                        "Active jobs, recovery records, and pending notifications cannot be deleted."
                    )
                await session.commit()
            self.events.publish("job.deleted", {"job_id": job_id}, key=f"job.deleted:{job_id}")
            self.events.publish(
                "job.deletion",
                {"job_id": job_id, "outcome": "deleted", "reason": "Job deleted."},
                key=f"job.deletion:{job_id}:{request_id}",
            )
            if file_id is not None:
                await publish_file_status(self.events, self.sessionmaker, file_id)
        except OperationalError, InterfaceError:
            # Deletion only changes database rows. Keep its saved request pending.
            raise
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            LOG.error(
                "Job %s deletion failed",
                job_id,
                extra={"error": reason},
                exc_info=not isinstance(exc, JobRequestError),
            )
            async with self.sessionmaker() as session:
                job = await lock_job_source(session, job_id)
                if job is None or not job.delete_requested:
                    return
                job.delete_requested = False
                event = JobEvent(job_id=job_id, type="deletion", payload={"outcome": "failed", "reason": reason})
                session.add(event)
                await session.commit()
            self.events.publish(
                "job.deletion",
                {"job_id": job_id, "outcome": "failed", "reason": reason},
                key=f"job.deletion:{job_id}:{event.id}",
            )
            self.healthchecks.wake()
            self.staging.wake()
            self.hub.wake()

    def _announce(self, jobs: list[Job]) -> None:
        for job in jobs:
            self.events.publish(
                "job.status",
                {
                    "job_id": job.id,
                    "media_file_id": job.media_file_id,
                    "kind": job.kind,
                    "status": job.status,
                    "updated_at": iso_utc(job.updated_at),
                },
            )
            self.events.publish("job.requested", {"job_id": job.id}, key=f"job.requested:{job.id}")
        self._wake.set()

    async def _eligible(
        self, session: AsyncSession, media: MediaFile, *, kind: str, retry: Job | None = None
    ) -> Library:
        library = await session.get(Library, media.library_id, populate_existing=True)
        if library is None:
            raise JobRequestError("library not found", status=404)
        if library.suspended or library.deleted or library.id in self._draining:
            raise JobRequestError("Library is unavailable or suspended.")
        busy = self.staging.active_jobs | self.staging.busy_jobs
        if await session.scalar(select(Job.id).where(Job.media_file_id == media.id, Job.id.in_(busy)).limit(1)):
            raise JobRequestError("Media file still has active work.")
        actions = set(await session.scalars(post_action_jobs(scoped=True).where(Job.media_file_id == media.id)))
        # Historical actions can finish after this retry's exclusive workspace lock is acquired.
        pruneable = {retry.id} if retry is not None and retry.kind == "transcode" else set()
        if actions - pruneable:
            raise JobRequestError("Media file has unfinished post-actions. Finish them before requesting work.")
        # A healthcheck covers the publication check and clears its pending marker.
        if await session.scalar(
            recovery_jobs(include_actions=False, include_artifact_checks=kind != "healthcheck", scoped=True)
            .where(Job.media_file_id == media.id)
            .limit(1)
        ):
            raise JobRequestError("Media file has unresolved recovery. Complete recovery before requesting work.")
        if await session.scalar(
            select(Job.id).where(Job.media_file_id == media.id, Job.status.in_(ACTIVE_STATUSES)).limit(1)
        ):
            raise JobRequestError("media file already has an active job")
        return library

    async def _run(self) -> None:
        while not self._closed:
            self._wake.clear()
            try:
                for job_id, (request_id, outcome, reason) in tuple(self._pending_results.items()):
                    task = self._tasks.get(job_id)
                    if task is not None:
                        # Request completion cannot overtake cancelled filesystem preparation.
                        results = await asyncio.shield(asyncio.gather(task, return_exceptions=True))
                        for result in results:
                            if isinstance(result, Exception):
                                raise result
                    await self._finish_request(job_id, request_id, outcome, reason)
                await self._scan()
            except OperationalError, InterfaceError:
                LOG.exception("Database operation failed while processing job requests. Retrying in 1 second.")
                await asyncio.sleep(1)
                continue
            if not self._wake.is_set():
                await self._wake.wait()

    async def _scan(self) -> None:
        eligible = or_(Job.status == JobStatus.PENDING, Job.delete_requested.is_(True))
        after = 0
        upper = None
        while not self._closed:
            async with self.sessionmaker() as session:
                # Wait for the database connection to open before cancellation closes the session.
                await drain(asyncio.create_task(session.connection()))
                if upper is None:
                    upper = await session.scalar(select(func.max(Job.id)).where(eligible)) or 0
                rows = list(
                    await session.execute(
                        select(Job.id, Job.library_id)
                        .where(eligible, Job.id > after, Job.id <= upper)
                        .order_by(Job.id)
                        .limit(REQUEST_BATCH)
                    )
                )
            if not rows:
                return
            after = rows[-1].id
            for job_id, library_id in rows:
                if self._closed:
                    return
                if library_id in self._draining:
                    continue
                task = self._schedule(job_id)
                # A child cancelled before its first step must not cancel the service loop.
                results = await asyncio.shield(asyncio.gather(task, return_exceptions=True))
                for result in results:
                    if isinstance(result, Exception):
                        raise result

    def _schedule(self, job_id: int) -> asyncio.Task[None]:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            task = asyncio.create_task(self._process(job_id), name=f"job-requests:{job_id}")
            self._tasks[job_id] = task
            task.add_done_callback(lambda done: self._finished(job_id, done))
        return task

    def _finished(self, job_id: int, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Job %s request completion failed", job_id, exc_info=task.exception())
            self._wake.set()

    async def _process(self, job_id: int) -> None:
        request_id = None
        preparing = False
        try:
            async with self.sessionmaker() as session:
                await drain(asyncio.create_task(session.connection()))
                job = await session.get(Job, job_id)
                if job is not None and job.delete_requested:
                    await session.close()
                    await self._delete(job_id)
                    return
                if job is None or job.status != JobStatus.PENDING or job.library_id in self._draining:
                    return
                event = await session.scalar(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id, JobEvent.type == "requested")
                    .order_by(JobEvent.id.desc())
                    .limit(1)
                )
                assert event is not None
                request_id = event.id
            if event.payload.get("cancelled"):
                await self._finish_request(job_id, request_id, "cancelled", "Job request cancelled.")
                return
            if job.waiting_metadata:
                async with self.sessionmaker() as session:
                    current = await lock_job_source(session, job.id)
                    await current_request(session, job.id, event.id)
                    prerequisite = await session.get(Job, event.payload["metadata_job_id"])
                    media = await session.get(MediaFile, job.media_file_id)
                    if prerequisite is not None and prerequisite.status in ACTIVE_STATUSES:
                        return
                    if prerequisite is not None and prerequisite.status == JobStatus.SUCCEEDED and media is not None:
                        if media.probe_id is None:
                            self.probes.wake()
                            self.hub.wake()
                            return
                        assert current is not None
                        current.waiting_metadata = False
                        current.warning = None
                        await session.commit()
                    else:
                        reason = prerequisite.error if prerequisite is not None else "Metadata job was removed."
                        await session.close()
                        await self._finish_request(
                            job.id,
                            event.id,
                            "failed",
                            f"MKV statistics did not complete. {reason or 'The metadata job was cancelled.'}",
                        )
                        return
            if event.payload["operation"] == "retry":
                if job.kind == "transcode":
                    preparing = True
                    cleanup = await self.staging.prune_job(job_id, request_id=request_id)
                    if cleanup["outcome"] == "error":
                        raise ValueError(cleanup["reason"])  # noqa: TRY301 -- Record cleanup failure as a visible outcome.
                try:
                    async with self.sessionmaker() as session:
                        await drain(asyncio.create_task(session.connection()))
                        # A lost commit acknowledgement still needs the recorded outcome's wake.
                        preparing = True
                        retried = await retry_job(session, job_id, request_id=request_id)
                        if retried is None:
                            raise ValueError("Job request was cancelled, replaced, or is no longer pending.")
                        await session.commit()
                except StaleJobError:
                    preparing = True
                    await self._evaluate(job, event, refresh=True)
                    return
                await self._finish_request(job_id, request_id, "queued", "Job requeued.")
            else:
                preparing = True
                await self._evaluate(job, event, refresh=event.payload["refresh"])
        except asyncio.CancelledError:
            # Shutdown leaves uncommitted preparation pending. Explicit cancellation
            # owns a separate drainer, including cancellation before the first step.
            if request_id is not None:
                await drain(asyncio.create_task(self._publish_committed(job_id, request_id)))
        except (OperationalError, InterfaceError) as exc:
            if preparing and request_id is not None and job_id not in self._pending_results:
                # Preparation may already have touched storage. Retry recording the result, not filesystem changes.
                reason = f"Job preparation interrupted by database outage: {exc}"
                self._pending_results[job_id] = (request_id, "failed", reason)
            raise
        except Exception as exc:
            LOG.exception("Job %s request failed", job_id)
            if request_id is not None:
                await drain(
                    asyncio.create_task(
                        self._finish_request(job_id, request_id, "failed", str(exc) or type(exc).__name__)
                    )
                )
            else:
                raise

    async def _evaluate(self, job: Job, event: JobEvent, *, refresh: bool) -> None:
        async with self.sessionmaker() as session:
            await drain(asyncio.create_task(session.connection()))
            await current_request(session, job.id, event.id)
            media = await session.get(MediaFile, job.media_file_id)
            if media is None:
                raise ValueError("Source is missing.")
            library = await authorized_library(session, job.library_id, media)
            if library is None:
                raise ValueError("Library no longer authorizes this source.")
            stored = await session.get(ProbeRow, media.probe_id) if media.probe_id is not None and not refresh else None
            probe = stored_probe(media, stored)
        source = {}
        if probe is not None:
            try:
                source = await capture_source(media)
                refresh |= source["path"] != media.path or source["size"] != media.size
            except ValueError:
                refresh = True
        if refresh or probe is None:
            snapshot = await LibraryFS(
                library.id,
                sessionmaker=self.sessionmaker,
                ffprobe_bin=self.ffprobe_bin,
                mkvpropedit_bin=self.mkvpropedit_bin,
                workspace_dir=self.workspace_dir,
            ).refresh_current(job.media_file_id, job_id=job.id, request_id=event.id)
        else:
            snapshot = ProbeResult(
                media=MediaSnapshot.from_media(media), probe=probe, source=source, healthcheck_id=None, diagnostics=()
            )
        if snapshot.media.probe_id is None:
            from server.metadata_jobs import enqueue_metadata_job

            async with self.sessionmaker() as session:
                current = await lock_job_source(session, job.id)
                requested = await current_request(session, job.id, event.id)
                row = await session.get(MediaFile, job.media_file_id)
                library = await session.get(Library, job.library_id)
                assert current is not None
                assert row is not None
                assert library is not None
                current.waiting_metadata = True
                current.warning = "Waiting for MKV statistics."
                await session.flush()
                prerequisite = await enqueue_metadata_job(
                    session, row, library, kind="mkv_stats", priority=current.priority
                )
                requested.payload = {**requested.payload, "metadata_job_id": prerequisite.id}
                await session.commit()
            await publish_job_status(self.events, self.sessionmaker, job.id)
            self.probes.wake()
            self.hub.wake()
            return
        async with self.sessionmaker() as session:
            await drain(asyncio.create_task(session.connection()))
            await current_request(session, job.id, event.id)
            library = await session.get(Library, job.library_id)
            selected = event.payload.get("flow_snapshot")
            if selected is not None:
                flow = Flow(
                    id=selected["id"],
                    name=selected["name"],
                    version=selected["version"],
                    definition=json.dumps(selected["definition"]),
                )
            else:
                # Library-default requests use the current binding after probing.
                flow = (
                    await session.get(Flow, library.flow_id)
                    if library is not None and library.flow_id is not None
                    else None
                )
            if library is None or flow is None:
                raise ValueError("Library has no current flow.")
        result, flow_snapshot = await evaluate_file(snapshot.media.path, snapshot.probe, library, flow, self.host)
        decision = await apply_decision(
            self.sessionmaker,
            snapshot=snapshot,
            evaluation=result,
            flow_snapshot=flow_snapshot,
            job_id=job.id,
            request_id=event.id,
        )
        if decision.healthcheck_id is not None:
            self.healthchecks.wake()
            await publish_job_status(self.events, self.sessionmaker, decision.healthcheck_id)
        await self._publish_committed(job.id, event.id)

    async def _finish_request(self, job_id: int, request_id: int, outcome: str, reason: str) -> None:
        self._pending_results[job_id] = (request_id, outcome, reason)
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id)
            event = await session.get(JobEvent, request_id, populate_existing=True)
            if job is None or event is None:
                self._pending_results.pop(job_id, None)
                return
            latest = await session.scalar(
                select(JobEvent.id)
                .where(JobEvent.job_id == job_id, JobEvent.type == "requested")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            if latest != request_id:
                self._pending_results.pop(job_id, None)
                return
            retry = event.payload["operation"] == "retry"
            recorded = await session.scalar(
                select(JobEvent)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.type == ("requeue" if retry else "request_result"),
                    JobEvent.payload["request_id"].as_integer() == request_id,
                )
                .limit(1)
            )
            if recorded is not None:
                payload = {key: value for key, value in recorded.payload.items() if key != "request_id"}
            else:
                if event.payload.get("cancelled"):
                    outcome, reason = "cancelled", "Job request cancelled."
                if job.status == JobStatus.PENDING:
                    if retry:
                        if outcome == "cancelled":
                            job.status = JobStatus.CANCELLED
                            job.finished_at = utcnow()
                        else:
                            restore_retry_result(job, event)
                    else:
                        # Preparation can fail because the catalog was updated in the meantime.
                        # Finish the request without changing the newer file's retained result.
                        await finish(
                            session,
                            job.id,
                            JobStatus.CANCELLED if outcome == "cancelled" else JobStatus.FAILED,
                            error=reason if outcome == "failed" else None,
                            update_result=False,
                        )
                    job.waiting_metadata = False
                payload = record_request_outcome(session, job, event, outcome=outcome, reason=reason)
                if outcome == "cancelled" and job.kind in {"probe", "mkv_stats"}:
                    from server.metadata_jobs import cancel_metadata

                    await cancel_metadata(session, job)
            await session.commit()
        await publish_job_status(self.events, self.sessionmaker, job_id)
        topic = "job.requeue" if retry else "job.request_result"
        self.events.publish(topic, payload, key=f"{topic}:{job_id}")
        self.probes.wake()
        if payload["outcome"] == "queued":
            if job.kind == "healthcheck":
                self.healthchecks.wake()
            elif job.kind == "transcode":
                self.staging.wake()
                self.hub.wake()
        self._pending_results.pop(job_id, None)

    async def _publish_committed(self, job_id: int, request_id: int) -> None:
        async with self.sessionmaker() as session:
            await drain(asyncio.create_task(session.connection()))
            outcome = await session.scalar(
                select(JobEvent)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.type.in_(["requeue", "request_result"]),
                    JobEvent.payload["request_id"].as_integer() == request_id,
                )
                .limit(1)
            )
        if outcome is not None:
            await self._finish_request(job_id, request_id, outcome.payload["outcome"], outcome.payload["reason"])

    async def cancel(self, job_id: int) -> bool:
        return await drain(asyncio.create_task(self._cancel(job_id)))

    async def _cancel(self, job_id: int) -> bool:
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id)
            if job is None or job.status != JobStatus.PENDING:
                return False
            event = await session.scalar(
                select(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.type == "requested")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            assert event is not None
            if event.payload.get("cancelled"):
                return True
            event.payload = {**event.payload, "cancelled": True}
            await session.commit()
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
        finishing = asyncio.create_task(self._cancel_and_drain(job_id, event.id, task))
        self._tasks[job_id] = finishing
        finishing.add_done_callback(lambda done: self._finished(job_id, done))
        self._wake.set()
        return True

    async def _cancel_and_drain(self, job_id: int, request_id: int, task: asyncio.Task[None] | None) -> None:
        if task is not None:
            await drain(asyncio.gather(task, return_exceptions=True))
        await drain(
            asyncio.create_task(self._finish_request(job_id, request_id, "cancelled", "Job request cancelled."))
        )

    async def drain_library(self, library_id: int) -> None:
        self._draining.add(library_id)
        async with self.sessionmaker() as session:
            await drain(asyncio.create_task(session.connection()))
            ids = list(
                await session.scalars(
                    select(Job.id).where(
                        Job.library_id == library_id,
                        or_(Job.status == JobStatus.PENDING, Job.delete_requested.is_(True)),
                    )
                )
            )
        for job_id in ids:
            if not await self.cancel(job_id):
                self._schedule(job_id)
        tasks = [self._tasks[job_id] for job_id in ids if job_id in self._tasks]
        if tasks:
            await drain(asyncio.gather(*tasks))

    async def close(self) -> None:
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await drain(self._closing)

    async def _close(self) -> None:
        tasks = [*self._tasks.values(), *([self._loop] if self._loop is not None else [])]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop = None
