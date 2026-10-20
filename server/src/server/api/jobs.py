from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import ExitStack
from copy import copy
from dataclasses import replace
from itertools import batched
from typing import TYPE_CHECKING, Annotated, Literal

from common.enums import ExecutionLocation, JobStatus
from common.fs.acquire import directory_fd
from common.media.probe import ProbeData
from common.plan import FFmpegPlan, dst_for
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, literal, select, text, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased, defer, load_only, with_expression
from starlette.responses import JSONResponse, Response, StreamingResponse

from server.api.auth import current_principal
from server.event_rows import file_summary, job_row
from server.events import publish_job_status
from server.healthlogs import capture_healthcheck_log, healthcheck_log_path
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, NotificationDelivery
from server.nodehub.hub import NodeHub, probe_for_file
from server.queue.store import (
    TERMINAL_STATUSES,
    bump_job,
    bump_jobs,
    cancel_pending,
    get_job,
    get_media_file,
    job_events,
    list_jobs,
    top_queue_priority,
)
from server.scheduler.schedule import active, parse_schedule
from server.serialize import iso_utc, orm_columns
from server.workspace import _file, attempt_log_path, post_action_jobs, read_attempt_logs

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy import ColumnElement, Row, Select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.orm import InstrumentedAttribute

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)

_TERMINAL_VALUES = sorted(item.value for item in TERMINAL_STATUSES)
_QUEUED = JobStatus.QUEUED.value
_STAGING = JobStatus.STAGING.value
_HELD = JobStatus.HOLD.value
_QUEUE_BATCH = 100


class BumpBody(BaseModel):
    priority: int | None = Field(default=None, ge=-100, le=100)


class BulkBody(BaseModel):
    action: Literal["cancel", "retry", "bump", "delete"]
    job_ids: list[int] = Field(min_length=1, max_length=999)


def _file_dict(media_file) -> dict:
    return file_summary(media_file)


def _event_dict(event: JobEvent) -> dict:
    return orm_columns(event)


def _attempt_dict(attempt: JobAttempt) -> dict:
    return orm_columns(attempt)


async def _current_attempt_log(session, job: Job, attempt: JobAttempt, request: Request) -> bytes | None:
    if attempt.log_data is not None:
        return attempt.log_data
    local_healthcheck = job.kind == "healthcheck" and attempt.capabilities is None
    if not local_healthcheck and (job.status != JobStatus.RUNNING.value or attempt.status != JobStatus.RUNNING.value):
        return None
    if job.kind in {"healthcheck", "mkv_stats", "transcode"} and attempt.workload == job.kind:
        relative_path = attempt.log_path
    else:
        return None
    if not relative_path:
        return None
    try:
        if local_healthcheck:
            acquired = await session.scalar(
                select(JobEvent).where(JobEvent.attempt_id == attempt.id, JobEvent.type == "healthcheck_log_created")
            )
            if acquired is None or acquired.payload["log_path"] != relative_path:
                return None
            return await asyncio.to_thread(capture_healthcheck_log, request.app.state.healthcheck_dir, relative_path)
        return await read_attempt_logs(session, request.app.state.workspace_dir, attempt)
    except OSError, ValueError:
        return None


async def _job_detail(session, job_id: int, request: Request, *, include_events: bool) -> dict:
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    stored_events = await job_events(session, job_id) if include_events else []
    media_file = await get_media_file(session, job.media_file_id) if job.media_file_id is not None else None
    probe = await probe_for_file(session, media_file) if media_file is not None else None
    attempts = list(
        (
            await session.execute(
                select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_number)
            )
        ).scalars()
    )
    detail = orm_columns(job)
    detail["cooldown_started_at"] = (
        iso_utc(media_file.first_seen) if job.status == JobStatus.HOLD.value and media_file is not None else None
    )
    file_detail = _file_dict(media_file) if media_file is not None else None
    detail["file"] = file_detail
    if media_file is not None:
        video = next((stream for stream in (probe.real_video if probe else []) if stream.disposition_default), None)
        video = video or (probe.real_video[0] if probe and probe.real_video else None)
        audio = next((stream for stream in (probe.audio if probe else []) if stream.disposition_default), None)
        audio = audio or (probe.audio[0] if probe and probe.audio else None)
        assert file_detail is not None
        file_detail.update(
            {
                "video_codec": video.codec if video and video.codec else file_detail["video_codec"],
                "video_fps": video.fps if video else None,
                "video_bit_rate": video.bit_rate if video else None,
                "audio_codec": audio.codec if audio else None,
                "audio_channels": audio.channels if audio else None,
                "audio_channel_layout": audio.channel_layout if audio else None,
                "audio_bit_rate": audio.bit_rate if audio else None,
            }
        )
    detail["events"] = [_event_dict(event) for event in stored_events] if include_events else []
    detail["notification_deliveries"] = [
        orm_columns(delivery)
        for delivery in await session.scalars(
            select(NotificationDelivery).where(NotificationDelivery.job_id == job_id).order_by(NotificationDelivery.id)
        )
    ]
    detail["attempt_count"] = job.attempts
    detail["attempts"] = [_attempt_dict(attempt) for attempt in attempts]
    for attempt, row in zip(attempts, detail["attempts"], strict=True):
        if row["log_data"] is None:
            live_log = await _current_attempt_log(session, job, attempt, request)
            row["log_data"] = live_log.decode(errors="replace") if live_log is not None else None
    detail["file_probe"] = probe.model_dump(mode="json") if probe else None
    detail["flow_snapshot"] = job.flow_snapshot
    return detail


async def _load_job(request: Request, job_id: int) -> Job:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


async def _job_rows(session: AsyncSession, jobs: list[Job]) -> list[dict]:
    rows = []
    for batch in batched(jobs, 100, strict=False):
        files = {
            media_file.id: media_file
            for media_file in (
                await session.scalars(
                    select(MediaFile)
                    .where(MediaFile.id.in_([job.media_file_id for job in batch]))
                    .options(
                        load_only(
                            MediaFile.id,
                            MediaFile.library_id,
                            MediaFile.path,
                            MediaFile.size,
                            MediaFile.container,
                            MediaFile.video_codec,
                            MediaFile.width,
                            MediaFile.height,
                            MediaFile.duration_s,
                            MediaFile.first_seen,
                            raiseload=True,
                        )
                    )
                )
            )
        }
        latest_attempts = (
            select(JobAttempt.job_id, func.max(JobAttempt.attempt_number).label("attempt_number"))
            .where(JobAttempt.job_id.in_([job.id for job in batch]))
            .group_by(JobAttempt.job_id)
            .subquery()
        )
        attempts = await session.scalars(
            select(JobAttempt)
            .join(
                latest_attempts,
                (JobAttempt.job_id == latest_attempts.c.job_id)
                & (JobAttempt.attempt_number == latest_attempts.c.attempt_number),
            )
            .options(
                defer(JobAttempt.log_data, raiseload=True),
                *(
                    with_expression(column, case((column == JSONB.NULL, None), else_=column.op("-")("raw")))
                    for column in (JobAttempt.input_probe, JobAttempt.output_probe)
                ),
            )
        )
        latest = {attempt.job_id: attempt for attempt in attempts}
        rows.extend(job_row(job, files.get(job.media_file_id), latest.get(job.id)) for job in batch)
    return rows


@router.get("", summary="List jobs")
async def get_jobs(
    request: Request,
    job_status: Annotated[str | None, Query(alias="status")] = None,
    kind: str | None = None,
    query: str | None = None,
    limit: Annotated[int, Query(ge=1, le=999)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Literal["id", "created_at", "updated_at", "priority", "status", "path"] = "created_at",
    sort_dir: Literal["asc", "desc"] = "desc",
) -> dict:
    db: async_sessionmaker = request.app.state.db
    statuses = [part.strip() for part in (job_status or "").split(",") if part.strip()] or None
    async with db() as session:
        jobs, total = await list_jobs(
            session,
            statuses=statuses,
            kind=kind,
            query=query,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        rows = await _job_rows(session, jobs)
        return {"items": rows, "pagination": {"limit": limit, "offset": offset, "total": total}}


async def _queue_batches(
    session: AsyncSession,
    query: Select,
    order: tuple[ColumnElement | InstrumentedAttribute, ...],
    *,
    reverse: bool = False,
) -> AsyncGenerator[Sequence[Row]]:
    after = None
    while True:
        page = query
        if after is not None:
            page = page.where(tuple_(*order) < after if reverse else tuple_(*order) > after)
        batch = (
            await session.execute(
                page.order_by(*(column.desc() for column in order) if reverse else order).limit(_QUEUE_BATCH)
            )
        ).all()
        if not batch:
            return
        after = tuple(batch[-1][1:])
        # Buffered pages leave no live cursor across policy checks or other awaits.
        yield batch


async def _queued_preview(
    session: AsyncSession, where: list[ColumnElement[bool]], kind: str, cursors: dict[int, int]
) -> AsyncGenerator[Sequence[Row]]:
    if kind in {"probe", "mkv_stats"}:
        order = (Job.priority, Job.created_at, Job.id)
        async for batch in _queue_batches(session, select(Job.id, *order).where(*where), order, reverse=True):
            yield batch
        return
    if kind == "healthcheck":
        order = ((-Job.priority).label("priority_key"), Job.created_at, Job.id)
        async for batch in _queue_batches(session, select(Job.id, *order).where(*where), order):
            yield batch
        return
    async for priorities in _queue_batches(
        session,
        select(Job.priority, (-Job.priority).label("priority_key")).where(*where).distinct(),
        (-Job.priority,),
    ):
        for priority, _ in priorities:
            same_priority = [*where, Job.priority == priority]
            cursor = cursors.get(priority)
            anchor = (
                (
                    await session.execute(
                        select(Job.created_at, Job.id)
                        .where(*same_priority, Job.library_id == cursor)
                        .order_by(Job.created_at, Job.id)
                        .limit(1)
                    )
                ).first()
                if cursor is not None
                else None
            )
            library_order = (Job.created_at, Job.id)
            turns = (
                select(
                    Job.id,
                    func.row_number().over(partition_by=Job.library_id, order_by=library_order).label("turn"),
                    func.first_value(Job.created_at)
                    .over(partition_by=Job.library_id, order_by=library_order)
                    .label("first_at"),
                    func.first_value(Job.id)
                    .over(partition_by=Job.library_id, order_by=library_order)
                    .label("first_id"),
                )
                .where(*same_priority)
                .subquery()
            )
            wrap = (
                case(
                    ((turns.c.turn == 1) & (tuple_(turns.c.first_at, turns.c.first_id) <= tuple(anchor)), 1),
                    else_=0,
                )
                if anchor is not None
                else literal(0)
            )
            # Only the first turn rotates. Subsequent turns use the original library order.
            order = (turns.c.turn, wrap, turns.c.first_at, turns.c.first_id, turns.c.id)
            async for batch in _queue_batches(session, select(turns.c.id, *order), order):
                yield batch


async def _queue_reasons(
    session: AsyncSession, identifiers: list[int], kind: str, hub: NodeHub, busy: set[int], now: datetime
) -> AsyncGenerator[tuple[Row, str | None]]:
    latest = (
        select(JobEvent.payload)
        .where(JobEvent.job_id == Job.id, JobEvent.type == "enqueued")
        .order_by(JobEvent.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    related_job = aliased(Job)
    actions = (
        select(related_job.id)
        .where(related_job.media_file_id == Job.media_file_id, related_job.id.in_(post_action_jobs()))
        .exists()
    )
    for identifiers_batch in batched(identifiers, _QUEUE_BATCH, strict=False):
        candidates = (
            await session.execute(
                select(
                    Job.id,
                    Job.status,
                    Job.priority,
                    Job.library_id,
                    Job.created_at,
                    Job.plan,
                    Job.requires,
                    Job.requires_encoders,
                    Job.warning,
                    Library.id.label("library"),
                    Library.suspended,
                    Library.schedule,
                    MediaFile.path,
                    (latest if kind == "transcode" else literal(None)).label("captured"),
                    (actions if kind == "transcode" else literal(value=False)).label("actions"),
                )
                .outerjoin(Library, Library.id == Job.library_id)
                .outerjoin(MediaFile, MediaFile.id == Job.media_file_id)
                .where(Job.id.in_(identifiers_batch))
            )
        ).all()
        by_id = {candidate.id: candidate for candidate in candidates}
        for identifier in identifiers_batch:
            job = by_id[identifier]
            reason = None
            if job.library is None:
                reason = "Library is missing."
            elif job.suspended:
                reason = "Library is suspended."
            elif job.path is None:
                reason = "Media file is missing."
            elif kind in {"healthcheck", "mkv_stats"}:
                location = ExecutionLocation.LOCAL
                if kind == "healthcheck" and hub.healthcheck_service is not None:
                    location = hub.healthcheck_service.location
                elif kind == "mkv_stats" and hub.probe_service is not None:
                    location = hub.probe_service.statistics_location
                if location == ExecutionLocation.NODE and not hub.inspection_available(kind):
                    reason = "Waiting for eligible node capacity."
            elif kind == "transcode":
                data = job.captured or {}
                if job.status == _QUEUED and job.id in busy:
                    reason = "Job is already assigned to a node."
                elif job.status == _QUEUED and job.actions:
                    reason = "Post-actions are still running for this file."
                elif job.status == _QUEUED and not active(parse_schedule(job.schedule), now, hub.timezone):
                    reason = "Library schedule is inactive."
                elif not data.get("source"):
                    reason = "Captured source is missing. Rescan and enqueue a new job."
                elif data.get("probe") is None:
                    reason = "Captured probe is missing. Rescan and enqueue a new job."
                else:
                    try:
                        plan = FFmpegPlan.model_validate(job.plan)
                        probe = ProbeData.model_validate(data["probe"])
                        dst_for(job.path, plan, input_container=probe.container)
                        if job.status == _QUEUED:
                            _, reason = hub._pick(plan, probe, job.requires, job.requires_encoders)
                    except ValueError as exc:
                        reason = f"Invalid dispatch plan: {exc}"
            yield job, reason


@router.get("/queue", summary="Preview the scheduling queue")
async def get_queue(
    request: Request,
    kind: Literal["healthcheck", "transcode", "probe", "mkv_stats"],
    limit: Annotated[int, Query(ge=1, le=999)] = 6,
) -> dict:
    db: async_sessionmaker = request.app.state.db
    live: NodeHub = request.app.state.hub
    # Matching must see the same load and capabilities on both classification passes.
    hub = copy(live)
    busy = {job_id for entry in live.entries.values() for job_id in entry.in_flight}
    hub.entries = {}
    for name, entry in live.entries.items():
        disconnected = asyncio.Event()
        if entry.disconnected.is_set():
            disconnected.set()
        in_flight = dict(entry.in_flight)
        # Freeze completion backlog as load instead of retaining tasks whose done state can change.
        in_flight.update(
            (key[1], "transcode")
            for key, task in live._completions.items()
            if key[0] == entry.node_db_id and not task.done()
        )
        hub.entries[name] = replace(
            entry,
            capabilities=entry.capabilities.model_copy(deep=True),
            in_flight=in_flight,
            disconnected=disconnected,
        )
    hub._completions = {}
    cursors = dict(live._library_cursors)
    now = hub.clock()
    where = [Job.kind == kind, Job.delete_requested.is_(False)]
    queued_where = [*where, Job.status == _QUEUED]
    all_where = [*where, Job.status.in_([_QUEUED, _STAGING] if kind == "transcode" else [_QUEUED])]
    ranked: list[tuple[int, str, str | None]] = []
    async with db() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execution_options(read_only=True)
        )
        total = await session.scalar(select(func.count()).select_from(Job).where(*all_where))
        async for batch in _queued_preview(session, queued_where, kind, cursors):
            async for job, reason in _queue_reasons(session, [row[0] for row in batch], kind, hub, busy, now):
                if reason is None:
                    ranked.append((job.id, "ready", None))
                    if len(ranked) == limit:
                        break
            if len(ranked) == limit:
                break
        order = ((-Job.priority).label("priority_key"), Job.created_at, Job.id)
        if kind == "transcode" and len(ranked) < limit:
            async for batch in _queue_batches(
                session, select(Job.id, *order).where(*where, Job.status == _STAGING), order
            ):
                async for job, reason in _queue_reasons(session, [row[0] for row in batch], kind, hub, busy, now):
                    if reason is None:
                        # Filesystem capacity and identity checks belong to staging, not this read-only preview.
                        ranked.append((job.id, "staging", job.warning))
                        if len(ranked) == limit:
                            break
                if len(ranked) == limit:
                    break
        remaining = limit - len(ranked)
        if remaining and kind in {"healthcheck", "probe", "mkv_stats"}:
            async for batch in _queue_batches(session, select(Job.id, *order).where(*all_where), order):
                async for job, reason in _queue_reasons(session, [row[0] for row in batch], kind, hub, busy, now):
                    if reason is not None:
                        ranked.append((job.id, "blocked", reason))
                        if len(ranked) == limit:
                            break
                if len(ranked) == limit:
                    break
        elif remaining:
            # Inspect each library's blocked turns separately. Its first blocked job fixes the
            # library order, without retaining a queue-wide reason map or per-library counters.
            blocked = []
            group_order = ((-Job.priority).label("priority_key"), Job.library_id)
            groups = select(Job.library_id, *group_order).where(*all_where).group_by(Job.priority, Job.library_id)
            priority = None
            anchor = None
            async for batch in _queue_batches(session, groups, group_order):
                for library_id, negative_priority, _ in batch:
                    if priority != negative_priority:
                        if len(blocked) == remaining:
                            break
                        priority = negative_priority
                        anchor = None
                        cursor = cursors.get(-priority)
                        if cursor is not None:
                            cursor_query = select(Job.id, *order).where(
                                *all_where, Job.priority == -priority, Job.library_id == cursor
                            )
                            async for cursor_batch in _queue_batches(session, cursor_query, order):
                                async for job, reason in _queue_reasons(
                                    session, [row[0] for row in cursor_batch], kind, hub, busy, now
                                ):
                                    if reason is not None:
                                        anchor = (job.created_at, job.id)
                                        break
                                if anchor is not None:
                                    break
                    query = select(Job.id, *order).where(
                        *all_where, Job.priority == -negative_priority, Job.library_id == library_id
                    )
                    turn = 0
                    first = None
                    done = False
                    async for library_batch in _queue_batches(session, query, order):
                        async for job, reason in _queue_reasons(
                            session, [row[0] for row in library_batch], kind, hub, busy, now
                        ):
                            if reason is None:
                                continue
                            turn += 1
                            first = first or (job.created_at, job.id)
                            wrap = int(turn == 1 and anchor is not None and first <= anchor)
                            key = (negative_priority, turn, wrap, *first)
                            if len(blocked) == remaining and key >= blocked[-1][0]:
                                done = True
                                break
                            if len(blocked) == remaining:
                                blocked.pop()
                            blocked.append((key, job.id, reason))
                            blocked.sort()
                        if done:
                            break
                else:
                    continue
                break
            ranked.extend((identifier, "blocked", reason) for _, identifier, reason in blocked)
        jobs = {}
        for batch in batched([identifier for identifier, _, _ in ranked], 100, strict=False):
            jobs.update(
                (job.id, job)
                for job in await session.scalars(
                    select(Job)
                    .where(Job.id.in_(batch))
                    .options(
                        load_only(
                            Job.id,
                            Job.media_file_id,
                            Job.library_id,
                            Job.kind,
                            Job.status,
                            Job.priority,
                            Job.attempts,
                            Job.node_id,
                            Job.error,
                            Job.warning,
                            Job.hold_until,
                            Job.created_at,
                            Job.updated_at,
                            Job.started_at,
                            Job.finished_at,
                            Job.progress,
                            Job.decision_log,
                            raiseload=True,
                        )
                    )
                )
            )
        rows = await _job_rows(session, [jobs[identifier] for identifier, _, _ in ranked])
        for row, (_, state, reason) in zip(rows, ranked, strict=True):
            row.update(queue_state=state, queue_reason=reason)
        return {"items": rows, "pagination": {"limit": limit, "offset": 0, "total": total}}


@router.post("/bulk", response_model=None, summary="Apply an action to multiple jobs")
async def bulk(body: BulkBody, request: Request) -> dict | Response:
    job_ids = list(dict.fromkeys(body.job_ids))
    if any(job_id <= 0 for job_id in job_ids):
        raise HTTPException(status_code=422, detail="job_ids must be positive")
    if body.action in {"retry", "delete"}:
        try:
            operation = request.app.state.jobs.retry if body.action == "retry" else request.app.state.jobs.delete
            accepted = await operation(job_ids)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(accepted, status_code=202)
    db: async_sessionmaker = request.app.state.db
    succeeded: list[int] = []
    failed: list[dict] = []
    changed: list[Job] = []
    cancel_requested: list[int] = []
    cancelled_pending: list[Job] = []
    async with db() as session:
        if body.action == "bump":
            bumped = {job.id: job for job in await bump_jobs(session, job_ids)}
            for job_id in job_ids:
                if job_id in bumped:
                    succeeded.append(job_id)
                else:
                    detail = "only staging or queued jobs can be reprioritized"
                    if await get_job(session, job_id) is None:
                        detail = "job not found"
                    failed.append({"id": job_id, "detail": detail})
            changed = list(bumped.values())
        else:
            for job_id in job_ids:
                job = await get_job(session, job_id)
                if job is None:
                    failed.append({"id": job_id, "detail": "job not found"})
                    continue
                if job.status == JobStatus.PENDING:
                    await session.commit()
                    if await request.app.state.jobs.cancel(job_id):
                        succeeded.append(job_id)
                        cancel_requested.append(job_id)
                    else:
                        failed.append({"id": job_id, "detail": "job is no longer pending"})
                elif job.status in {_QUEUED, _STAGING, _HELD}:
                    if (changed_job := await cancel_pending(session, job_id)) is not None:
                        succeeded.append(job_id)
                        changed.append(changed_job)
                        cancelled_pending.append(changed_job)
                    else:
                        current = await get_job(session, job_id)
                        if current is not None and current.status not in _TERMINAL_VALUES:
                            # The hub can finish unassigned jobs in its own write transaction.
                            await session.commit()
                            cancelled = (
                                await request.app.state.probes.cancel_job(job_id)
                                if current.node_id is None and current.kind in {"probe", "mkv_stats"}
                                else request.app.state.healthchecks.cancel_job(job_id)
                                if current.node_id is None and current.kind == "healthcheck"
                                else await request.app.state.hub.cancel_job(job_id)
                            )
                            if cancelled:
                                succeeded.append(job_id)
                                cancel_requested.append(job_id)
                            else:
                                failed.append({"id": job_id, "detail": "job is not running on a connected node"})
                        else:
                            failed.append({"id": job_id, "detail": "job already finished"})
                elif job.status in _TERMINAL_VALUES:
                    failed.append({"id": job_id, "detail": "job already finished"})
                elif job.node_id is not None or job.kind == "transcode":
                    await session.commit()
                    if await request.app.state.hub.cancel_job(job_id):
                        succeeded.append(job_id)
                        cancel_requested.append(job_id)
                    else:
                        failed.append({"id": job_id, "detail": "job is not running on a connected node"})
                elif job.kind in {"probe", "mkv_stats"}:
                    await session.commit()
                    if await request.app.state.probes.cancel_job(job_id):
                        succeeded.append(job_id)
                        cancel_requested.append(job_id)
                    else:
                        failed.append({"id": job_id, "detail": "metadata job is not running locally"})
                elif job.kind == "healthcheck" and request.app.state.healthchecks.cancel_job(job_id):
                    succeeded.append(job_id)
                    cancel_requested.append(job_id)
                elif job.kind == "healthcheck":
                    failed.append({"id": job_id, "detail": "healthcheck is not running locally"})
                else:
                    failed.append({"id": job_id, "detail": "job is not running on a connected node"})
        await session.commit()
    events = request.app.state.events
    for job_id in cancel_requested:
        events.publish("job.cancel_requested", {"job_id": job_id}, key=f"job.cancel_requested:{job_id}")
    for job in changed:
        await publish_job_status(events, db, job.id)
    for job in cancelled_pending:
        await request.app.state.staging.prune_job(job.id, attempt_number=job.attempts)
    request.app.state.probes.wake()
    return {"succeeded": succeeded, "failed": failed}


@router.delete("/{job_id}", status_code=202, summary="Request job deletion")
async def delete_job(job_id: int, request: Request) -> dict:
    try:
        result = await request.app.state.jobs.delete([job_id])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result["failed"]:
        detail = result["failed"][0]["detail"]
        raise HTTPException(status_code=404 if detail == "job not found" else 409, detail=detail)
    return {"id": job_id}


@router.get("/{job_id}/state", summary="Read compact job state")
async def read_job_state(job_id: int, request: Request) -> dict:
    job = await _load_job(request, job_id)
    return {key: getattr(job, key) for key in ("id", "status", "priority", "progress", "error", "warning")} | {
        key: iso_utc(getattr(job, key)) for key in ("started_at", "finished_at")
    }


@router.get("/{job_id}", summary="Show a job")
async def read_job(
    job_id: int,
    request: Request,
    events: Annotated[int, Query(ge=0, le=1)] = 0,
) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        return await _job_detail(session, job_id, request, include_events=bool(events))


@router.get("/{job_id}/export.json", summary="Export a job")
async def export_job(job_id: int, request: Request) -> Response:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        snapshot = await _job_detail(session, job_id, request, include_events=True)

    def serialize() -> str:
        return json.dumps(snapshot, indent=2, ensure_ascii=False)

    body = await asyncio.to_thread(serialize)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}.json"'},
    )


class _LogDownloadResponse(StreamingResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if isinstance(self.body_iterator, AsyncGenerator):
                await self.body_iterator.aclose()


def _download_log(root: Path, relative: str, *, healthcheck: bool = False) -> tuple[Path, os.stat_result]:
    path = healthcheck_log_path(root, relative) if healthcheck else attempt_log_path(root, relative)
    with directory_fd(path.parent) as directory, _file(directory, path.name) as handle:
        return path, os.fstat(handle.fileno())


@router.get("/{job_id}/attempts/{attempt_id}/log", summary="Download an attempt log")
async def attempt_log(job_id: int, attempt_id: int, request: Request) -> StreamingResponse:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        job = await get_job(session, job_id)
        attempt = await session.get(JobAttempt, attempt_id)
        if job is None or attempt is None or attempt.job_id != job_id:
            raise HTTPException(status_code=404, detail="log not found")
        local_healthcheck = job.kind == "healthcheck" and attempt.capabilities is None
        earlier = (
            list(
                await session.scalars(
                    select(JobEvent.payload["log_path"].as_string())
                    .where(
                        JobEvent.attempt_id == attempt.id,
                        JobEvent.type == "stage",
                        JobEvent.payload["log"].as_string().is_not(None),
                        JobEvent.payload["log_path"].as_string() != attempt.log_path,
                    )
                    .order_by(JobEvent.id)
                )
            )
            if attempt.log_data is None and job.kind == "transcode"
            else []
        )
        acquired = (
            await session.scalar(
                select(JobEvent).where(JobEvent.attempt_id == attempt.id, JobEvent.type == "healthcheck_log_created")
            )
            if local_healthcheck and attempt.log_data is None
            else None
        )
    data = attempt.log_data
    segments: list[tuple[Path | bytes, int]] = []
    if data is None:
        if not local_healthcheck and (
            job.status != JobStatus.RUNNING.value or attempt.status != JobStatus.RUNNING.value
        ):
            raise HTTPException(status_code=404, detail="log not found")
        if job.kind in {"healthcheck", "mkv_stats", "transcode"} and attempt.workload == job.kind:
            relative = attempt.log_path
        else:
            relative = None
        if not relative:
            raise HTTPException(status_code=404, detail="log not found")
        if local_healthcheck and (acquired is None or acquired.payload["log_path"] != relative):
            raise HTTPException(status_code=404, detail="log not found")
        try:
            for relative_path in [*earlier, relative]:
                root = request.app.state.healthcheck_dir if local_healthcheck else request.app.state.workspace_dir
                path, info = await asyncio.to_thread(_download_log, root, relative_path, healthcheck=local_healthcheck)
                if segments:
                    segments.append((b"\n", 1))
                segments.append((path, info.st_size))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="log not found") from exc
    else:
        segments.append((data, len(data)))
    size = sum(length for _, length in segments)
    range_header = request.headers.get("range")
    start, end = 0, size - 1
    status = 200
    if range_header:
        if not range_header.startswith("bytes=") or "," in range_header:
            raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        spec = range_header[6:]
        try:
            left, right = spec.split("-", 1)
            if left:
                start = int(left)
                end = int(right) if right else size - 1
            else:
                length = int(right)
                start, end = max(size - length, 0), size - 1
        except (ValueError, IndexError) as exc:
            raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{size}"}) from exc
        if size == 0 or start < 0 or start > end or start >= size:
            raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        status = 206
    length = max(0, end - start + 1)

    async def body():
        with ExitStack() as stack:
            sources = []
            for segment, segment_size in segments:
                if isinstance(segment, bytes):
                    sources.append((segment, segment_size))
                    continue
                directory = stack.enter_context(directory_fd(segment.parent))
                handle = stack.enter_context(_file(directory, segment.name))
                sources.append((handle, segment_size))
            position = 0
            for source, segment_size in sources:
                offset = max(0, start - position)
                remaining = min(segment_size - offset, end + 1 - max(start, position))
                position += segment_size
                if remaining <= 0:
                    continue
                if isinstance(source, bytes):
                    for index in range(offset, offset + remaining, 65536):
                        yield source[index : min(index + 65536, offset + remaining)]
                    continue
                handle = source
                handle.seek(offset)
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)

    headers = {
        "Content-Type": "text/plain",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="attempt-{attempt_id}.log"',
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return _LogDownloadResponse(body(), status_code=status, headers=headers, media_type="text/plain")


@router.post("/{job_id}/cancel", response_model=None, summary="Cancel a job")
async def cancel(job_id: int, request: Request) -> dict | Response:
    job = await _load_job(request, job_id)
    if job.status in _TERMINAL_VALUES:
        raise HTTPException(status_code=409, detail="job already finished")

    db: async_sessionmaker = request.app.state.db
    if job.status == JobStatus.PENDING:
        if not await request.app.state.jobs.cancel(job_id):
            raise HTTPException(status_code=409, detail="job is no longer pending")
        return JSONResponse({"id": job_id, "status": "cancelling"}, status_code=202)
    cancel_requested = False
    if job.status in {_QUEUED, _STAGING, _HELD}:
        async with db() as session:
            row = await cancel_pending(session, job_id)
            if row is None:
                current = await get_job(session, job_id)
                if current is None or current.status in _TERMINAL_VALUES:
                    raise HTTPException(status_code=409, detail="job is no longer active")
                await session.commit()
                cancelled = (
                    await request.app.state.hub.cancel_job(job_id)
                    if current.node_id is not None or current.kind == "transcode"
                    else await request.app.state.probes.cancel_job(job_id)
                    if current.kind in {"probe", "mkv_stats"}
                    else request.app.state.healthchecks.cancel_job(job_id)
                    if current.kind == "healthcheck"
                    else False
                )
                if not cancelled:
                    raise HTTPException(status_code=409, detail="job is no longer active")
                cancel_requested = True
            await session.commit()
        if cancel_requested:
            request.app.state.events.publish(
                "job.cancel_requested", {"job_id": job_id}, key=f"job.cancel_requested:{job_id}"
            )
            LOG.info(
                "job cancellation requested",
                extra={"operation": "job_cancel", "job_id": job_id, "outcome": "cancelling"},
            )
            if job.kind in {"probe", "mkv_stats"}:
                return JSONResponse({"id": job_id, "status": "cancelling"}, status_code=202)
            return {"id": job_id, "status": "cancelling"}
        if row is None:
            raise HTTPException(status_code=409, detail="job is no longer active")
        events = request.app.state.events
        await publish_job_status(events, db, job_id)
        await request.app.state.staging.prune_job(row.id, attempt_number=row.attempts)
        request.app.state.probes.wake()
        LOG.info("job cancelled", extra={"operation": "job_cancel", "job_id": job_id, "outcome": "cancelled"})
        return {"id": job_id, "status": "cancelled"}

    if job.node_id is None and job.kind in {"probe", "mkv_stats"}:
        if not await request.app.state.probes.cancel_job(job_id):
            raise HTTPException(status_code=409, detail="metadata job is not running locally")
        request.app.state.events.publish(
            "job.cancel_requested", {"job_id": job_id}, key=f"job.cancel_requested:{job_id}"
        )
        return JSONResponse({"id": job_id, "status": "cancelling"}, status_code=202)
    if job.node_id is None and job.kind == "healthcheck" and not request.app.state.healthchecks.cancel_job(job_id):
        raise HTTPException(status_code=409, detail="healthcheck is not running locally")
    if job.node_id is None and job.kind == "healthcheck":
        request.app.state.events.publish(
            "job.cancel_requested", {"job_id": job_id}, key=f"job.cancel_requested:{job_id}"
        )
        LOG.info(
            "job cancellation requested", extra={"operation": "job_cancel", "job_id": job_id, "outcome": "cancelling"}
        )
        return {"id": job_id, "status": "cancelling"}
    hub = request.app.state.hub
    if not await hub.cancel_job(job_id):
        raise HTTPException(status_code=409, detail="job is not running on a connected node")
    request.app.state.events.publish("job.cancel_requested", {"job_id": job_id}, key=f"job.cancel_requested:{job_id}")
    return {"id": job_id, "status": "cancelling"}


@router.post("/{job_id}/retry", status_code=202, summary="Retry a finished job")
async def retry(job_id: int, request: Request) -> dict:
    try:
        result = await request.app.state.jobs.retry([job_id])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result["failed"]:
        detail = result["failed"][0]["detail"]
        raise HTTPException(status_code=404 if detail == "job not found" else 409, detail=detail)
    return {"id": job_id, "status": JobStatus.PENDING.value}


@router.post("/{job_id}/bump", summary="Reorder a queued job")
async def bump(job_id: int, body: BumpBody | None, request: Request) -> dict:
    job = await _load_job(request, job_id)
    if job.status not in {_QUEUED, _STAGING}:
        raise HTTPException(status_code=409, detail="only staging or queued jobs can be reprioritized")

    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        priority = (
            body.priority if body is not None and body.priority is not None else await top_queue_priority(session)
        )
        bumped = await bump_job(session, job_id, priority)
        await session.commit()
    if bumped is None:
        raise HTTPException(status_code=409, detail="job is no longer queued")
    events = request.app.state.events
    await publish_job_status(events, db, job_id)
    return {"id": bumped.id, "priority": bumped.priority}
