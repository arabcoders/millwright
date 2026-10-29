from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from common.diagnostics import error_message
from common.media.tools import ProbeError
from sqlalchemy import func, select, text

from server.core.auth import Principal, WsTickets
from server.flows.definition import FlowDefinition
from server.flows.dryrun import dry_run
from server.flows.sidecars import sidecar_metadata, sidecar_subtitles
from server.flows.validation import validate_definition
from server.models import Flow, Library, MediaFile, ProbeRow, SessionRow, utcnow
from server.scanner.normalize import normalize
from server.serialize import iso_utc
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from common.media.probe import ProbeData
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.plugins.host import PluginHost

LOG = logging.getLogger(__name__)
BATCH_SIZE = 100
FILE_TIMEOUT_S = 60
RUN_TIMEOUT_S = 3600
REPORT_TTL_S = 3600
MAX_REPORTS = 8
MAX_RETAINED_FILES = 100_000
MAX_REASON = 2000
ACTIVE_STATES = frozenset({"preparing", "running", "cancelling"})
type BatchStatus = Literal["matched", "deferred", "skipped", "vetoed", "error", "unavailable", "not_evaluated"]


class BatchError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(kw_only=True, frozen=True, slots=True)
class IndexedInput:
    id: int
    library_id: int
    # Library settings stay pinned. Reassignment, not a settings edit, invalidates the source.
    library_name: str = field(compare=False)
    roots: tuple[str, ...] = field(compare=False)
    path: str
    size: int
    mtime_ns: int
    identity: dict[str, int] | None
    created_at: datetime
    probe_id: int | None
    probe_created: datetime | None
    probe_updated: datetime | None
    probe_digest: str | None


@dataclass(kw_only=True, slots=True)
class BatchRow:
    file_id: int
    root: str
    file: str
    status: str = "not_evaluated"
    reason: str = ""
    deferred_output: bool = False
    deferred_encoder: bool = False


@dataclass(kw_only=True)
class BatchReport:
    id: str
    library_id: int | None
    file_ids: list[int] | None = None
    requested_by: tuple[int | None, str | None]
    created_at: datetime
    library_name: str = ""
    roots: list[str] = field(default_factory=list)
    flow_id: int
    flow_name: str = ""
    flow_version: int
    definition: str | FlowDefinition | None = None
    inputs: list[IndexedInput] = field(default_factory=list)
    rows: list[BatchRow] = field(default_factory=list)
    total: int = 0
    captured: int = 0
    snapshot_complete: bool = False
    processed: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    state: str = "preparing"
    error: str = ""
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    started: bool = False
    session_expires_at: datetime | None = None

    def payload(self, *, limit: int = 50, offset: int = 0, statuses: list[BatchStatus] | None = None) -> dict:
        rows = [row for row in self.rows if row.status in statuses] if statuses else self.rows
        return {
            "id": self.id,
            "library_id": self.library_id,
            "file_ids": self.file_ids,
            "library_name": self.library_name,
            "flow_id": self.flow_id,
            "flow_name": self.flow_name,
            "flow_version": self.flow_version,
            "created_at": iso_utc(self.created_at),
            "finished_at": iso_utc(self.finished_at),
            "expires_at": iso_utc(self.expires_at),
            "state": self.state,
            "error": self.error,
            "processed": self.processed,
            "total": self.total,
            "captured": self.captured,
            "snapshot_complete": self.snapshot_complete,
            "counts": dict(self.counts),
            "items": [asdict(row) for row in rows[offset : offset + limit]],
            "pagination": {"offset": offset, "limit": limit, "total": len(rows) if statuses else self.total},
        }


async def _drain(task: asyncio.Task[None]) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if not task.cancelled():
        task.result()
    if cancelled:
        raise asyncio.CancelledError


def _digest(raw: str | None) -> str | None:
    return hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None


def _input(file: MediaFile, probe: ProbeRow | None, library: Library) -> IndexedInput:
    return IndexedInput(
        id=file.id,
        library_id=file.library_id,
        library_name=library.name,
        roots=tuple(library.roots),
        path=file.path,
        size=file.size,
        mtime_ns=file.mtime_ns,
        identity=file.source_identity,
        created_at=file.created_at,
        probe_id=file.probe_id,
        probe_created=probe.created_at if probe is not None else None,
        probe_updated=probe.updated_at if probe is not None else None,
        probe_digest=_digest(probe.raw_json) if probe is not None and probe.media_file_id == file.id else None,
    )


def _row(file: IndexedInput) -> BatchRow:
    path = Path(file.path)
    root = next((root for root in file.roots if path.is_relative_to(Path(root))), "")
    return BatchRow(file_id=file.id, root=root, file=str(path.relative_to(root)) if root else file.path)


class BatchDryRuns:
    """Owns ephemeral previews, not processing jobs or persisted file decisions."""

    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        host: PluginHost,
        tickets: WsTickets,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.host = host
        self.tickets = tickets
        self.clock = clock
        self.reports: dict[str, BatchReport] = {}
        self.active: BatchReport | None = None
        self._closed = False
        self._paused = 0

    async def start(
        self,
        library_id: int | None,
        flow_id: int,
        version: int,
        principal: Principal,
        *,
        file_ids: list[int] | None = None,
    ) -> BatchReport:
        await self.purge()
        if self._closed or self._paused:
            raise BatchError("Dry runs are unavailable during shutdown or configuration changes.", 503)
        if self.active is not None:
            raise BatchError("Another batch dry run is active. Wait for it to finish or cancel it.", 409)
        if len(self.reports) >= MAX_REPORTS:
            raise BatchError("Dry-run report storage is full. Wait for a retained report to expire.", 429)
        if (library_id is None and not file_ids) or (library_id is not None and file_ids is not None):
            raise BatchError("Choose a library or a nonempty selection of files.", 422)
        report = BatchReport(
            id=uuid.uuid4().hex,
            library_id=library_id,
            file_ids=sorted(set(file_ids)) if file_ids is not None else None,
            requested_by=(principal.user_id, principal.session_hash),
            created_at=self.clock(),
            flow_id=flow_id,
            flow_version=version,
            session_expires_at=principal.session_expires_at,
        )
        self.active = report
        report.task = asyncio.current_task()
        report.started = True
        session = self.db()
        revoked = self.tickets.watch_session(principal.session_hash) if principal.session_hash is not None else None
        try:
            if revoked is not None:
                await self._authorize(principal, revoked)
            await self._prepare_report(report, session)
        except BaseException:
            try:
                await _drain(asyncio.create_task(session.close()))
            finally:
                if revoked is not None and principal.session_hash is not None:
                    self.tickets.unwatch_session(principal.session_hash, revoked)
                if self.active is report:
                    self.active = None
            raise
        self.reports[report.id] = report
        report.started = False
        report.task = asyncio.create_task(
            self._execute(report, principal, session, revoked), name=f"batch-dry-run:{report.id}"
        )
        return report

    async def _prepare_report(self, report: BatchReport, session: AsyncSession) -> None:
        # Keep this read-only transaction open after the HTTP request returns.
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execution_options(read_only=True)
        )
        library = await session.get(Library, report.library_id) if report.library_id is not None else None
        flow = await session.get(Flow, report.flow_id)
        if (report.library_id is not None and library is None) or flow is None:
            raise BatchError("Library or flow not found.", 404)
        if flow.version != report.flow_version:
            raise BatchError("Flow changed. Refresh flows and try again.", 409)
        if library is not None:
            report.library_name = library.name
            report.roots = list(library.roots)
        report.flow_name = flow.name
        report.definition = flow.definition
        if report.file_ids is not None:
            for offset in range(0, len(report.file_ids), BATCH_SIZE):
                ids = report.file_ids[offset : offset + BATCH_SIZE]
                found = set(await session.scalars(select(MediaFile.id).where(MediaFile.id.in_(ids))))
                missing = [file_id for file_id in ids if file_id not in found]
                if missing:
                    message = f"Indexed files not found: {', '.join(map(str, missing))}."
                    raise BatchError(message, 404)
            report.total = len(report.file_ids)
        else:
            report.total = (
                await session.execute(
                    select(func.count()).select_from(MediaFile).where(MediaFile.library_id == report.library_id)
                )
            ).scalar_one()
        retained = sum(len(item.rows) for item in self.reports.values())
        if retained + report.total > MAX_RETAINED_FILES:
            raise BatchError("The batch exceeds available report capacity. No files were evaluated.", 413)
        if self.host.unconfirmed_exit:
            raise BatchError("Cannot start the preview because we couldn't confirm that a plugin runner exited.", 409)

    async def _snapshot(self, report: BatchReport, session: AsyncSession) -> None:
        last_id = 0
        try:
            while True:
                scope = (
                    MediaFile.id.in_(report.file_ids[report.captured : report.captured + BATCH_SIZE])
                    if report.file_ids is not None
                    else MediaFile.library_id == report.library_id
                )
                batch = (
                    await session.execute(
                        select(MediaFile, ProbeRow, Library)
                        .join(Library, MediaFile.library_id == Library.id)
                        .outerjoin(ProbeRow, MediaFile.probe_id == ProbeRow.id)
                        .where(scope, MediaFile.id > last_id)
                        .order_by(MediaFile.id)
                        .limit(BATCH_SIZE)
                    )
                ).all()
                if not batch:
                    break
                for file, probe, library in batch:
                    captured = _input(file, probe, library)
                    report.inputs.append(captured)
                    report.rows.append(_row(captured))
                report.captured = len(report.inputs)
                last_id = report.inputs[-1].id
                session.expunge_all()
            report.snapshot_complete = report.captured == report.total
        finally:
            await _drain(asyncio.create_task(session.close()))

    async def _watch_session(self, report: BatchReport, principal: Principal, revoked: asyncio.Event) -> None:
        assert principal.session_expires_at is not None
        timeout = max(0, (principal.session_expires_at - self.clock()).total_seconds())
        try:
            await asyncio.wait_for(revoked.wait(), timeout)
        except TimeoutError:
            pass
        self._request_cancel(report)

    async def _authorize(self, principal: Principal, revoked: asyncio.Event) -> None:
        async with self.db() as session:
            valid = await session.scalar(
                select(SessionRow.id).where(
                    SessionRow.token_hash == principal.session_hash, SessionRow.expires_at > self.clock()
                )
            )
        if valid is None or revoked.is_set() or principal.session_expires_at is None:
            raise BatchError("Invalid or expired session.", 401)

    async def _prepare(self, report: BatchReport, session: AsyncSession) -> None:
        snapshot = asyncio.create_task(self._snapshot(report, session))
        try:
            await asyncio.shield(snapshot)
        except asyncio.CancelledError:
            # Drain capture so a cancelled report still names every unevaluated file.
            await _drain(snapshot)
            raise
        if report.cancel_requested:
            raise asyncio.CancelledError
        assert isinstance(report.definition, str)
        report.definition = FlowDefinition.model_validate_json(report.definition)
        errors = await validate_definition(self.host, report.definition)
        if errors:
            raise BatchError(" ".join(errors), 422)

    async def _execute(
        self, report: BatchReport, principal: Principal, session: AsyncSession, revoked: asyncio.Event | None
    ) -> None:
        report.started = True
        watcher = None
        try:
            if revoked is not None:
                watcher = asyncio.create_task(self._watch_session(report, principal, revoked))
            async with asyncio.timeout(RUN_TIMEOUT_S):
                await self._prepare(report, session)
                report.state = "running"
                await self._evaluate(report)
                report.state = "completed"
        except asyncio.CancelledError:
            report.state = "cancelled"
        except Exception as exc:
            report.state = "failed"
            report.error = (
                "Dry run exceeded its time limit." if isinstance(exc, TimeoutError) else str(exc)[:MAX_REASON]
            )
            LOG.exception("Batch dry run %s failed", report.id)
        finally:
            await _drain(asyncio.create_task(self._finish(report, principal, session, revoked, watcher)))

    async def _finish(
        self,
        report: BatchReport,
        principal: Principal,
        session: AsyncSession,
        revoked: asyncio.Event | None,
        watcher: asyncio.Task[None] | None,
    ) -> None:
        try:
            await session.close()
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if principal.session_hash is not None and revoked is not None:
                self.tickets.unwatch_session(principal.session_hash, revoked)
            report.finished_at = self.clock()
            report.expires_at = report.finished_at + timedelta(seconds=REPORT_TTL_S)
            for row in report.rows[report.processed :]:
                row.status = "not_evaluated"
                row.reason = ""
                row.deferred_output = row.deferred_encoder = False
            # Only compact rows survive completion. Probes never enter the report.
            report.inputs.clear()
            report.definition = None
            if self.host.unconfirmed_exit:
                report.state = "failed"
                report.error = "Couldn't confirm that the plugin runner exited. New previews are blocked."
            elif self.active is report:
                self.active = None

    async def _load(self, inputs: list[IndexedInput]) -> dict[int, tuple[IndexedInput, str | None]]:
        async with self.db() as session:
            rows = (
                await session.execute(
                    select(MediaFile, ProbeRow, Library)
                    .join(Library, MediaFile.library_id == Library.id)
                    .outerjoin(ProbeRow, MediaFile.probe_id == ProbeRow.id)
                    .where(MediaFile.id.in_([item.id for item in inputs]))
                )
            ).all()
            return {
                file.id: (_input(file, probe, library), probe.raw_json if probe is not None else None)
                for file, probe, library in rows
            }

    async def _unchanged(self, captured: IndexedInput) -> bool:
        current = await self._load([captured])
        return captured.id in current and current[captured.id][0] == captured

    async def _evaluate(self, report: BatchReport) -> None:
        assert isinstance(report.definition, FlowDefinition)
        definition = report.definition
        for offset in range(0, len(report.inputs), BATCH_SIZE):
            inputs = report.inputs[offset : offset + BATCH_SIZE]
            current = await self._load(inputs)
            for index, captured in enumerate(inputs, offset):
                row = report.rows[index]
                found = current.get(captured.id)
                if found is None or found[0] != captured:
                    row.status, row.reason = (
                        "unavailable",
                        "Indexed source or stored probe changed since the preview started.",
                    )
                elif found[1] is None or captured.probe_digest is None:
                    row.status, row.reason = "unavailable", "No stored probe is available."
                elif not row.root:
                    row.status, row.reason = "unavailable", "Indexed path is outside the captured library roots."
                else:
                    try:
                        raw = json.loads(found[1])
                        stat = os.stat_result((0, 0, 0, 0, 0, 0, captured.size, 0, 0, 0))
                        probe: ProbeData = normalize(Path(captured.path), stat, raw).probe
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                        detail = error_message(exc)
                        row.status, row.reason = "unavailable", f"Stored probe is invalid. {detail}"[:MAX_REASON]
                        LOG.warning(
                            "Batch dry run %s file %s (%s) has an invalid stored probe",
                            report.id,
                            captured.id,
                            captured.path,
                            extra={"error": detail},
                        )
                    else:
                        try:
                            async with asyncio.timeout(FILE_TIMEOUT_S):
                                sidecars = await wait_workspace_io(lambda path=captured.path: sidecar_subtitles(path))
                                result = await dry_run(
                                    report.definition,
                                    probe,
                                    self.host,
                                    captured.path,
                                    library_name=captured.library_name,
                                    sidecars=sidecars,
                                    metadata=await wait_workspace_io(
                                        lambda path=captured.path: sidecar_metadata(path, definition)
                                    ),
                                )
                            row.status = result.flow.status
                            row.reason = (
                                result.flow.reason
                                or " ".join(
                                    step.error or step.reason
                                    for step in result.flow.steps
                                    if step.decision != "deferred" and (step.error or step.reason)
                                )
                            )[:MAX_REASON]
                            row.deferred_output = any(
                                step.decision == "deferred" and step.reason == "awaits the verified output probe"
                                for step in result.flow.steps
                            )
                            row.deferred_encoder = any(
                                step.decision == "deferred"
                                and step.reason == "awaits compatible node encoder selection"
                                for step in result.flow.steps
                            )
                        except TimeoutError as exc:
                            row.status, row.reason = "error", f"File evaluation exceeded {FILE_TIMEOUT_S:g} seconds."
                            LOG.warning(
                                "Batch dry run %s file %s (%s) timed out after %s seconds",
                                report.id,
                                captured.id,
                                captured.path,
                                FILE_TIMEOUT_S,
                                extra={"error": error_message(exc)},
                            )
                        except (OSError, ProbeError) as exc:
                            detail = error_message(exc)
                            row.status, row.reason = (
                                "unavailable",
                                f"Source is missing, changed, or unreadable. {detail}"[:MAX_REASON],
                            )
                            LOG.warning(
                                "Batch dry run %s file %s (%s) is unavailable",
                                report.id,
                                captured.id,
                                captured.path,
                                extra={"error": detail},
                            )
                        except Exception as exc:
                            detail = error_message(exc)
                            row.status, row.reason = "error", detail[:MAX_REASON]
                            LOG.warning(
                                "Batch dry run %s file %s (%s) failed",
                                report.id,
                                captured.id,
                                captured.path,
                                extra={"error": detail},
                            )
                        if not await self._unchanged(captured):
                            row.status, row.reason = (
                                "unavailable",
                                "Indexed source or stored probe changed during evaluation.",
                            )
                            row.deferred_output = row.deferred_encoder = False
                report.processed += 1
                report.counts[row.status] += 1

    @staticmethod
    def _request_cancel(report: BatchReport) -> None:
        if (
            report.state in ACTIVE_STATES
            and report.task is not None
            and not report.task.done()
            and not report.cancel_requested
        ):
            report.cancel_requested = True
            report.state = "cancelling"
            if report.started:
                report.task.cancel()

    async def cancel(self, report: BatchReport) -> None:
        self._request_cancel(report)
        if report.task is not None:
            await _drain(report.task)

    @asynccontextmanager
    async def pause(self) -> AsyncGenerator[None]:
        self._paused += 1
        try:
            if self.active is not None:
                await self.cancel(self.active)
            if self.host.unconfirmed_exit:
                raise BatchError(
                    "Cannot change configuration because we couldn't confirm that a plugin runner exited.", 409
                )
            yield
        finally:
            self._paused -= 1

    async def aclose(self) -> None:
        self._closed = True
        async with self.pause():
            self.reports.clear()

    async def purge(self) -> None:
        now = self.clock()
        if (
            self.active is not None
            and self.active.task is not None
            and self.active.task.done()
            and not self.host.unconfirmed_exit
        ):
            self.active = None
        if (
            self.active is not None
            and self.active.session_expires_at is not None
            and self.active.session_expires_at <= now
        ):
            await self.cancel(self.active)
        for id_, report in list(self.reports.items()):
            if report.expires_at is not None and report.expires_at <= now:
                self.reports.pop(id_, None)

    async def get(self, library_id: int | None, run_id: str, principal: Principal) -> BatchReport:
        await self.purge()
        report = self.reports.get(run_id)
        if (
            report is None
            or report.library_id != library_id
            or report.requested_by != (principal.user_id, principal.session_hash)
        ):
            raise BatchError("Dry-run report not found or expired.", 404)
        return report

    async def list_reports(
        self, library_id: int | None, principal: Principal, *, file_ids: list[int] | None = None
    ) -> list[dict]:
        await self.purge()
        selection = sorted(set(file_ids)) if file_ids is not None else None
        return [
            report.payload(limit=0)
            for report in reversed(self.reports.values())
            if report.library_id == library_id
            and report.file_ids == selection
            and report.requested_by == (principal.user_id, principal.session_hash)
        ]

    @staticmethod
    def export(report: BatchReport) -> str:
        if report.state in ACTIVE_STATES:
            raise BatchError("Wait for the dry run to finish or cancel it before exporting.", 409)
        if not report.snapshot_complete:
            raise BatchError("The file snapshot is incomplete. This report cannot be exported.", 409)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "root",
                "file",
                "status",
                "reason",
                "deferred_output",
                "deferred_encoder",
                "flow",
                "flow_version",
                "run_state",
                "complete",
                "run_error",
            ]
        )
        for row in report.rows:
            values = [
                row.root,
                row.file,
                row.status,
                row.reason,
                row.deferred_output,
                row.deferred_encoder,
                report.flow_name,
                report.flow_version,
                report.state,
                report.state == "completed",
                report.error,
            ]
            writer.writerow(
                [
                    "'" + value
                    if isinstance(value, str)
                    and value
                    and (value[0].isspace() or value.startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")))
                    else value
                    for value in values
                ]
            )
        return output.getvalue()
