from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from common.ctx import PluginContext
from common.enums import HealthStatus, TranscodeStatus
from sqlalchemy import and_, exists, or_, select

from server.events import publish_file_status, publish_job_status
from server.flows.definition import FlowDefinition
from server.flows.engine import FlowResult, decision_entry, evaluate
from server.flows.sidecars import sidecar_metadata, sidecar_subtitles
from server.models import Flow, Job, Library, MediaFile, utcnow
from server.nodehub.hub import probe_for_file
from server.plugins.migrations import migrate_definition
from server.processing_cutoff import processing_allowed, processing_clause
from server.queue.store import (
    ACTIVE_STATUSES,
    ActiveJobError,
    capture_source,
    check_file_idle,
    cooldown_hold_until,
    enqueue_file_job,
    enqueue_healthcheck_job,
    record_flow_decision,
    set_transcode_result,
)
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.events import EventBus
    from server.plugins.host import PluginHost

LOG = logging.getLogger(__name__)

_ACTIVE_VALUES = sorted(status.value for status in ACTIVE_STATUSES)


def _candidate_statement(batch: int, after_id: int = 0):
    has_active = exists(select(Job.id).where(Job.media_file_id == MediaFile.id, Job.status.in_(_ACTIVE_VALUES)))
    return (
        select(MediaFile)
        .join(Library, MediaFile.library_id == Library.id)
        .where(
            MediaFile.probe_id.is_not(None),
            MediaFile.metadata_cancelled.is_(False),
            or_(
                and_(MediaFile.health_status == HealthStatus.HEALTHY.value, MediaFile.health_source.is_not(None)),
                and_(MediaFile.health_status == HealthStatus.PENDING.value, Library.healthcheck_enabled.is_(True)),
            ),
            Library.flow_id.is_not(None),
            Library.suspended.is_(False),
            processing_clause(),
            MediaFile.transcode_decided.is_(False),
            ~has_active,
            MediaFile.id > after_id,
        )
        .order_by(MediaFile.id)
        .limit(batch)
    )


class FlowEnqueueService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        plugin_host: PluginHost,
        *,
        events: EventBus | None = None,
        hub_wake: Callable[[], None] | None = None,
        staging_wake: Callable[[], None] | None = None,
        healthcheck_wake: Callable[[], None] | None = None,
        batch: int = 50,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.plugin_host = plugin_host
        self.events = events
        self.hub_wake = hub_wake
        self.staging_wake = staging_wake
        self.healthcheck_wake = healthcheck_wake
        self.batch = batch
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._cursor = 0
        self._more = False
        self._cooldown_until: datetime | None = None

    async def start(self) -> None:
        if self._close_task is not None and not self._close_task.done():
            raise RuntimeError("flow enqueue shutdown is still active")
        self._close_task = None
        self._closed = False
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run())
        self._wake.set()

    def wake(self) -> None:
        if not self._closed:
            self._wake.set()

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
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
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        self._loop_task = None
        self._wake = asyncio.Event()

    async def _run(self) -> None:
        while not self._closed:
            self._wake.clear()
            await self._pass()
            if not self._more:
                timeout = (
                    max(0, (self._cooldown_until - utcnow()).total_seconds())
                    if self._cooldown_until is not None
                    else None
                )
                try:
                    async with asyncio.timeout(timeout):
                        await self._wake.wait()
                except TimeoutError:
                    pass

    async def _pass(self) -> int:
        if self._cursor == 0:
            self._cooldown_until = None
        async with self.sessionmaker() as session:
            rows = (await session.execute(_candidate_statement(self.batch, self._cursor))).scalars().all()
            ids = [row.id for row in rows]
        self._more = len(ids) == self.batch
        self._cursor = ids[-1] if self._more else 0
        processed = 0
        for media_id in ids:
            if await self._process(media_id):
                processed += 1
        return processed

    async def _process(self, media_id: int) -> bool:
        async with self.sessionmaker() as session:
            row = await session.get(MediaFile, media_id)
            if row is None:
                return False
            try:
                return await self._decide(session, row)
            except ActiveJobError:
                return False
            except Exception as exc:
                LOG.exception("flow enqueue failed for media %s (%s)", row.id, row.path)
                if self.events is not None:
                    library = await session.get(Library, row.library_id)
                    self.events.publish(
                        "operator.alert",
                        {
                            "operation": "flow evaluation",
                            "media_id": row.id,
                            "media_path": row.path,
                            "library_id": row.library_id,
                            "library_name": library.name if library is not None else None,
                            "error": str(exc) or type(exc).__name__,
                        },
                        key=f"operator.alert:flow:{row.id}",
                    )
                return False

    async def _decide(self, session: AsyncSession, row: MediaFile) -> bool:
        if row.transcode_decided:
            return False
        library = await session.get(Library, row.library_id)
        flow_row = await session.get(Flow, library.flow_id) if library is not None else None
        if library is None or library.suspended or library.flow_id is None or flow_row is None:
            return False
        if not processing_allowed(library, row):
            return False
        health_source = row.health_source
        health_status = row.health_status
        needs_healthcheck = health_status == HealthStatus.PENDING.value and library.healthcheck_enabled
        path = Path(row.path)
        if (
            row.metadata_cancelled
            or not any(path.is_relative_to(Path(root)) for root in library.roots)
            or (
                not needs_healthcheck
                and (
                    health_status != HealthStatus.HEALTHY.value or health_source != {"path": row.path, "size": row.size}
                )
            )
        ):
            return False
        probe = await probe_for_file(session, row)
        if probe is None:
            LOG.debug("flow enqueue skipping media %s: no probe", row.id)
            return False
        source = await capture_source(row)
        definition = FlowDefinition.model_validate(
            migrate_definition(json.loads(flow_row.definition), (info.manifest for info in self.plugin_host.plugins()))
        )
        ctx = PluginContext(
            probe=probe,
            original_filename=Path(row.path).name,
            library={"name": library.name},
            sidecar_subtitles=sidecar_subtitles(row.path),
            sidecar_metadata=await wait_workspace_io(lambda: sidecar_metadata(row.path, definition)),
        )
        try:
            result = await evaluate(definition, ctx, self.plugin_host)
        except Exception as exc:
            result = FlowResult(status="error", reason=str(exc) or type(exc).__name__)
        if result.status == "matched" and result.plan is not None:
            until = cooldown_hold_until(library.cooldown_s, row.first_seen, utcnow())
            if until is not None:
                if self._cooldown_until is None or until < self._cooldown_until:
                    self._cooldown_until = until
                return False
        # Match scanner and manual queue ordering while keeping eligibility stable.
        eligible = await session.scalar(
            select(Library.id)
            .where(
                Library.id == library.id,
                Library.suspended.is_(False),
                Library.deleted.is_(False),
                Library.flow_id == flow_row.id,
                Library.roots == library.roots,
                Library.healthcheck_enabled == library.healthcheck_enabled,
            )
            .with_for_update(read=True)
        )
        if eligible is None:
            return False
        current = await session.scalar(
            select(MediaFile.id)
            .join(Library, MediaFile.library_id == Library.id)
            .where(
                MediaFile.id == row.id,
                MediaFile.library_id == library.id,
                MediaFile.transcode_decided.is_(False),
                MediaFile.health_status == health_status,
                *([MediaFile.health_source == health_source] if health_source is not None else []),
                MediaFile.probe_id == row.probe_id,
                MediaFile.metadata_cancelled.is_(False),
                MediaFile.path == row.path,
                MediaFile.size == row.size,
                MediaFile.mtime_ns == row.mtime_ns,
                MediaFile.first_seen == row.first_seen,
                processing_clause(),
            )
            .with_for_update(of=MediaFile)
        )
        if current is None:
            return False
        decision = [decision_entry(outcome) for outcome in result.steps]
        flow_snapshot = {
            "id": flow_row.id,
            "name": flow_row.name,
            "version": flow_row.version,
            "definition": definition.model_dump(mode="json"),
        }
        if result.status == "matched" and result.plan is not None:
            if needs_healthcheck:
                await check_file_idle(session, row.id)
                job = await enqueue_healthcheck_job(session, row.id, library.id)
                job.decision_log = decision
                job.flow_snapshot = flow_snapshot
                # Keep failed or cancelled checks from being queued again on every wake.
                # A healthy result releases this decision for fresh flow evaluation.
                row.transcode_decided = True
                await set_transcode_result(session, row.id, TranscodeStatus.PENDING)
            else:
                job = await enqueue_file_job(
                    session,
                    row.id,
                    library.id,
                    plan=result.plan,
                    decision_log=decision,
                    continuation=result.continuation,
                    facets=result.facets,
                    pre_actions=result.actions,
                    post_actions=result.post_actions,
                    requires=result.requires,
                    requires_encoders=result.requires_encoders,
                    flow_snapshot=flow_snapshot,
                    hold_until=cooldown_hold_until(library.cooldown_s, row.first_seen, utcnow()),
                    source=source,
                    probe=probe,
                )
        else:
            job = await record_flow_decision(
                session,
                row.id,
                library.id,
                decision_log=decision,
                reason=result.reason,
                flow_snapshot=flow_snapshot,
                status=result.status,
                source=source,
                first_seen=row.first_seen,
            )
        await session.commit()
        if job is None:
            if self.events is not None:
                await publish_file_status(self.events, self.sessionmaker, row.id, bulk=True)
            if result.status == "error":
                raise RuntimeError(result.reason)
            LOG.warning("flow %s decided %s for media %s: %s", library.flow_id, result.status, row.id, result.reason)
            return False
        if self.hub_wake is not None:
            self.hub_wake()
        if self.staging_wake is not None:
            self.staging_wake()
        if job.kind == "healthcheck" and self.healthcheck_wake is not None:
            self.healthcheck_wake()
        if self.events is not None:
            await publish_job_status(self.events, self.sessionmaker, job.id, bulk=True)
        return True
