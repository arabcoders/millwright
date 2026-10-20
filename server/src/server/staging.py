from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING

from common.enums import JobStatus
from common.fs.acquire import directory_fd
from common.media.probe import ProbeData, mkv_repair_ratio
from common.media.tools import ProbeError
from common.plan import FFmpegPlan, SubtitleBurn
from common.workspace import DEFAULT_HARD_SIZE_CEILING, DEFAULT_MIN_FREE_GB
from sqlalchemy import Integer, and_, any_, delete, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY

from server.events import EventBus, publish_job_status
from server.fs.source import MediaSnapshot, authorized_library, catalog_generation, lock_job_source
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.queue.store import (
    current_request,
    finish,
    get_job,
    get_media_file,
    mark_staged,
    release_expired_holds,
)
from server.retention import delete_job_graph, protected_history_jobs
from server.storage import storage_io
from server.workspace import (
    _remove_attempt,
    _remove_job,
    _stage_source,
    _stage_subtitle,
    _StagingConflictError,
    finish_workspace_actions,
    snapshot_attempt_log,
    wait_workspace_io,
)
from server.workspaces import job_details

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

LOG = logging.getLogger("server.staging")


def _subtitle_size(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise ValueError("subtitle source must be a regular adjacent file")
    return path.stat().st_size


def _workspace_free(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def _catalog_prunable(
    row: MediaSnapshot,
    roots: list[str],
    unavailable: set[Path],
    excluded: tuple[Path, ...],
) -> bool:
    path = Path(row.path)
    excluded_file = any(path.resolve().is_relative_to(root) for root in excluded)
    if not excluded_file and any(path.is_relative_to(root) for root in unavailable):
        return False
    matching_roots = [Path(root) for root in roots if path.is_relative_to(Path(root))]
    if len(matching_roots) != 1:
        return False
    root = matching_roots[0]
    try:
        with directory_fd(root) as root_fd:
            with os.scandir(root_fd) as entries:
                next(entries, None)
            return True
    except (OSError, ValueError, ProbeError) as exc:
        LOG.warning(
            "Catalog pruning withheld for media %s. Source storage could not be verified",
            row.id,
            extra={"error": str(exc)},
        )
        return False


class StagingService:
    def __init__(
        self,
        sessionmaker,
        workspace_dir: Path,
        interval_s: float = 60.0,
        clock: Callable[[], datetime] = utcnow,
        events: EventBus | None = None,
        staged: Callable[[int, int], None] | None = None,
        healthcheck_wake: Callable[[], None] | None = None,
        min_free_gb: float = DEFAULT_MIN_FREE_GB,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.workspace_dir = workspace_dir
        self.min_free_gb = min_free_gb
        self.interval_s = interval_s
        self.clock = clock
        self.events = events
        self.staged = staged
        self.healthcheck_wake = healthcheck_wake
        self.tasks: dict[int, asyncio.Task[int]] = {}
        self._task: asyncio.Task | None = None
        self._closed = False
        self._wake = asyncio.Event()
        self.active_jobs: set[int] = set()
        self.busy_jobs: set[int] = set()
        self._stage_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    async def wait_idle(self) -> None:
        from common.process import drain

        while self.tasks:
            await drain(asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True))
        # Every staging entry point retains this lock until filesystem work drains.
        async with self._stage_lock:
            pass

    def start(self) -> None:
        if self._close_task is not None and not self._close_task.done():
            return
        if self._task is None:
            self._closed = False
            self._close_task = None
            self._task = asyncio.create_task(self._run())

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            if self._task is not None:
                self._task.cancel()
            for task in self.tasks.values():
                task.cancel()
            self._close_task = asyncio.create_task(self._close())
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

    async def _close(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.wait_idle()

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.release_holds()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("cooldown release failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass

    async def _announce(self, session, job_id: int) -> None:
        if self.events is None:
            return
        job = await get_job(session, job_id)
        if job is not None:
            await publish_job_status(self.events, self.sessionmaker, job_id)

    async def release_holds(self) -> int:
        async with self.sessionmaker() as session:
            jobs = await release_expired_holds(session, self.clock())
            await session.commit()
        for job in jobs:
            async with self.sessionmaker() as session:
                await self._announce(session, job.id)
        if self.healthcheck_wake is not None and any(job.kind == "healthcheck" for job in jobs):
            self.healthcheck_wake()
        return len(jobs)

    async def prune_job(self, job_id: int, *, attempt_number: int | None = None, request_id: int | None = None) -> dict:
        if job_id in self.active_jobs:
            return {"workspace_id": job_id, "outcome": "skipped", "reason": "Source files are still being copied."}
        async with self._stage_lock:
            return await self._prune_job(job_id, attempt_number, request_id=request_id)

    async def _prune_job(self, job_id: int, attempt_number: int | None, *, request_id: int | None = None) -> dict:
        if job_id in self.busy_jobs:
            return {"workspace_id": job_id, "outcome": "skipped", "reason": "Job is still processing."}
        try:
            async with self.sessionmaker() as session:
                if request_id is not None:
                    await current_request(session, job_id, request_id)
                guard = (
                    select(Job.id)
                    .where(
                        Job.id == job_id,
                        *([Job.attempts == attempt_number] if attempt_number is not None else []),
                        Job.status.in_(
                            [
                                *([JobStatus.PENDING] if request_id is not None else []),
                                JobStatus.SUCCEEDED,
                                JobStatus.FAILED,
                                JobStatus.CANCELLED,
                                JobStatus.SKIPPED,
                                JobStatus.VERIFYING,
                                JobStatus.FINALIZING,
                            ]
                        ),
                    )
                    .with_for_update(of=Job)
                )
                won = await session.scalar(guard)
                details = (await job_details(session, [job_id])).get(job_id)
                if request_id is not None and details is not None and details["status"] == JobStatus.PENDING:
                    details["eligible"] = True
                if details is not None and not (details["eligible"] or details["empty_only"]):
                    return {
                        "workspace_id": job_id,
                        "outcome": "skipped",
                        "reason": details["reason"],
                    }
                if won is None and details is not None:
                    return {"workspace_id": job_id, "outcome": "skipped", "reason": "Job attempt changed."}
                if job_id in self.busy_jobs:
                    return {"workspace_id": job_id, "outcome": "skipped", "reason": "Job is still processing."}
                attempts = list(await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)))
                for attempt in attempts:
                    if attempt.workload != "healthcheck" or attempt.capabilities is not None:
                        if request_id is not None:
                            await session.commit()
                        await snapshot_attempt_log(session, self.workspace_dir, attempt)
                await session.commit()
                won = await session.scalar(guard)
                if request_id is not None:
                    await current_request(session, job_id, request_id)
                details = (await job_details(session, [job_id])).get(job_id)
                if request_id is not None and details is not None and details["status"] == JobStatus.PENDING:
                    details["eligible"] = True
                if won is None and details is not None:
                    return {"workspace_id": job_id, "outcome": "skipped", "reason": "Job attempt changed."}
                if details is not None and not (details["eligible"] or details["empty_only"]):
                    return {"workspace_id": job_id, "outcome": "skipped", "reason": details["reason"]}
                if job_id in self.busy_jobs:
                    return {"workspace_id": job_id, "outcome": "skipped", "reason": "Job is still processing."}
                # Keep the job locked through removal, including cancellation cleanup.
                # A saved request does not replace this row lock.
                if details is not None and details["empty_only"]:
                    removed = await wait_workspace_io(lambda: _remove_job(self.workspace_dir, job_id, empty_only=True))
                    if not removed:
                        return {
                            "workspace_id": job_id,
                            "outcome": "skipped",
                            "reason": "Workspace contains files or its job attempt changed.",
                        }
                else:
                    await wait_workspace_io(lambda: _remove_job(self.workspace_dir, job_id))
                    if details is not None:
                        if request_id is not None:
                            await current_request(session, job_id, request_id)
                        await finish_workspace_actions(session, job_id)
                await session.commit()
            return {"workspace_id": job_id, "outcome": "removed", "reason": None}
        except FileNotFoundError:
            if request_id is not None:
                async with self.sessionmaker() as session:
                    await current_request(session, job_id, request_id)
                    await finish_workspace_actions(session, job_id)
                    await session.commit()
            return {"workspace_id": job_id, "outcome": "skipped", "reason": "Workspace no longer exists."}
        except BlockingIOError:
            return {"workspace_id": job_id, "outcome": "skipped", "reason": "Job is still processing."}
        except (OSError, ValueError) as exc:
            LOG.exception("workspace prune failed for job %s", job_id)
            return {
                "workspace_id": job_id,
                "outcome": "error",
                "reason": f"Workspace could not be safely removed. {type(exc).__name__}: {exc}",
            }

    async def prune_attempt(self, attempt_id: int) -> bool:
        async with self._stage_lock:
            return await self._prune_attempt(attempt_id)

    async def _prune_attempt(self, attempt_id: int) -> bool:
        async with self.sessionmaker() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            if (
                attempt is None
                or (attempt.workload == "healthcheck" and attempt.capabilities is None)
                or attempt.job_id in self.active_jobs | self.busy_jobs
            ):
                return False
            guard = (
                select(Job.id)
                .where(
                    Job.id == attempt.job_id,
                    Job.kind.in_(["transcode", "healthcheck", "mkv_stats"])
                    if attempt.capabilities is not None
                    else Job.kind == "transcode",
                    or_(
                        Job.status.in_(
                            [
                                JobStatus.SUCCEEDED,
                                JobStatus.FAILED,
                                JobStatus.CANCELLED,
                                JobStatus.SKIPPED,
                                JobStatus.VERIFYING,
                                JobStatus.FINALIZING,
                            ]
                        ),
                        Job.attempts > attempt.attempt_number,
                        and_(
                            Job.status.in_([JobStatus.STAGING, JobStatus.QUEUED, JobStatus.HOLD]),
                            exists(
                                select(JobAttempt.id).where(
                                    JobAttempt.id == attempt_id,
                                    JobAttempt.job_id == Job.id,
                                    JobAttempt.attempt_number == Job.attempts,
                                    JobAttempt.status.in_(
                                        [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED]
                                    ),
                                )
                            ),
                        ),
                    ),
                )
                .with_for_update(of=Job)
            )
            won = await session.scalar(guard)
            if won is None:
                return False
            await snapshot_attempt_log(session, self.workspace_dir, attempt)
            await session.commit()
            won = await session.scalar(guard)
            if won is None:
                return False
            if attempt.job_id in self.active_jobs | self.busy_jobs:
                return False
            try:
                await wait_workspace_io(
                    lambda: _remove_attempt(self.workspace_dir, attempt.job_id, attempt.attempt_number)
                )
            except BlockingIOError:
                return False
            await finish_workspace_actions(session, attempt.job_id, attempt_id=attempt.id)
            await session.commit()
            return True

    async def prune_catalog(
        self,
        snapshots: list[MediaSnapshot],
        *,
        library_updated_at: datetime,
        scan_id: int,
        unavailable: set[Path],
        excluded: tuple[Path, ...],
        stopped: threading.Event | None = None,
    ) -> int:
        if not snapshots:
            return 0
        library_id = snapshots[0].library_id
        if any(row.library_id != library_id for row in snapshots):
            raise ValueError("Catalog pruning requires one library.")
        async with self._stage_lock, self.sessionmaker() as session:
            library_query = (
                select(Library.id)
                .where(
                    Library.id == library_id,
                    Library.updated_at == library_updated_at,
                    Library.suspended.is_(False),
                    Library.scan_id == scan_id,
                )
                .with_for_update(read=True, of=Library)
            )
            current_library_id = await session.scalar(library_query)
            if current_library_id is None:
                return 0
            roots = await session.scalar(select(Library.roots).where(Library.id == library_id))
            assert roots is not None
            identifiers = sorted(row.id for row in snapshots)
            scope = any_(literal(identifiers, ARRAY(Integer)))
            await session.execute(
                select(MediaFile.id).where(MediaFile.id == scope).order_by(MediaFile.id).with_for_update(of=MediaFile)
            )
            await session.execute(
                select(Job.id).where(Job.media_file_id == scope).order_by(Job.id).with_for_update(of=Job)
            )
            completed = []
            removed_jobs: list[int] = []
            for row in sorted(snapshots, key=lambda row: row.id):
                if stopped is not None and stopped.is_set():
                    await session.rollback()
                    return 0
                if not await wait_workspace_io(lambda row=row: _catalog_prunable(row, roots, unavailable, excluded)):
                    continue
                candidate = (
                    update(MediaFile)
                    .where(
                        catalog_generation(row),
                        MediaFile.last_seen_scan_id < scan_id,
                        ~exists(
                            select(Job.id).where(
                                Job.media_file_id == MediaFile.id,
                                or_(
                                    Job.status.in_(
                                        [
                                            JobStatus.PENDING,
                                            JobStatus.CLAIMED,
                                            JobStatus.RUNNING,
                                            JobStatus.VERIFYING,
                                            JobStatus.FINALIZING,
                                        ]
                                    ),
                                    Job.id.in_(protected_history_jobs()),
                                    Job.id.in_(self.active_jobs | self.busy_jobs),
                                ),
                            )
                        ),
                    )
                    .values(updated_at=MediaFile.updated_at)
                    .returning(MediaFile.id)
                )
                won = await session.scalar(candidate)
                if won is None:
                    continue
                jobs = (
                    await session.execute(
                        select(
                            Job.id,
                            Job.kind,
                            exists(
                                select(JobAttempt.id).where(
                                    JobAttempt.job_id == Job.id, JobAttempt.capabilities.is_not(None)
                                )
                            ).label("node_workspace"),
                        )
                        .where(Job.media_file_id == row.id)
                        .order_by(Job.id)
                    )
                ).all()
                for job_id, kind, node_workspace in jobs:
                    if kind == "transcode" or node_workspace:
                        try:
                            await wait_workspace_io(lambda job_id=job_id: _remove_job(self.workspace_dir, job_id))
                        except BlockingIOError:
                            break
                else:
                    if not await wait_workspace_io(
                        lambda row=row: _catalog_prunable(row, roots, unavailable, excluded)
                    ):
                        continue
                    for job_id, _, _ in jobs:
                        await delete_job_graph(session, job_id)
                        removed_jobs.append(job_id)
                    completed.append(row.id)
            if stopped is not None and stopped.is_set():
                await session.rollback()
                return 0
            for identifiers in batched(completed, 100, strict=False):
                await session.execute(update(MediaFile).where(MediaFile.id.in_(identifiers)).values(probe_id=None))
                await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id.in_(identifiers)))
                await session.execute(delete(MediaFile).where(MediaFile.id.in_(identifiers)))
            if stopped is not None and stopped.is_set():
                await session.rollback()
                return 0
            await session.commit()
            if self.events is not None:
                for job_id in removed_jobs:
                    self.events.publish("job.deleted", {"job_id": job_id}, key=f"job.deleted:{job_id}")
                for file_id in completed:
                    self.events.publish(
                        "file.status",
                        {"action": "pruned", "media_file_id": file_id, "library_id": library_id},
                        key=f"file.status:file:{file_id}",
                    )
            return len(completed)

    def schedule(self, job_id: int) -> bool:
        if self._closed or job_id in self.tasks or job_id in self.active_jobs | self.busy_jobs:
            return False
        task = asyncio.create_task(self.stage_job(job_id), name=f"staging:{job_id}")
        self.tasks[job_id] = task
        task.add_done_callback(lambda finished: self._staged(job_id, finished))
        return True

    def _staged(self, job_id: int, task: asyncio.Task[int]) -> None:
        self.tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Staging cleanup failed for job %s", job_id, exc_info=task.exception())
        if self.staged is not None:
            self.staged(job_id, task.result() if not task.cancelled() and task.exception() is None else 0)

    async def stage_job(self, job_id: int) -> int:
        async with self._stage_lock:
            if self._closed or job_id in self.busy_jobs:
                return 0
            return await self._stage_job(job_id)

    async def _capacity_warning(
        self,
        session: AsyncSession,
        job_id: int,
        source_size: int,
        subtitle_size: int,
        *,
        copy_source: bool = True,
        min_free_gb: float | None = None,
    ) -> str | None:
        active_jobs = (
            select(Job.id, MediaFile.size)
            .join(MediaFile, Job.media_file_id == MediaFile.id)
            .where(
                or_(Job.kind == "transcode", (Job.kind.in_(["healthcheck", "mkv_stats"])) & Job.node_id.is_not(None)),
                Job.status.in_(
                    [
                        JobStatus.QUEUED,
                        JobStatus.CLAIMED,
                        JobStatus.RUNNING,
                        JobStatus.VERIFYING,
                        JobStatus.FINALIZING,
                    ]
                ),
            )
            .union_all(select(literal(job_id), literal(source_size)))
            .subquery()
        )
        # A refreshed requeue keeps history, but old inputs no longer reserve workspace capacity.
        latest_enqueue = (
            select(JobEvent.job_id, func.max(JobEvent.id).label("id"))
            .where(JobEvent.type == "enqueued")
            .group_by(JobEvent.job_id)
            .subquery()
        )
        payloads = await session.execute(
            select(active_jobs.c.id, active_jobs.c.size, JobEvent.type, JobEvent.payload)
            .outerjoin(latest_enqueue, latest_enqueue.c.job_id == active_jobs.c.id)
            .outerjoin(
                JobEvent,
                (JobEvent.job_id == active_jobs.c.id)
                & JobEvent.type.in_(["enqueued", "stage"])
                & (JobEvent.id >= func.coalesce(latest_enqueue.c.id, 0)),
            )
        )
        input_sizes: dict[int, int] = {}
        repair_ratios: dict[int, int] = {}
        for identifier, size, kind, payload in payloads:
            sizes = [size]
            if kind == "enqueued":
                sizes.append((payload.get("source") or {}).get("size", 0))
            elif kind == "stage":
                sizes.extend((payload.get(key) or {}).get("size_bytes", 0) for key in ("probe", "output_probe"))
            input_sizes[identifier] = max(input_sizes.get(identifier, size), *sizes)
            if payload is not None and payload.get("probe") is not None:
                ratio = mkv_repair_ratio(ProbeData.model_validate(payload["probe"]))
                repair_ratios[identifier] = max(repair_ratios.get(identifier, 0), ratio)
        remote = (
            await session.execute(
                select(Job.id, Job.kind, JobAttempt.source_path)
                .join(JobAttempt, (JobAttempt.job_id == Job.id) & (JobAttempt.attempt_number == Job.attempts))
                .where(
                    Job.node_id.is_not(None),
                    Job.kind.in_(["healthcheck", "mkv_stats"]),
                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING]),
                )
            )
        ).all()
        read_only = {identifier for identifier, kind, _ in remote if kind == "healthcheck"}
        # Remote claims reserve their copy before waiting for the shared staging
        # lock. Statistics also need a separate output. Decoding has no output.
        pending_copies = sum(
            input_sizes.get(identifier, 0) for identifier, _, path in remote if path is None and identifier != job_id
        )
        # Existing copies/outputs already consume measured space. Reserve one further
        # output and required repair files per active job, using its largest input.
        required = (
            (source_size if copy_source else 0)
            + subtitle_size
            + pending_copies
            + math.ceil((self.min_free_gb if min_free_gb is None else min_free_gb) * 1e9)
            + sum(
                math.ceil(size * (DEFAULT_HARD_SIZE_CEILING + repair_ratios.get(identifier, 0)))
                for identifier, size in input_sizes.items()
                if identifier not in read_only
            )
        )
        try:
            free = await wait_workspace_io(lambda: _workspace_free(self.workspace_dir))
        except OSError as exc:
            return f"staging waiting: workspace capacity unavailable: {exc}"
        if free < required:
            return f"Staging waiting. Workspace needs {required} free bytes. {free} available."
        return None

    async def stage_continuation_subtitle(self, attempt_id: int, filename: str) -> str:
        SubtitleBurn(filename=filename)
        async with self._stage_lock, self.sessionmaker() as session:
            if self._closed:
                raise RuntimeError("Staging service is closed.")
            attempt = await session.get(JobAttempt, attempt_id)
            job = await lock_job_source(session, attempt.job_id, attempt_id=attempt_id) if attempt is not None else None
            if (
                attempt is None
                or job is None
                or job.kind != "transcode"
                or attempt.attempt_number != job.attempts
                or job.status not in {JobStatus.RUNNING, JobStatus.VERIFYING}
                or attempt.status not in {JobStatus.RUNNING, JobStatus.VERIFYING}
            ):
                raise ValueError("Continuation attempt is no longer current.")
            media = await get_media_file(session, job.media_file_id)
            if media is None or await authorized_library(session, job.library_id, media) is None:
                raise ValueError("Source is no longer authorized for staging.")
            enqueued = await session.scalar(
                select(JobEvent.payload)
                .where(JobEvent.job_id == job.id, JobEvent.type == "enqueued")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            expected_source = (enqueued or {}).get("source")
            if not expected_source:
                raise ValueError("Captured source is missing. Rescan and enqueue a new job.")
            source = Path(media.path)
            selected = source.parent / filename
            subtitle_size = await wait_workspace_io(lambda: _subtitle_size(selected))
            stage = await session.scalar(
                select(JobEvent.payload)
                .where(JobEvent.attempt_id == attempt_id, JobEvent.type == "stage")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            input_size = ((stage or {}).get("output_probe") or {}).get("size_bytes", 0)
            if input_size <= 0:
                raise ValueError("Continuation output probe is missing.")
            warning = await self._capacity_warning(session, job.id, input_size, subtitle_size, copy_source=False)
            if warning is not None:
                raise ValueError(warning)
            # Keep cancellation and configuration changes blocked until the copy finishes.
            won = await session.scalar(
                update(Job)
                .where(Job.id == job.id, Job.attempts == attempt.attempt_number, Job.status == job.status)
                .values(status=Job.status)
                .returning(Job.id)
            )
            if won is None or await authorized_library(session, job.library_id, media) is None:
                raise ValueError("Continuation attempt is no longer authorized.")
            self.active_jobs.add(job.id)
            try:
                result = await storage_io(_stage_subtitle, self.workspace_dir, job.id, source, filename)
                await session.commit()
                return result
            except _StagingConflictError as exc:
                session.add(
                    JobEvent(
                        job_id=job.id, attempt_id=attempt.id, type="staging_unresolved", payload={"error": str(exc)}
                    )
                )
                await session.commit()
                raise
            finally:
                self.active_jobs.discard(job.id)

    async def _stage_job(self, job_id: int) -> int:
        min_free_gb = self.min_free_gb
        self.active_jobs.add(job_id)
        attempt_number = None
        try:
            async with self.sessionmaker() as session:
                job = await get_job(session, job_id)
                if job is None:
                    return 0
                attempt_number = job.attempts
                # A queued staging snapshot can outlive cancellation or history deletion.
                if await session.scalar(select(Job.status).where(Job.id == job.id)) != JobStatus.STAGING.value:
                    return 0
                media = await get_media_file(session, job.media_file_id)
                source: Path | None = None
                missing = False
                try:
                    if media is not None:
                        library = await authorized_library(session, job.library_id, media)
                        if library is None:
                            return 0
                        snapshot = MediaSnapshot.from_media(media)
                        library_stamp = library.updated_at
                        source = Path(media.path)
                        enqueued = await session.scalar(
                            select(JobEvent)
                            .where(JobEvent.job_id == job.id, JobEvent.type == "enqueued")
                            .order_by(JobEvent.id.desc())
                            .limit(1)
                        )
                        published = await session.scalar(
                            select(JobAttempt.id)
                            .where(
                                JobAttempt.job_id == job.id,
                                JobAttempt.status == JobStatus.SUCCEEDED.value,
                                JobAttempt.id > ((enqueued.attempt_id if enqueued is not None else None) or 0),
                            )
                            .limit(1)
                        )
                        if published is not None:
                            raise ValueError("Source was already processed. Rescan and enqueue a new job.")
                        captured_source = enqueued.payload.get("source") if enqueued is not None else None
                        if not captured_source:
                            raise ValueError("Captured source is missing. Rescan and enqueue a new job.")
                        plan = FFmpegPlan.model_validate(job.plan)
                        # Capacity and subtitle inspection do not reserve a copy slot.
                        await session.commit()
                        subtitle_size = 0
                        if plan.burn_subtitle is not None:
                            selected = source.parent / plan.burn_subtitle.filename
                            subtitle_size = await wait_workspace_io(lambda: _subtitle_size(selected))
                        warning = await self._capacity_warning(
                            session, job.id, captured_source["size"], subtitle_size, min_free_gb=min_free_gb
                        )
                        prior_warning = await session.scalar(select(Job.warning).where(Job.id == job.id))
                        # Storage inspection must finish before taking the copy locks.
                        await session.commit()
                        if await lock_job_source(session, job.id) is None:
                            return 0
                        current_job_id = await session.scalar(
                            update(Job)
                            .where(
                                Job.id == job.id,
                                Job.status == JobStatus.STAGING,
                                Job.attempts == attempt_number,
                                exists(select(MediaFile.id).where(catalog_generation(snapshot))),
                                exists(
                                    select(Library.id).where(
                                        Library.id == job.library_id,
                                        Library.updated_at == library_stamp,
                                        Library.suspended.is_(False),
                                    )
                                ),
                            )
                            .values(warning=warning)
                            .returning(Job.id)
                        )
                        # Copies create disposable workspace output. Release catalog and
                        # Job locks so cancellation can commit while filesystem I/O drains.
                        await session.commit()
                        if current_job_id is None:
                            return 0
                        if warning is not None:
                            if prior_warning != warning:
                                LOG.warning("%s for job %s", warning, job.id)
                                await self._announce(session, job.id)
                            return 0
                        await storage_io(
                            _stage_source,
                            self.workspace_dir,
                            job.id,
                            source,
                            expected_source=captured_source,
                            fresh=job.attempts > 0,
                        )
                except FileNotFoundError:
                    missing = True
                if source is None or missing:
                    error = "staging cancelled: source missing"
                    LOG.info("%s for job %s, path %s", error, job.id, source or "unknown")
                    await finish(session, job.id, JobStatus.CANCELLED, error=error)
                    await session.commit()
                    await self._announce(session, job.id)
                    return 0
                subtitle = plan.burn_subtitle
                assert captured_source is not None
                if await lock_job_source(session, job.id) is None:
                    return 0
                if subtitle is not None:
                    if await session.scalar(select(Job.status).where(Job.id == job.id)) != JobStatus.STAGING:
                        return 0
                    media = await session.scalar(
                        select(MediaFile)
                        .where(MediaFile.id == job.media_file_id)
                        .execution_options(populate_existing=True)
                    )
                    if media is None or await authorized_library(session, job.library_id, media) is None:
                        return 0
                    await storage_io(_stage_subtitle, self.workspace_dir, job.id, source, subtitle.filename)
                media = await session.scalar(
                    select(MediaFile).where(MediaFile.id == job.media_file_id).execution_options(populate_existing=True)
                )
                if media is None:
                    await session.rollback()
                    return 0
                library = await authorized_library(session, job.library_id, media)
                if library is None:
                    await session.rollback()
                    return 0
                library_stamp = library.updated_at
                staged_job_id = await session.scalar(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        Job.status == JobStatus.STAGING,
                        Job.attempts == attempt_number,
                        exists(select(MediaFile.id).where(catalog_generation(snapshot))),
                        exists(
                            select(Library.id).where(
                                Library.id == job.library_id,
                                Library.updated_at == library_stamp,
                                Library.suspended.is_(False),
                            )
                        ),
                    )
                    .values(status=Job.status, updated_at=Job.updated_at)
                    .returning(Job.id)
                )
                if staged_job_id is None or not await mark_staged(session, job.id):
                    await session.rollback()
                    return 0
                await session.commit()
                await self._announce(session, job.id)
                return 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            error = f"staging failed: {reason}"
            LOG.exception("staging failed for job %s", job_id, extra={"error": reason})
            async with self.sessionmaker() as session:
                await finish(session, job_id, JobStatus.FAILED, error=error)
                if isinstance(exc, _StagingConflictError):
                    session.add(JobEvent(job_id=job_id, type="staging_unresolved", payload={"error": error}))
                await session.commit()
                await self._announce(session, job_id)
            return 0
        finally:
            self.active_jobs.discard(job_id)
            if attempt_number is not None:
                async with self.sessionmaker() as session:
                    status = await session.scalar(select(Job.status).where(Job.id == job_id))
                if status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED}:
                    await self._prune_job(job_id, attempt_number)
