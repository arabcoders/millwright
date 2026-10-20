from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING

from common.enums import HealthStatus, JobStatus, TranscodeStatus
from common.media.probe import ProbeData, probe_from_raw
from common.media.tools import ProbeError
from psycopg.errors import UniqueViolation
from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from server.flows.engine import decision_entry
from server.fs.source import authorized_library, check_source_root, claim_catalog, lock_job_source
from server.fs.source import capture_source as capture_source_path
from server.models import (
    FinalizationJournal,
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NodeRow,
    NotificationDelivery,
    ProbeRow,
    utcnow,
)
from server.notifications import record_publication_notifications
from server.processing_cutoff import processing_allowed, processing_clause
from server.scheduler.schedule import active, parse_schedule
from server.serialize import iso_utc
from server.workspace import FINISHED_FINALIZATION_STATES, post_action_jobs, wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable

    from common.actions import Action
    from common.ctx import FacetValue
    from common.plan.models import FFmpegPlan
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.flows.engine import FlowContinuation, FlowResult
    from server.fs.library import ProbeResult

ACTIVE_STATUSES = frozenset(
    {
        JobStatus.PENDING,
        JobStatus.STAGING,
        JobStatus.HOLD,
        JobStatus.QUEUED,
        JobStatus.CLAIMED,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
        JobStatus.FINALIZING,
    }
)
TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED})

_ACTIVE_VALUES = sorted(status.value for status in ACTIVE_STATUSES)
_TERMINAL_VALUES = sorted(status.value for status in TERMINAL_STATUSES)
QUERY_BATCH = 500


class ActiveJobError(Exception):
    pass


@dataclass(kw_only=True, slots=True)
class DecisionResult:
    job_id: int | None
    healthcheck_id: int | None


class StaleJobError(Exception):
    pass


async def check_file_idle(
    session: AsyncSession, media_file_id: int, *, excluding_job: int | None = None, metadata: bool = False
) -> None:
    active = await session.scalar(
        select(Job.id)
        .where(
            Job.media_file_id == media_file_id,
            Job.status.in_(_ACTIVE_VALUES),
            *([~((Job.status == JobStatus.PENDING) & Job.waiting_metadata.is_(True))] if metadata else []),
            *([Job.id != excluding_job] if excluding_job is not None else []),
        )
        .limit(1)
    )
    if active is not None:
        raise ActiveJobError("media file already has an active job")
    actions = await session.scalar(post_action_jobs(scoped=True).where(Job.media_file_id == media_file_id).limit(1))
    if actions is not None:
        raise ActiveJobError("Media file has unfinished post-actions. Finish them before requeuing.")
    pending = await session.scalar(
        select(FinalizationJournal.id)
        .join(JobAttempt, JobAttempt.id == FinalizationJournal.attempt_id)
        .join(Job, Job.id == JobAttempt.job_id)
        .where(
            Job.media_file_id == media_file_id,
            FinalizationJournal.state.not_in(FINISHED_FINALIZATION_STATES),
        )
        .limit(1)
    )
    if pending is not None:
        raise ActiveJobError("Media file has unresolved finalization. Complete recovery before requeuing.")


class NodeRemovalError(Exception):
    pass


async def request_file_job(
    session: AsyncSession, media: MediaFile, *, kind: str, refresh: bool, flow_snapshot: dict | None = None
) -> Job:
    if kind == "healthcheck":
        job = await _insert_job(session, media.id, media.library_id, kind=kind, priority=0, now=None)
        covered = select(Job.id).where(
            Job.media_file_id == media.id, Job.kind == "transcode", Job.status == JobStatus.SUCCEEDED
        )
        await session.execute(
            update(JobEvent)
            .where(JobEvent.type == "artifact_check_pending", JobEvent.job_id.in_(covered))
            .values(type="artifact_check_finished")
        )
    else:
        job = Job(media_file_id=media.id, library_id=media.library_id, kind=kind, status=JobStatus.PENDING)
        session.add(job)
        await session.flush()
        media.transcode_decided = True
    payload = {"operation": kind, "refresh": refresh}
    if flow_snapshot is not None:
        payload["flow_snapshot"] = flow_snapshot
    session.add(JobEvent(job_id=job.id, type="requested", payload=payload))
    return job


def record_request_outcome(
    session: AsyncSession, job: Job, request: JobEvent, *, outcome: str, reason: str, decision: dict | None = None
) -> dict:
    retry = request.payload["operation"] == "retry"
    payload = {"job_id": job.id, "outcome": outcome, "reason": reason}
    if not retry:
        payload.update(file_id=job.media_file_id, kind=job.kind)
    if decision is not None:
        payload["decision"] = decision
    session.add(
        JobEvent(
            job_id=job.id, type="requeue" if retry else "request_result", payload={"request_id": request.id, **payload}
        )
    )
    return payload


async def current_request(session: AsyncSession, job_id: int, request_id: int) -> JobEvent:
    await session.get(Job, job_id, populate_existing=True, with_for_update=True)
    latest = await session.scalar(
        select(func.max(JobEvent.id)).where(JobEvent.job_id == job_id, JobEvent.type == "requested")
    )
    event = await session.scalar(
        select(JobEvent)
        .join(Job, Job.id == JobEvent.job_id)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.PENDING,
            JobEvent.id == request_id,
            JobEvent.type == "requested",
        )
        .execution_options(populate_existing=True)
    )
    if latest != request_id or event is None or event.payload.get("cancelled"):
        raise ValueError("Job request was cancelled, replaced, or is no longer pending.")
    return event


async def release_transcode_decision(session: AsyncSession, job: Job, *, prior_health: str) -> None:
    # Metadata and publication checks do not request a flow run. Only a flow gate
    # or an operator's healthcheck/retry may release a retained transcode decision.
    requested = await session.scalar(
        select(JobEvent.id)
        .where(
            JobEvent.job_id == job.id,
            JobEvent.type == "requested",
            JobEvent.payload["operation"].as_string().in_(["healthcheck", "retry"]),
        )
        .limit(1)
    )
    if job.flow_snapshot is None and requested is None:
        return
    resumable = [TranscodeStatus.PENDING.value]
    if prior_health == HealthStatus.PENDING.value:
        resumable.append(TranscodeStatus.CANCELLED.value)
    await session.execute(
        update(MediaFile)
        .where(MediaFile.id == job.media_file_id, MediaFile.transcode_status.in_(resumable))
        .values(transcode_decided=False, transcode_status=TranscodeStatus.PENDING.value)
    )


def restore_retry_result(job: Job, request: JobEvent) -> None:
    for key, value in request.payload["previous"].items():
        if key in {"started_at", "finished_at", "lease_expires_at", "hold_until"} and value is not None:
            value = datetime.fromisoformat(value)
        setattr(job, key, value)


async def remove_node(session: AsyncSession, node_id: int) -> bool:
    node = await session.get(NodeRow, node_id, with_for_update=True)
    if node is None:
        return False
    if node.state != "offline":
        msg = "only offline nodes may be removed"
        raise NodeRemovalError(msg)
    active = await session.scalar(select(Job.id).where(Job.node_id == node_id, Job.status.in_(_ACTIVE_VALUES)).limit(1))
    if active is not None:
        msg = "node owns an active job"
        raise NodeRemovalError(msg)
    removed = await session.scalar(
        update(NodeRow)
        .where(
            NodeRow.id == node_id,
            NodeRow.name == node.name,
            NodeRow.token_hash == node.token_hash,
            NodeRow.state == "offline",
            ~select(Job.id).where(Job.node_id == node_id, Job.status.in_(_ACTIVE_VALUES)).exists(),
        )
        .values(state=NodeRow.state, updated_at=NodeRow.updated_at)
        .returning(NodeRow.id)
    )
    if removed is None:
        raise NodeRemovalError("node changed, connected, or acquired work during removal")
    await session.execute(update(Job).where(Job.node_id == node_id).values(node_id=None))
    await session.execute(update(JobAttempt).where(JobAttempt.node_id == node_id).values(node_id=None))
    await session.execute(delete(NodeRow).where(NodeRow.id == node_id))
    return True


def _effective(now: datetime | None) -> datetime:
    return now if now is not None else utcnow()


def _seconds(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


async def _record(
    session: AsyncSession, job_id: int, event_type: str, payload: dict, ts: datetime, attempt_id: int | None = None
) -> None:
    event = JobEvent(job_id=job_id, attempt_id=attempt_id, type=event_type, payload=payload, ts=ts)
    session.add(event)
    if event_type not in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
        return
    job = await get_job(session, job_id)
    if job is None or (job.kind == "healthcheck" and event_type != JobStatus.FAILED.value):
        return
    if event_type == JobStatus.SUCCEEDED.value and attempt_id is not None:
        await record_publication_notifications(session, job, attempt_id)
    if not job.pre_actions:
        return
    await session.flush()
    targets = dict.fromkeys(
        target
        for action in job.pre_actions
        if action.get("kind") == "notify" and f"job.{event_type}" in action.get("on", [])
        for target in action.get("targets", [])
    )
    session.add_all(
        NotificationDelivery(job_id=job_id, job_event_id=event.id, target_name=target, status="pending")
        for target in targets
    )


async def current_attempt(session: AsyncSession, job_id: int) -> JobAttempt | None:
    stmt = (
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id)
        .order_by(JobAttempt.attempt_number.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def enqueue_file_job(
    session: AsyncSession,
    media_file_id: int,
    library_id: int,
    plan: FFmpegPlan,
    decision_log: list[dict] | None = None,
    kind: str = "transcode",
    priority: int = 0,
    now: datetime | None = None,
    continuation: FlowContinuation | None = None,
    facets: dict[str, FacetValue] | None = None,
    pre_actions: list[Action] | None = None,
    post_actions: list[Action] | None = None,
    requires: list[str] | None = None,
    requires_encoders: list[str] | None = None,
    flow_snapshot: dict | None = None,
    hold_until: datetime | None = None,
    source: dict | None = None,
    probe: ProbeData | None = None,
) -> Job:
    return await _enqueue(
        session,
        media_file_id,
        library_id,
        kind=kind,
        plan=plan,
        decision_log=decision_log,
        priority=priority,
        now=now,
        continuation=continuation,
        facets=facets,
        pre_actions=pre_actions,
        post_actions=post_actions,
        requires=requires,
        requires_encoders=requires_encoders,
        flow_snapshot=flow_snapshot,
        hold_until=hold_until,
        source=source,
        probe=probe,
    )


async def enqueue_healthcheck_job(
    session: AsyncSession,
    media_file_id: int,
    library_id: int,
    priority: int = 0,
    now: datetime | None = None,
) -> Job:
    job = await _enqueue(session, media_file_id, library_id, kind="healthcheck", priority=priority, now=now)
    # Queueing also covers a publication refresh interrupted before it could queue its check.
    covered = select(Job.id).where(
        Job.media_file_id == media_file_id,
        Job.library_id == library_id,
        Job.kind == "transcode",
        Job.status == JobStatus.SUCCEEDED.value,
    )
    await session.execute(
        update(JobEvent)
        .where(JobEvent.type == "artifact_check_pending", JobEvent.job_id.in_(covered))
        .values(type="artifact_check_finished")
    )
    return job


async def finish_artifact_check(session: AsyncSession, *, attempt_id: int, library_id: int) -> None:
    current_attempt = (
        select(JobAttempt.id)
        .join(Job, Job.id == JobAttempt.job_id)
        .where(
            JobAttempt.id == attempt_id,
            JobAttempt.attempt_number == Job.attempts,
            JobAttempt.status == JobStatus.SUCCEEDED.value,
            Job.status == JobStatus.SUCCEEDED.value,
            Job.library_id == library_id,
            Job.id == JobEvent.job_id,
        )
    )
    await session.execute(
        update(JobEvent)
        .where(
            JobEvent.attempt_id == attempt_id,
            JobEvent.type == "artifact_check_pending",
            exists(current_attempt),
        )
        .values(type="artifact_check_finished")
    )


async def record_flow_decision(
    session: AsyncSession,
    media_file_id: int,
    library_id: int,
    decision_log: list[dict],
    reason: str,
    flow_snapshot: dict | None = None,
    now: datetime | None = None,
    *,
    status: str,
    source: dict,
    first_seen: datetime,
) -> Job | None:
    ts = _effective(now)
    if not {"path", "size"}.issubset(source):
        raise ValueError("Source is missing. Rescan and enqueue a new job.")
    library = await _queue_library(session, library_id)
    media = await session.scalar(
        select(MediaFile)
        .where(
            MediaFile.id == media_file_id,
            MediaFile.library_id == library_id,
            MediaFile.path == source["path"],
            MediaFile.size == source["size"],
            MediaFile.first_seen == first_seen,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if media is None:
        raise ValueError("Source changed during evaluation. Try again.")
    await check_file_idle(session, media.id)
    check_source_root(session, media, library)
    if status != "skipped":
        await set_transcode_result(
            session,
            media_file_id,
            {
                "error": TranscodeStatus.FAILED,
                "vetoed": TranscodeStatus.REJECTED,
                "matched": TranscodeStatus.NOT_NEEDED,
            }[status],
        )
        return None
    return await _insert_skipped(
        session,
        media_file_id,
        library_id,
        decision_log=decision_log,
        reason=reason,
        flow_snapshot=flow_snapshot,
        now=ts,
    )


async def _insert_skipped(
    session: AsyncSession,
    media_file_id: int,
    library_id: int,
    *,
    decision_log: list[dict],
    reason: str,
    flow_snapshot: dict | None,
    now: datetime,
) -> Job:
    await session.execute(update(MediaFile).where(MediaFile.id == media_file_id).values(transcode_decided=True))
    await set_transcode_result(session, media_file_id, TranscodeStatus.NOT_NEEDED)
    job = Job(
        media_file_id=media_file_id,
        library_id=library_id,
        kind="transcode",
        priority=0,
        status=JobStatus.SKIPPED.value,
        decision_log=decision_log,
        flow_snapshot=flow_snapshot,
        created_at=now,
        finished_at=now,
    )
    session.add(job)
    await session.flush()
    await _record(session, job.id, JobStatus.SKIPPED.value, {"status": job.status, "reason": reason}, now)
    await session.flush()
    return job


async def apply_decision(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    snapshot: ProbeResult,
    evaluation: FlowResult,
    flow_snapshot: dict,
    job_id: int,
    request_id: int,
) -> DecisionResult:
    operation = asyncio.create_task(
        _apply_decision(
            sessionmaker,
            snapshot=snapshot,
            evaluation=evaluation,
            flow_snapshot=flow_snapshot,
            job_id=job_id,
            request_id=request_id,
        )
    )
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            if not cancelled:
                operation.cancel()
            cancelled = True
        except Exception:
            break
    try:
        result = operation.result()
    except ProbeError as exc:
        raise ValueError("Current file changed during evaluation. Try again.") from exc
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _apply_decision(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    snapshot: ProbeResult,
    evaluation: FlowResult,
    flow_snapshot: dict,
    job_id: int,
    request_id: int,
) -> DecisionResult:
    media = snapshot.media
    if media.source_identity is None:
        raise ValueError("Source identity is unknown. Refresh the current file before evaluating it.")
    async with sessionmaker() as session:
        await session.get(Library, media.library_id, populate_existing=True, with_for_update={"read": True})
        library = await authorized_library(session, media.library_id, media)
        if library is None:
            raise ValueError("Library no longer authorizes this source.")
        library_version, roots = library.updated_at, list(library.roots)
        if await claim_catalog(session, media) is None:
            raise ValueError("Current file changed during evaluation. Try again.")
        current = await wait_workspace_io(lambda: capture_source_path(Path(media.path)))
        if current["size"] != media.size:
            raise ValueError("Current file changed during evaluation. Try again.")
        current_library_id = await session.scalar(
            select(Library.id)
            .where(
                Library.id == library.id,
                Library.updated_at == library_version,
                Library.roots == roots,
                Library.suspended.is_(False),
                Library.deleted.is_(False),
            )
            .with_for_update(read=True)
        )
        if current_library_id is None:
            raise ValueError("Library changed during evaluation. Try again.")
        event = await current_request(session, job_id, request_id)
        job = await session.get(Job, job_id, populate_existing=True)
        assert job is not None
        if job.media_file_id != media.id or job.library_id != library.id:
            raise ValueError("Job no longer owns this catalog source.")
        if "flow_snapshot" not in event.payload:
            flow = (
                await session.get(Flow, library.flow_id, populate_existing=True, with_for_update={"read": True})
                if library.flow_id is not None
                else None
            )
            if flow is None or flow.id != flow_snapshot["id"] or flow.version != flow_snapshot["version"]:
                raise ValueError("Library flow changed during evaluation. Try again.")
        await check_file_idle(session, media.id, excluding_job=job.id)
        retry = event.payload["operation"] == "retry"
        decisions = [decision_entry(step) for step in evaluation.steps]
        reason = evaluation.reason or next(
            (step.error or step.reason for step in reversed(evaluation.steps) if step.error or step.reason), ""
        )
        healthcheck: Job | None = None
        queued = evaluation.status == "matched" and evaluation.plan is not None
        if evaluation.status == "matched" and evaluation.plan is not None:
            if retry:
                previous = {
                    key: getattr(job, key)
                    for key in (
                        "plan",
                        "flow_snapshot",
                        "decision_log",
                        "continuation",
                        "facets",
                        "pre_actions",
                        "post_actions",
                        "requires",
                        "requires_encoders",
                    )
                }
                if await _restart_job(session, job, utcnow(), previous=previous, request_id=request_id) is None:
                    raise ValueError("Job request was cancelled, replaced, or is no longer pending.")
            else:
                job.status = JobStatus.STAGING
                await set_transcode_result(session, media.id, TranscodeStatus.PENDING)
            job.plan = evaluation.plan.model_dump(mode="json")
            job.flow_snapshot = flow_snapshot
            job.decision_log = decisions
            job.continuation = evaluation.continuation.model_dump(mode="json") if evaluation.continuation else None
            job.facets = dict(evaluation.facets)
            job.pre_actions = [action.model_dump(mode="json") for action in evaluation.actions]
            job.post_actions = [action.model_dump(mode="json") for action in evaluation.post_actions]
            job.requires = list(dict.fromkeys(evaluation.requires))
            job.requires_encoders = list(dict.fromkeys(evaluation.requires_encoders))
            attempt = await current_attempt(session, job.id)
            await _record(
                session,
                job.id,
                "enqueued",
                {"source": snapshot.source, "probe": snapshot.probe.model_dump(mode="json")},
                utcnow(),
                attempt.id if attempt else None,
            )
            await session.execute(update(MediaFile).where(MediaFile.id == media.id).values(transcode_decided=True))
        else:
            if retry:
                restore_retry_result(job, event)
            else:
                job.flow_snapshot = flow_snapshot
                job.decision_log = decisions
                await finish(
                    session,
                    job.id,
                    JobStatus.FAILED if evaluation.status == "error" else JobStatus.SKIPPED,
                    error=reason if evaluation.status == "error" else None,
                    update_result=False,
                )
            await session.flush()
            await _record(
                session,
                job.id,
                "requeue_evaluated" if retry else "evaluated",
                {
                    "status": evaluation.status,
                    "reason": reason,
                    "flow_snapshot": flow_snapshot,
                    "decision_log": decisions,
                    "source": snapshot.source,
                    "probe": snapshot.probe.model_dump(mode="json"),
                },
                utcnow(),
            )
            await set_transcode_result(
                session,
                media.id,
                {
                    "error": TranscodeStatus.FAILED,
                    "vetoed": TranscodeStatus.REJECTED,
                    "matched": TranscodeStatus.NOT_NEEDED,
                    "skipped": TranscodeStatus.NOT_NEEDED,
                }[evaluation.status],
            )
            if evaluation.status == "skipped" or not retry:
                await session.execute(update(MediaFile).where(MediaFile.id == media.id).values(transcode_decided=True))
            if (
                library.healthcheck_enabled
                and processing_allowed(library, media)
                and media.health_status == "pending"
                and media.probe_id is not None
            ):
                healthcheck = await _insert_job(
                    session, media.id, media.library_id, kind="healthcheck", priority=0, now=None
                )
        decision = {"status": evaluation.status, "reason": reason, "steps": decisions}
        record_request_outcome(
            session,
            job,
            event,
            outcome="queued" if queued else "failed" if evaluation.status == "error" else "not_queued",
            reason=reason or ("Job queued." if queued else "No transcode plan selected."),
            decision=decision,
        )
        await session.commit()
        return DecisionResult(job_id=job.id, healthcheck_id=healthcheck.id if healthcheck is not None else None)


async def capture_source(media: MediaFile) -> dict:
    return await wait_workspace_io(lambda: _capture_source(media))


def _capture_source(media: MediaFile) -> dict:
    if media.source_identity is None:
        raise ValueError("Source identity is unknown. Refresh the current file before enqueuing a new job.")
    source = {"path": media.path, "size": media.size}
    path = Path(media.path)
    try:
        current = capture_source_path(path)
    except OSError:
        return source  # Missing sources retain the ordinary staging cancellation behavior.
    return current


async def _queue_library(session: AsyncSession, library_id: int) -> Library:
    library = await session.scalar(
        select(Library)
        .where(Library.id == library_id, Library.suspended.is_(False), Library.deleted.is_(False))
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if library is None:
        raise ValueError("Library is suspended. Review its settings and resume it before enqueuing.")
    return library


async def _enqueue(
    session: AsyncSession,
    media_file_id: int,
    library_id: int,
    *,
    kind: str,
    plan: FFmpegPlan | None = None,
    decision_log: list[dict] | None = None,
    continuation: FlowContinuation | None = None,
    facets: dict[str, FacetValue] | None = None,
    pre_actions: list[Action] | None = None,
    post_actions: list[Action] | None = None,
    requires: list[str] | None = None,
    requires_encoders: list[str] | None = None,
    flow_snapshot: dict | None = None,
    priority: int,
    now: datetime | None,
    hold_until: datetime | None = None,
    source: dict | None = None,
    probe: ProbeData | None = None,
) -> Job:
    library = await _queue_library(session, library_id)
    media = await session.get(MediaFile, media_file_id, populate_existing=True, with_for_update=True)
    if media is None or media.library_id != library.id:
        raise ValueError("Source is missing.")
    if await active_job_for_file(session, media_file_id) is not None:
        msg = f"media file {media_file_id} already has an active job"
        raise ActiveJobError(msg)
    if kind == "healthcheck":
        media = await get_media_file(session, media_file_id)
        if media is None:
            raise ValueError("Source is missing.")
        hold_until = cooldown_hold_until(library.cooldown_s, media.first_seen, _effective(now))
    if kind == "transcode":
        await check_file_idle(session, media_file_id)
        media = await get_media_file(session, media_file_id)
        if media is None:
            raise ValueError("Source is missing.")
        check_source_root(session, media, library)
        if source is None:
            source = await capture_source(media)
        if probe is None and media.probe_id is not None:
            stored = await session.get(ProbeRow, media.probe_id)
            if stored is not None:
                probe = probe_from_raw(Path(media.path), media.size, json.loads(stored.raw_json))
    return await _insert_job(
        session,
        media_file_id,
        library_id,
        kind=kind,
        plan=plan,
        decision_log=decision_log,
        continuation=continuation,
        facets=facets,
        pre_actions=pre_actions,
        post_actions=post_actions,
        requires=requires,
        requires_encoders=requires_encoders,
        flow_snapshot=flow_snapshot,
        priority=priority,
        now=now,
        hold_until=hold_until,
        source=source,
        probe=probe,
    )


async def _insert_job(
    session: AsyncSession,
    media_file_id: int,
    library_id: int,
    *,
    kind: str,
    plan: FFmpegPlan | None = None,
    decision_log: list[dict] | None = None,
    continuation: FlowContinuation | None = None,
    facets: dict[str, FacetValue] | None = None,
    pre_actions: list[Action] | None = None,
    post_actions: list[Action] | None = None,
    requires: list[str] | None = None,
    requires_encoders: list[str] | None = None,
    flow_snapshot: dict | None = None,
    priority: int,
    now: datetime | None,
    hold_until: datetime | None = None,
    source: dict | None = None,
    probe: ProbeData | None = None,
) -> Job:
    ts = _effective(now)
    if hold_until is not None:
        status = JobStatus.HOLD.value
    elif kind in {"healthcheck", "probe", "mkv_stats"}:
        status = JobStatus.QUEUED.value
    else:
        status = JobStatus.STAGING.value
    job = Job(
        media_file_id=media_file_id,
        library_id=library_id,
        kind=kind,
        priority=priority,
        status=status,
        plan=plan.model_dump(mode="json") if plan is not None else None,
        decision_log=decision_log,
        flow_snapshot=flow_snapshot,
        continuation=continuation.model_dump(mode="json") if continuation is not None else None,
        facets=dict(facets) if facets is not None else None,
        pre_actions=[action.model_dump(mode="json") for action in pre_actions] if pre_actions is not None else None,
        post_actions=[action.model_dump(mode="json") for action in post_actions] if post_actions is not None else None,
        requires=list(dict.fromkeys(requires)) if requires else None,
        requires_encoders=list(dict.fromkeys(requires_encoders)) if requires_encoders else None,
        hold_until=hold_until,
        created_at=ts,
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError as exc:
        if not isinstance(exc.orig, UniqueViolation) or exc.orig.diag.constraint_name != "uq_jobs_active_media_file":
            raise
        msg = f"media file {media_file_id} already has an active job"
        raise ActiveJobError(msg) from exc
    if kind == "transcode":
        await session.execute(update(MediaFile).where(MediaFile.id == media_file_id).values(transcode_decided=True))
        await set_transcode_result(session, media_file_id, TranscodeStatus.PENDING)
    payload = {"source": source} if source else {}
    if probe is not None:
        payload["probe"] = probe.model_dump(mode="json")
    await _record(session, job.id, "enqueued", payload, ts)
    if hold_until is not None:
        await _record(session, job.id, "held", {"until": hold_until.isoformat(), "reason": "library cooldown"}, ts)
    if kind in {"healthcheck", "probe", "mkv_stats"} and hold_until is None:
        await _record(session, job.id, "queued", {}, ts)
    return job


def cooldown_hold_until(cooldown_s: int, first_seen: datetime | None, now: datetime) -> datetime | None:
    if cooldown_s <= 0 or first_seen is None:
        return None
    until = first_seen.replace(tzinfo=UTC) + timedelta(seconds=cooldown_s)
    return until if until > now else None


async def release_expired_holds(session: AsyncSession, now: datetime, *, kinds: set[str] | None = None) -> list[Job]:
    statement = select(Job).where(Job.status == JobStatus.HOLD.value, Job.hold_until <= now)
    if kinds is not None:
        statement = statement.where(Job.kind.in_(kinds))
    statement = statement.order_by(Job.hold_until, Job.id) if kinds else statement.order_by(Job.id)
    statement = statement.limit(QUERY_BATCH)
    jobs = list((await session.execute(statement)).scalars())
    released = []
    for job in jobs:
        if job.kind in {"healthcheck", "mkv_stats"}:
            library = await session.get(Library, job.library_id)
            media = await get_media_file(session, job.media_file_id)
            if library is not None and media is not None:
                until = cooldown_hold_until(library.cooldown_s, media.first_seen, now)
                if until is not None:
                    await session.execute(
                        update(Job)
                        .where(Job.id == job.id, Job.status == JobStatus.HOLD.value, Job.hold_until <= now)
                        .values(hold_until=until)
                        .execution_options(synchronize_session=False)
                    )
                    continue
        won = await session.scalar(
            update(Job)
            .where(Job.id == job.id, Job.status == JobStatus.HOLD.value, Job.hold_until <= now)
            .values(status=JobStatus.STAGING.value if job.kind == "transcode" else JobStatus.QUEUED.value)
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if won is None:
            continue
        await session.refresh(job)
        await _record(session, job.id, "released", {}, now)
        released.append(job)
    await session.flush()
    return released


async def mark_staged(session: AsyncSession, job_id: int, now: datetime | None = None) -> bool:
    job = await get_job(session, job_id)
    if job is None or job.status != JobStatus.STAGING.value:
        return False
    ts = _effective(now)
    won = await session.scalar(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.STAGING.value)
        .values(status=JobStatus.QUEUED.value)
        .returning(Job.id)
    )
    if won is None:
        return False
    await _record(session, job_id, "staged", {}, ts)
    await _record(session, job_id, "queued", {}, ts)
    await session.flush()
    return True


async def claim(
    session: AsyncSession,
    job_id: int,
    node_id: int,
    lease_ttl_s: float = 300.0,
    now: datetime | None = None,
    *,
    stage: bool = False,
    clock: Callable[[], datetime] | None = None,
    timezone: tzinfo = UTC,
) -> Job | None:
    candidate = await lock_job_source(session, job_id, skip_locked=True)
    if candidate is None:
        return None
    library = await session.get(Library, candidate.library_id)
    media = await session.get(MediaFile, candidate.media_file_id)
    if (
        library is None
        or media is None
        or library.suspended
        or library.deleted
        or candidate.status != (JobStatus.RUNNING.value if stage else JobStatus.QUEUED.value)
        or candidate.delete_requested
        or (stage and candidate.node_id is not None)
    ):
        return None
    ts = clock() if clock is not None else _effective(now)
    if not active(parse_schedule(library.schedule), ts, timezone):
        return None
    try:
        check_source_root(session, media, library)
    except ValueError:
        return None
    won = await session.scalar(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == (JobStatus.RUNNING.value if stage else JobStatus.QUEUED.value),
            Job.delete_requested.is_(False),
            *([Job.node_id.is_(None)] if stage else []),
            Job.library_id.in_(select(Library.id).where(Library.suspended.is_(False))),
            Job.id.not_in(post_action_jobs()),
            or_(
                Job.kind != "transcode",
                Job.media_file_id.not_in(select(Job.media_file_id).where(Job.id.in_(post_action_jobs()))),
            ),
        )
        .values(
            status=JobStatus.CLAIMED.value,
            node_id=node_id,
            lease_expires_at=ts + _seconds(lease_ttl_s),
            attempts=Job.attempts + (0 if stage else 1),
            started_at=func.coalesce(Job.started_at, ts),
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    if won is None:
        return None
    job = (
        await session.execute(select(Job).where(Job.id == job_id).execution_options(populate_existing=True))
    ).scalar_one()
    job.lease_expires_at = ts + _seconds(lease_ttl_s)
    node = await session.get(NodeRow, node_id)
    if stage:
        attempt = await current_attempt(session, job_id)
        assert attempt is not None
        attempt.node_id = node_id
        attempt.node_name = node.display_name if node is not None else None
        attempt.capabilities = dict(node.capabilities) if node is not None else None
        await session.flush()
        return job
    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=job.attempts,
        node_id=node_id,
        node_name=node.display_name if node is not None else None,
        capabilities=dict(node.capabilities) if node is not None else None,
        workload=job.kind,
        status=JobStatus.CLAIMED.value,
        claimed_at=ts,
    )
    session.add(attempt)
    await session.flush()
    await _record(session, job_id, "claimed", {"node_id": node_id}, ts, attempt.id)
    await session.flush()
    return job


async def transition(session: AsyncSession, job_id: int, status: JobStatus, now: datetime | None = None) -> bool:
    if status not in {JobStatus.VERIFYING, JobStatus.FINALIZING}:
        msg = f"invalid intermediate status: {status!r}"
        raise ValueError(msg)
    job = await session.get(Job, job_id, populate_existing=True, with_for_update=True)
    attempt = await current_attempt(session, job_id)
    if job is None or attempt is None or job.status in _TERMINAL_VALUES:
        return False
    ts = _effective(now)
    job.status = status.value
    attempt.status = status.value
    await _record(session, job_id, status.value, {}, ts, attempt.id)
    await session.flush()
    return True


async def set_transcode_result(session: AsyncSession, media_file_id: int, status: TranscodeStatus) -> None:
    # A failed reprocessing attempt doesn't undo the current file's verified publication.
    await session.execute(
        update(MediaFile)
        .where(MediaFile.id == media_file_id, MediaFile.transcode_status != TranscodeStatus.TRANSCODED.value)
        .values(transcode_status=status.value)
    )


async def mark_transcode_cancelled(session: AsyncSession, job: Job) -> None:
    if job.kind != "healthcheck":
        return
    # Cancelling a required healthcheck marks its waiting transcode result cancelled.
    # Standalone checks and a new source generation must not change the result.
    await session.execute(
        update(MediaFile)
        .where(
            MediaFile.id == job.media_file_id,
            MediaFile.library_id == job.library_id,
            MediaFile.transcode_decided.is_(True),
            MediaFile.transcode_status == TranscodeStatus.PENDING.value,
            MediaFile.health_status == HealthStatus.PENDING.value,
        )
        .values(transcode_status=TranscodeStatus.CANCELLED.value)
    )


async def finish(
    session: AsyncSession,
    job_id: int,
    status: JobStatus,
    error: str | None = None,
    stderr_tail: str | None = None,
    source_size: int | None = None,
    output_size: int | None = None,
    argv_hash: str | None = None,
    output_probe: dict | None = None,
    health: dict | None = None,
    action_results: list | None = None,
    now: datetime | None = None,
    *,
    warning: str | None = None,
    update_result: bool = True,
) -> bool:
    """
    The caller commits the terminal result.

    Omitting warning clears the previous warning.
    """
    if status not in TERMINAL_STATUSES:
        msg = f"finish requires a terminal status, got {status!r}"
        raise ValueError(msg)
    job = await lock_job_source(session, job_id)
    if job is None or job.status in _TERMINAL_VALUES:
        return False
    ts = _effective(now)
    job.status = status.value
    job.error = error
    job.warning = warning
    job.stderr_tail = stderr_tail
    job.finished_at = ts
    if update_result and job.kind == "transcode" and status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        await set_transcode_result(session, job.media_file_id, TranscodeStatus(status.value))
    if update_result and status == JobStatus.CANCELLED:
        await mark_transcode_cancelled(session, job)
    attempt = await current_attempt(session, job_id)
    if attempt is not None and (
        attempt.attempt_number != job.attempts
        or attempt.status not in {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING}
    ):
        attempt = None
    if attempt is not None:
        attempt.status = status.value
        attempt.result = status.value
        attempt.error = error
        attempt.source_size = source_size
        attempt.output_size = output_size
        attempt.argv_hash = argv_hash
        attempt.output_probe = output_probe
        attempt.health = health
        attempt.action_results = action_results
        attempt.finished_at = ts
    await _record(
        session,
        job_id,
        status.value,
        {"status": status.value, "error": error, "source_size": source_size, "output_size": output_size},
        ts,
        attempt.id if attempt else None,
    )
    await session.flush()
    return True


async def cancel_pending(session: AsyncSession, job_id: int, now: datetime | None = None) -> Job | None:
    await lock_job_source(session, job_id)
    ts = _effective(now)
    won = await session.scalar(
        update(Job)
        .where(
            Job.id == job_id, Job.status.in_([JobStatus.QUEUED.value, JobStatus.STAGING.value, JobStatus.HOLD.value])
        )
        .values(status=JobStatus.CANCELLED.value, error="cancelled", warning=None, finished_at=ts)
        .returning(Job.id)
    )
    if won is None:
        return None
    job = await get_job(session, job_id)
    if job is not None and job.kind == "transcode":
        await set_transcode_result(session, job.media_file_id, TranscodeStatus.CANCELLED)
    if job is not None:
        await mark_transcode_cancelled(session, job)
    if job is not None and job.kind in {"probe", "mkv_stats"}:
        from server.metadata_jobs import cancel_metadata

        await cancel_metadata(session, job)
    await _record(
        session,
        job_id,
        JobStatus.CANCELLED.value,
        {"status": JobStatus.CANCELLED.value, "error": "cancelled"},
        ts,
        None,
    )
    await session.flush()
    return job


async def requeue(
    session: AsyncSession,
    job_id: int,
    now: datetime | None = None,
    max_attempts: int = 3,
    *,
    result: str = "interrupted",
    error: str | None = None,
) -> Job | None:
    job = await lock_job_source(session, job_id)
    if job is None or job.delete_requested or job.status not in {JobStatus.CLAIMED.value, JobStatus.RUNNING.value}:
        return None
    if job.kind in {"probe", "mkv_stats"}:
        requested = await session.scalar(
            select(JobEvent.id)
            .join(JobAttempt, JobAttempt.id == JobEvent.attempt_id)
            .where(
                JobEvent.job_id == job.id,
                JobEvent.type == "metadata_cancel_requested",
                JobAttempt.attempt_number == job.attempts,
            )
            .limit(1)
        )
        if requested is not None:
            from server.metadata_jobs import cancel_metadata

            await cancel_metadata(session, job)
            await finish(session, job.id, JobStatus.CANCELLED, error="Cancelled by operator.")
            return job
    ts = _effective(now)
    baseline = await session.scalar(
        select(JobEvent.payload["attempts"].as_integer())
        .where(JobEvent.job_id == job_id, JobEvent.type == "retried", JobEvent.payload["manual"].as_boolean().is_(True))
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    exhausted = job.attempts - (baseline or 0) >= max_attempts
    job.node_id = None
    job.lease_expires_at = None
    job.progress = None
    attempt = (
        await session.execute(
            select(JobAttempt).where(
                JobAttempt.job_id == job_id,
                JobAttempt.attempt_number == job.attempts,
                JobAttempt.status.in_([JobStatus.CLAIMED.value, JobStatus.RUNNING.value]),
            )
        )
    ).scalar_one_or_none()
    if attempt is not None:
        attempt.status = JobStatus.FAILED.value
        attempt.result = result
        attempt.error = error or ("max attempts exceeded" if exhausted else "requeued")
        attempt.finished_at = ts
    if exhausted:
        job.status = JobStatus.FAILED.value
        job.error = f"max attempts exceeded: {error}" if error else "max attempts exceeded"
        job.finished_at = ts
        if job.kind == "transcode":
            await set_transcode_result(session, job.media_file_id, TranscodeStatus.FAILED)
        await _record(
            session, job_id, "failed", {"status": job.status, "error": job.error}, ts, attempt.id if attempt else None
        )
    else:
        job.status = JobStatus.STAGING.value if job.kind == "transcode" else JobStatus.QUEUED.value
        job.error = None
        job.warning = error
        job.started_at = None
        job.finished_at = None
        await _record(
            session,
            job_id,
            "retried",
            {"error": error, "result": result} if error else {},
            ts,
            attempt.id if attempt else None,
        )
    await session.flush()
    return job


async def requeue_startup(session: AsyncSession, now: datetime, max_attempts: int = 3) -> int:
    requeued = after = 0
    while True:
        ids = list(
            await session.scalars(
                select(Job.id)
                .where(
                    Job.kind.in_(["healthcheck", "probe", "mkv_stats"]),
                    Job.node_id.is_(None),
                    Job.status.in_([JobStatus.CLAIMED.value, JobStatus.RUNNING.value]),
                    Job.id > after,
                )
                .order_by(Job.id)
                .limit(QUERY_BATCH)
            )
        )
        if not ids:
            break
        after = ids[-1]
        for job_id in ids:
            if await requeue(session, job_id, now=now, max_attempts=max_attempts) is not None:
                requeued += 1
    return requeued


async def active_job_for_file(session: AsyncSession, media_file_id: int) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.media_file_id == media_file_id, Job.status.in_(_ACTIVE_VALUES))
        .order_by(Job.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_job(session: AsyncSession, job_id: int) -> Job | None:
    return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()


async def get_media_file(session: AsyncSession, media_file_id: int) -> MediaFile | None:
    return (await session.execute(select(MediaFile).where(MediaFile.id == media_file_id))).scalar_one_or_none()


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_jobs(
    session: AsyncSession,
    *,
    statuses: list[str] | None = None,
    kind: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> tuple[list[Job], int]:
    stmt = select(Job)
    if query or sort_by == "path":
        stmt = stmt.outerjoin(MediaFile, Job.media_file_id == MediaFile.id)
    if query:
        term = query.strip()
        # Keep numeric filenames searchable without binding oversized integer IDs.
        if match := re.fullmatch(r"(job_id|file_id):\s*([0-9]{1,10})", term, flags=re.IGNORECASE):
            column = Job.id if match[1].lower() == "job_id" else Job.media_file_id
            stmt = stmt.where(column == int(match[2]))
        else:
            clause = MediaFile.path.ilike(_like_pattern(query), escape="\\")
            if re.fullmatch(r"[0-9]{1,10}", term):
                clause = or_(Job.id == int(term), clause)
            stmt = stmt.where(clause)
    if statuses:
        stmt = stmt.where(Job.status.in_(set(statuses) & {status.value for status in JobStatus}))
    if kind is not None:
        stmt = stmt.where(Job.kind == kind)
    total = int(
        await session.scalar(stmt.with_only_columns(func.count(), maintain_column_froms=True).order_by(None)) or 0
    )
    columns = {
        "id": Job.id,
        "created_at": Job.created_at,
        "updated_at": Job.updated_at,
        "priority": Job.priority,
        "status": Job.status,
        "path": MediaFile.path,
    }
    column = columns[sort_by]
    ordering = column.asc() if sort_dir == "asc" else column.desc()
    tie = Job.id.asc() if sort_dir == "asc" else Job.id.desc()
    nulls = [column.is_(None)] if sort_by == "path" else []
    rows = list(
        (
            await session.scalars(
                stmt.order_by(*nulls, ordering, tie)
                .limit(limit)
                .offset(offset)
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
        ).all()
    )
    return rows, total


async def create_library(
    session: AsyncSession,
    *,
    name: str,
    roots: list[str],
    cooldown_s: int = 0,
    schedule: dict | None = None,
    watch: bool = False,
    suspended: bool = False,
    scan_on_start: bool = False,
    scan_interval_s: int | None = None,
    mkv_stats: bool = False,
    healthcheck_enabled: bool = False,
    processing_cutoff: dict | None = None,
    trash_mode: str = "none",
    trash_retention_days: int = 30,
    flow_id: int | None = None,
) -> Library:
    if trash_retention_days < 1:
        msg = "trash_retention_days must be at least 1"
        raise ValueError(msg)
    if schedule is not None:
        parse_schedule(schedule)
    if scan_interval_s is not None and scan_interval_s <= 0:
        raise ValueError("scan_interval_s must be positive")
    await _check_flow_id(session, flow_id)
    library = Library(
        name=name,
        roots=roots,
        cooldown_s=cooldown_s,
        schedule=schedule,
        watch=watch,
        suspended=suspended,
        scan_on_start=scan_on_start,
        scan_interval_s=scan_interval_s,
        mkv_stats=mkv_stats,
        healthcheck_enabled=healthcheck_enabled,
        processing_cutoff={} if processing_cutoff is None else processing_cutoff,
        trash_mode=trash_mode,
        trash_retention_days=trash_retention_days,
        flow_id=flow_id,
    )
    session.add(library)
    await session.flush()
    return library


async def update_library(session: AsyncSession, library_id: int, fields: dict) -> Library | None:
    library = await session.get(Library, library_id, populate_existing=True, with_for_update=True)
    if library is None:
        return None
    if library.deleted:
        raise ValueError("Library is being deleted. Its settings cannot be changed.")
    if "schedule" in fields and fields["schedule"] is not None:
        parse_schedule(fields["schedule"])
    if fields.get("scan_interval_s") is not None and fields["scan_interval_s"] <= 0:
        raise ValueError("scan_interval_s must be positive")
    if fields.get("flow_id") is not None:
        await _check_flow_id(session, fields["flow_id"])
    if "trash_retention_days" in fields and fields["trash_retention_days"] < 1:
        msg = "trash_retention_days must be at least 1"
        raise ValueError(msg)
    for key, value in fields.items():
        setattr(library, key, value)
    await session.flush()
    return library


async def _check_flow_id(session: AsyncSession, flow_id: int | None) -> None:
    if flow_id is not None and await session.get(Flow, flow_id) is None:
        msg = f"flow {flow_id} does not exist"
        raise ValueError(msg)


async def get_library(session: AsyncSession, library_id: int) -> Library | None:
    return (await session.execute(select(Library).where(Library.id == library_id))).scalar_one_or_none()


async def list_libraries(session: AsyncSession) -> list[Library]:
    return list((await session.execute(select(Library).order_by(Library.id))).scalars())


async def job_events(session: AsyncSession, job_id: int) -> list[JobEvent]:
    stmt = select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
    return list((await session.execute(stmt)).scalars())


async def retry_job(session: AsyncSession, job_id: int, now: datetime | None = None, *, request_id: int) -> Job | None:
    locked_job = await lock_job_source(session, job_id)
    if locked_job is None:
        return None
    library = await session.get(Library, locked_job.library_id, populate_existing=True)
    media = await session.get(MediaFile, locked_job.media_file_id, populate_existing=True)
    event = await current_request(session, job_id, request_id)
    job = await get_job(session, job_id)
    assert job is not None
    previous_status = event.payload["previous"]["status"]
    if library is None or library.suspended or library.deleted:
        raise ValueError("Library is unavailable or suspended.")
    library_version, roots = library.updated_at, list(library.roots)
    await session.refresh(job)
    if media is None:
        raise ValueError("Source is missing.")
    await wait_workspace_io(lambda: check_source_root(session, media, library))
    if await session.scalar(post_action_jobs(scoped=True).where(Job.id == job_id)) is not None:
        raise ActiveJobError("Job has unfinished post-actions. Finish them before requeuing.")
    ts = _effective(now)
    if job.kind == "transcode":
        await check_file_idle(session, job.media_file_id, excluding_job=job_id)
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
        stale = published is not None or previous_status == JobStatus.SUCCEEDED.value
        # Preparation can fail before a plan and its input have been captured.
        stale |= job.plan is None or enqueued is None or not enqueued.payload.get("source")
        if media is not None and not stale:
            path = Path(media.path)
            try:
                identity = await capture_source(media)
                captured_source = enqueued.payload.get("source") if enqueued is not None else None
                if captured_source:
                    stale |= captured_source.get("path") != media.path or any(
                        captured_source[key] != identity[key] for key in ("path", "size")
                    )
            except ValueError:
                stale = True
            attempt = await current_attempt(session, job.id)
            if (
                attempt is not None
                and (enqueued is None or attempt.id != enqueued.attempt_id)
                and attempt.input_probe is not None
                and media.probe_id is not None
            ):
                stored = await session.get(ProbeRow, media.probe_id)
                if stored is not None:
                    captured = ProbeData.model_validate(attempt.input_probe).model_dump(exclude={"file_path", "raw"})
                    current = probe_from_raw(path, media.size, json.loads(stored.raw_json)).model_dump(
                        exclude={"file_path", "raw"}
                    )
                    stale |= captured != current
        if stale:
            raise StaleJobError("Current source needs a fresh probe and plan.")
    else:
        await check_file_idle(session, job.media_file_id, excluding_job=job_id)
    current_library_id = await session.scalar(
        select(Library.id)
        .where(
            Library.id == library.id,
            Library.updated_at == library_version,
            Library.roots == roots,
            Library.suspended.is_(False),
            Library.deleted.is_(False),
        )
        .with_for_update(read=True)
    )
    if current_library_id is None:
        raise ValueError("Library changed during requeue. Try again.")
    if await claim_catalog(session, media) is None:
        raise ValueError("Current file changed during requeue. Try again.")
    retried = await _restart_job(session, job, ts, request_id=request_id)
    if retried is not None:
        record_request_outcome(session, retried, event, outcome="queued", reason="Job requeued.")
    return retried


async def _restart_job(
    session: AsyncSession, job: Job, ts: datetime, *, request_id: int, previous: dict | None = None
) -> Job | None:
    job_id = job.id
    await current_request(session, job_id, request_id)
    try:
        async with session.begin_nested():
            won = await session.scalar(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.PENDING,
                    Job.id.not_in(post_action_jobs()),
                    or_(
                        Job.kind != "transcode",
                        Job.media_file_id.not_in(select(Job.media_file_id).where(Job.id.in_(post_action_jobs()))),
                    ),
                )
                .values(
                    status=JobStatus.STAGING.value if job.kind == "transcode" else JobStatus.QUEUED.value,
                    error=None,
                    warning=None,
                    stderr_tail=None,
                    progress=None,
                    node_id=None,
                    lease_expires_at=None,
                    hold_until=None,
                    started_at=None,
                    finished_at=None,
                )
                .returning(Job.id)
            )
    except IntegrityError as exc:
        if not isinstance(exc.orig, UniqueViolation) or exc.orig.diag.constraint_name != "uq_jobs_active_media_file":
            raise
        raise ActiveJobError("media file already has an active job") from exc
    if won is None:
        return None
    attempt = await current_attempt(session, job_id)
    if job.kind == "transcode":
        await set_transcode_result(session, job.media_file_id, TranscodeStatus.PENDING)
    if job.kind in {"probe", "mkv_stats"}:
        from server.metadata_jobs import retry_metadata

        media = await get_media_file(session, job.media_file_id)
        assert media is not None
        await retry_metadata(session, job, media)
    await _record(
        session,
        job_id,
        "retried",
        {"manual": True, "attempts": job.attempts, **({"previous": previous} if previous is not None else {})},
        ts,
        attempt.id if attempt else None,
    )
    await session.flush()
    return job


async def bump_job(session: AsyncSession, job_id: int, priority: int) -> Job | None:
    return await session.scalar(
        update(Job)
        .where(Job.id == job_id, Job.status.in_([JobStatus.STAGING.value, JobStatus.QUEUED.value]))
        .values(priority=priority)
        .returning(Job)
        .execution_options(populate_existing=True)
    )


async def bump_jobs(session: AsyncSession, job_ids: list[int]) -> list[Job]:
    jobs = []
    for ids in batched(dict.fromkeys(job_ids), QUERY_BATCH, strict=False):
        jobs.extend(
            await session.scalars(
                select(Job)
                .where(Job.id.in_(ids), Job.status.in_([JobStatus.STAGING.value, JobStatus.QUEUED.value]))
                .order_by(Job.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
    jobs.sort(key=lambda job: (-job.priority, job.created_at, job.id))
    if not jobs:
        return []
    highest = await session.scalar(
        select(func.max(Job.priority)).where(Job.status.in_([JobStatus.STAGING.value, JobStatus.QUEUED.value]))
    )
    priority = (highest if highest is not None else 0) + len(jobs)
    for job in jobs:
        job.priority = priority
        priority -= 1
    await session.flush()
    return jobs


async def top_queue_priority(session: AsyncSession) -> int:
    highest = await session.scalar(
        select(func.max(Job.priority)).where(Job.status.in_([JobStatus.STAGING.value, JobStatus.QUEUED.value]))
    )
    return (highest if highest is not None else 0) + 1


async def queue_depth(session: AsyncSession) -> dict:
    def depth(rows) -> dict:
        return {str(key): count for key, count in rows}

    active = Job.status.in_(_ACTIVE_VALUES)
    by_status = depth(
        (await session.execute(select(Job.status, func.count()).where(active).group_by(Job.status))).all()
    )
    by_library = depth(
        (await session.execute(select(Job.library_id, func.count()).where(active).group_by(Job.library_id))).all()
    )
    return {"by_status": by_status, "by_library": by_library}


async def jobs_finished_since(session: AsyncSession, since: datetime) -> dict:
    duration_s = func.extract("epoch", Job.finished_at - Job.started_at)
    row = (
        await session.execute(
            select(func.count(), func.avg(duration_s)).where(
                Job.status == JobStatus.SUCCEEDED.value,
                Job.started_at.is_not(None),
                Job.finished_at.is_not(None),
                Job.finished_at >= since,
            )
        )
    ).one()
    count, avg = row[0], row[1]
    return {"count": count, "avg_duration_s": round(float(avg), 3) if avg is not None else 0.0}


async def codec_distribution(session: AsyncSession) -> dict:
    rows = (
        await session.execute(
            select(func.coalesce(MediaFile.video_codec, "unknown").label("codec"), func.count()).group_by(
                MediaFile.video_codec
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def processing_backlog(
    session: AsyncSession,
    *,
    healthcheck_slots: int,
    healthchecks_on_nodes: bool = False,
    probe_slots: int = 0,
    probe_mean_s: float | None = None,
    mkv_stats_slots: int = 0,
    mkv_stats_on_nodes: bool = False,
    mkv_stats_mean_s: float | None = None,
    scanner_paused: bool = False,
    timezone: tzinfo = UTC,
    now: datetime | None = None,
) -> dict:
    """Count unfinished files once. Approximate known stages at unchanged availability."""
    ts = _effective(now)
    has_active = exists(select(Job.id).where(Job.media_file_id == MediaFile.id, Job.status.in_(_ACTIVE_VALUES)))
    automatic = processing_clause()
    pending_probe = (
        Library.suspended.is_(False)
        & MediaFile.probe_id.is_(None)
        & (MediaFile.health_status == HealthStatus.PENDING.value)
        & MediaFile.metadata_cancelled.is_(False)
    )
    pending_flow = (
        Library.suspended.is_(False)
        & automatic
        & Library.flow_id.is_not(None)
        & MediaFile.transcode_decided.is_(False)
        & MediaFile.probe_id.is_not(None)
        & MediaFile.metadata_cancelled.is_(False)
        & or_(
            (MediaFile.health_status == HealthStatus.HEALTHY.value) & MediaFile.health_source.is_not(None),
            (MediaFile.health_status == HealthStatus.PENDING.value) & Library.healthcheck_enabled.is_(True),
        )
        & ~has_active
    )
    pending_artifact = exists(
        select(JobEvent.id)
        .join(JobAttempt, JobAttempt.id == JobEvent.attempt_id)
        .join(Job, Job.id == JobEvent.job_id)
        .where(
            Job.media_file_id == MediaFile.id,
            Job.kind == "transcode",
            Job.status == JobStatus.SUCCEEDED.value,
            JobAttempt.job_id == Job.id,
            JobAttempt.status == JobStatus.SUCCEEDED.value,
            JobAttempt.attempt_number == Job.attempts,
            Job.id
            == select(func.max(Job.id))
            .where(Job.media_file_id == MediaFile.id, Job.kind == "transcode")
            .correlate(MediaFile)
            .scalar_subquery(),
            JobEvent.type == "artifact_check_pending",
        )
    )
    undecided_healthcheck = (
        Library.flow_id.is_not(None)
        & automatic
        & exists(
            select(Job.id).where(
                Job.media_file_id == MediaFile.id,
                Job.kind == "healthcheck",
                Job.status.in_(_ACTIVE_VALUES),
                or_(
                    MediaFile.transcode_decided.is_(False),
                    (MediaFile.transcode_status == TranscodeStatus.PENDING.value)
                    & Job.flow_snapshot["id"].as_integer().is_not(None),
                ),
            )
        )
    )
    probe_flow = pending_probe & automatic & Library.flow_id.is_not(None) & MediaFile.transcode_decided.is_(False)
    metadata_active = exists(
        select(Job.id).where(
            Job.media_file_id == MediaFile.id, Job.kind.in_(["probe", "mkv_stats"]), Job.status.in_(_ACTIVE_VALUES)
        )
    )
    stored_raw = exists(select(ProbeRow.id).where(ProbeRow.media_file_id == MediaFile.id))
    (
        remaining_files,
        probes,
        flows,
        undecided_checks,
        artifacts,
        uncreated_checks,
        undecided_probes,
        waiting_probes,
        stats_pending,
        unknown_stats,
    ) = (
        await session.execute(
            select(
                func.count(),
                func.sum(case((pending_probe & ~metadata_active & ~stored_raw, 1), else_=0)),
                func.sum(case((pending_flow, 1), else_=0)),
                func.sum(case((undecided_healthcheck, 1), else_=0)),
                func.sum(case((pending_artifact, 1), else_=0)),
                func.sum(
                    case(
                        (
                            pending_probe
                            & automatic
                            & (~has_active | metadata_active)
                            & Library.healthcheck_enabled.is_(True),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(case((probe_flow, 1), else_=0)),
                func.sum(case((pending_probe & ~has_active, 1), else_=0)),
                func.sum(case((pending_probe & ~metadata_active & stored_raw, 1), else_=0)),
                func.sum(
                    case(
                        (pending_probe & ~stored_raw & Library.mkv_stats.is_(True) & (MediaFile.container == "mkv"), 1),
                        else_=0,
                    )
                ),
            )
            .select_from(MediaFile)
            .join(Library, MediaFile.library_id == Library.id)
            .where(or_(has_active, pending_probe, pending_flow, pending_artifact))
        )
    ).one()
    counts = (
        await session.execute(
            select(Job.kind, Job.status, func.count(), func.count(Job.node_id))
            .where(Job.status.in_(_ACTIVE_VALUES))
            .group_by(Job.kind, Job.status)
        )
    ).all()
    outstanding: dict[str, int] = {}
    transcodes_pending = False
    preparing = 0
    held = 0
    waiting_jobs = 0
    for kind, status, count, assigned in counts:
        if status in {JobStatus.PENDING.value, JobStatus.STAGING.value, JobStatus.QUEUED.value, JobStatus.HOLD.value}:
            waiting_jobs += count
        if status == JobStatus.PENDING.value:
            preparing += count
        if status == JobStatus.HOLD.value:
            held += count
            continue
        outstanding[kind] = outstanding.get(kind, 0) + count
        if kind == "transcode" and (
            status in {JobStatus.STAGING.value, JobStatus.QUEUED.value}
            or (status == JobStatus.RUNNING.value and assigned < count)
        ):
            transcodes_pending = True
    estimate = {
        "remaining_files": remaining_files,
        "remaining_s": None,
        "estimated_finish_at": None,
        "reason": None,
    }
    if not remaining_files:
        estimate["remaining_s"] = 0
        return estimate
    if healthchecks_on_nodes or mkv_stats_on_nodes:
        node_checks, node_statistics = (
            await session.execute(
                select(
                    func.sum(NodeRow.healthcheck_limit),
                    func.sum(
                        case(
                            (NodeRow.capabilities["mkvpropedit"].as_boolean().is_(True), NodeRow.mkv_stats_limit),
                            else_=0,
                        )
                    ),
                ).where(NodeRow.state == "online", NodeRow.paused.is_(False), NodeRow.workspace_ready.is_(True))
            )
        ).one()
        if healthchecks_on_nodes:
            healthcheck_slots += int(node_checks or 0)
        if mkv_stats_on_nodes:
            mkv_stats_slots += int(node_statistics or 0)
    if scanner_paused and (waiting_probes or waiting_jobs or flows or uncreated_checks):
        estimate["reason"] = "Activity paused."
        return estimate
    if preparing:
        estimate["reason"] = "Files are awaiting job preparation."
        return estimate
    if held:
        estimate["reason"] = "Files are on hold."
        return estimate
    if artifacts:
        estimate["reason"] = "Files are awaiting artifact metadata refresh."
        return estimate
    if flows or undecided_checks or undecided_probes:
        estimate["reason"] = "Files are awaiting automatic flow decisions."
        return estimate
    if uncreated_checks:
        outstanding["healthcheck"] = outstanding.get("healthcheck", 0) + uncreated_checks
    probe_s = 0.0
    if unknown_stats:
        estimate["reason"] = "Basic probes may require MKV statistics."
        return estimate
    if probes:
        if probe_slots <= 0:
            estimate["reason"] = "No available probe capacity."
            return estimate
        if probe_mean_s is None or probe_mean_s <= 0:
            estimate["reason"] = "No recent completed probe timing history."
            return estimate
        probe_s = math.ceil(probes / probe_slots) * probe_mean_s
    if stats_pending:
        if mkv_stats_slots <= 0:
            estimate["reason"] = "No available MKV statistics capacity."
            return estimate
        if mkv_stats_mean_s is None or mkv_stats_mean_s <= 0:
            estimate["reason"] = "No recent completed MKV statistics timing history."
            return estimate
        probe_s += math.ceil(stats_pending / mkv_stats_slots) * mkv_stats_mean_s

    waiting = or_(
        Job.status.in_([JobStatus.STAGING.value, JobStatus.QUEUED.value]),
        (Job.kind == "transcode") & (Job.status == JobStatus.RUNNING.value) & Job.node_id.is_(None),
    )
    libraries = (
        await session.execute(
            select(Library.suspended, Library.schedule, Job.kind)
            .join(Job, Job.library_id == Library.id)
            .where(waiting)
            .distinct()
        )
    ).all()
    if any(suspended for suspended, _, _ in libraries):
        estimate["reason"] = "Queued work belongs to a suspended library."
        return estimate
    if any(
        kind == "transcode" and not active(parse_schedule(schedule), ts, timezone) for _, schedule, kind in libraries
    ):
        estimate["reason"] = "Queued transcodes are outside their library schedule."
        return estimate
    blocked = await session.scalar(
        select(Job.id)
        .where(
            waiting,
            Job.warning.is_not(None),
            Job.warning != "no node can claim this job: all nodes are at max transcodes",
        )
        .limit(1)
    )
    if blocked is not None:
        estimate["reason"] = "Queued work has a staging or dispatch blocker."
        return estimate

    disconnected = await session.scalar(
        select(Job.id)
        .outerjoin(NodeRow, NodeRow.id == Job.node_id)
        .where(
            Job.status.in_(
                [
                    JobStatus.CLAIMED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.VERIFYING.value,
                    JobStatus.FINALIZING.value,
                ]
            ),
            Job.node_id.is_not(None),
            or_(NodeRow.state != "online", NodeRow.workspace_ready.is_(False)),
        )
        .limit(1)
    )
    if disconnected is not None:
        estimate["reason"] = "Active work has an offline or unavailable node."
        return estimate

    duration_s = func.extract("epoch", Job.finished_at - Job.started_at)
    history = (
        await session.execute(
            select(Job.kind, func.avg(duration_s))
            .where(
                Job.status == JobStatus.SUCCEEDED.value,
                Job.finished_at >= ts - timedelta(hours=24),
                Job.finished_at <= ts,
                duration_s > 0,
            )
            .group_by(Job.kind)
        )
    ).all()
    means = {kind: float(mean) for kind, mean in history}
    if probe_mean_s is not None:
        means["probe"] = probe_mean_s
    if mkv_stats_mean_s is not None:
        means["mkv_stats"] = mkv_stats_mean_s
    capacity = {"healthcheck": healthcheck_slots, "probe": probe_slots, "mkv_stats": mkv_stats_slots}
    # Libraries that opt in add an artifact healthcheck without adding another file.
    # Estimate these as a final batch after the currently known stages drain.
    artifact_checks = int(
        await session.scalar(
            select(func.count())
            .select_from(Job)
            .join(Library, Library.id == Job.library_id)
            .join(MediaFile, MediaFile.id == Job.media_file_id)
            .where(
                Job.kind == "transcode",
                Job.status.in_(_ACTIVE_VALUES),
                Library.healthcheck_enabled.is_(True),
                automatic,
            )
        )
        or 0
    )
    if artifact_checks:
        outstanding.setdefault("healthcheck", 0)
        suspended_artifacts = await session.scalar(
            select(Job.id)
            .join(Library, Library.id == Job.library_id)
            .join(MediaFile, MediaFile.id == Job.media_file_id)
            .where(
                Job.kind == "transcode",
                Job.status.in_(_ACTIVE_VALUES),
                Library.suspended.is_(True),
                Library.healthcheck_enabled.is_(True),
                automatic,
            )
            .limit(1)
        )
        if suspended_artifacts is not None:
            estimate["reason"] = "Artifact healthchecks would wait for a suspended library."
            return estimate
    if "transcode" in outstanding:
        processing = Job.status.in_(
            [JobStatus.CLAIMED.value, JobStatus.RUNNING.value, JobStatus.VERIFYING.value, JobStatus.FINALIZING.value]
        )
        capacity["transcode"] = int(
            await session.scalar(
                select(func.sum(NodeRow.transcode_limit)).where(
                    NodeRow.state == "online", NodeRow.paused.is_(False), NodeRow.workspace_ready.is_(True)
                )
            )
            or 0
        )
        # Paused nodes still finish in-flight work, but cannot drain pending work.
        if not transcodes_pending:
            capacity["transcode"] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .join(NodeRow, NodeRow.id == Job.node_id)
                    .where(
                        Job.kind == "transcode",
                        processing,
                        NodeRow.state == "online",
                        NodeRow.workspace_ready.is_(True),
                    )
                )
                or 0
            )
    durations = []
    for kind, count in outstanding.items():
        slots = capacity.get(kind, 0)
        if slots <= 0:
            estimate["reason"] = f"No available {kind} capacity."
            return estimate
        mean = means.get(kind)
        if mean is None:
            estimate["reason"] = f"No recent completed {kind} timing history."
            return estimate
        durations.append(math.ceil(count / slots) * mean)
    artifact_s = math.ceil(artifact_checks / healthcheck_slots) * means["healthcheck"] if artifact_checks else 0
    remaining_s = max(1, math.ceil(round(probe_s + max(durations, default=0) + artifact_s, 3)))
    estimate["remaining_s"] = remaining_s
    estimate["estimated_finish_at"] = iso_utc(ts + timedelta(seconds=remaining_s))
    return estimate
