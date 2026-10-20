from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from server.models import Flow, Job, JobAttempt, JobEvent, NotificationDelivery, PluginState, utcnow
from server.plugins.migrations import (
    AppliedMigration,
    migrate_definition,
    migrate_event,
    migrate_execution,
    migrate_plugin_id,
    migrate_results,
)
from server.plugins.states import disabled_ids
from server.queue.store import ACTIVE_STATUSES
from server.workspace import post_action_jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.plugins.host import PluginHost
    from server.plugins.manifest import PluginManifest

LOG = logging.getLogger(__name__)


def log_migrations(applied: list[AppliedMigration]) -> None:
    for migration in applied:
        changes = ". ".join(migration.changes) or "Recorded configuration version. No field or ID renames were needed"
        LOG.info(
            "Applied plugin %s migration %s -> %s to %s. %s.",
            migration.plugin_id,
            migration.from_version,
            migration.to_version,
            migration.record,
            changes,
        )


def migrate_job(
    job: Job,
    manifests: list[PluginManifest],
    *,
    applied: list[AppliedMigration],
    post_actions_only: bool = False,
) -> None:
    fields = (
        ("post_actions",)
        if post_actions_only
        else ("pre_actions", "post_actions", "continuation", "flow_snapshot", "decision_log")
    )
    original = {field: getattr(job, field) for field in fields}
    migrated = migrate_execution(original, manifests, applied=applied, record=f"job {job.id}")
    for field in fields:
        if migrated[field] != original[field]:
            setattr(job, field, migrated[field])


async def migrate_saved_plugins(host: PluginHost, db: async_sessionmaker[AsyncSession]) -> None:
    manifests = [info.manifest for info in host.plugins()]
    async with db() as session:
        state_changes: list[AppliedMigration] = []
        states = list(await session.scalars(select(PluginState).order_by(PluginState.id).with_for_update()))
        current = {state.plugin_id: state for state in states}
        for state in states:
            plugin_id = migrate_plugin_id(
                state.plugin_id, manifests, applied=state_changes, record=f"saved plugin setting {state.id}"
            )
            if state.plugin_id == plugin_id:
                continue
            other = current.get(plugin_id)
            if other is not None:
                if other.enabled != state.enabled:
                    msg = f"Conflicting saved settings for plugin {state.plugin_id!r} and {plugin_id!r}."
                    raise ValueError(msg)
                await session.delete(state)
                state_changes[-1].changes.append("Merged duplicate saved plugin setting")
            else:
                state.plugin_id = plugin_id
                current[plugin_id] = state
        await session.commit()
        log_migrations(state_changes)
        host.apply_disabled(await disabled_ids(session))
        flow_ids = list(await session.scalars(select(Flow.id).order_by(Flow.id)))

    for flow_id in flow_ids:
        async with db() as session:
            flow = await session.scalar(select(Flow).where(Flow.id == flow_id).with_for_update())
            if flow is None:
                continue
            try:
                applied: list[AppliedMigration] = []
                original = json.loads(flow.definition)
                migrated = migrate_definition(original, manifests, applied=applied, record=f"flow {flow.name!r}")
                if not applied or migrated == original:
                    continue
                previous = {node["id"]: node for node in original["nodes"]}
                ready = {info.manifest.id for info in host.plugins() if info.state == "ready"}
                errors: list[str] = []
                for node in migrated["nodes"]:
                    if node["type"] == "plugin" and node != previous[node["id"]] and node["plugin"] in ready:
                        problems = await host.validate_inputs(node["plugin"], node.get("inputs", {}))
                        errors.extend(f"Step {node['id']!r}: {problem}" for problem in problems)
                if errors:
                    LOG.error("Cannot migrate flow %s: %s", flow.name, ". ".join(errors))
                    continue
            except ValueError as exc:
                LOG.error("Cannot migrate flow %s: %s", flow.name, exc)
                continue
            flow.definition = json.dumps(migrated)
            flow.version += 1
            flow.updated_at = utcnow()
            await session.commit()
            log_migrations(applied)

    if not any(manifest.migrations for manifest in manifests):
        return
    await _migrate_jobs(manifests, db)


async def _migrate_jobs(manifests: list[PluginManifest], db: async_sessionmaker[AsyncSession]) -> None:
    after_id = 0
    while True:
        async with db() as session:
            jobs = list(
                await session.scalars(
                    select(Job)
                    .where(
                        Job.id > after_id,
                        or_(
                            Job.status.in_(ACTIVE_STATUSES),
                            Job.id.in_(post_action_jobs()),
                            Job.id.in_(
                                select(NotificationDelivery.job_id).where(NotificationDelivery.status == "pending")
                            ),
                        ),
                    )
                    .order_by(Job.id)
                    .limit(100)
                    .with_for_update()
                )
            )
            if not jobs:
                break
            after_id = jobs[-1].id
            by_id = {job.id: job for job in jobs}
            post_action_ids = set(await session.scalars(post_action_jobs().where(Job.id.in_(by_id))))
            work_ids = {job.id for job in jobs if job.status in ACTIVE_STATUSES} | post_action_ids
            # Delivery workers save command progress while holding the delivery lock.
            # Lock pending deliveries before their events, in the same order as the workers.
            deliveries = list(
                await session.scalars(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.job_id.in_(by_id), NotificationDelivery.status == "pending")
                    .order_by(NotificationDelivery.id)
                    .with_for_update()
                )
            )
            by_event: dict[int, list[NotificationDelivery]] = defaultdict(list)
            for delivery in deliveries:
                by_event[delivery.job_event_id].append(delivery)
            attempts = list(
                await session.scalars(
                    select(JobAttempt)
                    .join(Job, Job.id == JobAttempt.job_id)
                    .where(Job.id.in_(work_ids), JobAttempt.attempt_number == Job.attempts)
                    .order_by(JobAttempt.id)
                    .with_for_update(of=JobAttempt)
                )
            )
            running_attempts = {attempt.id for attempt in attempts if attempt.status in ACTIVE_STATUSES}
            latest_requests = (
                select(func.max(JobEvent.id))
                .where(
                    JobEvent.job_id.in_([job.id for job in jobs if job.status == "pending"]),
                    JobEvent.type == "requested",
                )
                .group_by(JobEvent.job_id)
            )
            latest_actions = (
                select(func.max(JobEvent.id))
                .where(JobEvent.job_id.in_(work_ids), JobEvent.type.in_(["actions_pending", "actions_finished"]))
                .group_by(JobEvent.job_id, JobEvent.attempt_id)
            )
            recorded = {
                (row.job_id, row.attempt_id)
                for row in await session.execute(
                    select(JobEvent.job_id, JobEvent.attempt_id).where(JobEvent.id.in_(latest_actions))
                )
            }
            current_attempts = {attempt.job_id: attempt.id for attempt in attempts}
            # Stage and job post-actions are only a fallback before an action marker exists.
            deferred_ids = {
                job_id for job_id in post_action_ids if (job_id, current_attempts.get(job_id)) not in recorded
            }
            post_action_attempts = {attempt.id for attempt in attempts if attempt.job_id in deferred_ids}
            without_attempts = deferred_ids - current_attempts.keys()
            deferred_actions = (
                select(func.max(JobEvent.id))
                .where(
                    JobEvent.job_id.in_(deferred_ids),
                    JobEvent.type.in_(["stage", "post_actions"]),
                    or_(
                        JobEvent.attempt_id.in_(post_action_attempts),
                        and_(JobEvent.attempt_id.is_(None), JobEvent.job_id.in_(without_attempts)),
                    ),
                )
                .group_by(JobEvent.job_id)
            )
            events = list(
                await session.scalars(
                    select(JobEvent)
                    .where(
                        or_(
                            JobEvent.id.in_(latest_requests),
                            and_(JobEvent.type == "actions_pending", JobEvent.id.in_(latest_actions)),
                            and_(
                                JobEvent.type.in_(["stage", "post_actions"]),
                                JobEvent.attempt_id.in_(running_attempts),
                            ),
                            JobEvent.id.in_(deferred_actions),
                            JobEvent.id.in_(by_event),
                        )
                    )
                    .order_by(JobEvent.id)
                    .with_for_update()
                )
            )
            applied: list[AppliedMigration] = []
            for job in jobs:
                if job.status not in ACTIVE_STATUSES and job.id not in deferred_ids:
                    continue
                migrate_job(job, manifests, applied=applied, post_actions_only=job.status not in ACTIVE_STATUSES)
            for event in events:
                payload = event.payload
                # A finished conversion's stage is history except for deferred post-actions.
                if event.id in by_event:
                    associated = by_event[event.id]
                    event_type, payload, targets = migrate_event(
                        event.type,
                        payload,
                        [delivery.target_name for delivery in associated],
                        manifests,
                        applied=applied,
                        record=f"job {event.job_id} event {event.id}",
                    )
                    if event.type != event_type:
                        event.type = event_type
                    for delivery, target in zip(associated, targets, strict=True):
                        if delivery.target_name != target:
                            delivery.target_name = target
                elif event.attempt_id not in running_attempts and event.type in {"stage", "post_actions"}:
                    field = "post_actions" if event.type == "stage" else "actions"
                    migrated = migrate_execution(
                        {field: payload[field]} if field in payload else {},
                        manifests,
                        applied=applied,
                        record=f"job {event.job_id} event {event.id}",
                    )
                    payload = {**payload, **migrated}
                else:
                    payload = migrate_execution(
                        payload,
                        manifests,
                        applied=applied,
                        record=f"job {event.job_id} event {event.id}",
                    )
                if payload != event.payload:
                    event.payload = payload
            for attempt in attempts:
                if attempt.id not in running_attempts:
                    continue
                results = migrate_results(
                    attempt.action_results,
                    manifests,
                    applied=applied,
                    record=f"job {attempt.job_id} attempt {attempt.id}.action_results",
                )
                if results != attempt.action_results:
                    attempt.action_results = results
            if applied:
                await session.commit()
                log_migrations(applied)
