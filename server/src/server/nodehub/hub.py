from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from common.actions import Action, ActionOutcome, PreserveTimestamps, extraction_error
from common.ctx import PluginContext
from common.diagnostics import error_message, redact
from common.enums import ExecutionLocation, HealthStatus, JobStatus, TranscodeStatus
from common.journal import read_messages
from common.media.probe import ProbeData, probe_from_raw
from common.media.tools import ProbeError, run_ffprobe
from common.plan import FFmpegPlan, dst_for, render
from common.protocol import (
    ActionReport,
    ActionTask,
    AttemptMissing,
    Cancel,
    Enrolled,
    EnrollmentSaved,
    Error,
    Finished,
    Heartbeat,
    Hello,
    JobOffer,
    NodeCapabilities,
    NodeLogs,
    OfferedJob,
    Progress,
    Welcome,
    WorkspaceChallenge,
    WorkspaceResponse,
    parse_message,
)
from common.protocol.messages import SetLimits
from common.workspace import DEFAULT_MIN_FREE_GB, resolve_workspace_path, workspace_lock
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, func, or_, select, tuple_, update

from server.events import EventBus, publish_job_status
from server.finalization import (
    FinalizationError,
    discard_rejected_artifact,
    finalize,
    publish_artifacts,
    validate_artifact,
)
from server.flows.engine import FlowContinuation, FlowResult, decision_entry, evaluate_post
from server.flows.policy import filter_actions
from server.flows.sidecars import sidecar_subtitles
from server.fs.library import LibraryFS, LibraryProbeError, apply_probe_metadata
from server.fs.source import authorized_library, capture_source, lock_job_source
from server.metadata_jobs import cancel_metadata, fail_metadata
from server.metrics import JOBS_FINISHED, NODES_CONNECTED
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, NodeRow, ProbeRow, utcnow
from server.nodehub.matching import can_execute, requirements_met
from server.nodehub.store import get_node, hash_token, register_node, set_node_state, touch_node
from server.queue.store import (
    TERMINAL_STATUSES,
    claim,
    current_attempt,
    finish,
    finish_artifact_check,
    get_job,
    release_transcode_decision,
    requeue,
    transition,
)
from server.scanner.normalize import normalize
from server.scanner.scan import _lacks_stream_bitrates
from server.scheduler.schedule import active, parse_schedule
from server.serialize import iso_utc
from server.staging import StagingService
from server.storage import storage_io, storage_scope
from server.workspace import (
    _stage_source,
    create_attempt_layout,
    post_action_jobs,
    read_attempt_log,
    read_attempt_logs,
    recovery_jobs,
    retain_unread_log,
    snapshot_attempt_log,
    staged_source,
    staged_subtitle,
    wait_workspace_io,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
    from typing import NoReturn

    from sqlalchemy.ext.asyncio import AsyncSession

    from server.healthcheck import HealthcheckService
    from server.plugins.host import PluginHost
    from server.probing import ProbeService

LOG = logging.getLogger(__name__)

_T = TypeVar("_T")
_ACTIONS_ADAPTER = TypeAdapter(list[Action])

_TERMINAL_STATUS_VALUES = frozenset(status.value for status in TERMINAL_STATUSES)
QUERY_BATCH = 100


def _post_error(message: str) -> NoReturn:
    raise ValueError(message)


async def _inspect_output(
    root: Path, relative: str, binary: str | Path, input_probe: ProbeData, plan: FFmpegPlan
) -> tuple[ProbeData, str | None]:
    path = await wait_workspace_io(lambda: resolve_workspace_path(root, relative))
    with workspace_lock(path.parent):
        if not await wait_workspace_io(path.is_file):
            raise ValueError("workspace artifact is not a regular file")
        raw = await run_ffprobe(binary, path)
        inspected = await wait_workspace_io(lambda: probe_from_raw(path, path.stat().st_size, raw))
        try:
            verified = await wait_workspace_io(lambda: validate_artifact(raw, path, input_probe, plan))
        except Exception as exc:
            return inspected, error_message(exc)
        return verified, None


def _inspect_artifact(
    root: Path, relative: str, binary: str | Path, input_probe: ProbeData, plan: FFmpegPlan
) -> tuple[ProbeData, str | None]:
    # Keep the tool and its workspace lock in the storage process until actual
    # exit. The API retains the inspected probe even when validation rejects it.
    return asyncio.run(_inspect_output(root, relative, binary, input_probe, plan))


def _inspection_probe(root: Path, relative: str, binary: str) -> tuple[ProbeData, bool]:
    path = resolve_workspace_path(root, relative)
    raw = asyncio.run(run_ffprobe(binary, path))
    return probe_from_raw(path, path.stat().st_size, raw).model_copy(
        update={"file_path": relative}
    ), _lacks_stream_bitrates(raw)


def _prepare_challenge(root: Path, relative: str, challenge: bytes) -> None:
    directory = resolve_workspace_path(root, relative)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "challenge").write_bytes(challenge)


def _check_challenge(root: Path, relative: str, expected: bytes) -> str | None:
    path = root / relative
    if not path.is_file():
        return f"Workspace response file is missing or not a regular file: {path}"
    try:
        resolve_workspace_path(root, relative)
    except ValueError:
        return f"Workspace response path is unsafe or contains a symlink: {path}"
    if path.stat().st_size != len(expected) or path.read_bytes() != expected:
        return f"Workspace response digest does not match the challenge: {path}"
    return None


def _cleanup_challenge(root: Path, relative: str) -> None:
    try:
        directory = resolve_workspace_path(root, relative)
    except ValueError:
        # Preparation can refuse an unsafe root. Never clean an unconfined path.
        return
    shutil.rmtree(directory, ignore_errors=True)


def _hardware_name(caps: NodeCapabilities) -> str:
    cpu = caps.cpu_model.lower()
    tier = re.search(r"\bi[3579](?=\W|$)", cpu)
    if tier:
        parts = [tier.group()]
    elif match := re.search(r"\bryzen\s+([3579])\b", cpu):
        parts = [f"ryzen{match.group(1)}"]
    else:
        parts = [next((name for name in ("xeon", "epyc", "apple") if name in cpu), "node")]
    gpu_names = " ".join(caps.gpus).lower()
    for label, brand in (("igpu", "intel"), ("nvidia", "nvidia"), ("amd", "amd")):
        if brand in gpu_names:
            parts.append(label)
    if caps.gpus and len(parts) == 1:
        parts.append("gpu")
    parts.append(secrets.token_hex(2))
    return "-".join(parts)


class NodeConnection(Protocol):
    async def send_json(self, data: dict) -> None: ...
    async def recv_json(self) -> dict | None: ...
    async def close(self, code: int = 1000) -> None: ...


@dataclass(kw_only=True, slots=True)
class NodeEntry:
    name: str
    node_db_id: int
    capabilities: NodeCapabilities
    workspace_ready: bool
    conn: NodeConnection
    paused: bool = False
    in_flight: dict[int, str] = field(default_factory=dict)
    offered_at: dict[int, datetime] = field(default_factory=dict)
    last_progress_at: dict[int, datetime] = field(default_factory=dict)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)


async def probe_for_file(session: AsyncSession, media_file: MediaFile) -> ProbeData | None:
    probe = await session.scalar(select(ProbeRow).where(ProbeRow.media_file_id == media_file.id))
    return stored_probe(media_file, probe)


def stored_probe(media_file: MediaFile, probe: ProbeRow | None) -> ProbeData | None:
    if probe is None:
        return None
    try:
        raw = json.loads(probe.raw_json)
        stat = os.stat_result((0, 0, 0, 0, 0, 0, media_file.size, 0, 0, 0))
        return normalize(Path(media_file.path), stat, raw).probe
    except ValueError, TypeError, KeyError, AttributeError, OverflowError:
        LOG.warning("stored probe unavailable for media file %s", media_file.id)
        return None


class NodeHub:
    def __init__(
        self,
        sessionmaker,
        *,
        workspace_dir: Path,
        min_free_gb: float = DEFAULT_MIN_FREE_GB,
        trash_dir: Path | None = None,
        lease_ttl_s: float = 300.0,
        heartbeat_s: float = 15.0,
        stall_timeout_s: float = 600.0,
        max_attempts: int = 3,
        dispatch_interval_s: float = 1.0,
        sweep_interval_s: float = 1.0,
        hello_timeout_s: float = 10.0,
        enrollment_token: str | None = None,
        replace_original: bool = True,
        action_task_timeout_s: float = 30.0,
        ffprobe_bin: str = "ffprobe",
        mkvpropedit_bin: str = "mkvpropedit",
        events: EventBus | None = None,
        plugin_host: PluginHost | None = None,
        clock: Callable[[], datetime] = utcnow,
        timezone: tzinfo = UTC,
        activity_paused: Callable[[], bool] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.workspace_dir = workspace_dir.resolve()
        self.staging = StagingService(
            sessionmaker,
            self.workspace_dir,
            events=events,
            clock=clock,
            staged=self._staged,
            min_free_gb=min_free_gb,
        )
        configured_trash = trash_dir or self.workspace_dir.parent / "trash"
        if configured_trash.is_symlink():
            raise ValueError("trash directory must not be a symlink")
        self.trash_dir = configured_trash.resolve()
        if self.trash_dir.is_relative_to(self.workspace_dir) or self.workspace_dir.is_relative_to(self.trash_dir):
            raise ValueError("workspace and trash directories must be separate without overlap")
        self.lease_ttl_s = lease_ttl_s
        self.heartbeat_s = heartbeat_s
        self.stall_timeout_s = stall_timeout_s
        self.max_attempts = max_attempts
        self.dispatch_interval_s = dispatch_interval_s
        self.sweep_interval_s = sweep_interval_s
        self.hello_timeout_s = hello_timeout_s
        self.enrollment_token = enrollment_token
        self.replace_original = replace_original
        self.action_task_timeout_s = action_task_timeout_s
        self.ffprobe_bin = ffprobe_bin
        self.mkvpropedit_bin = mkvpropedit_bin
        self.events = events
        self.plugin_host = plugin_host
        self.clock = clock
        self.timezone = timezone
        self.activity_paused = activity_paused
        self.healthcheck_wake: Callable[[], None] | None = None
        self.healthcheck_service: HealthcheckService | None = None
        self.probe_service: ProbeService | None = None
        self.entries: dict[str, NodeEntry] = {}
        self._wake = asyncio.Event()
        self._stall_cancelled: set[int] = set()
        self._cancel_requested: set[int] = set()
        self._signals: dict[str, asyncio.Event] = {}
        self._action_reports: dict[tuple[int, int], asyncio.Future[list[ActionOutcome]]] = {}
        self._action_nodes: dict[tuple[int, int], int] = {}
        self._tasks: list[asyncio.Task] = []
        self._close_task: asyncio.Task[None] | None = None
        self._action_waits: set[asyncio.Task] = set()
        self._action_jobs: dict[asyncio.Task, int] = {}
        self._finishing: dict[int, set[asyncio.Future[None]]] = {}
        self._completions: dict[tuple[int, int, int, str, str | None], asyncio.Task[None]] = {}
        self._completion_kinds: dict[tuple[int, int, int, str, str | None], str] = {}
        self._library_cursors: dict[int, int] = {}
        self._staging_slots: dict[
            int, tuple[FFmpegPlan, ProbeData, list[str] | None, list[str] | None, int | None]
        ] = {}
        self._staging_retry_at: dict[int, datetime] = {}
        self.dispatch_lock = asyncio.Lock()
        self._reset_active = False
        self._reset_done = asyncio.Event()
        self._reset_done.set()
        self._entry_removed = asyncio.Event()
        self._inspection_tasks: dict[int, asyncio.Task[None]] = {}

    async def serve_connection(self, conn: NodeConnection) -> None:
        entry: NodeEntry | None = None
        node_name = "unidentified node"
        try:
            hello = await self._read_hello(conn)
            if hello is None:
                return
            node_name = hello.node_id or "enrolling node"
            while True:
                await self._reset_done.wait()
                async with self.dispatch_lock:
                    if self._reset_active:
                        continue
                    handshake = await self._handshake_locked(conn, hello)
                    if handshake is None:
                        return
                    hello, node_db_id, limits, paused = handshake
                    node_name = hello.node_id
                    assert node_name is not None
                    entry = NodeEntry(
                        name=node_name,
                        node_db_id=node_db_id,
                        capabilities=hello.capabilities.model_copy(update=limits),
                        workspace_ready=False,
                        conn=conn,
                        paused=paused,
                    )
                    previous = self.entries.get(entry.name)
                    if previous is not None:
                        entry.in_flight = dict(previous.in_flight)
                        entry.offered_at = dict(previous.offered_at)
                        entry.last_progress_at = dict(previous.last_progress_at)
                        self.entries[entry.name] = entry
                        if not previous.disconnected.is_set():
                            await previous.conn.close()
                    else:
                        self.entries[entry.name] = entry
                    async with self.sessionmaker() as session:
                        after = 0
                        while True:
                            jobs = list(
                                await session.scalars(
                                    select(Job)
                                    .where(
                                        Job.id > after,
                                        Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                                        Job.node_id == node_db_id,
                                        Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                                    )
                                    .order_by(Job.id)
                                    .limit(QUERY_BATCH)
                                )
                            )
                            if not jobs:
                                break
                            after = jobs[-1].id
                            for job in jobs:
                                entry.in_flight[job.id] = job.kind
                                entry.offered_at.setdefault(job.id, self.clock())
                    NODES_CONNECTED.set(sum(not item.disconnected.is_set() for item in self.entries.values()))
                    self._wake.set()
                    break
            await self.read_journals(entry.node_db_id, wait=False)
            active = {(item.job_id, item.attempt_id) for item in hello.active_attempts}
            after = 0
            while True:
                async with self.sessionmaker() as session:
                    attempts = (
                        await session.execute(
                            select(JobAttempt.job_id, JobAttempt.id, JobAttempt.log_path)
                            .join(Job, (Job.id == JobAttempt.job_id) & (Job.attempts == JobAttempt.attempt_number))
                            .where(
                                Job.id > after,
                                Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                                Job.node_id == entry.node_db_id,
                                Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                                JobAttempt.node_id == entry.node_db_id,
                            )
                            .order_by(Job.id)
                            .limit(QUERY_BATCH)
                        )
                    ).all()
                if not attempts:
                    break
                after = attempts[-1].job_id
                for attempt in attempts:
                    if (attempt.job_id, attempt.id) not in active:
                        await self._finish_missing(
                            entry,
                            AttemptMissing(job_id=attempt.job_id, attempt_id=attempt.id, log_path=attempt.log_path),
                        )
            while True:
                await self._reset_done.wait()
                async with self.dispatch_lock:
                    if self._reset_active:
                        continue
                    if self.entries.get(entry.name) is not entry:
                        return
                    entry.workspace_ready = True
                    self._fire(f"node:{entry.name}:ready")
                    self._wake.set()
                    break
            await self._receive_loop(entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception(
                "Node connection failed for %s in workspace %s",
                node_name,
                self.workspace_dir,
                extra={"error": error_message(exc)},
            )
        finally:
            if entry is not None:
                cleanup = asyncio.create_task(self._disconnect_entry(entry))
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError
            self._wake.set()

    async def _disconnect_entry(self, entry: NodeEntry) -> None:
        try:
            async with self.dispatch_lock:
                if self.entries.get(entry.name) is not entry:
                    return
                entry.disconnected.set()
                entry.workspace_ready = False
                if not entry.in_flight:
                    self.entries.pop(entry.name, None)
                NODES_CONNECTED.set(sum(not item.disconnected.is_set() for item in self.entries.values()))
                self._entry_removed.set()
                LOG.info(
                    "Node %s disconnected",
                    entry.name,
                    extra={"operation": "node_connection", "outcome": "disconnected", "node_id": entry.name},
                )
                async with self.sessionmaker() as session:
                    await session.execute(
                        update(Job)
                        .where(
                            Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                            Job.node_id == entry.node_db_id,
                            Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                        )
                        .values(lease_expires_at=self.clock() + timedelta(seconds=self.lease_ttl_s))
                    )
                    await session.execute(
                        update(NodeRow)
                        .where(NodeRow.id == entry.node_db_id)
                        .values(
                            state="offline", last_seen_at=self.clock(), workspace_ready=False, workspace_free_bytes=None
                        )
                    )
                    await session.commit()
                self._fire(f"node:{entry.name}:offline")
                self._publish_node(entry.name, "offline")
            await self.read_journals(entry.node_db_id)
            await asyncio.gather(
                *(asyncio.shield(task) for key, task in tuple(self._completions.items()) if key[0] == entry.node_db_id),
                return_exceptions=True,
            )
        except Exception:
            LOG.exception("cleanup for node %s failed", entry.name)
        finally:
            entry.disconnected.set()
            self._wake.set()

    async def wait_node_state(self, name: str, state: str, timeout_s: float = 5.0) -> None:
        async def check() -> bool | None:
            async with self.sessionmaker() as session:
                node = await get_node(session, name)
                if node is not None and node.state == state:
                    return True
                return None

        await self._await_signal(f"node:{name}:{state}", check, timeout_s)

    def _fire(self, key: str) -> None:
        # Pop before set. A transition releases current waiters. Later waiters
        # re-register a fresh event and verify the DB themselves.
        event = self._signals.pop(key, None)
        if event is not None:
            event.set()

    def _publish(self, type_: str, payload: dict, key: str | None = None) -> None:
        if self.events is not None:
            self.events.publish(type_, payload, key=key)

    def _publish_node(self, name: str, state: str) -> None:
        entry = self.entries.get(name)
        self._publish(
            "node.status",
            {
                "name": name,
                "state": state,
                "active_jobs": len(entry.in_flight) if entry is not None else 0,
                "active_transcodes": sum(kind == "transcode" for kind in entry.in_flight.values())
                if entry is not None
                else 0,
                "active_healthchecks": sum(kind == "healthcheck" for kind in entry.in_flight.values())
                if entry is not None
                else 0,
                "active_mkv_stats": sum(kind == "mkv_stats" for kind in entry.in_flight.values())
                if entry is not None
                else 0,
                "active_job_ids": sorted(entry.in_flight) if entry is not None else [],
            },
            key=f"node.status:{name}",
        )

    async def _publish_status(self, job: Job) -> None:
        if self.events is not None:
            await publish_job_status(self.events, self.sessionmaker, job.id)

    def _signal_for(self, key: str) -> asyncio.Event:
        event = self._signals.get(key)
        if event is None:
            event = asyncio.Event()
            self._signals[key] = event
        return event

    async def _await_signal(self, key: str, check: Callable[[], Awaitable[_T | None]], timeout_s: float) -> _T | None:
        async with asyncio.timeout(timeout_s):
            while True:
                event = self._signal_for(key)
                found = await check()
                if found is not None:
                    return found
                await event.wait()

    async def dispatch_once(self) -> int:
        async with self.dispatch_lock:
            if self._reset_active or self._close_task is not None:
                return 0
            return await self._dispatch_inspections() + await self._dispatch_once()

    def inspection_available(self, kind: str) -> bool:
        return self._inspection_node(kind) is not None

    def _inspection_node(self, kind: str) -> NodeEntry | None:
        return next(
            (
                entry
                for entry in self.entries.values()
                if not entry.disconnected.is_set()
                and not entry.paused
                and entry.workspace_ready
                and (kind != "mkv_stats" or entry.capabilities.mkvpropedit)
                and sum(value == kind for value in entry.in_flight.values())
                < getattr(entry.capabilities, f"max_{'healthchecks' if kind == 'healthcheck' else 'mkv_stats'}")
            ),
            None,
        )

    async def _dispatch_inspections(self) -> int:
        count = 0
        if self.activity_paused is not None and self.activity_paused():
            return 0
        self._staging_retry_at = {
            job_id: until for job_id, until in self._staging_retry_at.items() if until > self.clock()
        }
        for kind in ("healthcheck", "mkv_stats"):
            if kind == "healthcheck":
                if self.healthcheck_service is None:
                    continue
                location = self.healthcheck_service.location
            else:
                if self.probe_service is None:
                    continue
                location = self.probe_service.statistics_location
            if location == ExecutionLocation.LOCAL:
                continue
            while (entry := self._inspection_node(kind)) is not None:
                min_free_gb = self.staging.min_free_gb
                hwaccel = self.healthcheck_service.hwaccel if self.healthcheck_service is not None else True
                if kind == "healthcheck":
                    assert self.healthcheck_service is not None
                    async with self.healthcheck_service.dispatch_lock:
                        claimed = await self.healthcheck_service.claim_next(
                            node_id=entry.node_db_id,
                            exclude_jobs=set(self._staging_retry_at) | self._inspection_tasks.keys(),
                            lease_expires_at=self.clock() + timedelta(seconds=self.lease_ttl_s),
                        )
                else:
                    assert self.probe_service is not None
                    async with self.probe_service.dispatch_lock:
                        claimed = await self.probe_service._claim(
                            kind,
                            node_id=entry.node_db_id,
                            exclude_jobs=set(self._staging_retry_at) | self._inspection_tasks.keys(),
                            lease_expires_at=self.clock() + timedelta(seconds=self.lease_ttl_s),
                        )
                if claimed is None:
                    break
                job_id, attempt_id = claimed[:2]
                entry.in_flight[job_id] = kind
                entry.offered_at[job_id] = self.clock()
                task = asyncio.create_task(
                    self._offer_inspection(entry, job_id, attempt_id, min_free_gb=min_free_gb, hwaccel=hwaccel)
                )
                self._inspection_tasks[job_id] = task
                task.add_done_callback(lambda done, jid=job_id: self._inspection_done(jid, done))
                count += 1
        return count

    def _inspection_done(self, job_id: int, task: asyncio.Task[None]) -> None:
        self._inspection_tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Inspection staging failed for job %s", job_id, exc_info=task.exception())
        self.wake()

    async def _offer_inspection(
        self, entry: NodeEntry, job_id: int, attempt_id: int, *, min_free_gb: float, hwaccel: bool
    ) -> None:
        async with contextlib.AsyncExitStack() as copying:
            await copying.enter_async_context(self.staging._stage_lock)
            await self._stage_inspection(
                entry, job_id, attempt_id, min_free_gb=min_free_gb, hwaccel=hwaccel, copying=copying
            )

    async def _stage_inspection(
        self,
        entry: NodeEntry,
        job_id: int,
        attempt_id: int,
        *,
        min_free_gb: float,
        hwaccel: bool,
        copying: contextlib.AsyncExitStack,
    ) -> None:
        current = asyncio.current_task()
        assert current is not None
        current.set_name("inspection-running")
        delivered = False
        self.staging.busy_jobs.add(job_id)
        try:
            if job_id in self._cancel_requested:
                raise asyncio.CancelledError  # noqa: TRY301 -- Operator cancelled staging.
            async with self.sessionmaker() as session:
                job = await lock_job_source(session, job_id)
                attempt = await session.get(JobAttempt, attempt_id)
                media = await session.get(MediaFile, job.media_file_id) if job is not None else None
                if (
                    job is None
                    or attempt is None
                    or media is None
                    or attempt.job_id != job_id
                    or attempt.node_id != entry.node_db_id
                    or job.node_id != entry.node_db_id
                    or job.attempts != attempt.attempt_number
                    or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                    or attempt.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                ):
                    return
                library = await authorized_library(session, job.library_id, media)
                if library is None:
                    _post_error("Inspection source is no longer authorized.")
                source = await wait_workspace_io(lambda: capture_source(Path(media.path)))
                if source["size"] != media.size:
                    _post_error("Inspection source changed before staging. Rescan the file.")
                attempt.source_size = source["size"]
                attempt.capabilities = entry.capabilities.model_dump(mode="json")
                job.lease_expires_at = self.clock() + timedelta(seconds=self.lease_ttl_s)
                session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="inspection_source", payload=source))
                await session.commit()
                warning = await self.staging._capacity_warning(
                    session, job_id, source["size"], 0, min_free_gb=min_free_gb
                )
                await session.commit()
                if warning is not None:
                    job = await lock_job_source(session, job_id)
                    attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
                    if (
                        job is not None
                        and attempt is not None
                        and job.attempts == attempt.attempt_number
                        and job.status == JobStatus.CLAIMED
                        and attempt.log_path is None
                    ):
                        # No files were copied or job offered. Do not count a
                        # disk-space wait as an attempt.
                        await session.execute(delete(JobEvent).where(JobEvent.attempt_id == attempt_id))
                        await session.delete(attempt)
                        job.attempts -= 1
                        job.status = JobStatus.QUEUED
                        job.node_id = None
                        job.lease_expires_at = None
                        job.progress = None
                        job.warning = warning
                        session.add(JobEvent(job_id=job_id, type="waiting", payload={"reason": warning}))
                        await session.commit()
                    self._staging_retry_at[job_id] = self.clock() + timedelta(seconds=self.dispatch_interval_s)
                    self._release_slot(entry, job_id)
                    if job is not None:
                        await self._publish_status(job)
                    return
                relative = await storage_io(
                    _stage_source,
                    self.workspace_dir,
                    job_id,
                    Path(source["path"]),
                    expected_source=source,
                    fresh=attempt.attempt_number > 1,
                )
                layout = await wait_workspace_io(
                    lambda: create_attempt_layout(
                        self.workspace_dir, job_id, attempt.attempt_number, Path(source["path"]).suffix
                    )
                )
                probe, lacks_bitrates = await storage_io(
                    _inspection_probe, self.workspace_dir, relative, self.ffprobe_bin
                )
                statistics_edit = library.mkv_stats and Path(source["path"]).suffix.lower() == ".mkv" and lacks_bitrates
                job = await lock_job_source(session, job_id)
                attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
                if (
                    job is None
                    or attempt is None
                    or job.node_id != entry.node_db_id
                    or job.attempts != attempt.attempt_number
                    or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                ):
                    return
                if job_id in self._cancel_requested:
                    raise asyncio.CancelledError  # noqa: TRY301 -- Operator cancelled staging.
                attempt.source_path = relative
                job.warning = None
                attempt.output_path = layout["output"]
                attempt.log_path = layout["log"]
                attempt.input_probe = probe.model_dump(mode="json")
                attempt.log_data = None
                job.plan = FFmpegPlan().model_dump(mode="json")
                session.add(
                    JobEvent(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        type="stage",
                        payload={
                            "number": 1,
                            "plan": job.plan,
                            "probe": attempt.input_probe,
                            "output_path": attempt.output_path,
                            "log_path": attempt.log_path,
                            "actions": [],
                            "post_actions": [],
                            "node": entry.name,
                            "statistics_edit": statistics_edit,
                        },
                    )
                )
                await session.commit()
                await copying.aclose()
                self.staging.busy_jobs.discard(job_id)
                offer = OfferedJob(
                    job_id=job_id,
                    attempt_id=attempt_id,
                    kind="healthcheck" if job.kind == "healthcheck" else "mkv_stats",
                    source_path=relative,
                    output_path=layout["output"],
                    log_path=layout["log"],
                    probe=probe,
                    healthcheck_hwaccel=hwaccel,
                    statistics_edit=statistics_edit,
                    min_free_gb=min_free_gb,
                )

                async def ready() -> tuple[NodeEntry | None] | None:
                    async with self.sessionmaker() as checking:
                        current_job = (
                            await checking.execute(
                                select(Job.node_id, Job.attempts, Job.status).where(Job.id == job_id)
                            )
                        ).one_or_none()
                    if (
                        current_job is None
                        or current_job.node_id != entry.node_db_id
                        or current_job.attempts != attempt.attempt_number
                        or current_job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                    ):
                        return (None,)
                    target = self.entries.get(entry.name)
                    return (
                        (target,)
                        if target is not None
                        and target.node_db_id == entry.node_db_id
                        and target.workspace_ready
                        and not target.disconnected.is_set()
                        else None
                    )

                try:
                    async with asyncio.timeout(self.lease_ttl_s):
                        while True:
                            target = await self._await_signal(f"node:{entry.name}:ready", ready, self.lease_ttl_s)
                            if target is None or target[0] is None:
                                return
                            async with self.dispatch_lock:
                                target = await ready()
                                if target is None:
                                    continue
                                if target[0] is None:
                                    return
                                entry = target[0]
                                # A send error does not prove the node received nothing.
                                # Keep the attempt until a report or timeout.
                                entry.offered_at[job_id] = self.clock()
                                current.set_name("inspection-offered")
                                delivered = True
                                await entry.conn.send_json(JobOffer(job=offer).model_dump(mode="json"))
                                break
                except TimeoutError:
                    if delivered:
                        raise
                    # No job was sent. Let the existing timeout retry it.
                    return
                self._publish_node(entry.name, "online")
                await self._publish_status(job)
        except (Exception, asyncio.CancelledError) as exc:
            if delivered:
                LOG.warning("Inspection offer delivery is uncertain for job %s: %s", job_id, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            async with self.sessionmaker() as session:
                job = await lock_job_source(session, job_id)
                attempt = await session.get(JobAttempt, attempt_id)
                can_finish = (
                    job is not None
                    and attempt is not None
                    and attempt.job_id == job_id
                    and attempt.node_id == entry.node_db_id
                    and job.node_id == entry.node_db_id
                    and job.attempts == attempt.attempt_number
                    and job.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
                    and attempt.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
                )
                if can_finish:
                    assert job is not None
                    if job.kind == "mkv_stats":
                        if isinstance(exc, asyncio.CancelledError):
                            await cancel_metadata(session, job)
                        else:
                            await fail_metadata(session, job)
                    await finish(
                        session,
                        job_id,
                        JobStatus.CANCELLED if isinstance(exc, asyncio.CancelledError) else JobStatus.FAILED,
                        error=str(exc) or "Inspection staging cancelled.",
                    )
                    job.node_id = None
                    job.lease_expires_at = None
                    await session.commit()
                    await self._publish_status(job)
            if can_finish:
                self._release_slot(entry, job_id)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.staging.busy_jobs.discard(job_id)

    @asynccontextmanager
    async def _dispatch_candidates(self) -> AsyncIterator[AsyncIterator[int]]:
        async with self.sessionmaker() as session:
            upper = await session.scalar(select(func.max(Job.id))) or 0
        eligible = (
            Job.id <= upper,
            Job.kind == "transcode",
            Job.delete_requested.is_(False),
            or_(
                Job.status.in_([JobStatus.QUEUED, JobStatus.STAGING]),
                (Job.status == JobStatus.RUNNING) & Job.node_id.is_(None),
            ),
        )

        async def candidates() -> AsyncGenerator[int]:
            priority = None
            while True:
                async with self.sessionmaker() as session:
                    priority = await session.scalar(
                        select(func.max(Job.priority)).where(
                            *eligible, *([Job.priority < priority] if priority is not None else [])
                        )
                    )
                if priority is None:
                    return
                stmt = select(Job.created_at, Job.id).where(*eligible, Job.priority == priority)
                groups: dict[int, deque[tuple[datetime, int]]] = {}
                heads: list[tuple[datetime, int, int]] = []
                after_library = None
                while True:
                    async with self.sessionmaker() as session:
                        library_ids = list(
                            await session.scalars(
                                select(Job.library_id)
                                .where(
                                    *eligible,
                                    Job.priority == priority,
                                    *([Job.library_id > after_library] if after_library is not None else []),
                                )
                                .distinct()
                                .order_by(Job.library_id)
                                .limit(QUERY_BATCH)
                            )
                        )
                        for library_id in library_ids:
                            rows = (
                                await session.execute(
                                    stmt.where(Job.library_id == library_id)
                                    .order_by(Job.created_at, Job.id)
                                    .limit(QUERY_BATCH)
                                )
                            ).all()
                            if rows:
                                groups[library_id] = deque((created, job_id) for created, job_id in rows)
                                heads.append((*groups[library_id][0], library_id))
                    if not library_ids:
                        break
                    after_library = library_ids[-1]

                # Freeze each library's head order before claims remove its head.
                # Subsequent rounds keep that order, not the order of new heads.
                libraries = [library_id for _created, _job_id, library_id in sorted(heads)]
                cursor = self._library_cursors.get(priority)
                start = (libraries.index(cursor) + 1) % len(libraries) if cursor in groups else 0
                after: dict[int, tuple[datetime, int]] = {}
                while libraries:
                    for offset in range(len(libraries)):
                        library_id = libraries[(start + offset) % len(libraries)]
                        jobs = groups[library_id]
                        if not jobs:
                            async with self.sessionmaker() as session:
                                rows = (
                                    await session.execute(
                                        stmt.where(
                                            Job.library_id == library_id,
                                            tuple_(Job.created_at, Job.id) > after[library_id],
                                        )
                                        .order_by(Job.created_at, Job.id)
                                        .limit(QUERY_BATCH)
                                    )
                                ).all()
                            jobs.extend((created, job_id) for created, job_id in rows)
                        if jobs:
                            after[library_id] = jobs.popleft()
                            yield after[library_id][1]
                        else:
                            groups.pop(library_id)
                            after.pop(library_id)
                    libraries = [library_id for library_id in libraries if library_id in groups]
                    start = 0

        async with contextlib.aclosing(candidates()) as stream:
            yield stream

    async def _dispatch_once(self) -> int:
        offered = 0
        now = self.clock()
        touched: list[int] = []
        requeued: list[Job] = []
        claimed_jobs: list[Job] = []
        waiting: list[Job] = []
        invalid: list[Job] = []
        cleanup_attempts: list[tuple[int, int, bool]] = []
        self._staging_retry_at = {job_id: until for job_id, until in self._staging_retry_at.items() if until > now}
        if not self._has_free_slot():
            return 0
        staging_reserved = self._staging_reservations()
        staging_current = len(self.staging.tasks)
        async with self.sessionmaker() as session, self._dispatch_candidates() as candidates:

            async def publish_updates() -> None:
                await session.commit()
                for job_id in touched:
                    self._fire(f"job:{job_id}")
                for job in invalid:
                    await self.staging.prune_job(job.id, attempt_number=job.attempts)
                for job in [*claimed_jobs, *waiting, *invalid]:
                    await self._publish_status(job)
                for job in requeued:
                    await self._cleanup_workspace(job.id, cleanup_attempts)
                    self._fire(f"job:{job.id}")
                    await self._publish_status(job)
                touched.clear()
                invalid.clear()
                claimed_jobs.clear()
                waiting.clear()
                requeued.clear()
                cleanup_attempts.clear()

            while True:
                await publish_updates()
                job_id = await anext(candidates, None)
                if job_id is None:
                    break
                job = await get_job(session, job_id)
                if job is None or job.status not in {JobStatus.QUEUED, JobStatus.STAGING, JobStatus.RUNNING}:
                    continue
                if job.delete_requested:
                    continue
                if job.status == JobStatus.RUNNING and job.node_id is not None:
                    continue
                # Later stages belong to an already active job and must still finish.
                if self.activity_paused is not None and self.activity_paused() and job.status != JobStatus.RUNNING:
                    continue
                if not self._has_free_slot():
                    break
                # A previous candidate's writes must not cover this candidate's storage checks.
                now = self.clock()
                if (
                    any(job.id in entry.in_flight for entry in self.entries.values())
                    or job.id in self.staging.tasks
                    or job.id in self.staging.active_jobs
                    or job.id in self.staging.busy_jobs
                    or job.id in self._finishing
                    or (job.status == JobStatus.STAGING and job.id in self._staging_retry_at)
                ):
                    continue
                if await session.scalar(
                    post_action_jobs(scoped=True).where(Job.media_file_id == job.media_file_id).limit(1)
                ):
                    continue
                library = await session.get(Library, job.library_id)
                if library is not None and library.suspended:
                    continue
                if not active(parse_schedule(library.schedule) if library is not None else None, now, self.timezone):
                    LOG.debug("Library %s schedule inactive. Skipping job %s", job.library_id, job.id)
                    continue
                media_file = await session.get(MediaFile, job.media_file_id)
                if media_file is None:
                    continue
                attempt = await current_attempt(session, job.id)
                stage = await self._stage_event(session, attempt) if job.status == JobStatus.RUNNING else None
                avoid_node_id = attempt.node_id if attempt is not None and stage is None else None
                data = stage.payload if stage is not None else {}
                captured: dict = {}
                if stage is None:
                    enqueued = await session.scalar(
                        select(JobEvent.payload)
                        .where(JobEvent.job_id == job.id, JobEvent.type == "enqueued")
                        .order_by(JobEvent.id.desc())
                        .limit(1)
                    )
                    captured = enqueued or {}
                    source = captured.get("source")
                    error = "Captured source is missing. Rescan and enqueue a new job." if not source else None
                    if error is not None:
                        if not await self._fail_inspection(session, job, error, now):
                            continue
                        waiting.append(job)
                        touched.append(job.id)
                        continue
                try:
                    plan = FFmpegPlan.model_validate(data.get("plan", job.plan))
                    raw_probe = data.get("probe") if stage is not None else captured.get("probe")
                    if raw_probe is None:
                        raise ValueError("probe missing")  # noqa: TRY301 -- Finish through the same per-job failure path.
                    probe = ProbeData.model_validate(raw_probe)
                    target, why = self._pick(
                        plan,
                        probe,
                        data.get("requires", job.requires),
                        data.get("requires_encoders", job.requires_encoders),
                        avoid_node_id=avoid_node_id,
                        reserved=staging_reserved if job.status == JobStatus.STAGING else None,
                    )
                    dst = dst_for(media_file.path, plan, input_container=probe.container)
                except ValueError as exc:
                    if not await self._fail_inspection(session, job, f"invalid dispatch plan: {exc}", now):
                        continue
                    invalid.append(job)
                    touched.append(job.id)
                    continue
                if target is None:
                    if await self._mark_waiting(session, job, why, now):
                        waiting.append(job)
                    continue
                if job.status == JobStatus.STAGING:
                    # Reserve compatible capacity without holding dispatch through filesystem copying.
                    free_slots = sum(
                        max(0, entry.capabilities.max_transcodes - self._node_load(entry))
                        for entry in self.entries.values()
                        if not entry.disconnected.is_set() and not entry.paused and entry.workspace_ready
                    )
                    # A restarted node can invalidate a reservation before its old writer drains.
                    if staging_current >= free_slots:
                        continue
                    if self.staging.schedule(job.id):
                        self._staging_slots[job.id] = (plan, probe, job.requires, job.requires_encoders, avoid_node_id)
                        # Keep this pass's reservations even if a fast copy completes during inspection.
                        staging_reserved[target.node_db_id] = staging_reserved.get(target.node_db_id, 0) + 1
                        staging_current += 1
                        self._library_cursors[job.priority] = job.library_id
                    continue
                first_start = job.started_at is None
                claimed = await claim(
                    session,
                    job.id,
                    target.node_db_id,
                    lease_ttl_s=self.lease_ttl_s,
                    now=now,
                    stage=stage is not None,
                    # Re-check the schedule at the locked claim transition.
                    clock=self.clock,
                    timezone=self.timezone,
                )
                if claimed is None:
                    continue
                now = self.clock()
                library = await session.get(Library, claimed.library_id, populate_existing=True)
                if (
                    library is None
                    or library.suspended
                    or library.deleted
                    or not active(parse_schedule(library.schedule), now, self.timezone)
                ):
                    # Claim can wait on the node lock after its schedule check.
                    # No workspace or offer exists yet, so release this claim entirely.
                    await session.rollback()
                    continue
                job = claimed
                job.lease_expires_at = now + timedelta(seconds=self.lease_ttl_s)
                if first_start:
                    job.started_at = now
                if job.warning is not None:
                    job.warning = None
                attempt = await current_attempt(session, job.id)
                if attempt is not None and stage is None:
                    attempt.claimed_at = now
                if attempt is None:
                    handled = await requeue(session, job.id, now=now, max_attempts=self.max_attempts)
                    if handled is not None:
                        requeued.append(handled)
                    continue
                try:
                    if stage is None:
                        # Detect existing recovery files before input validation can fail.
                        layout = create_attempt_layout(
                            self.workspace_dir, job.id, attempt.attempt_number, Path(dst).suffix
                        )
                    else:
                        parent = f"jobs/{job.id}/attempts/{attempt.attempt_number}"
                        number = data["number"]
                        layout = {
                            "output": f"{parent}/output-{number}{Path(dst).suffix}",
                            "log": f"{parent}/ffmpeg-{number}.log",
                        }
                    source_path = (
                        data["probe"]["file_path"] if stage is not None else staged_source(self.workspace_dir, job.id)
                    )
                    subtitle_path = (
                        staged_subtitle(self.workspace_dir, job.id, plan.burn_subtitle.filename)
                        if plan.burn_subtitle is not None
                        else None
                    )
                except (OSError, ValueError) as exc:
                    detail = f"Workspace staging failed: {error_message(exc)}"
                    LOG.warning(
                        "Could not stage job %s attempt %s for node %s (id %s), media %s (%s), workspace %s",
                        job.id,
                        attempt.id,
                        target.name,
                        target.node_db_id,
                        media_file.id,
                        media_file.path,
                        self.workspace_dir,
                        extra={"error": detail},
                    )
                    handled = await requeue(session, job.id, now=now, max_attempts=self.max_attempts)
                    if handled is not None:
                        # A failed directory claim does not permit deleting the existing directory.
                        retained = stage is not None or isinstance(exc, FileExistsError)
                        attempt.result = "interrupted" if retained else "failed"
                        handled.warning = f"{detail}. Existing files retained." if retained else detail
                        requeued.append(handled)
                        cleanup_attempts.append(
                            (job.id, attempt.attempt_number, handled.status in _TERMINAL_STATUS_VALUES)
                        )
                    session.add(
                        JobEvent(
                            job_id=job.id,
                            attempt_id=attempt.id,
                            type="offer_failed",
                            payload={
                                "error": detail,
                                "node": target.name,
                                "node_id": target.node_db_id,
                                "media_file_id": media_file.id,
                                "media_path": media_file.path,
                                "workspace": str(self.workspace_dir),
                            },
                            ts=now,
                        )
                    )
                    continue
                source_probe = probe.model_copy(update={"file_path": source_path})
                try:
                    offer_actions, rejected = self._offer_actions(
                        data.get("actions") if stage is not None else job.pre_actions
                    )
                    plan = plan.select_encoder(target.capabilities.encoders)
                    argv = render(plan, source_probe, source_path, layout["output"], subtitle_path=subtitle_path).argv
                    offered_job = OfferedJob(
                        job_id=job.id,
                        attempt_id=attempt.id,
                        source_path=source_path,
                        output_path=layout["output"],
                        log_path=layout["log"],
                        plan=plan,
                        probe=source_probe,
                        subtitle_path=subtitle_path,
                        actions=offer_actions,
                        stall_timeout_s=self.stall_timeout_s,
                        min_free_gb=self.staging.min_free_gb,
                    )
                except ValueError as exc:
                    await self._fail_dispatch(session, job, attempt, exc, now)
                    invalid.append(job)
                    touched.append(job.id)
                    continue
                if stage is None:
                    attempt.source_path = source_path
                    attempt.input_probe = source_probe.model_dump(mode="json") if source_probe else None
                attempt.output_path = layout["output"]
                attempt.log_path = layout["log"]
                attempt.log_data = None
                stage_data = {
                    **data,
                    "number": data.get("number", 1),
                    "plan": plan.model_dump(mode="json"),
                    "probe": source_probe.model_dump(mode="json"),
                    "output_path": layout["output"],
                    "log_path": layout["log"],
                    "node": target.name,
                    "capabilities": target.capabilities.model_dump(mode="json"),
                    "continuation": data.get("continuation", job.continuation),
                    "facets": data.get("facets", job.facets),
                    "post_actions": data.get("post_actions", job.post_actions or []),
                    "post_requires": data.get("post_requires", []),
                    "post_requires_encoders": data.get("post_requires_encoders", []),
                    "actions": [action.model_dump(mode="json") for action in offer_actions],
                    "argv": argv,
                }
                if stage is None:
                    session.add(
                        JobEvent(job_id=job.id, attempt_id=attempt.id, type="stage", payload=stage_data, ts=now)
                    )
                else:
                    stage.payload = stage_data
                if rejected:
                    session.add(
                        JobEvent(
                            job_id=job.id,
                            type="actions_rejected",
                            payload={"phase": "pre", "rejected": rejected},
                            ts=now,
                        )
                    )
                session.add(
                    JobEvent(
                        job_id=job.id,
                        attempt_id=attempt.id,
                        type="offered",
                        payload={"node": target.name},
                        ts=now,
                    )
                )
                await session.commit()
                target.in_flight[job.id] = "transcode"
                target.offered_at[job.id] = now
                self._stall_cancelled.discard(job.id)
                try:
                    await target.conn.send_json(JobOffer(job=offered_job).model_dump(mode="json"))
                except (Exception, asyncio.CancelledError) as exc:
                    # A failed send may already have delivered work. Only report
                    # completion or disconnect may release its assigned attempt.
                    detail = f"Job offer send failed: {error_message(exc)}"
                    LOG.warning(
                        "Could not send job %s attempt %s to node %s (id %s), media %s (%s), workspace %s, output %s. "
                        "Delivery may have occurred. The send failure does not release the assigned job.",
                        job.id,
                        attempt.id,
                        target.name,
                        target.node_db_id,
                        media_file.id,
                        media_file.path,
                        self.workspace_dir,
                        layout["output"],
                        extra={"error": detail},
                    )
                    session.add(
                        JobEvent(
                            job_id=job.id,
                            attempt_id=attempt.id,
                            type="offer_failed",
                            payload={
                                "error": detail,
                                "node": target.name,
                                "node_id": target.node_db_id,
                                "media_file_id": media_file.id,
                                "media_path": media_file.path,
                                "workspace": str(self.workspace_dir),
                                "output_path": layout["output"],
                            },
                            ts=now,
                        )
                    )
                    await session.commit()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    continue
                self._publish_node(target.name, "online")
                await session.refresh(job)
                claimed_jobs.append(claimed)
                touched.append(job.id)
                offered += 1
                self._library_cursors[job.priority] = job.library_id
                if not self._has_free_slot():
                    break
            await publish_updates()
        LOG.debug("dispatch pass offered %d job(s)", offered)
        return offered

    async def _fail_dispatch(
        self, session: AsyncSession, job: Job, attempt: JobAttempt | None, exc: ValueError, now: datetime
    ) -> None:
        if attempt is not None:
            await snapshot_attempt_log(session, self.workspace_dir, attempt)
        await finish(
            session,
            job.id,
            JobStatus.FAILED,
            error=f"invalid dispatch plan: {exc}",
            source_size=attempt.input_probe["size_bytes"] if attempt is not None and attempt.input_probe else None,
            now=now,
        )
        job.node_id = None
        job.lease_expires_at = None
        await session.commit()

    async def _fail_inspection(self, session: AsyncSession, job: Job, error: str, now: datetime) -> bool:
        job_id, attempts, status, node_id = job.id, job.attempts, job.status, job.node_id
        previous = await current_attempt(session, job_id)
        source_size = previous.input_probe["size_bytes"] if previous is not None and previous.input_probe else None
        log_data = previous.log_data if previous is not None else None
        log_path = previous.log_path if previous is not None else None
        paths = (
            list(
                (
                    await session.scalars(
                        select(JobEvent.payload["log_path"].as_string())
                        .where(
                            JobEvent.attempt_id == previous.id,
                            JobEvent.type == "stage",
                            JobEvent.payload["log"].as_string().is_not(None),
                        )
                        .order_by(JobEvent.id)
                    )
                ).all()
            )
            if previous is not None
            else []
        )
        # Inspection is provisional. Drop its cached rows and connection before starting work.
        await session.close()
        log_error = None
        if log_data is None and log_path:
            try:
                log_data = await wait_workspace_io(
                    lambda: b"\n".join(
                        read_attempt_log(self.workspace_dir, relative)
                        for relative in [*[path for path in paths if path and path != log_path], log_path]
                    )
                )
            except (OSError, ValueError) as exc:
                log_error = exc
        locked = await lock_job_source(session, job_id)
        if locked is None or (locked.attempts, locked.status, locked.node_id) != (attempts, status, node_id):
            return False
        current = await current_attempt(session, job_id)
        if current is not None:
            current.log_data = log_data
            if log_error is not None:
                await retain_unread_log(session, self.workspace_dir, current, log_error)
        return await finish(session, job_id, JobStatus.FAILED, error=error, source_size=source_size, now=now)

    async def recover_startup(self) -> None:
        async with self.sessionmaker() as session:
            await session.execute(
                update(Job)
                .where(
                    Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                    Job.node_id.is_not(None),
                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                )
                .values(lease_expires_at=self.clock() + timedelta(seconds=self.lease_ttl_s))
            )
            await session.execute(
                update(NodeRow).values(state="offline", workspace_ready=False, workspace_free_bytes=None)
            )
            await session.commit()
        await self.read_journals()

    async def read_journals(self, node_id: int | None = None, *, wait: bool = True) -> None:
        async with self.sessionmaker() as session:
            upper = await session.scalar(select(func.max(Job.id))) or 0
        stmt = (
            select(Job.id, JobAttempt.id, JobAttempt.node_id, JobAttempt.log_path, JobAttempt.attempt_number)
            .join(JobAttempt, (JobAttempt.job_id == Job.id) & (JobAttempt.attempt_number == Job.attempts))
            .where(
                Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                JobAttempt.node_id == Job.node_id,
                JobAttempt.log_path.is_not(None),
                or_(
                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                    (Job.status == JobStatus.SUCCEEDED) & Job.id.in_(post_action_jobs()),
                ),
            )
        )
        if node_id is not None:
            stmt = stmt.where(Job.node_id == node_id)
        after = 0
        while True:
            async with self.sessionmaker() as session:
                attempts = (
                    await session.execute(
                        stmt.where(Job.id > after, Job.id <= upper).order_by(Job.id).limit(QUERY_BATCH)
                    )
                ).all()
            if not attempts:
                break
            after = attempts[-1][0]
            for job_id, attempt_id, assigned_node_id, log_path, number in attempts:
                if (
                    assigned_node_id is None
                    or log_path is None
                    or Path(log_path).parent != Path(f"jobs/{job_id}/attempts/{number}")
                ):
                    continue
                try:
                    messages = await asyncio.to_thread(read_messages, self.workspace_dir, log_path)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as exc:
                    LOG.warning("Job journal unavailable for job %s", job_id, extra={"error": error_message(exc)})
                    continue
                for data in messages:
                    if data.get("type") not in {"finished", "action_report"}:
                        continue
                    try:
                        msg = parse_message(data)
                    except ValueError, TypeError:
                        continue
                    if (
                        isinstance(msg, (Finished, ActionReport))
                        and msg.job_id == job_id
                        and msg.attempt_id == attempt_id
                    ):
                        if isinstance(msg, Finished) and msg.log_path != log_path:
                            continue
                        task, _ = self._launch_completion(assigned_node_id, msg)
                        if wait and task is not None:
                            await asyncio.shield(task)

    async def sweep_once(self) -> None:
        await self.read_journals()
        now = self.clock()
        async with self.sessionmaker() as session:
            upper = await session.scalar(select(func.max(Job.id))) or 0
        after = 0
        while True:
            async with self.dispatch_lock, self.sessionmaker() as session:
                expired = list(
                    await session.scalars(
                        select(Job.id)
                        .where(
                            Job.id > after,
                            Job.id <= upper,
                            Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                            Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                            Job.node_id.is_not(None),
                            or_(Job.lease_expires_at < now, Job.lease_expires_at.is_(None)),
                        )
                        .order_by(Job.id)
                        .limit(QUERY_BATCH)
                    )
                )
                if not expired:
                    break
                after = expired[-1]
                finished: list[Job] = []
                cleanup_attempts: list[tuple[int, int, bool]] = []
                for job_id in expired:
                    job = await lock_job_source(session, job_id)
                    now = self.clock()
                    if job is None:
                        await session.commit()
                        continue
                    if job.id in self.staging.busy_jobs or (job.status == JobStatus.RUNNING and job.node_id is None):
                        await session.commit()
                        continue
                    if job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING} or (
                        job.lease_expires_at is not None and job.lease_expires_at >= now
                    ):
                        await session.commit()
                        continue
                    attempt = await current_attempt(session, job.id)
                    if attempt is not None:
                        await snapshot_attempt_log(session, self.workspace_dir, attempt)
                    if job.id in self._cancel_requested:
                        await finish(session, job.id, JobStatus.CANCELLED, error="cancelled", now=now)
                        job.node_id = None
                        job.lease_expires_at = None
                    else:
                        await requeue(
                            session,
                            job.id,
                            now=now,
                            max_attempts=self.max_attempts,
                            result="failed",
                            error="Node completion lease expired.",
                        )
                    finished.append(job)
                    if job.kind == "mkv_stats":
                        if job.status == JobStatus.FAILED:
                            await fail_metadata(session, job)
                        elif job.status == JobStatus.CANCELLED:
                            await cancel_metadata(session, job)
                    if attempt is not None:
                        cleanup_attempts.append((job.id, attempt.attempt_number, True))
                    # Do not carry one file's locks into the next file's completion.
                    await session.commit()
                for job in finished:
                    for entry in tuple(self.entries.values()):
                        if job.id in entry.in_flight:
                            self._release_slot(entry, job.id)
            for job in finished:
                await self._cleanup_workspace(job.id, cleanup_attempts)
                self._fire(f"job:{job.id}")
                await self._publish_status(job)
        async with self.dispatch_lock:
            released: list[tuple[NodeEntry, int]] = []
            async with self.sessionmaker() as session:
                for entry in self.entries.values():
                    for job_id in sorted(entry.in_flight):
                        job = await get_job(session, job_id)
                        if (
                            job is None
                            or job.node_id != entry.node_db_id
                            or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                        ):
                            released.append((entry, job_id))
                            continue
                        staging = self._inspection_tasks.get(job_id)
                        if staging is not None and staging.get_name() != "inspection-offered":
                            # Server staging cannot report node progress before the offer exists.
                            continue
                        if entry.disconnected.is_set():
                            continue
                        if job_id in self._stall_cancelled:
                            continue
                        stamps = [t for t in (entry.last_progress_at.get(job_id), entry.offered_at.get(job_id)) if t]
                        reference = max(stamps, default=None)
                        if reference is None or now - reference <= timedelta(seconds=self.stall_timeout_s):
                            continue
                        with contextlib.suppress(Exception):
                            attempt = await current_attempt(session, job_id)
                            if attempt is not None:
                                await entry.conn.send_json(
                                    Cancel(
                                        job_id=job_id,
                                        attempt_id=attempt.id,
                                        reason="stalled",
                                        log_path=attempt.log_path,
                                    ).model_dump(mode="json")
                                )
                        self._stall_cancelled.add(job_id)
                        session.add(
                            JobEvent(job_id=job_id, type="stall_cancelled", payload={"reason": "stalled"}, ts=now)
                        )
                await session.commit()
            for entry, job_id in released:
                self._release_slot(entry, job_id)
        after = 0
        while True:
            async with self.sessionmaker() as session:
                pending_actions = list(
                    await session.scalars(
                        select(Job.id)
                        .where(
                            Job.id > after,
                            Job.id <= upper,
                            Job.status.in_(_TERMINAL_STATUS_VALUES),
                            Job.id.in_(post_action_jobs()),
                        )
                        .order_by(Job.id)
                        .limit(QUERY_BATCH)
                    )
                )
            if not pending_actions:
                break
            after = pending_actions[-1]
            for job_id in pending_actions:
                await self.staging.prune_job(job_id)
        LOG.debug("sweep pass complete")

    def start(self) -> None:
        if self._tasks or self._close_task is not None:
            return
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="hub-dispatch"),
            asyncio.create_task(self._sweep_loop(), name="hub-sweep"),
        ]

    def begin_shutdown(self) -> None:
        if self._close_task is None:
            tasks = [*self._tasks, *self._action_waits]
            for task in tasks:
                task.cancel()
            self._close_task = asyncio.create_task(self._close(tasks))

    async def aclose(self) -> None:
        self.begin_shutdown()
        assert self._close_task is not None
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self, tasks: list[asyncio.Task]) -> None:
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            inspections = list(self._inspection_tasks.items())
            for job_id, task in inspections:
                if task.get_name() != "inspection-offered":
                    self._cancel_requested.add(job_id)
                if task.get_name() == "inspection-running":
                    task.cancel()
            if inspections:
                await asyncio.gather(*(task for _, task in inspections), return_exceptions=True)
            for task in tuple(self.staging.tasks.values()):
                task.cancel()
            await self.staging.wait_idle()
            while self._completions:
                await asyncio.gather(*tuple(self._completions.values()), return_exceptions=True)
        finally:
            self._tasks.clear()

    async def cancel_job(self, job_id: int) -> bool:
        if (task := self._inspection_tasks.get(job_id)) is not None and task.get_name() != "inspection-offered":
            self._cancel_requested.add(job_id)
            if task.get_name() == "inspection-running":
                task.cancel()
            return True
        for entry in self.entries.values():
            if job_id in entry.in_flight and not entry.disconnected.is_set():
                self._cancel_requested.add(job_id)
                try:
                    async with self.sessionmaker() as session:
                        attempt = await current_attempt(session, job_id)
                    if attempt is None:
                        return False
                    await entry.conn.send_json(
                        Cancel(
                            job_id=job_id, attempt_id=attempt.id, reason="cancelled", log_path=attempt.log_path
                        ).model_dump(mode="json")
                    )
                except Exception:
                    LOG.exception("failed to send cancel for job %d to node %s", job_id, entry.name)
                    return False
                return True
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id)
            if job is not None and job.status == JobStatus.RUNNING and job.node_id is None:
                attempt = await current_attempt(session, job_id)
                await finish(
                    session,
                    job_id,
                    JobStatus.CANCELLED,
                    error="cancelled",
                    source_size=attempt.input_probe["size_bytes"]
                    if attempt is not None and attempt.input_probe is not None
                    else None,
                    now=self.clock(),
                )
                await session.commit()
                if attempt is not None:
                    await self._cleanup_workspace(job_id, [(job_id, attempt.attempt_number, True)])
                await self._publish_status(job)
                return True
        return False

    async def drain_library(self, library_id: int) -> None:
        from common.process import drain

        async with self.dispatch_lock, self.sessionmaker() as session:
            rows = list(
                await session.execute(
                    select(Job.id, Job.status).where(
                        Job.library_id == library_id, or_(Job.kind == "transcode", Job.node_id.is_not(None))
                    )
                )
            )
        ids = {job_id for job_id, _ in rows}
        staging = [task for job_id, task in self.staging.tasks.items() if job_id in ids]
        inspections = [(job_id, task) for job_id, task in self._inspection_tasks.items() if job_id in ids]
        for job_id, task in inspections:
            self._cancel_requested.add(job_id)
            if task.get_name() == "inspection-running":
                task.cancel()
        if inspections:
            await drain(asyncio.gather(*(task for _, task in inspections), return_exceptions=True))
        for task in staging:
            task.cancel()
        if staging:
            await drain(asyncio.gather(*staging, return_exceptions=True))
        actions = [task for task, job_id in self._action_jobs.items() if job_id in ids]
        for task in actions:
            task.cancel()
        running = []
        failure = None
        for job_id, status in rows:
            if status in {JobStatus.CLAIMED, JobStatus.RUNNING}:
                if not await self.cancel_job(job_id):
                    async with self.sessionmaker() as session:
                        current = await session.scalar(select(Job.status).where(Job.id == job_id))
                    if current not in _TERMINAL_STATUS_VALUES and job_id not in self._finishing:
                        failure = f"Node cancellation is unconfirmed for job {job_id}. Library rows were retained."
                else:
                    running.append(job_id)
        if actions:
            results = await drain(asyncio.gather(*actions, return_exceptions=True))
            action_error = next((result for result in results if isinstance(result, Exception)), None)
        else:
            action_error = None
        for job_id in running:

            async def finished(job_id: int = job_id) -> bool | None:
                async with self.sessionmaker() as session:
                    status = await session.scalar(select(Job.status).where(Job.id == job_id))
                return True if status in _TERMINAL_STATUS_VALUES else None

            try:
                await self._await_signal(f"job:{job_id}", finished, max(1.0, self.action_task_timeout_s))
            except TimeoutError as exc:
                failure = f"Node cancellation is unconfirmed for job {job_id}. Library rows were retained."
                LOG.warning(
                    "Job %s did not confirm library deletion cancellation", job_id, extra={"error": error_message(exc)}
                )
        # Terminal status and released node slots precede metadata refresh and
        # action completion. Completion signals track those remaining tasks.
        while completions := [future for job_id in ids for future in self._finishing.get(job_id, ())] + [
            task for task, job_id in self._action_jobs.items() if job_id in ids
        ]:
            for task, job_id in tuple(self._action_jobs.items()):
                if job_id in ids and not task.cancelling():
                    task.cancel()
            results = await drain(asyncio.gather(*completions, return_exceptions=True))
            for result in results:
                if isinstance(result, Exception) and action_error is None:
                    action_error = result
        if action_error is not None:
            raise action_error
        if failure is not None:
            raise ValueError(failure)

    async def _read_hello(self, conn: NodeConnection) -> Hello | None:
        try:
            data = await asyncio.wait_for(conn.recv_json(), timeout=self.hello_timeout_s)
        except TimeoutError as exc:
            LOG.warning(
                "Node hello timed out after %s seconds in workspace %s",
                self.hello_timeout_s,
                self.workspace_dir,
                extra={"error": error_message(exc)},
            )
            await self._reject(conn, "handshake", "hello timed out")
            return None
        except Exception as exc:
            LOG.warning(
                "Could not receive node hello in workspace %s", self.workspace_dir, extra={"error": error_message(exc)}
            )
            await self._reject(conn, "handshake", "connection failed")
            return None
        if data is None:
            return None
        try:
            msg = parse_message(data)
        except Exception as exc:
            LOG.warning(
                "Could not parse node hello in workspace %s", self.workspace_dir, extra={"error": error_message(exc)}
            )
            await self._reject(conn, "handshake", "expected a hello message")
            return None
        if not isinstance(msg, Hello):
            LOG.warning("Expected node hello in workspace %s, received %s", self.workspace_dir, type(msg).__name__)
            await self._reject(conn, "handshake", "expected a hello message")
            return None
        if await self._authenticate_hello(conn, msg) is None:
            return None
        return msg

    async def _authenticate_hello(self, conn: NodeConnection, msg: Hello) -> tuple[int | None, bool] | None:
        node_db_id: int | None = None
        async with self.sessionmaker() as session:
            node = await get_node(session, msg.node_id) if msg.node_id is not None else None
            known = node is not None and hmac.compare_digest(node.token_hash, hash_token(msg.token))
            enrolled = self.enrollment_token is not None and hmac.compare_digest(
                hash_token(msg.token), hash_token(self.enrollment_token)
            )
            if not (known or (enrolled and msg.node_id is None)):
                if node is None:
                    LOG.error("unknown node %s rejected", msg.node_id)
                    await self._reject(conn, "auth", "unknown node", 4401)
                else:
                    LOG.error("authentication failed for node %s", msg.node_id)
                    await self._reject(conn, "auth", "authentication failed", 4401)
                return None
            if node is not None:
                node_db_id = node.id
        return node_db_id, enrolled

    async def _handshake_locked(
        self, conn: NodeConnection, msg: Hello
    ) -> tuple[Hello, int, dict[str, int], bool] | None:
        # A reset may have changed node credentials while this connection waited for its dispatch lock.
        authentication = await self._authenticate_hello(conn, msg)
        if authentication is None:
            return None
        node_db_id, enrolled = authentication
        issued_token = secrets.token_urlsafe(32) if enrolled else msg.token
        challenge_id = secrets.token_hex(16)
        directory = f"nodes/{node_db_id if node_db_id is not None else 'enrollment'}/{challenge_id}"
        relative_response = f"{directory}/response"
        challenge = secrets.token_bytes(32)
        async with storage_scope():
            try:
                await storage_io(_prepare_challenge, self.workspace_dir, directory, challenge)
                await conn.send_json(
                    WorkspaceChallenge(
                        challenge_id=challenge_id,
                        challenge_path=f"{directory}/challenge",
                        response_path=relative_response,
                    ).model_dump(mode="json")
                )
                data = await asyncio.wait_for(conn.recv_json(), timeout=self.hello_timeout_s)
                response = parse_message(data) if data is not None else None
                expected = hashlib.sha256(challenge).hexdigest().encode() + b"\n"
                if not isinstance(response, WorkspaceResponse):
                    cause = (
                        "Connection ended without a workspace response."
                        if response is None
                        else f"Expected WorkspaceResponse but received {type(response).__name__}."
                    )
                elif response.challenge_id != challenge_id:
                    cause = "Workspace response challenge ID does not match."
                elif response.response_path != relative_response:
                    cause = "Workspace response path does not match."
                elif not response.workspace_ready:
                    cause = f"Node reported workspace unavailable: {response.error or 'No failure reason provided.'}"
                else:
                    cause = await storage_io(_check_challenge, self.workspace_dir, relative_response, expected)
                if cause is not None:
                    self._log_workspace_failure(msg, cause)
                    await self._reject(conn, "workspace", "workspace verification failed")
                    return None
                assert isinstance(response, WorkspaceResponse)
            except TimeoutError as exc:
                self._log_workspace_failure(
                    msg, f"Workspace response timed out after {self.hello_timeout_s} seconds: {error_message(exc)}"
                )
                await self._reject(conn, "workspace", "workspace verification timed out")
                return None
            except (OSError, ValueError, ValidationError) as exc:
                self._log_workspace_failure(msg, error_message(exc))
                await self._reject(conn, "workspace", "workspace unavailable")
                return None
            finally:
                await storage_io(_cleanup_challenge, self.workspace_dir, directory)
        async with self.sessionmaker() as session:
            if enrolled:
                node_name = f"node-{secrets.token_hex(8)}"
                node = await register_node(
                    session,
                    node_name,
                    issued_token,
                    msg.capabilities,
                    display_name=_hardware_name(msg.capabilities),
                )
                node_db_id = node.id
                await conn.send_json(Enrolled(node_id=node_name, token=issued_token).model_dump(mode="json"))
                cause = None
                try:
                    data = await asyncio.wait_for(conn.recv_json(), timeout=self.hello_timeout_s)
                    saved = parse_message(data) if data is not None else None
                except (TimeoutError, ValueError) as exc:
                    cause = error_message(exc)
                    saved = None
                if not isinstance(saved, EnrollmentSaved) or saved.node_id != node_name:
                    if cause is None:
                        cause = (
                            f"Acknowledgement named node {saved.node_id} instead of {node_name}."
                            if isinstance(saved, EnrollmentSaved)
                            else f"Expected EnrollmentSaved, got {type(saved).__name__ if saved else 'no acknowledgement'}."
                        )
                    LOG.warning(
                        "Could not confirm credential storage for enrolling node %s in workspace %s",
                        node_name,
                        self.workspace_dir,
                        extra={"error": cause},
                    )
                    await self._reject(conn, "enrollment", "node did not save credentials")
                    return None
                msg = msg.model_copy(update={"node_id": node_name, "token": issued_token})
            assert msg.node_id is not None
            current = await session.scalar(
                update(NodeRow)
                .where(NodeRow.name == msg.node_id, NodeRow.token_hash == hash_token(msg.token))
                .values(state="online", updated_at=NodeRow.updated_at)
                .returning(NodeRow.id)
            )
            if current is None:
                await self._reject(conn, "auth", "authentication revoked during workspace verification", 4401)
                return None
            node = await get_node(session, msg.node_id)
            assert node is not None
            node_db_id = node.id
            node.capabilities = msg.capabilities.model_dump(mode="json")
            limits = {
                "max_transcodes": node.transcode_limit,
                "max_healthchecks": node.healthcheck_limit,
                "max_mkv_stats": node.mkv_stats_limit,
            }
            paused = node.paused
            node.workspace_ready = True
            node.workspace_free_bytes = response.workspace_free_bytes
            node.usage = None
            await set_node_state(session, msg.node_id, "online", now=self.clock())
            verified = []
            for batch in batched(msg.active_attempts, QUERY_BATCH, strict=False):
                authorized = set(
                    (
                        await session.execute(
                            select(Job.id, JobAttempt.id)
                            .join(
                                JobAttempt, (JobAttempt.job_id == Job.id) & (JobAttempt.attempt_number == Job.attempts)
                            )
                            .where(
                                Job.id.in_([item.job_id for item in batch]),
                                Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                                Job.node_id == node_db_id,
                                JobAttempt.node_id == node_db_id,
                                or_(
                                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING])
                                    & (Job.lease_expires_at >= self.clock()),
                                    (Job.status == JobStatus.SUCCEEDED) & Job.id.in_(post_action_jobs()),
                                ),
                            )
                            .limit(QUERY_BATCH)
                        )
                    ).all()
                )
                verified.extend(item for item in batch if (item.job_id, item.attempt_id) in authorized)
            await session.commit()
        self._fire(f"node:{msg.node_id}:online")
        LOG.info(
            "Node %s connected",
            msg.node_id,
            extra={"operation": "node_connection", "outcome": "connected", "node_id": msg.node_id},
        )
        self._publish_node(msg.node_id, "online")
        msg = msg.model_copy(update={"active_attempts": verified})
        await conn.send_json(
            Welcome(
                timezone=str(self.timezone),
                heartbeat_s=self.heartbeat_s,
                lease_ttl_s=self.lease_ttl_s,
                active_attempts=msg.active_attempts,
            ).model_dump(mode="json")
        )
        await conn.send_json(SetLimits.model_validate(limits).model_dump(mode="json"))
        return msg, node_db_id, limits, paused

    def _log_workspace_failure(self, msg: Hello, cause: str) -> None:
        credentials = (msg.token, self.enrollment_token or "")
        LOG.warning(
            "Workspace verification failed for node %s. Resolved server workspace: %s. "
            "Check server workspace_dir/MW_WORKSPACE_DIR and node workspace_dir/MW_NODE_WORKSPACE_DIR. "
            "Both must access the same shared files. Paths can differ between hosts.",
            redact(msg.node_id or "enrollment", secrets=credentials),
            redact(str(self.workspace_dir), secrets=credentials),
            extra={"error": redact(cause, secrets=credentials)},
        )

    async def _receive_loop(self, entry: NodeEntry) -> None:
        while True:
            data = await entry.conn.recv_json()
            if data is None or self.entries.get(entry.name) is not entry or entry.disconnected.is_set():
                return
            try:
                msg = parse_message(data)
            except Exception as exc:
                LOG.warning("invalid message from node %s", entry.name, extra={"error": error_message(exc)})
                with contextlib.suppress(Exception):
                    await entry.conn.send_json(
                        Error(code="protocol", message="unparseable message").model_dump(mode="json")
                    )
                continue
            if isinstance(msg, Heartbeat):
                await self._handle_heartbeat(entry, msg)
            elif isinstance(msg, AttemptMissing):
                await self.read_journals(entry.node_db_id, wait=False)
                await self._finish_missing(entry, msg)
            elif isinstance(msg, Progress):
                await self._handle_progress(entry, msg)
            elif isinstance(msg, (Finished, ActionReport)):
                self._launch_completion(entry.node_db_id, msg)
            elif isinstance(msg, NodeLogs):
                self._handle_node_logs(entry, msg)

    @staticmethod
    def _handle_node_logs(entry: NodeEntry, msg: NodeLogs) -> None:
        logger = logging.getLogger(f"node.{entry.name}")
        for item in msg.entries:
            logger.log(
                getattr(logging, item.level),
                item.message,
                extra={
                    "_from_node": True,
                    "source_logger": item.logger,
                    "node_id": entry.name,
                    **{
                        key: value
                        for key, value in item.model_dump().items()
                        if key not in {"logger", "level", "message"} and value is not None
                    },
                },
            )

    async def _finish_missing(self, entry: NodeEntry, msg: AttemptMissing) -> None:
        async with self.dispatch_lock, self.sessionmaker() as session:
            if self.entries.get(entry.name) is not entry or entry.disconnected.is_set():
                return
            staging = self._inspection_tasks.get(msg.job_id)
            if staging is not None and staging.get_name() != "inspection-offered":
                return
            if (entry.node_db_id, msg.job_id, msg.attempt_id, "finished", msg.log_path) in self._completions:
                return
            job = await lock_job_source(session, msg.job_id)
            if job is None:
                return
            current_job_id = await session.scalar(
                update(Job)
                .where(
                    Job.id == msg.job_id,
                    Job.kind.in_(["transcode", "healthcheck", "mkv_stats"]),
                    Job.node_id == entry.node_db_id,
                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                    select(JobAttempt.id)
                    .where(
                        JobAttempt.id == msg.attempt_id,
                        JobAttempt.job_id == Job.id,
                        JobAttempt.attempt_number == Job.attempts,
                        JobAttempt.node_id == entry.node_db_id,
                        JobAttempt.log_path.is_not_distinct_from(msg.log_path),
                    )
                    .exists(),
                )
                .values(status=Job.status)
                .returning(Job.id)
            )
            if current_job_id is None:
                return
            job = await get_job(session, msg.job_id)
            attempt = await current_attempt(session, msg.job_id)
            assert job is not None
            assert attempt is not None
            await snapshot_attempt_log(session, self.workspace_dir, attempt)
            cancelled = msg.job_id in self._cancel_requested
            error = "cancelled" if cancelled else "Node no longer owns this attempt."
            if cancelled:
                await finish(
                    session, msg.job_id, JobStatus.CANCELLED, error=error, now=self.clock(), warning=job.warning
                )
                attempt.result = "cancelled"
            else:
                await requeue(
                    session, msg.job_id, now=self.clock(), max_attempts=self.max_attempts, result="failed", error=error
                )
            job.node_id = None
            job.lease_expires_at = None
            if job.kind == "mkv_stats":
                if job.status == JobStatus.FAILED:
                    await fail_metadata(session, job)
                elif job.status == JobStatus.CANCELLED:
                    await cancel_metadata(session, job)
            await session.commit()
            self._release_slot(entry, msg.job_id)
        self._fire(f"job:{msg.job_id}")
        await self._publish_status(job)
        await self._cleanup_workspace(job.id, [(job.id, attempt.attempt_number, True)])

    async def _handle_heartbeat(self, entry: NodeEntry, msg: Heartbeat) -> None:
        if self.entries.get(entry.name) is not entry:
            return
        renewed: list[int] = []
        usage = {
            "cpu_percent": msg.cpu_percent,
            "memory_percent": msg.memory_percent,
            "gpus": [gpu.model_dump(mode="json") for gpu in msg.gpus],
        }
        async with self.sessionmaker() as session:
            now = self.clock()
            eligible = set(entry.in_flight) - self._stall_cancelled
            if eligible:
                renewed = list(
                    await session.scalars(
                        update(Job)
                        .where(
                            Job.id.in_(eligible),
                            Job.node_id == entry.node_db_id,
                            Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                        )
                        .values(lease_expires_at=now + timedelta(seconds=self.lease_ttl_s))
                        .returning(Job.id)
                    )
                )
            await touch_node(session, entry.node_db_id, now=now, usage=usage)
            if self.entries.get(entry.name) is not entry:
                return
            await session.commit()
        for job_id in eligible - set(renewed):
            entry.in_flight.pop(job_id, None)
            entry.offered_at.pop(job_id, None)
            entry.last_progress_at.pop(job_id, None)
        self._publish(
            "node.usage",
            {"name": entry.name, "usage": usage, "last_seen_at": iso_utc(now)},
            key=f"node.usage:{entry.name}",
        )
        for job_id in renewed:
            self._fire(f"job:{job_id}")

    async def _handle_progress(self, entry: NodeEntry, msg: Progress) -> None:
        if self.entries.get(entry.name) is not entry or msg.job_id in self._stall_cancelled:
            return
        progress = msg.model_dump(mode="json", exclude={"v", "type", "job_id"}, exclude_none=True)
        kind = entry.in_flight.get(msg.job_id)
        if msg.phase is not None and kind in {"healthcheck", "mkv_stats"}:
            progress.pop("phase", None)
            if kind == "healthcheck":
                progress["stage"] = msg.phase
            else:
                progress.update(tool="ffprobe" if msg.phase == "probe" else "mkvpropedit", stage="running")
        async with self.sessionmaker() as session:
            now = self.clock()
            previous_status = (
                await session.execute(
                    update(Job)
                    .where(
                        Job.id == msg.job_id,
                        Job.node_id == entry.node_db_id,
                        Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                        select(JobAttempt.id)
                        .where(
                            JobAttempt.id == msg.attempt_id,
                            JobAttempt.job_id == Job.id,
                            JobAttempt.node_id == entry.node_db_id,
                            JobAttempt.attempt_number == Job.attempts,
                        )
                        .exists(),
                    )
                    .values(
                        progress=progress, lease_expires_at=now + timedelta(seconds=self.lease_ttl_s), updated_at=now
                    )
                    .returning(Job.status)
                )
            ).scalar_one_or_none()
            if self.entries.get(entry.name) is not entry or msg.job_id in self._stall_cancelled:
                return
            if previous_status is None:
                latest_attempt = (
                    await session.execute(
                        select(Job.node_id, JobAttempt.id)
                        .outerjoin(
                            JobAttempt, (JobAttempt.job_id == Job.id) & (JobAttempt.attempt_number == Job.attempts)
                        )
                        .where(Job.id == msg.job_id)
                    )
                ).one_or_none()
                if latest_attempt is not None and (
                    latest_attempt.node_id != entry.node_db_id or latest_attempt.id != msg.attempt_id
                ):
                    LOG.warning("node %s sent progress for job %s it does not own", entry.name, msg.job_id)
                    await self._record_foreign(session, entry.name, msg.job_id, msg.attempt_id, "foreign_progress", now)
                    await session.commit()
                return
            if previous_status == JobStatus.CLAIMED:
                await session.execute(
                    update(Job)
                    .where(Job.id == msg.job_id)
                    .values(
                        status=JobStatus.RUNNING.value, updated_at=now, started_at=func.coalesce(Job.started_at, now)
                    )
                )
                await session.execute(
                    update(JobAttempt)
                    .where(JobAttempt.id == msg.attempt_id)
                    .values(status=JobStatus.RUNNING.value, started_at=func.coalesce(JobAttempt.started_at, now))
                )
                session.add(JobEvent(job_id=msg.job_id, attempt_id=msg.attempt_id, type="started", payload={}, ts=now))
            await session.commit()
        entry.last_progress_at[msg.job_id] = now
        self._fire(f"job:{msg.job_id}")
        if previous_status == JobStatus.CLAIMED and self.events is not None:
            # Progress alone leaves live clients in claimed, including after a stage handoff.
            await publish_job_status(self.events, self.sessionmaker, msg.job_id)
        self._publish(
            "job.progress",
            {"job_id": msg.job_id, "progress": progress, "updated_at": iso_utc(now)},
            key=f"job.progress:{msg.job_id}",
        )

    async def _handle_finished(self, entry: NodeEntry, msg: Finished) -> None:
        if self.entries.get(entry.name) is not entry or entry.disconnected.is_set():
            return
        await self._finish(entry.node_db_id, msg)

    async def _finish(self, node_id: int, msg: Finished) -> None:
        task, created = self._launch_completion(node_id, msg)
        if task is not None:
            if created:
                await task
            else:
                await asyncio.shield(task)

    def _launch_completion(self, node_id: int, msg: Finished | ActionReport) -> tuple[asyncio.Task[None] | None, bool]:
        # Journal replay and socket delivery share one worker per reported stage.
        key = (
            node_id,
            msg.job_id,
            msg.attempt_id,
            msg.type,
            msg.log_path if isinstance(msg, Finished) else msg.model_dump_json(),
        )
        if task := self._completions.get(key):
            return task, False
        report = self._action_reports.get((msg.job_id, msg.attempt_id)) if isinstance(msg, ActionReport) else None
        acknowledgement = report is not None and not report.done()
        if self._close_task is not None and not acknowledgement:
            return None, False
        previous = [task for previous_key, task in self._completions.items() if previous_key[1] == msg.job_id]
        completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        completions = self._finishing.setdefault(msg.job_id, set())
        completions.add(completed)
        self.staging.busy_jobs.add(msg.job_id)
        task = asyncio.create_task(
            self._complete(node_id, msg, completed, [] if acknowledgement else previous),
            name=f"hub-completion-{msg.job_id}-{msg.attempt_id}",
        )
        self._completions[key] = task
        self._completion_kinds[key] = msg.kind if isinstance(msg, Finished) else "transcode"

        def completion_finished(finished: asyncio.Task[None]) -> None:
            self._completions.pop(key, None)
            self._completion_kinds.pop(key, None)
            if not completed.done():
                # Cancellation before the coroutine starts still owns this registration.
                completions.discard(completed)
                if not completions:
                    self._finishing.pop(msg.job_id, None)
                    if msg.job_id not in self._action_jobs.values() and msg.job_id in self.staging.busy_jobs:
                        self.staging.busy_jobs.remove(msg.job_id)
                        if self.healthcheck_wake is not None:
                            self.healthcheck_wake()
                completed.set_result(None)
                self._fire(f"job:{msg.job_id}")
            if not finished.cancelled() and (error := finished.exception()) is not None:
                LOG.error(
                    "Could not process %s for job %s attempt %s on node %s",
                    msg.type,
                    msg.job_id,
                    msg.attempt_id,
                    node_id,
                    extra={"error": error_message(error)},
                    exc_info=error,
                )
            self._wake.set()

        task.add_done_callback(completion_finished)
        return task, True

    async def _complete(
        self,
        node_id: int,
        msg: Finished | ActionReport,
        completed: asyncio.Future[None],
        previous: list[asyncio.Task[None]],
    ) -> None:
        completions = self._finishing[msg.job_id]
        try:
            if previous:
                await asyncio.gather(*(asyncio.shield(task) for task in previous), return_exceptions=True)
            if isinstance(msg, Finished):
                await self._finish_attempt(node_id, msg)
            else:
                await self._handle_action_report(node_id, msg)
        finally:
            try:
                if completions == {completed} and msg.job_id not in self._action_jobs.values():
                    if msg.job_id in self.staging.busy_jobs:
                        self.staging.busy_jobs.remove(msg.job_id)
                        if self.healthcheck_wake is not None:
                            self.healthcheck_wake()
                    await self._cleanup_action_workspace(msg.job_id, msg.attempt_id)
            finally:
                completions.discard(completed)
                if not completions:
                    self._finishing.pop(msg.job_id, None)
                completed.set_result(None)
                self._fire(f"job:{msg.job_id}")

    async def _finish_attempt(self, node_id: int, msg: Finished) -> None:
        async with self.sessionmaker() as session:
            kind = await session.scalar(select(Job.kind).where(Job.id == msg.job_id))
        if kind in {"healthcheck", "mkv_stats"}:
            await self._finish_inspection(node_id, msg)
            return
        now = self.clock()
        async with self.sessionmaker() as session:
            node = await session.get(NodeRow, node_id)
            if node is None:
                return
            node_name = node.name
            entry = self.entries.get(node_name)
            job = await get_job(session, msg.job_id)
            attempt = await current_attempt(session, msg.job_id)
            if (
                job is None
                or job.kind != "transcode"
                or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                or job.node_id != node_id
                or attempt is None
                or attempt.node_id != node_id
                or attempt.attempt_number != job.attempts
                or attempt.id != msg.attempt_id
                or attempt.log_path != msg.log_path
            ):
                LOG.debug("Ignored finished for job %s attempt %s on node %s", msg.job_id, msg.attempt_id, node_name)
                if job is not None:
                    await self._record_foreign(session, node_name, msg.job_id, msg.attempt_id, "foreign_finished", now)
                    await session.commit()
                return
            stage = await self._stage_event(session, attempt)
            stage_data = stage.payload if stage is not None else {}
            if stage is not None and (stage_data.get("log_path") != msg.log_path or "result" in stage_data):
                return
            plan = FFmpegPlan.model_validate(stage_data.get("plan", job.plan))
            step_probe = (
                ProbeData.model_validate(stage_data["probe"])
                if stage is not None
                else (ProbeData.model_validate(attempt.input_probe) if attempt.input_probe is not None else None)
            )
            await session.close()
            log = None
            attempt_logs = None
            log_error = None
            context = (
                f"job {job.id} attempt {attempt.id} on node {node_name} (id {node_id}), "
                f"media {job.media_file_id}, workspace {self.workspace_dir}, output {attempt.output_path}"
            )
            try:
                log = await asyncio.to_thread(read_attempt_log, self.workspace_dir, attempt.log_path or "")
                attempt_logs = await read_attempt_logs(session, self.workspace_dir, attempt)
            except (OSError, ValueError) as exc:
                LOG.warning("Could not read execution log for %s", context, extra={"error": error_message(exc)})
                log_error = exc
            await session.close()
            media_id: int | None = None
            source_probe = ProbeData.model_validate(attempt.input_probe) if attempt.input_probe is not None else None
            verified_output_probe: ProbeData | None = None
            gate_actions: list[Action] = []
            gate_requires: list[str] = []
            gate_encoders: list[str] = []
            recovery_pending = False
            actions_event: JobEvent | None = None
            next_stage: FlowResult | None = None
            required_artifacts: dict[str, str] = {}
            gate_error: str | None = None
            artifact_probe: ProbeData | None = None
            inspected_probe: ProbeData | None = None
            stored_actions: list[Action] = []
            post_evaluated = False
            # Inspection can wait on storage and plugins. No changes are made before the source and job checks below.
            if msg.result == "succeeded":
                try:
                    if step_probe is None:
                        _post_error("stage input probe missing")
                    assert step_probe is not None
                    actions = _ACTIONS_ADAPTER.validate_python(stage_data.get("actions", job.pre_actions or []))
                    error = extraction_error(actions, step_probe, msg.action_results)
                    if error is not None:
                        _post_error(error)
                    inspected, inspection_error = await storage_io(
                        _inspect_artifact,
                        self.workspace_dir,
                        attempt.output_path or "",
                        self.ffprobe_bin,
                        step_probe,
                        plan,
                    )
                    if stage is not None:
                        inspected_probe = inspected.model_copy(update={"file_path": attempt.output_path})
                    if inspection_error is not None:
                        raise FinalizationError(inspection_error)  # noqa: TRY301 -- Preserve the inspected probe above.
                    artifact_probe = inspected
                    if stage is not None:
                        artifact_probe = artifact_probe.model_copy(update={"file_path": attempt.output_path})
                        inspected_probe = artifact_probe
                except Exception as exc:
                    gate_error = f"Server artifact verification failed: {error_message(exc)}"
                    LOG.exception("Could not verify artifact for %s", context, extra={"error": gate_error})
                if gate_error is None:
                    post_evaluated = True
                    try:
                        next_stage, stored_actions, gate_error, gate_requires, gate_encoders = await self._post_gate(
                            session, job, artifact_probe, step_probe, stage_data
                        )
                    except Exception as exc:
                        gate_error = f"Post evaluation failed: {error_message(exc)}"
                        LOG.exception("Could not evaluate post steps for %s", context, extra={"error": gate_error})

            await session.close()
            locked = await lock_job_source(session, msg.job_id)
            if locked is None:
                return
            current_job_id = await session.scalar(
                update(Job)
                .where(
                    Job.id == msg.job_id,
                    Job.node_id == node_id,
                    Job.attempts == attempt.attempt_number,
                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
                    select(JobAttempt.id)
                    .where(
                        JobAttempt.id == msg.attempt_id,
                        JobAttempt.job_id == msg.job_id,
                        JobAttempt.node_id == node_id,
                        JobAttempt.log_path == msg.log_path,
                    )
                    .exists(),
                )
                .values(status=JobStatus.VERIFYING.value if msg.result == "succeeded" else Job.status)
                .returning(Job.id)
            )
            if current_job_id is None:
                return
            job = await session.get(Job, msg.job_id, populate_existing=True)
            attempt = await session.get(JobAttempt, msg.attempt_id, with_for_update=True, populate_existing=True)
            stage = (
                await session.get(JobEvent, stage.id, with_for_update=True, populate_existing=True)
                if stage is not None
                else None
            )
            assert job is not None
            assert attempt is not None
            library = await session.get(Library, job.library_id, populate_existing=True)
            if library is not None and library.deleted:
                self._cancel_requested.add(job.id)
            if msg.result == "succeeded":
                attempt.status = JobStatus.VERIFYING.value
            if log is not None and stage is not None:
                stage.payload = {**stage_data, "log": log.decode(errors="replace")}
            if attempt_logs is not None:
                attempt.log_data = attempt_logs
            if log_error is not None:
                await retain_unread_log(session, self.workspace_dir, attempt, log_error)
            if stage is not None:
                stage.payload = {
                    **stage.payload,
                    "result": msg.result,
                    "error": msg.error,
                    "argv": msg.argv or stage_data.get("argv", []),
                    "argv_hash": msg.argv_hash,
                    "source_size": msg.source_size,
                    "output_size": msg.output_size,
                    "output_probe": msg.output_probe.model_dump(mode="json") if msg.output_probe else None,
                    "action_results": [outcome.model_dump(mode="json") for outcome in msg.action_results],
                }
                if inspected_probe is not None:
                    stage.payload = {**stage.payload, "output_probe": inspected_probe.model_dump(mode="json")}
            if post_evaluated and gate_error is not None:
                session.add(
                    JobEvent(
                        job_id=job.id, attempt_id=attempt.id, type="post_actions", payload={"error": gate_error}, ts=now
                    )
                )
            elif post_evaluated and stage_data.get("continuation", job.continuation) is None and stored_actions:
                session.add(
                    JobEvent(
                        job_id=job.id,
                        attempt_id=attempt.id,
                        type="post_actions",
                        payload={
                            "steps": [],
                            "vetoed": False,
                            "actions": [action.model_dump(mode="json") for action in stored_actions],
                            "requires": gate_requires,
                            "requires_encoders": gate_encoders,
                        },
                        ts=now,
                    )
                )
            if msg.result == "succeeded":
                if gate_error is None:
                    gate_veto = None
                    try:
                        gate_actions = stored_actions
                        if next_stage is not None and next_stage.plan is not None:
                            subtitle = next_stage.plan.burn_subtitle
                            if subtitle is not None:
                                try:
                                    current = await current_attempt(session, job.id)
                                    if current is None:
                                        _post_error("continuation attempt missing")
                                    assert current is not None
                                    # Release resource locks before staging,
                                    # but do not persist successful post decisions until subtitle staging succeeds.
                                    await session.commit()
                                    await self.staging.stage_continuation_subtitle(current.id, subtitle.filename)
                                    current_job = await lock_job_source(session, msg.job_id)
                                    refreshed_attempt = await session.get(
                                        JobAttempt, msg.attempt_id, with_for_update=True, populate_existing=True
                                    )
                                    if (
                                        current_job is None
                                        or refreshed_attempt is None
                                        or current_job.attempts != refreshed_attempt.attempt_number
                                        or current_job.status != JobStatus.VERIFYING
                                    ):
                                        return
                                    job, attempt = current_job, refreshed_attempt
                                except Exception as exc:
                                    gate_error = f"Post evaluation failed: {error_message(exc)}"
                                    LOG.exception(
                                        "Could not stage continuation subtitle for %s",
                                        context,
                                        extra={"error": gate_error},
                                    )
                                    session.add(
                                        JobEvent(
                                            job_id=job.id,
                                            attempt_id=attempt.id,
                                            type="post_actions",
                                            payload={"error": gate_error},
                                            ts=now,
                                        )
                                    )
                                    await session.flush()
                                    next_stage, gate_actions, gate_requires, gate_encoders = None, [], [], []
                        if next_stage is not None:
                            (
                                gate_veto,
                                gate_actions,
                                gate_error,
                                gate_requires,
                                gate_encoders,
                            ) = await self._record_post_result(
                                session,
                                job,
                                next_stage,
                                stored_actions,
                                gate_requires,
                                gate_encoders,
                                now,
                                attempt_id=attempt.id,
                            )
                    except Exception as exc:
                        gate_error = f"Post evaluation failed: {error_message(exc)}"
                        LOG.exception("Could not record post evaluation for %s", context, extra={"error": gate_error})
                        gate_veto = None
                else:
                    gate_veto = None
                if msg.job_id in self._cancel_requested:
                    await finish(session, msg.job_id, JobStatus.CANCELLED, error="cancelled", now=now)
                elif gate_error is not None:
                    await session.commit()
                    cleanup_error = await discard_rejected_artifact(
                        self.sessionmaker, attempt.id, workspace_dir=self.workspace_dir
                    )
                    await finish(
                        session, msg.job_id, JobStatus.FAILED, error=gate_error, warning=cleanup_error, now=now
                    )
                    if cleanup_error is not None:
                        session.add(
                            JobEvent(job_id=job.id, type="post_cleanup", payload={"error": cleanup_error}, ts=now)
                        )
                elif gate_veto is not None:
                    await session.commit()
                    cleanup_error = await discard_rejected_artifact(
                        self.sessionmaker, attempt.id, workspace_dir=self.workspace_dir
                    )
                    # The post graph rejected the output before anything replaced
                    # the source: fail with the veto message, leave the library alone.
                    await finish(session, msg.job_id, JobStatus.FAILED, error=gate_veto, warning=cleanup_error, now=now)
                    if cleanup_error is not None:
                        session.add(
                            JobEvent(job_id=job.id, type="post_cleanup", payload={"error": cleanup_error}, ts=now)
                        )
                elif next_stage is not None and next_stage.plan is not None:
                    assert artifact_probe is not None
                    pre_actions, _ = filter_actions(next_stage.actions)
                    post_actions = [action for action in next_stage.actions if action not in pre_actions]
                    deferred = bool(post_actions)
                    post_actions += _ACTIONS_ADAPTER.validate_python(
                        stage_data.get("post_actions", job.post_actions or [])
                    )
                    session.add(
                        JobEvent(
                            job_id=job.id,
                            attempt_id=attempt.id,
                            type="stage",
                            ts=now,
                            payload={
                                "number": stage_data.get("number", 1) + 1,
                                "plan": next_stage.plan.model_dump(mode="json"),
                                "probe": artifact_probe.model_dump(mode="json"),
                                "continuation": next_stage.continuation.model_dump(mode="json")
                                if next_stage.continuation
                                else None,
                                "facets": next_stage.facets,
                                "actions": [action.model_dump(mode="json") for action in pre_actions],
                                "post_actions": [action.model_dump(mode="json") for action in post_actions],
                                "post_requires": gate_requires if deferred else stage_data.get("post_requires", []),
                                "post_requires_encoders": gate_encoders
                                if deferred
                                else stage_data.get("post_requires_encoders", []),
                                "requires": next_stage.requires,
                                "requires_encoders": next_stage.requires_encoders,
                            },
                        )
                    )
                    job.status, job.node_id, job.lease_expires_at = JobStatus.RUNNING.value, None, None
                    job.progress = None
                    attempt.status = JobStatus.RUNNING.value
                    await session.commit()
                    self._release_slot(entry, msg.job_id)
                    await self._publish_status(job)
                    return
                else:
                    self._release_slot(entry, msg.job_id)
                    try:
                        media = await session.get(MediaFile, job.media_file_id)
                        library = await session.get(Library, job.library_id)
                        if (
                            media is None
                            or library is None
                            or job.plan is None
                            or attempt.output_path is None
                            or source_probe is None
                            or step_probe is None
                        ):
                            raise FinalizationError("finalization context missing")  # noqa: TRY301
                        assert stage is not None
                        # Recovery needs the final accepted actions, not intermediate stage actions.
                        stage.payload = {
                            **stage.payload,
                            "publication_actions": (job.pre_actions or [])
                            + [action.model_dump(mode="json") for action in gate_actions],
                        }
                        await transition(session, msg.job_id, JobStatus.VERIFYING, now=now)
                        await transition(session, msg.job_id, JobStatus.FINALIZING, now=now)
                        await session.commit()
                        await self._publish_status(job)
                        publication = await finalize(
                            self.sessionmaker,
                            attempt.id,
                            workspace_dir=self.workspace_dir,
                            trash_root=self.trash_dir,
                            ffprobe_bin=self.ffprobe_bin,
                            preserve_timestamps=any(isinstance(action, PreserveTimestamps) for action in gate_actions),
                        )
                        required_artifacts = publication.required_artifacts
                        current_job = await lock_job_source(session, msg.job_id)
                        refreshed_attempt = await session.get(
                            JobAttempt, msg.attempt_id, with_for_update=True, populate_existing=True
                        )
                        if (
                            current_job is None
                            or refreshed_attempt is None
                            or current_job.attempts != refreshed_attempt.attempt_number
                            or current_job.status != JobStatus.FINALIZING
                        ):
                            return
                        job, attempt = current_job, refreshed_attempt
                        media = await session.get(MediaFile, job.media_file_id, populate_existing=True)
                        if media is None:
                            raise FinalizationError("finalization media context missing")  # noqa: TRY301 -- Persist finalization failure below.
                        if publication.warning is not None:
                            session.add(
                                JobEvent(
                                    job_id=job.id,
                                    attempt_id=attempt.id,
                                    type="finalization_cleanup" if publication.committed else "finalization_pending",
                                    payload={"error": publication.warning},
                                    ts=now,
                                )
                            )
                        if publication.committed:
                            verified = publication.probe
                            assert verified is not None
                            media.path = verified.file_path
                            media.size = verified.size_bytes
                            media.source_identity = {"size": verified.size_bytes}
                            # Publication replaces the source even when the output has the same size.
                            # Discovery must be able to resume an interrupted metadata refresh.
                            media.probe_id = None
                            media.health_status = HealthStatus.PENDING.value
                            media.health_source = None
                            await session.flush()
                            await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id == media.id))
                            verified_output_probe = verified
                            media.transcode_status = TranscodeStatus.TRANSCODED.value
                            media_id = media.id
                            await finish(
                                session,
                                msg.job_id,
                                JobStatus.SUCCEEDED,
                                source_size=attempt.source_size,
                                output_size=verified.size_bytes,
                                output_probe=verified.model_dump(mode="json"),
                                warning=publication.warning,
                                now=now,
                            )
                            session.add(
                                JobEvent(
                                    job_id=job.id,
                                    attempt_id=attempt.id,
                                    type="artifact_check_pending",
                                    ts=now,
                                )
                            )
                        else:
                            if publication.warning is not None:
                                job.warning = publication.warning
                            recovery_pending = publication.pending
                    except Exception as exc:
                        detail = f"Finalization failed: {error_message(exc)}"
                        LOG.exception(
                            "Could not finalize %s, source %s",
                            context,
                            source_probe.file_path if source_probe is not None else None,
                            extra={"error": detail},
                        )
                        await finish(session, msg.job_id, JobStatus.FAILED, error=detail, now=now)
            elif msg.result in {"failed", "aborted"}:
                # Failed transcodes must not run post steps.
                if msg.job_id in self._cancel_requested:
                    await finish(session, msg.job_id, JobStatus.CANCELLED, error="cancelled", now=now)
                else:
                    await requeue(
                        session,
                        msg.job_id,
                        now=now,
                        max_attempts=self.max_attempts,
                        result=msg.result,
                        error=msg.error or msg.result,
                    )
            elif msg.result == "cancelled":
                await finish(
                    session,
                    msg.job_id,
                    JobStatus.CANCELLED,
                    error=msg.error or msg.result,
                    now=now,
                )
            else:
                await requeue(
                    session,
                    msg.job_id,
                    now=now,
                    max_attempts=self.max_attempts,
                    result=msg.result,
                    error=msg.error or msg.result,
                )
                attempt.result = msg.result
                await session.flush()
                if await session.scalar(recovery_jobs(scoped=True).where(Job.id == job.id)) is None:
                    job.warning = None
            publication_details: dict = {}
            attempt = await current_attempt(session, msg.job_id)
            if attempt is not None and msg.result != "stalled":
                if not recovery_pending and (msg.result != "succeeded" or job.status != JobStatus.SUCCEEDED):
                    attempt.source_size = source_probe.size_bytes if source_probe is not None else msg.source_size
                    attempt.output_size = msg.output_size
                attempt.argv = msg.argv or stage_data.get("argv", [])
                attempt.argv_hash = msg.argv_hash
                if msg.result != "succeeded":
                    attempt.output_probe = msg.output_probe.model_dump(mode="json") if msg.output_probe else None
                stage_events = list(
                    (
                        await session.scalars(
                            select(JobEvent)
                            .where(JobEvent.attempt_id == attempt.id, JobEvent.type == "stage")
                            .order_by(JobEvent.id)
                        )
                    ).all()
                )
                action_groups = (
                    [
                        (
                            event.payload.get("output_path"),
                            TypeAdapter(list[ActionOutcome]).validate_python(event.payload.get("action_results", [])),
                        )
                        for event in stage_events
                    ]
                    if stage_events
                    else [(attempt.output_path, msg.action_results)]
                )
                all_actions = [outcome for _, group in action_groups for outcome in group]
                if job.status == JobStatus.SUCCEEDED and verified_output_probe is not None:
                    if any(outcome.ok and outcome.artifacts for outcome in all_actions):
                        actions_event = JobEvent(
                            job_id=job.id,
                            attempt_id=attempt.id,
                            type="actions_pending",
                            payload={
                                "phase": "pre",
                                "publication_failed": True,
                                "raw_results": [outcome.model_dump(mode="json") for outcome in all_actions],
                                "results": [],
                            },
                            ts=now,
                        )
                        session.add(actions_event)
                    outcomes = []
                    for _path, group in action_groups:
                        pending = []
                        destinations = required_artifacts
                        for outcome in group:
                            if outcome.artifacts and all(artifact in destinations for artifact in outcome.artifacts):
                                outcomes.append(
                                    outcome.model_copy(
                                        update={
                                            "artifacts": [str(destinations[artifact]) for artifact in outcome.artifacts]
                                        }
                                    )
                                )
                            else:
                                pending.append(outcome)
                        await session.commit()
                        promoted = await publish_artifacts(
                            self.sessionmaker, attempt.id, pending, workspace_dir=self.workspace_dir
                        )
                        current_job = await lock_job_source(session, msg.job_id)
                        refreshed_attempt = await session.get(
                            JobAttempt, msg.attempt_id, with_for_update=True, populate_existing=True
                        )
                        if (
                            current_job is None
                            or refreshed_attempt is None
                            or current_job.attempts != refreshed_attempt.attempt_number
                        ):
                            return
                        job, attempt = current_job, refreshed_attempt
                        for outcome in promoted:
                            if not outcome.ok and outcome.error is not None:
                                job.warning = outcome.error
                        if any(
                            outcome.ok and outcome.artifacts and not published_result.ok
                            for outcome, published_result in zip(pending, promoted, strict=True)
                        ):
                            publication_details = {"publication_failed": True, "phase": "pre"}
                        outcomes += promoted
                elif recovery_pending:
                    outcomes = [
                        outcome.model_copy(
                            update={"ok": False, "error": "Finalization pending. Workspace artifacts retained."}
                        )
                        if outcome.artifacts
                        else outcome
                        for outcome in all_actions
                    ]
                else:
                    outcomes = [
                        outcome.model_copy(
                            update={
                                "ok": False,
                                "error": "Transcode did not succeed. Artifacts discarded.",
                                "artifacts": [],
                            }
                        )
                        if outcome.artifacts
                        else outcome
                        for outcome in all_actions
                    ]
                attempt.action_results = [outcome.model_dump(mode="json") for outcome in outcomes]
                if publication_details:
                    publication_details.update(
                        raw_results=[outcome.model_dump(mode="json") for outcome in all_actions],
                        results=attempt.action_results,
                    )
            post_actions: list[Action] = []
            output_path: str | None = None
            if msg.result == "succeeded" and job.status == JobStatus.SUCCEEDED.value:
                post_actions = gate_actions
                if post_actions and attempt is not None:
                    output_path = attempt.output_path
            dispatchable, rejected = filter_actions(post_actions)
            terminal = job.status in _TERMINAL_STATUS_VALUES
            dispatch_post_actions = bool(dispatchable or rejected) and msg.result == "succeeded" and terminal
            if msg.result == "succeeded" and terminal:
                if actions_event is None:
                    actions_event = JobEvent(job_id=job.id, attempt_id=msg.attempt_id, ts=now)
                    session.add(actions_event)
                actions_event.type = "actions_pending" if dispatch_post_actions else "actions_finished"
                actions_event.payload = {
                    "actions": [action.model_dump(mode="json") for action in post_actions],
                    **publication_details,
                }
            attempt_started = attempt.started_at if attempt is not None else None
            await session.commit()
            self._release_slot(entry, msg.job_id)
            if verified_output_probe is not None:
                try:
                    refreshed = await LibraryFS(
                        job.library_id,
                        sessionmaker=self.sessionmaker,
                        ffprobe_bin=self.ffprobe_bin,
                        mkvpropedit_bin=self.mkvpropedit_bin,
                        workspace_dir=self.workspace_dir,
                    ).refresh_published(msg.attempt_id)
                except Exception as exc:
                    LOG.exception("post-finalize rescan failed for job %s", msg.job_id)
                    async with self.sessionmaker() as warning_session:
                        refreshed_job = await get_job(warning_session, msg.job_id)
                        if refreshed_job is not None:
                            refreshed_job.warning = (
                                "Metadata rescan or healthcheck enqueue failed "
                                f"({type(exc).__name__}): {exc or type(exc).__name__}. Transcode retained."
                            )
                            if not isinstance(exc, LibraryProbeError) or (
                                exc.cleanup != "unresolved" and exc.reason != "superseded"
                            ):
                                await finish_artifact_check(
                                    warning_session, attempt_id=msg.attempt_id, library_id=refreshed_job.library_id
                                )
                            await warning_session.commit()
                            await self._publish_status(refreshed_job)
                else:
                    if refreshed.healthcheck_id is not None:
                        if self.events is not None:
                            try:
                                await publish_job_status(self.events, self.sessionmaker, refreshed.healthcheck_id)
                            except Exception:
                                LOG.exception("Failed publishing refreshed healthcheck for job %s", msg.job_id)
                        if self.healthcheck_wake is not None:
                            self.healthcheck_wake()
        self._fire(f"job:{msg.job_id}")
        # finish()/requeue() mutate the identity-mapped row, so job carries the final status.
        await self._publish_status(job)
        if job.status in _TERMINAL_STATUS_VALUES:
            JOBS_FINISHED.labels(msg.result).inc()
            duration_ms = None
            if attempt_started is not None:
                duration_ms = round((now - attempt_started.replace(tzinfo=UTC)).total_seconds() * 1000)
            LOG.info(
                "Job %s attempt %s finished with status %s on node %s",
                job.id,
                msg.attempt_id,
                job.status,
                node_name,
                extra={
                    "operation": "job_completion",
                    "outcome": msg.result,
                    "job_id": job.id,
                    "attempt_id": msg.attempt_id,
                    "node_id": node_name,
                    "library_id": job.library_id,
                    "status": job.status,
                    "stage": "finalization" if msg.result == "succeeded" else "execution",
                    "duration_ms": duration_ms,
                },
            )
        if media_id is not None:
            self._fire(f"media:{media_id}")
        if dispatch_post_actions and entry is not None:
            self._spawn_action_dispatch(
                entry,
                msg.job_id,
                output_path,
                verified_output_probe,
                post_actions,
                msg.attempt_id,
                gate_requires,
                gate_encoders,
            )

    async def _finish_inspection(self, node_id: int, msg: Finished) -> None:
        from server.fs.library import metadata_handoff
        from server.processing_cutoff import processing_allowed

        async with self.sessionmaker() as session:
            source = await session.scalar(
                select(JobEvent.payload).where(
                    JobEvent.attempt_id == msg.attempt_id, JobEvent.type == "inspection_source"
                )
            )
        if source is None:
            return
        try:
            stat = await wait_workspace_io(lambda: Path(source["path"]).stat())
        except OSError:
            stat = None
        publication = None
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, msg.job_id)
            attempt = await session.get(JobAttempt, msg.attempt_id, with_for_update=True, populate_existing=True)
            if (
                job is None
                or attempt is None
                or attempt.job_id != msg.job_id
                or job.kind != msg.kind
                or job.node_id != node_id
                or attempt.node_id != node_id
                or job.attempts != attempt.attempt_number
                or job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                or attempt.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}
                or attempt.log_path != msg.log_path
            ):
                return
            node = await session.get(NodeRow, node_id)
            entry = self.entries.get(node.name) if node is not None else None
            media = await session.get(MediaFile, job.media_file_id)
            library = await authorized_library(session, job.library_id, media) if media is not None else None
            unchanged = (
                media is not None
                and library is not None
                and media.path == source["path"]
                and media.size == source["size"]
                and stat is not None
                and stat.st_size == source["size"]
            )
            error = msg.error
            status = (
                JobStatus.SUCCEEDED
                if msg.result == "succeeded"
                else JobStatus.CANCELLED
                if msg.result == "cancelled"
                else JobStatus.FAILED
            )
            if job.id in self._cancel_requested or library is None:
                status, error = JobStatus.CANCELLED, "Inspection cancelled."
            elif not unchanged:
                status, error = JobStatus.FAILED, "Source changed during inspection. Enqueue a new job."
                if media is not None and media.path == source["path"] and media.size == source["size"]:
                    media.health_status = HealthStatus.PENDING
                    media.health_source = None
            elif status == JobStatus.SUCCEEDED and msg.kind != job.kind:
                status, error = JobStatus.FAILED, "Inspection result kind does not match the attempt."
            await snapshot_attempt_log(session, self.workspace_dir, attempt)
            stage = await self._stage_event(session, attempt)
            if (
                status == JobStatus.SUCCEEDED
                and job.kind == "mkv_stats"
                and stage is not None
                and stage.payload.get("statistics_edit")
            ):
                if msg.output_path != attempt.output_path or msg.output_probe is None:
                    status, error = JobStatus.FAILED, "Statistics output does not match the attempt."
                else:
                    stage.payload = {
                        **stage.payload,
                        "result": "succeeded",
                        "output_probe": msg.output_probe.model_dump(mode="json"),
                    }
                    await transition(session, job.id, JobStatus.FINALIZING)
                    await session.commit()
                    try:
                        publication = await finalize(
                            self.sessionmaker,
                            attempt.id,
                            workspace_dir=self.workspace_dir,
                            trash_root=self.trash_dir,
                            ffprobe_bin=self.ffprobe_bin,
                            preserve_timestamps=True,
                        )
                    except (OSError, ValueError, ProbeError) as exc:
                        status, error = JobStatus.FAILED, f"Statistics publication failed: {exc}"
                    job = await lock_job_source(session, msg.job_id)
                    attempt = await session.get(
                        JobAttempt, msg.attempt_id, with_for_update=True, populate_existing=True
                    )
                    assert job is not None
                    assert attempt is not None
                    if publication is not None and publication.pending:
                        job.warning = publication.warning
                        await session.commit()
                        self._release_slot(entry, job.id)
                        return
                    media = await session.get(MediaFile, job.media_file_id, populate_existing=True)
                    library = await session.get(Library, job.library_id, populate_existing=True)
                    stat = await wait_workspace_io(lambda: Path(source["path"]).stat())
            if media is None:
                return
            prior_health = media.health_status
            metadata_probe = None
            if status == JobStatus.SUCCEEDED and msg.raw_probe is not None and stat is not None and library is not None:
                transcode_state = media.transcode_status, media.transcode_decided, media.first_seen
                _, metadata_probe = await apply_probe_metadata(
                    session,
                    path=Path(media.path),
                    stat=stat,
                    raw=msg.raw_probe,
                    row=media,
                    library=library,
                    purpose="healthcheck"
                    if job.kind == "healthcheck"
                    else "published"
                    if publication is not None
                    else "probe",
                )
                if job.kind == "mkv_stats":
                    media.transcode_status, media.transcode_decided, media.first_seen = transcode_state
            health = None
            if status == JobStatus.SUCCEEDED and job.kind == "healthcheck":
                if msg.health is None:
                    status, error = JobStatus.FAILED, "Healthcheck result is missing."
                else:
                    health = msg.health.model_dump(mode="json")
                    media.health_status = HealthStatus.HEALTHY if msg.health.healthy else HealthStatus.CORRUPT
                    media.health_source = {"path": source["path"], "size": source["size"]}
                    if msg.health.healthy and library is not None and processing_allowed(library, media):
                        await release_transcode_decision(session, job, prior_health=prior_health)
            elif job.kind == "mkv_stats" and status != JobStatus.SUCCEEDED:
                if status == JobStatus.CANCELLED:
                    await cancel_metadata(session, job)
                elif unchanged:
                    media.health_status = "probe_failed"
                    media.health_source = None
            await finish(
                session,
                job.id,
                status,
                error=error,
                source_size=attempt.source_size,
                health=health,
                output_probe=metadata_probe.model_dump(mode="json")
                if job.kind == "mkv_stats" and metadata_probe is not None
                else None,
            )
            job.node_id = None
            job.lease_expires_at = None
            if status == JobStatus.SUCCEEDED and job.kind == "mkv_stats" and library is not None:
                media.metadata_priority = 0
                await metadata_handoff(session, media, library)
                if self.events is not None and attempt.claimed_at is not None:
                    self.events.mkv_stats_samples.append(
                        (self.events.clock(), max(0.0, (self.clock() - attempt.claimed_at).total_seconds()))
                    )
            await session.commit()
            self._release_slot(entry, job.id)
            await self._publish_status(job)
        if self.healthcheck_service is not None:
            self.healthcheck_service.wake()
            if self.healthcheck_service._on_healthy is not None:
                self.healthcheck_service._on_healthy()
        if self.probe_service is not None:
            self.probe_service.wake()
        self._fire(f"job:{msg.job_id}")

    async def _record_foreign(
        self, session: AsyncSession, node_name: str, job_id: int, attempt_id: int, event_type: str, now: datetime
    ) -> None:
        if await session.get(Job, job_id, with_for_update=True, populate_existing=True) is None:
            return
        attempt = await session.get(JobAttempt, attempt_id)
        stored_attempt_id = attempt_id if attempt is not None and attempt.job_id == job_id else None
        stmt = (
            select(JobEvent.id)
            .where(JobEvent.job_id == job_id, JobEvent.attempt_id == stored_attempt_id, JobEvent.type == event_type)
            .limit(1)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            return
        session.add(
            JobEvent(
                job_id=job_id,
                attempt_id=stored_attempt_id,
                type=event_type,
                payload={"node": node_name, "attempt_id": attempt_id},
                ts=now,
            )
        )
        await session.flush()

    async def _stage_event(self, session: AsyncSession, attempt: JobAttempt | None) -> JobEvent | None:
        if attempt is None:
            return None
        return await session.scalar(
            select(JobEvent)
            .where(JobEvent.attempt_id == attempt.id, JobEvent.type == "stage")
            .order_by(JobEvent.id.desc())
            .limit(1)
        )

    def _release_slot(self, entry: NodeEntry | None, job_id: int) -> None:
        self._stall_cancelled.discard(job_id)
        self._cancel_requested.discard(job_id)
        self._wake.set()
        if self.healthcheck_wake is not None:
            self.healthcheck_wake()
        if entry is None:
            return
        current = self.entries.get(entry.name)
        if current is not None and current.node_db_id == entry.node_db_id:
            entry = current
        self._fire(f"node:{entry.name}:ready")
        entry.in_flight.pop(job_id, None)
        entry.offered_at.pop(job_id, None)
        entry.last_progress_at.pop(job_id, None)
        if entry.disconnected.is_set() and not entry.in_flight and self.entries.get(entry.name) is entry:
            self.entries.pop(entry.name)
            self._entry_removed.set()
        self._publish_node(entry.name, "offline" if entry.disconnected.is_set() else "online")

    async def _post_gate(
        self,
        session: AsyncSession,
        job: Job,
        output_probe: ProbeData | None,
        source_probe: ProbeData | None,
        stage: dict,
    ) -> tuple[FlowResult | None, list[Action], str | None, list[str], list[str]]:
        """Evaluate without writes so storage and plugins don't hold database row locks."""
        stored: list[Action] = []
        requires = stage.get("post_requires", [])
        encoders = stage.get("post_requires_encoders", [])
        post_actions = stage.get("post_actions", job.post_actions)
        if post_actions:
            try:
                stored = _ACTIONS_ADAPTER.validate_python(post_actions)
            except ValidationError as exc:
                # Corrupt rows must not kill the finish path. Record and skip.
                detail = f"Invalid stored post actions: {error_message(exc)}"
                LOG.exception(
                    "Could not load post actions for job %s, media %s",
                    job.id,
                    job.media_file_id,
                    extra={"error": detail},
                )
                return None, [], detail, [], []
        if output_probe is None:
            return None, [], "verified output probe missing", [], []
        resume = stage.get("continuation", job.continuation)
        if resume is None:
            return None, stored, None, requires, encoders
        if self.plugin_host is None:
            return None, [], "post evaluation unavailable: plugin host missing", [], []
        try:
            continuation = FlowContinuation.model_validate(resume)
            library = await session.get(Library, job.library_id)
            media = await session.get(MediaFile, job.media_file_id)
            if media is None:
                _post_error("original media context missing")
            assert media is not None
            ctx = PluginContext(
                probe=output_probe,
                original_filename=Path(media.path).name,
                sidecar_subtitles=await wait_workspace_io(lambda: sidecar_subtitles(media.path)),
                source_probe=source_probe,
                library={"name": library.name if library is not None else ""},
                facets=stage.get("facets", job.facets) or {},
            )
            await session.close()
            result = await evaluate_post(continuation, ctx, self.plugin_host)
        except Exception as exc:
            # Plugin evaluation must never roll the finish path back.
            detail = f"Post evaluation failed: {error_message(exc)}"
            LOG.exception(
                "Could not evaluate post steps for job %s, media %s, workspace %s, output %s",
                job.id,
                job.media_file_id,
                self.workspace_dir,
                output_probe.file_path,
                extra={"error": detail},
            )
            return None, [], detail, [], []
        return result, stored, None, requires, encoders

    async def _record_post_result(
        self,
        session: AsyncSession,
        job: Job,
        result: FlowResult,
        stored: list[Action],
        requires: list[str],
        encoders: list[str],
        now: datetime,
        *,
        attempt_id: int,
    ) -> tuple[str | None, list[Action], str | None, list[str], list[str]]:
        entries = [decision_entry(outcome) for outcome in result.steps]
        job.decision_log = (job.decision_log or []) + entries
        veto = result.reason if result.status == "vetoed" else None
        error = None
        if result.status == "error":
            details = [f"{step.node} ({step.plugin}): {step.error}" for step in result.steps if step.error]
            error = "post evaluation failed: " + (". ".join(details) or result.reason or "evaluation error")
        requires = sorted(set(requires) | set(result.requires))
        encoders = sorted(set(encoders) | set(result.requires_encoders))
        session.add(
            JobEvent(
                job_id=job.id,
                attempt_id=attempt_id,
                type="post_actions",
                payload={
                    "steps": entries,
                    "vetoed": veto is not None,
                    **({"error": error} if error is not None else {}),
                    "actions": [action.model_dump(mode="json") for action in result.actions + stored],
                    "requires": requires,
                    "requires_encoders": encoders,
                },
                ts=now,
            )
        )
        await session.flush()
        return veto, [] if error is not None else result.actions + stored, error, requires, encoders

    def wake(self) -> None:
        self._wake.set()

    @asynccontextmanager
    async def pause_for_reset(self) -> AsyncIterator[None]:
        async with self.dispatch_lock:
            if self._close_task is not None:
                raise ValueError("server is shutting down")
            if self._reset_active:
                raise ValueError("reset is already active")
            if any(entry.in_flight for entry in self.entries.values()) or self._action_waits or self._finishing:
                raise ValueError("cannot reset while node work is in flight")
            async with self.sessionmaker() as session:
                active_job_id = await session.scalar(
                    select(Job.id)
                    .where(
                        Job.kind == "transcode",
                        Job.node_id.is_not(None),
                        or_(
                            Job.status.in_(
                                [JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING]
                            ),
                            Job.id.in_(post_action_jobs()),
                        ),
                    )
                    .limit(1)
                )
                if active_job_id is not None:
                    raise ValueError("cannot reset while node work is in flight")
            previous = {name: entry.paused for name, entry in self.entries.items()}
            self._reset_active = True
            self._reset_done.clear()
            for entry in self.entries.values():
                entry.paused = True
        try:
            await self.staging.wait_idle()
            yield
        finally:

            async def release() -> None:
                async with self.dispatch_lock:
                    for name, paused in previous.items():
                        entry = self.entries.get(name)
                        if entry is not None:
                            entry.paused = paused
                    self._reset_active = False
                    self._reset_done.set()

            releasing = asyncio.create_task(release())
            cancelled = False
            while not releasing.done():
                try:
                    await asyncio.shield(releasing)
                except asyncio.CancelledError:
                    cancelled = True
            releasing.result()
            if cancelled:
                raise asyncio.CancelledError

    async def disconnect_for_reset(self, timeout_s: float = 5.0) -> None:
        if not self._reset_active:
            raise ValueError("disconnect_for_reset must be called inside pause_for_reset")
        async with self.dispatch_lock:
            connections = [
                entry.conn for entry in self.entries.values() if not entry.in_flight and not entry.disconnected.is_set()
            ]
        if not connections:
            return
        self._entry_removed.clear()
        closes = [asyncio.create_task(conn.close(1000)) for conn in connections]
        try:
            async with asyncio.timeout(timeout_s):
                await asyncio.gather(*closes)
                while any(any(entry.conn is conn for conn in connections) for entry in self.entries.values()):
                    await self._entry_removed.wait()
                    self._entry_removed.clear()
        finally:
            for task in closes:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*closes, return_exceptions=True)

    async def set_limits(self, node_id: int, *, max_transcodes: int, max_healthchecks: int, max_mkv_stats: int) -> None:
        limits = {
            "max_transcodes": max_transcodes,
            "max_healthchecks": max_healthchecks,
            "max_mkv_stats": max_mkv_stats,
        }
        entry = next((item for item in self.entries.values() if item.node_db_id == node_id), None)
        if entry is None or entry.disconnected.is_set():
            return
        try:
            await entry.conn.send_json(SetLimits.model_validate(limits).model_dump(mode="json"))
        except Exception:
            LOG.exception("Failed to update transcode limit for node %s. Reconnect required.", entry.name)
            with contextlib.suppress(Exception):
                await entry.conn.close()
            return
        entry.capabilities = entry.capabilities.model_copy(update=limits)
        self.wake()

    def set_paused(self, node_id: int, paused: bool) -> None:
        entry = next((item for item in self.entries.values() if item.node_db_id == node_id), None)
        if entry is None or entry.paused == paused:
            return
        entry.paused = paused
        self.wake()

    async def _cleanup_workspace(self, job_id: int, attempts: list[tuple[int, int, bool]]) -> None:
        try:
            for candidate_job, attempt_number, whole in attempts:
                if candidate_job != job_id:
                    continue
                async with self.sessionmaker() as session:
                    attempt_id = await session.scalar(
                        select(JobAttempt.id).where(
                            JobAttempt.job_id == job_id, JobAttempt.attempt_number == attempt_number
                        )
                    )
                if attempt_id is None or not await self.staging.prune_attempt(attempt_id):
                    continue
                if whole:
                    result = await self.staging.prune_job(job_id, attempt_number=attempt_number)
                    if result["outcome"] == "error":
                        _post_error(result["reason"])
        except Exception as exc:
            detail = f"workspace cleanup failed ({type(exc).__name__}): {exc or type(exc).__name__}"
            LOG.exception("workspace cleanup failed for job %d", job_id)
            async with self.sessionmaker() as session:
                current = await get_job(session, job_id)
                if current is not None:
                    current.warning = detail
                    session.add(
                        JobEvent(job_id=job_id, type="cleanup_failed", payload={"error": detail}, ts=self.clock())
                    )
                    await session.commit()

    def _offer_actions(self, raw: list | None) -> tuple[list[Action], list[dict]]:
        if not raw:
            return [], []
        actions = _ACTIONS_ADAPTER.validate_python(raw)
        return filter_actions(actions)

    async def _handle_report(self, node_id: int, msg: ActionReport) -> None:
        task, created = self._launch_completion(node_id, msg)
        if task is not None:
            if created:
                await task
            else:
                await asyncio.shield(task)

    async def _handle_action_report(self, node_id: int, msg: ActionReport) -> None:
        key = (msg.job_id, msg.attempt_id)
        async with self.sessionmaker() as session:
            job = await get_job(session, msg.job_id)
            attempt = await current_attempt(session, msg.job_id)
            if (
                job is None
                or job.kind != "transcode"
                or job.status != JobStatus.SUCCEEDED
                or job.node_id != node_id
                or attempt is None
                or attempt.id != msg.attempt_id
                or attempt.node_id != node_id
                or attempt.attempt_number != job.attempts
            ):
                return
            pending = await session.scalar(
                select(JobEvent)
                .where(
                    JobEvent.job_id == msg.job_id,
                    JobEvent.attempt_id == msg.attempt_id,
                    JobEvent.type.in_(["actions_pending", "actions_finished"]),
                )
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            if pending is None or pending.type != "actions_pending":
                return
            actions = _ACTIONS_ADAPTER.validate_python(pending.payload.get("actions", []))
            dispatchable, rejected = filter_actions(actions)
            kinds = {action.kind for action in dispatchable}
            if not dispatchable or any(result.kind not in kinds for result in msg.results):
                return
            report = self._action_reports.get(key)
            if report is not None:
                if self._action_nodes.get(key) == node_id and not report.done():
                    report.set_result(msg.results)
                return
            path = attempt.output_path
            probe = ProbeData.model_validate(attempt.output_probe) if attempt.output_probe is not None else None
        try:
            await self._finish_post_actions(
                msg.job_id,
                msg.attempt_id,
                path,
                probe,
                msg.results,
                [ActionOutcome(kind=str(item["kind"]), ok=False, error=str(item["reason"])) for item in rejected],
                finished=True,
                accepted=True,
                prerequisite_error=None,
                node_id=node_id,
            )
        except Exception as exc:
            LOG.exception(
                "Could not finish post-action report for job %s attempt %s on node %s, media %s, workspace %s, "
                "output %s.",
                msg.job_id,
                msg.attempt_id,
                node_id,
                job.media_file_id,
                self.workspace_dir,
                path,
                extra={"error": error_message(exc)},
            )
            raise
        self._fire(f"job:{msg.job_id}")
        await self._publish_status(job)

    async def _cancel_action_task(
        self, entry: NodeEntry, job_id: int, attempt_id: int, report: asyncio.Future[list[ActionOutcome]]
    ) -> bool:
        try:
            async with asyncio.timeout(max(10.0, self.action_task_timeout_s)):
                await entry.conn.send_json(
                    Cancel(job_id=job_id, attempt_id=attempt_id, log_path=None).model_dump(mode="json")
                )
                await asyncio.shield(report)
        except Exception as exc:
            LOG.exception(
                "Could not confirm post-action cancellation for job %s attempt %s on node %s (id %s), workspace %s",
                job_id,
                attempt_id,
                entry.name,
                entry.node_db_id,
                self.workspace_dir,
                extra={"error": error_message(exc)},
            )
            return False
        return True

    def _spawn_action_dispatch(
        self,
        entry: NodeEntry,
        job_id: int,
        path: str | None,
        probe: ProbeData | None,
        actions: list[Action],
        attempt_id: int,
        requires: list[str],
        requires_encoders: list[str],
    ) -> None:
        if self._close_task is not None or entry.disconnected.is_set() or self.entries.get(entry.name) is not entry:
            # Finish persisted pending actions before this handoff. Retain it
            # for recovery rather than starting work outside the shutdown drain.
            return
        task = asyncio.create_task(
            self._dispatch_post_actions(entry, job_id, attempt_id, path, probe, actions, requires, requires_encoders)
        )
        self.staging.busy_jobs.add(job_id)
        self._action_waits.add(task)
        self._action_jobs[task] = job_id

        def completed(finished: asyncio.Task) -> None:
            self._action_waits.discard(finished)
            self._action_jobs.pop(finished, None)
            if job_id not in self._finishing and job_id not in self._action_jobs.values():
                self.staging.busy_jobs.discard(job_id)

        task.add_done_callback(completed)

    async def _dispatch_post_actions(
        self,
        entry: NodeEntry,
        job_id: int,
        attempt_id: int,
        path: str | None,
        probe: ProbeData | None,
        actions: list[Action],
        requires: list[str],
        requires_encoders: list[str],
    ) -> None:
        dispatchable, rejected = filter_actions(actions)
        if not dispatchable and not rejected:
            return
        if entry.disconnected.is_set() or self.entries.get(entry.name) is not entry:
            return
        current_task = asyncio.current_task()
        assert current_task is not None
        self._action_jobs[current_task] = job_id
        self.staging.busy_jobs.add(job_id)
        results: list[ActionOutcome] = []
        finished = True
        cancelled = False
        accepted = False
        report: asyncio.Future[list[ActionOutcome]] | None = None
        operation = "preparation"
        try:
            preparation = asyncio.create_task(self._prepare_post_actions(job_id, attempt_id, actions))
            while not preparation.done():
                try:
                    await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    cancelled = True
            if not preparation.result():
                return
            if cancelled:
                raise asyncio.CancelledError  # noqa: TRY301 -- Cancellation follows the preparation drain.
            if entry.disconnected.is_set() or self.entries.get(entry.name) is not entry:
                return
            capable, why = requirements_met(entry.capabilities, requires, requires_encoders)
            if not entry.workspace_ready:
                capable, why = False, "workspace is not ready"
            prerequisite_error = f"post-action prerequisites unmet on node {entry.name}: {why}" if not capable else None
            if prerequisite_error is not None:
                results = [ActionOutcome(kind=a.kind, ok=False, error=prerequisite_error) for a in dispatchable]
            elif path is None:
                results = [ActionOutcome(kind=a.kind, ok=False, error="output path missing") for a in dispatchable]
            elif dispatchable:
                report = asyncio.get_running_loop().create_future()
                self._action_reports[(job_id, attempt_id)] = report
                self._action_nodes[(job_id, attempt_id)] = entry.node_db_id
                operation = "prepare"
                try:
                    task = ActionTask(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        path=path,
                        probe=probe.model_copy(update={"file_path": path, "raw": None}) if probe is not None else None,
                        actions=dispatchable,
                    ).model_dump(mode="json")
                    finished = False
                    operation = "send"
                    await entry.conn.send_json(task)
                    operation = "report"
                    results = await asyncio.wait_for(asyncio.shield(report), timeout=self.action_task_timeout_s)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as exc:
                    detail = error_message(exc)
                    action_kinds = [a.kind for a in dispatchable]
                    LOG.exception(
                        "Post-action %s failed for job %s attempt %s on node %s (id %s), actions %s, "
                        "media %s, workspace %s, output %s",
                        operation,
                        job_id,
                        attempt_id,
                        entry.name,
                        entry.node_db_id,
                        action_kinds,
                        probe.file_path if probe is not None else None,
                        self.workspace_dir,
                        path,
                        extra={"error": detail},
                    )
                    if finished:
                        cause = f"Post-action {operation} failed: {detail}"
                        results = [ActionOutcome(kind=a.kind, ok=False, error=cause) for a in dispatchable]
                finally:
                    if (
                        not finished
                        and not report.done()
                        and not entry.disconnected.is_set()
                        and self.entries.get(entry.name) is entry
                    ):
                        cancellation = asyncio.create_task(self._cancel_action_task(entry, job_id, attempt_id, report))
                        while not cancellation.done():
                            try:
                                await asyncio.shield(cancellation)
                            except asyncio.CancelledError:
                                cancelled = True
                        finished = cancellation.result()
                    if report.done() and not report.cancelled() and report.exception() is None:
                        finished = True
                        accepted = True
                        results = report.result()
                if not finished:
                    results = [
                        ActionOutcome(kind=action.kind, ok=False, error="Post-action cancellation timed out.")
                        for action in dispatchable
                    ]
                    with contextlib.suppress(Exception):
                        await entry.conn.close()
                    finished = True
            rejected_results = [
                ActionOutcome(
                    kind=str(item.get("kind", "rejected")), ok=False, error=str(item.get("reason", "action rejected"))
                )
                for item in rejected
            ]
            operation = "completion"
            completion = asyncio.create_task(
                self._finish_post_actions(
                    job_id,
                    attempt_id,
                    path,
                    probe,
                    results,
                    rejected_results,
                    finished=finished,
                    accepted=accepted,
                    prerequisite_error=prerequisite_error,
                    node_id=entry.node_db_id,
                )
            )
            while not completion.done():
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    cancelled = True
            finished = completion.result()
        except Exception as exc:
            LOG.exception(
                "Post-action %s failed for job %s attempt %s on node %s (id %s), media %s, workspace %s, output %s. "
                "Post-actions failed.",
                operation,
                job_id,
                attempt_id,
                entry.name,
                entry.node_db_id,
                probe.file_path if probe is not None else None,
                self.workspace_dir,
                path,
                extra={"error": error_message(exc)},
            )
            raise
        finally:
            if report is not None and self._action_reports.get((job_id, attempt_id)) is report:
                self._action_reports.pop((job_id, attempt_id), None)
                self._action_nodes.pop((job_id, attempt_id), None)
            self._action_jobs.pop(current_task, None)
            if (
                job_id not in self._finishing
                and job_id not in self._action_jobs.values()
                and job_id in self.staging.busy_jobs
            ):
                self.staging.busy_jobs.remove(job_id)
                if self.healthcheck_wake is not None:
                    self.healthcheck_wake()
            if job_id not in self.staging.busy_jobs:
                cleanup = asyncio.create_task(self._cleanup_action_workspace(job_id, attempt_id))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _prepare_post_actions(self, job_id: int, attempt_id: int, actions: list[Action]) -> bool:
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id)
            if job is None:
                raise ValueError("post-action job context missing")
            stored_attempt = await session.scalar(
                select(JobAttempt.id).where(JobAttempt.id == attempt_id, JobAttempt.job_id == job_id)
            )
            latest = await session.scalar(
                select(JobEvent.type)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.attempt_id.is_not_distinct_from(stored_attempt),
                    JobEvent.type.in_(["actions_pending", "actions_finished"]),
                )
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            recorded = await session.scalar(
                select(JobEvent.id).where(JobEvent.attempt_id == attempt_id, JobEvent.type == "action_report").limit(1)
            )
            if latest == "actions_finished" or recorded is not None:
                return False
            if latest != "actions_pending":
                session.add(
                    JobEvent(
                        job_id=job_id,
                        attempt_id=stored_attempt,
                        type="actions_pending",
                        payload={"actions": [action.model_dump(mode="json") for action in actions]},
                        ts=self.clock(),
                    )
                )
            await session.commit()
            return True

    async def _finish_post_actions(
        self,
        job_id: int,
        attempt_id: int,
        path: str | None,
        probe: ProbeData | None,
        results: list[ActionOutcome],
        rejected: list[ActionOutcome],
        *,
        finished: bool,
        accepted: bool,
        prerequisite_error: str | None,
        node_id: int,
    ) -> bool:
        if accepted:
            async with self.sessionmaker() as session:
                job = await lock_job_source(session, job_id)
                attempt = await current_attempt(session, job_id)
                if (
                    job is None
                    or job.status != JobStatus.SUCCEEDED
                    or attempt is None
                    or attempt.id != attempt_id
                    or attempt.attempt_number != job.attempts
                    or job.node_id != node_id
                    or attempt.node_id != node_id
                ):
                    return False
                recorded = await session.scalar(
                    select(JobEvent).where(JobEvent.attempt_id == attempt_id, JobEvent.type == "action_report").limit(1)
                )
                if recorded is None:
                    session.add(
                        JobEvent(
                            job_id=job_id,
                            attempt_id=attempt_id,
                            type="action_report",
                            payload={
                                "path": path,
                                "probe": probe.model_dump(mode="json") if probe is not None else None,
                                "results": [outcome.model_dump(mode="json") for outcome in results],
                            },
                            ts=self.clock(),
                        )
                    )
                else:
                    path = recorded.payload["path"]
                    probe = ProbeData.model_validate(recorded.payload["probe"]) if recorded.payload["probe"] else None
                    results = TypeAdapter(list[ActionOutcome]).validate_python(recorded.payload["results"])
                await session.commit()
        async with self.sessionmaker() as session:
            job = await lock_job_source(session, job_id)
            if job is None:
                return False
            completed = await session.scalar(
                select(JobEvent.id)
                .where(
                    JobEvent.attempt_id == attempt_id,
                    JobEvent.type == "action_results",
                    JobEvent.payload["phase"].as_string() == "post",
                )
                .limit(1)
            )
            if completed is not None:
                return False
            attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
            if accepted and (job is None or attempt is None or attempt.job_id != job_id):
                raise ValueError("post-action context missing")
            if job is not None and prerequisite_error is not None:
                job.warning = prerequisite_error
            if job is not None and not finished:
                job.warning = "Post-action cancellation did not finish. Workspace retained."
            await session.commit()
            if attempt is None or job is None:
                published_results = [
                    outcome.model_copy(update={"ok": False, "error": "Post-action context missing.", "artifacts": []})
                    if outcome.artifacts
                    else outcome
                    for outcome in results
                ]
            else:
                published_results = await publish_artifacts(
                    self.sessionmaker, attempt.id, results, workspace_dir=self.workspace_dir
                )
                job = await lock_job_source(session, job_id)
                attempt = await session.get(JobAttempt, attempt_id, with_for_update=True, populate_existing=True)
                if job is None or attempt is None or attempt.job_id != job_id or attempt.attempt_number != job.attempts:
                    return False
                if (
                    await session.scalar(
                        select(JobEvent.id)
                        .where(
                            JobEvent.attempt_id == attempt_id,
                            JobEvent.type == "action_results",
                            JobEvent.payload["phase"].as_string() == "post",
                        )
                        .limit(1)
                    )
                    is not None
                ):
                    return False
                for outcome in published_results:
                    if not outcome.ok and outcome.error is not None:
                        job.warning = outcome.error
            all_results = published_results + rejected
            if attempt is not None:
                attempt.action_results = (attempt.action_results or []) + [
                    outcome.model_dump(mode="json") for outcome in all_results
                ]
                if finished and attempt.log_data is not None:
                    try:
                        stage = await self._stage_event(session, attempt)
                        if stage is not None:
                            log = await asyncio.to_thread(read_attempt_log, self.workspace_dir, attempt.log_path or "")
                            stage.payload = {**stage.payload, "log": log.decode(errors="replace")}
                        attempt.log_data = await read_attempt_logs(session, self.workspace_dir, attempt)
                    except (OSError, ValueError) as exc:
                        LOG.warning(
                            "post-action execution log unavailable for job %s",
                            job_id,
                            extra={"error": error_message(exc)},
                        )
                        await retain_unread_log(session, self.workspace_dir, attempt, exc)
            session.add(
                JobEvent(
                    job_id=job_id,
                    attempt_id=attempt_id if attempt is not None else None,
                    type="action_results",
                    payload={"phase": "post", "results": [outcome.model_dump(mode="json") for outcome in all_results]},
                    ts=self.clock(),
                )
            )
            if finished:
                session.add(
                    JobEvent(
                        job_id=job_id,
                        attempt_id=attempt_id if attempt is not None else None,
                        type="actions_finished",
                        ts=self.clock(),
                    )
                )
            await session.commit()
            for outcome in all_results:
                if not outcome.ok:
                    LOG.warning(
                        "Post-action %s failed for job %s attempt %s on node %s, media %s, workspace %s, output %s",
                        outcome.kind,
                        job_id,
                        attempt_id,
                        job.node_id if job is not None else None,
                        job.media_file_id if job is not None else None,
                        self.workspace_dir,
                        path,
                        extra={"error": outcome.error or "No failure reason provided."},
                    )
            return finished

    async def _cleanup_action_workspace(self, job_id: int, attempt_id: int) -> None:
        async with self.sessionmaker() as session:
            stored_attempt = await session.get(JobAttempt, attempt_id)
            if stored_attempt is None or stored_attempt.job_id != job_id:
                return
            number = stored_attempt.attempt_number
        cleanup = asyncio.create_task(self._cleanup_workspace(job_id, [(job_id, number, True)]))
        try:
            async with asyncio.timeout(max(1.0, self.action_task_timeout_s)):
                await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                async with asyncio.timeout(max(1.0, self.action_task_timeout_s)):
                    await asyncio.shield(cleanup)
            except TimeoutError:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except TimeoutError:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)

    def _node_load(self, entry: NodeEntry) -> int:
        # Bound server-side completion backlog when storage is slower than execution.
        jobs = {job_id for job_id, kind in entry.in_flight.items() if kind == "transcode"}
        jobs.update(
            key[1]
            for key, task in self._completions.items()
            if key[0] == entry.node_db_id and not task.done() and self._completion_kinds.get(key) == "transcode"
        )
        return len(jobs)

    def _has_free_slot(self) -> bool:
        return any(
            not entry.disconnected.is_set() and self._node_load(entry) < entry.capabilities.max_transcodes
            for entry in self.entries.values()
        )

    def _staged(self, job_id: int, staged: int) -> None:
        self._staging_slots.pop(job_id, None)
        if not staged:
            # An outcome wakes dispatch, but capacity waits must not create a tight retry loop.
            self._staging_retry_at[job_id] = self.clock() + timedelta(seconds=self.dispatch_interval_s)
        self._fire(f"job:{job_id}")
        self._wake.set()

    def _staging_reservations(self) -> dict[int, int]:
        reserved: dict[int, int] = {}
        for job_id, (plan, probe, tags, encoders, avoid) in self._staging_slots.items():
            if job_id not in self.staging.tasks:
                continue
            target, _ = self._pick(plan, probe, tags, encoders, avoid_node_id=avoid, reserved=reserved)
            if target is not None:
                reserved[target.node_db_id] = reserved.get(target.node_db_id, 0) + 1
        return reserved

    async def _mark_waiting(self, session: AsyncSession, job: Job, reason: str | None, now: datetime) -> bool:
        why = reason or "unknown"
        warning = f"no node can claim this job: {why}"
        if job.warning == warning:
            return False
        won = await session.scalar(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == job.status,
                Job.attempts == job.attempts,
                Job.warning.is_distinct_from(warning),
            )
            .values(warning=warning)
            .returning(Job.id)
        )
        if won is None:
            return False
        job.warning = warning
        session.add(JobEvent(job_id=job.id, type="waiting", payload={"reason": why}, ts=now))
        LOG.debug("Job %s is waiting: %s", job.id, why)
        return True

    def _pick(
        self,
        plan: FFmpegPlan | None,
        probe: ProbeData,
        required_tags: list[str] | None = None,
        required_encoders: list[str] | None = None,
        *,
        avoid_node_id: int | None = None,
        reserved: dict[int, int] | None = None,
    ) -> tuple[NodeEntry | None, str | None]:
        connected = [entry for entry in self.entries.values() if not entry.disconnected.is_set()]
        if not connected:
            return None, "no nodes connected"
        dispatchable = [entry for entry in connected if not entry.paused]
        if plan is not None and plan.video is not None and plan.video.encoder_priority:
            priority = plan.video.encoder_priority
            dispatchable.sort(
                key=lambda entry: next(
                    (index for index, encoder in enumerate(priority) if encoder in entry.capabilities.encoders),
                    len(priority),
                )
            )
        if not dispatchable:
            return None, "all nodes are paused"
        dispatchable.sort(key=lambda entry: entry.node_db_id == avoid_node_id)
        reason = "all nodes are at max transcodes"
        for entry in dispatchable:
            if self._node_load(entry) + (reserved or {}).get(entry.node_db_id, 0) >= entry.capabilities.max_transcodes:
                continue
            capable, why = can_execute(
                entry.capabilities, entry.workspace_ready, plan, probe, required_tags, required_encoders
            )
            if capable:
                return entry, None
            reason = why
        return None, reason

    async def _dispatch_loop(self) -> None:
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.dispatch_interval_s)
                self._wake.clear()
                try:
                    await self.dispatch_once()
                except Exception:
                    LOG.exception("dispatch pass failed")
        except asyncio.CancelledError:
            pass

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.sweep_interval_s)
                try:
                    await self.sweep_once()
                except Exception:
                    LOG.exception("sweep pass failed")
        except asyncio.CancelledError:
            pass

    async def _reject(self, conn: NodeConnection, code: str, message: str, ws_code: int = 4400) -> None:
        with contextlib.suppress(Exception):
            await conn.send_json(Error(code=code, message=message).model_dump(mode="json"))
            await conn.close(ws_code)
