from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from common.enums import JobStatus
from sqlalchemy import delete, or_, select

from server.events import EventBus
from server.fs.source import lock_job_source
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, NotificationDelivery
from server.workspace import recovery_jobs

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

LOG = logging.getLogger(__name__)
TERMINAL = (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.SKIPPED.value)
DELETABLE = (*TERMINAL, JobStatus.QUEUED.value, JobStatus.STAGING.value, JobStatus.HOLD.value)
QUERY_BATCH = 500


def protected_history_jobs() -> Select[tuple[int]]:
    return select(Job.id).where(
        or_(
            Job.id.in_(recovery_jobs()),
            Job.id.in_(select(NotificationDelivery.job_id).where(NotificationDelivery.status == "pending")),
        )
    )


async def prune_jobs(
    sessionmaker, retention_days: int, *, now: datetime | None = None, events: EventBus | None = None
) -> int:
    if retention_days == 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    removed = last_id = 0
    while True:
        async with sessionmaker() as session:
            job_ids = list(
                (
                    await session.execute(
                        select(Job.id)
                        .where(
                            Job.id > last_id,
                            Job.status.in_(TERMINAL),
                            Job.finished_at < cutoff,
                            Job.id.not_in(protected_history_jobs()),
                            Job.delete_requested.is_(False),
                        )
                        .order_by(Job.id)
                        .limit(QUERY_BATCH)
                    )
                ).scalars()
            )
        if not job_ids:
            break
        last_id = job_ids[-1]
        async with sessionmaker() as session:
            # Retention removes history, not sources. Parent locks protect the graph
            # without blocking unrelated work on its library or current media file.
            locked_ids = list(
                await session.scalars(
                    select(Job.id).where(Job.id.in_(job_ids)).order_by(Job.id).with_for_update(skip_locked=True)
                )
            )
            if not locked_ids:
                continue
            await session.execute(
                select(JobAttempt.id)
                .where(JobAttempt.job_id.in_(locked_ids))
                .order_by(JobAttempt.id)
                .with_for_update(of=JobAttempt)
            )
            # Recheck eligibility with a fresh snapshot after acquiring the graph locks.
            removed_ids = list(
                await session.scalars(
                    select(Job.id)
                    .where(
                        Job.id.in_(locked_ids),
                        Job.status.in_(TERMINAL),
                        Job.delete_requested.is_(False),
                        Job.finished_at < cutoff,
                        Job.id.not_in(protected_history_jobs()),
                    )
                    .order_by(Job.id)
                )
            )
            if not removed_ids:
                continue
            await _delete_job_graphs(session, removed_ids)
            await session.commit()
        if events is not None:
            for job_id in removed_ids:
                events.publish("job.deleted", {"job_id": job_id}, key=f"job.deleted:{job_id}")
        removed += len(removed_ids)
    LOG.info("job retention pruned %d jobs", removed)
    return removed


async def delete_job_graph(session, job_id: int) -> None:
    # The parent lock blocks attempt, journal, and event insertions until deletion commits.
    if await lock_job_source(session, job_id) is None:
        return
    await session.execute(
        select(JobAttempt.id).where(JobAttempt.job_id == job_id).order_by(JobAttempt.id).with_for_update(of=JobAttempt)
    )
    await _delete_job_graphs(session, [job_id])


async def _delete_job_graphs(session, job_ids: list[int]) -> None:
    # Callers own the parent and attempt locks before removing their dependent rows.
    attempt_ids = select(JobAttempt.id).where(JobAttempt.job_id.in_(job_ids))
    await session.execute(delete(FinalizationJournal).where(FinalizationJournal.attempt_id.in_(attempt_ids)))
    await session.execute(delete(NotificationDelivery).where(NotificationDelivery.job_id.in_(job_ids)))
    await session.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
    await session.execute(delete(JobAttempt).where(JobAttempt.job_id.in_(job_ids)))
    await session.execute(delete(Job).where(Job.id.in_(job_ids)))


async def delete_eligible_job(session, job_id: int) -> bool:
    if await lock_job_source(session, job_id) is None:
        return False
    # Recheck recovery after any lock wait, using a fresh statement snapshot.
    eligible = await session.scalar(
        select(Job.id).where(Job.id == job_id, Job.status.in_(DELETABLE), Job.id.not_in(protected_history_jobs()))
    )
    if eligible is None:
        return False
    job = await session.get(Job, job_id, populate_existing=True)
    if job is not None and job.kind in {"probe", "mkv_stats"} and job.status in {"queued", "hold"}:
        from server.metadata_jobs import cancel_metadata

        await cancel_metadata(session, job)
    await delete_job_graph(session, job_id)
    return True
