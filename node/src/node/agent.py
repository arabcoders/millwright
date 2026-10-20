from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import websockets
from common.actions import ActionOutcome, extraction_error
from common.capabilities import MkvTools, low_priority_argv
from common.dates import Dates
from common.diagnostics import error_message, error_traceback, redact
from common.enums import ExecutionResult
from common.healthcheck import HealthcheckExitError, _decode_healthcheck
from common.journal import append_message
from common.media.probe import probe_from_raw
from common.media.tools import run_ffprobe, run_mkvpropedit
from common.plan import FFmpegPlan, render
from common.plan.verification import validate_duration, validate_streams
from common.process import ChildNotReapedError, drain, process_policy
from common.protocol import (
    ActionReport,
    ActionTask,
    ActiveAttempt,
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
    OfferedJob,
    Progress,
    SetLimits,
    Welcome,
    WorkspaceChallenge,
    WorkspaceResponse,
    parse_message,
)
from common.protocol.messages import HealthResult
from common.workspace import DEFAULT_HARD_SIZE_CEILING, DEFAULT_MIN_FREE_GB, resolve_workspace_path, workspace_lock

from node.actions import ActionsSpec, run_actions
from node.capabilities import resource_sample
from node.executor import JobSpec, execute
from node.files import check_free_space, create_output, open_log
from node.logging import HubLogHandler
from node.process import create_tool, spawn_tool, stop_tool
from node.repair import prepare_input

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from common.actions import Action
    from websockets.asyncio.client import ClientConnection

    from node.executor import JobResult, ProgressUpdate

LOG = logging.getLogger("node.agent")


def safe_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "invalid endpoint"
    if not parsed.hostname:
        return "unknown"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = " (invalid port)"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}{query}"


_HANDSHAKE_TIMEOUT_S = 10.0
_JOB_DRAIN_TIMEOUT_S = 15.0
_RECONNECT_ERROR = "Hub reconnect allowance expired."
_REVOKED_ERROR = "Server revoked this execution attempt."


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HubConnection(Protocol):
    async def send_json(self, data: dict) -> None: ...
    async def recv_json(self) -> dict | None: ...
    async def close(self) -> None: ...


@dataclass(kw_only=True)
class AgentConfig:
    node_id: str | None = None
    token: str
    enrollment_token: str | None = None
    credentials_path: Path | None = None
    server_url: str
    workspace_dir: str
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    mkv_tools: MkvTools = field(default_factory=MkvTools)
    capabilities: NodeCapabilities = field(default_factory=NodeCapabilities)
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    hard_size_ceiling: float = DEFAULT_HARD_SIZE_CEILING
    stall_timeout_s: float = 600.0
    heartbeat_s: float = 15.0
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0


class _WebSocketConnection:
    def __init__(self, ws: ClientConnection, config: AgentConfig) -> None:
        self._ws = ws
        self._config = config
        self._secrets = (config.token, config.enrollment_token or "")

    async def send_json(self, data: dict) -> None:
        await self._ws.send(json.dumps(data))

    async def recv_json(self) -> dict | None:
        try:
            payload = await self._ws.recv()
        except websockets.exceptions.ConnectionClosed as exc:
            frame = exc.rcvd or exc.sent
            reason = (frame.reason or "No reason provided.") if frame is not None else "No close frame received."
            if frame is not None and frame.code == 1012:
                LOG.info(
                    "Node hub is shutting down or restarting. Reconnecting.",
                    extra={"operation": "node.connection.closed"},
                )
                return None
            LOG.info(
                "Node hub WebSocket closed. Code: %s. Reason: %s",
                frame.code if frame is not None else 1006,
                redact(
                    reason,
                    secrets=(*self._secrets, self._config.token, self._config.enrollment_token or ""),
                ),
                extra={"operation": "node.connection.closed"},
            )
            return None
        return json.loads(payload)

    async def close(self) -> None:
        await self._ws.close()


async def _ws_connect(url: str, config: AgentConfig) -> HubConnection:
    return _WebSocketConnection(await websockets.connect(url), config)


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        connect: Callable[[str], Awaitable[HubConnection]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utcnow,
        dates: Dates | None = None,
    ) -> None:
        self._config = config
        self._connect = connect if connect is not None else lambda url: _ws_connect(url, config)
        self._sleep = sleep
        self._clock = clock
        self._dates = dates
        self._closed = False
        self._disconnecting = False
        self._conn: HubConnection | None = None
        self._welcomed = False
        self._run_task: asyncio.Task | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._outbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._jobs: dict[tuple[int, int], asyncio.Task] = {}
        self._cancel_reasons: dict[tuple[int, int], str] = {}
        self._started_jobs: set[tuple[int, int]] = set()
        self._action_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._log_paths: dict[tuple[int, int], str] = {}
        self._log_handler: HubLogHandler | None = None
        self._reconnect_timer: asyncio.TimerHandle | None = None
        self._slots = {
            "transcode": asyncio.Condition(),
            "healthcheck": asyncio.Condition(),
            "mkv_stats": asyncio.Condition(),
        }
        self._transcode_limit = 1
        self._lane_limits = {"healthcheck": 1, "mkv_stats": 1}
        self._active_lanes = {"healthcheck": 0, "mkv_stats": 0}
        self._enroll = config.node_id is None
        self._active_transcodes = 0
        self._cpu_sample: tuple[int, int] | None = None

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        backoff = self._config.backoff_base_s
        unreaped = False
        try:
            while not self._closed:
                try:
                    conn = await self._connect(self._config.server_url)
                except Exception as exc:
                    endpoint = safe_endpoint(self._config.server_url)
                    level = logging.WARNING
                    context = ""
                    if isinstance(exc, websockets.exceptions.InvalidStatus) and exc.response.status_code == 503:
                        level = logging.DEBUG
                        cause = "HTTP 503"
                        context = "The hub is temporarily unavailable. "
                    elif isinstance(exc, websockets.exceptions.InvalidStatus):
                        cause = f"HTTP {exc.response.status_code} rejected the WebSocket upgrade."
                        context = (
                            "No enrollment token has been sent. "
                            "Check /api/hub routing and WebSocket upgrades on the server and reverse proxy. "
                        )
                    else:
                        userinfo = urlsplit(self._config.server_url).netloc.rpartition("@")[0]
                        cause = error_message(
                            exc,
                            secrets=(self._config.token, self._config.enrollment_token or "", userinfo),
                        )
                        if isinstance(exc, TimeoutError):
                            context = "Connection timed out before the WebSocket upgrade completed. "
                    LOG.log(
                        level,
                        "Could not connect to node hub at %s. %sRetrying in %.1f seconds.",
                        endpoint,
                        context,
                        backoff,
                        extra={
                            "operation": "node.connect.failed",
                            "node_id": self._config.node_id,
                            "endpoint": endpoint,
                            "backoff_s": backoff,
                            "error": cause,
                        },
                    )
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, self._config.backoff_max_s)
                    continue
                try:
                    self._welcomed = False
                    await self._session(conn)
                except ChildNotReapedError:
                    unreaped = True
                    raise
                except websockets.exceptions.ConnectionClosed as exc:
                    frame = exc.rcvd or exc.sent
                    reason = (
                        "Connection dropped without a WebSocket close handshake"
                        if exc.rcvd is None and exc.sent is None
                        else redact(str(exc), secrets=(self._config.token, self._config.enrollment_token or ""))
                    )
                    if frame is not None and frame.code == 1012:
                        LOG.info("Node hub is shutting down or restarting. Reconnecting in %.1f seconds.", backoff)
                    else:
                        LOG.warning("Node hub connection closed (%s). Reconnecting in %.1f seconds.", reason, backoff)
                except Exception as exc:
                    LOG.error(
                        "Node hub session ended unexpectedly.",
                        extra={
                            "error": error_message(
                                exc, secrets=(self._config.token, self._config.enrollment_token or "")
                            ),
                            "traceback": error_traceback(
                                exc, secrets=(self._config.token, self._config.enrollment_token or "")
                            ),
                        },
                    )
                finally:
                    welcomed = self._welcomed
                    self._conn = None
                    if not self._closed:
                        await asyncio.wait_for(conn.close(), _HANDSHAKE_TIMEOUT_S)
                if self._closed:
                    break
                await self._sleep(backoff)
                backoff = self._config.backoff_base_s if welcomed else min(backoff * 2, self._config.backoff_max_s)
        finally:
            self._closed = True
            cleanup = asyncio.create_task(self._stop_jobs())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled and not unreaped:
                raise asyncio.CancelledError

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        self._closed = True
        conn, self._conn = self._conn, None
        run_task = self._run_task
        self._run_task = None
        wait_run = run_task is not None and run_task is not asyncio.current_task()
        if wait_run and run_task is not None:
            run_task.cancel()

        async def stop() -> None:
            try:
                if wait_run and run_task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait_for(run_task, _JOB_DRAIN_TIMEOUT_S)
            finally:
                try:
                    await self._stop_jobs()
                finally:
                    if conn is not None:
                        await asyncio.wait_for(conn.close(), _HANDSHAKE_TIMEOUT_S)

        cleanup = asyncio.create_task(stop())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        await cleanup
        if cancelled:
            raise asyncio.CancelledError

    async def _stop_jobs(self) -> None:
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        tasks = [*self._jobs.values(), *self._action_tasks.values()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        try:
            if tasks:
                await asyncio.gather(
                    *(asyncio.wait_for(task, _JOB_DRAIN_TIMEOUT_S) for task in tasks), return_exceptions=True
                )
        finally:
            if self._log_handler is not None:
                logging.getLogger().removeHandler(self._log_handler)
                self._log_handler.close()
                self._log_handler = None

    def _expire(self) -> None:
        self._reconnect_timer = None
        LOG.warning("Hub reconnect allowance expired. Cancelling node work.")
        for key, task in self._jobs.items():
            self._cancel_reasons.setdefault(key, _RECONNECT_ERROR)
            if not task.cancelling():
                task.cancel()
        for key, task in self._action_tasks.items():
            self._cancel_reasons.setdefault(key, _RECONNECT_ERROR)
            if not task.cancelling():
                task.cancel()

    async def _session(self, conn: HubConnection) -> bool:
        self._conn = conn
        credentials = (self._config.token, self._config.enrollment_token or "")
        hello = Hello(
            active_attempts=[
                ActiveAttempt(job_id=job_id, attempt_id=attempt_id)
                for job_id, attempt_id in dict.fromkeys(
                    key
                    for owners in (self._jobs, self._action_tasks)
                    for key, task in owners.items()
                    if not task.done()
                )
            ],
            node_id=None if self._enroll else self._config.node_id,
            token=self._config.enrollment_token
            if self._enroll and self._config.enrollment_token
            else self._config.token,
            capabilities=self._config.capabilities.model_copy(
                update={
                    "max_transcodes": 1,
                    "mkvpropedit": self._config.mkv_tools.mkvpropedit is not None,
                }
            ),
        ).model_dump(mode="json")
        await conn.send_json(hello)
        try:
            data = await asyncio.wait_for(conn.recv_json(), timeout=_HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            LOG.warning(
                "Node hub sent no initial handshake reply within %.1f seconds.",
                _HANDSHAKE_TIMEOUT_S,
                extra={"operation": "node.handshake.timeout", "node_id": self._config.node_id},
            )
            return False
        if data is None:
            LOG.warning("Node hub handshake ended without an initial reply.")
            return False
        try:
            first = parse_message(data)
        except Exception as exc:
            LOG.warning(
                "Node hub handshake reply was invalid.",
                extra={
                    "operation": "node.handshake.invalid",
                    "node_id": self._config.node_id,
                    "error": error_message(exc, secrets=credentials),
                },
            )
            return False
        if isinstance(first, Error):
            if first.code == "auth" and self._config.node_id is not None and self._config.enrollment_token:
                self._enroll = True
            LOG.warning(
                "Node hub handshake rejected at welcome: %s (%s)",
                redact(first.message, secrets=credentials),
                redact(first.code, secrets=credentials),
                extra={
                    "operation": "node.handshake.rejected",
                    "node_id": self._config.node_id,
                    "outcome": redact(first.code, secrets=credentials),
                },
            )
            return False
        if isinstance(first, WorkspaceChallenge):
            response = self._workspace_response(first)
            await conn.send_json(response)
            if not response["workspace_ready"]:
                return False
            try:
                data = await asyncio.wait_for(conn.recv_json(), timeout=_HANDSHAKE_TIMEOUT_S)
            except (TimeoutError, ValueError) as exc:
                LOG.warning(
                    "Node hub workspace handshake failed.",
                    extra={"error": error_message(exc, secrets=credentials)},
                )
                return False
            if data is None:
                LOG.warning("Node hub handshake ended without a reply after workspace verification.")
                return False
            try:
                first = parse_message(data)
            except ValueError as exc:
                LOG.warning(
                    "Node hub workspace handshake reply was invalid.",
                    extra={"error": error_message(exc, secrets=credentials)},
                )
                return False
        if isinstance(first, Error):
            if first.code == "auth" and self._config.node_id is not None and self._config.enrollment_token:
                self._enroll = True
            LOG.warning(
                "Node hub handshake rejected after workspace: %s (%s)",
                redact(first.message, secrets=credentials),
                redact(first.code, secrets=credentials),
                extra={
                    "operation": "node.handshake.rejected",
                    "node_id": self._config.node_id,
                    "outcome": redact(first.code, secrets=credentials),
                },
            )
            return False
        if isinstance(first, Enrolled):
            if not self._enroll:
                LOG.warning("Node hub handshake received unexpected Enrolled while not enrolling.")
                return False
            credentials = (*credentials, first.token)
            if self._config.credentials_path is not None:
                try:
                    self._save_credentials(first.node_id, first.token)
                except OSError as exc:
                    LOG.warning(
                        "Node hub enrollment failed while saving credentials.",
                        extra={"error": error_message(exc, secrets=credentials)},
                    )
                    return False
            self._config.node_id = first.node_id
            self._config.token = first.token
            self._enroll = False
            await conn.send_json(EnrollmentSaved(node_id=first.node_id).model_dump(mode="json"))
            try:
                data = await asyncio.wait_for(conn.recv_json(), timeout=_HANDSHAKE_TIMEOUT_S)
            except (TimeoutError, ValueError) as exc:
                LOG.warning(
                    "Node hub enrollment handshake failed.",
                    extra={"error": error_message(exc, secrets=credentials)},
                )
                return False
            if data is None:
                LOG.warning("Node hub handshake ended without a welcome reply after enrollment.")
                return False
            try:
                first = parse_message(data)
            except ValueError as exc:
                LOG.warning(
                    "Node hub enrollment handshake reply was invalid.",
                    extra={"error": error_message(exc, secrets=credentials)},
                )
                return False
            if isinstance(first, Error):
                LOG.warning(
                    "Node hub handshake rejected after enrollment: %s (%s)",
                    redact(first.message, secrets=credentials),
                    redact(first.code, secrets=credentials),
                    extra={
                        "operation": "node.handshake.rejected",
                        "node_id": self._config.node_id,
                        "outcome": redact(first.code, secrets=credentials),
                    },
                )
                return False
        if not isinstance(first, Welcome):
            LOG.warning(
                "Node hub handshake expected Welcome but received %s.",
                type(first).__name__,
                extra={
                    "operation": "node.handshake.rejected",
                    "node_id": self._config.node_id,
                    "outcome": "invalid",
                },
            )
            return False
        authorized = {(attempt.job_id, attempt.attempt_id) for attempt in first.active_attempts}
        self._disconnecting = True
        revoked = []
        for owners in (self._jobs, self._action_tasks):
            for key, task in owners.items():
                if not task.done() and key not in authorized:
                    self._cancel_reasons[key] = _REVOKED_ERROR
                    if not task.cancelling():
                        task.cancel()
                    revoked.append(task)
        if revoked:
            outcomes = await drain(asyncio.gather(*revoked, return_exceptions=True))
            for outcome in outcomes:
                if isinstance(outcome, ChildNotReapedError):
                    raise outcome
        self._welcomed = True
        self._disconnecting = False
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        for condition in self._slots.values():
            async with condition:
                condition.notify_all()
        if self._dates is not None:
            self._dates.timezone = Dates(first.timezone).timezone
        # One writer preserves message order.
        writer = asyncio.create_task(self._writer(conn))
        heartbeat = asyncio.create_task(self._heartbeat_loop(first.heartbeat_s or self._config.heartbeat_s))
        receiver = asyncio.create_task(self._receive_loop(conn))
        if self._log_handler is None:
            self._log_handler = HubLogHandler(asyncio.get_running_loop(), self._queue_logs)
            logging.getLogger().addHandler(self._log_handler)
        LOG.info(
            "Connected to node hub as node %s",
            self._config.node_id,
            extra={"operation": "node.handshake.accepted", "node_id": self._config.node_id},
        )
        unreaped = False
        try:
            done, _ = await asyncio.wait({receiver, writer, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            for task in (heartbeat, writer, receiver):
                if task in done:
                    await task
        except ChildNotReapedError:
            unreaped = True
            raise
        finally:
            self._disconnecting = True
            if not self._closed and self._reconnect_timer is None:
                self._reconnect_timer = asyncio.get_running_loop().call_later(max(0.0, first.lease_ttl_s), self._expire)
            for task in (writer, heartbeat, receiver):
                task.cancel()
            cleanup = asyncio.gather(writer, heartbeat, receiver, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if not heartbeat.cancelled():
                heartbeat.result()
            if cancelled and not unreaped:
                raise asyncio.CancelledError
        return True

    def _save_credentials(self, node_id: str, token: str) -> None:
        path = self._config.credentials_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        temp = path.with_name(f".{path.name}.partial")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"node_id": node_id, "token": token}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise

    async def _receive_loop(self, conn: HubConnection) -> None:
        while True:
            data = await conn.recv_json()
            if data is None or self._closed or self._disconnecting:
                return
            try:
                msg = parse_message(data)
            except Exception as exc:
                LOG.warning(
                    "Ignored invalid hub message.",
                    extra={
                        "error": error_message(exc, secrets=(self._config.token, self._config.enrollment_token or ""))
                    },
                )
                continue
            if isinstance(msg, Error):
                reason = f"hub rejected message ({msg.code}): {msg.message}"
                raise RuntimeError(reason)
            if isinstance(msg, JobOffer):
                key = (msg.job.job_id, msg.job.attempt_id)
                if key not in self._jobs:
                    self._log_paths[key] = msg.job.log_path
                    self._jobs[key] = asyncio.create_task(self._run_job(msg.job))
                    self._jobs[key].add_done_callback(lambda task, key=key: self._forget(key, task))
            elif isinstance(msg, SetLimits):
                async with self._slots["transcode"]:
                    self._transcode_limit = msg.max_transcodes
                    self._slots["transcode"].notify_all()
                for kind, limit in (("healthcheck", msg.max_healthchecks), ("mkv_stats", msg.max_mkv_stats)):
                    async with self._slots[kind]:
                        self._lane_limits[kind] = limit
                        self._slots[kind].notify_all()
            elif isinstance(msg, Cancel):
                key = (msg.job_id, msg.attempt_id)
                job_task = self._jobs.get(key) if self._log_paths.get(key) == msg.log_path else None
                if job_task is not None:
                    self._cancel_reasons.setdefault(key, msg.reason)
                    LOG.info(
                        "Cancelled job %s attempt %s on node %s",
                        msg.job_id,
                        msg.attempt_id,
                        self._config.node_id,
                        extra={
                            "operation": "job.cancelled",
                            "node_id": self._config.node_id,
                            "job_id": msg.job_id,
                            "attempt_id": msg.attempt_id,
                            "stage": "transcode",
                        },
                    )
                    if key in self._started_jobs and not job_task.cancelling():
                        job_task.cancel()
                action_task = self._action_tasks.get(key) if msg.log_path is None else None
                if action_task is not None:
                    self._cancel_reasons.setdefault(key, msg.reason)
                    if not action_task.cancelling():
                        action_task.cancel()
                if job_task is None and action_task is None:
                    await self._outbox.put(
                        AttemptMissing(job_id=msg.job_id, attempt_id=msg.attempt_id, log_path=msg.log_path).model_dump(
                            mode="json"
                        )
                    )
            elif isinstance(msg, ActionTask):
                key = (msg.job_id, msg.attempt_id)
                if key not in self._action_tasks:
                    self._action_tasks[key] = asyncio.create_task(self._run_action_task(msg))
                    self._action_tasks[key].add_done_callback(lambda task, key=key: self._forget(key, task))

    def _forget(self, key: tuple[int, int], task: asyncio.Task) -> None:
        # A task cancelled before its first turn never reaches its own cleanup.
        for owners in (self._jobs, self._action_tasks):
            if owners.get(key) is task:
                owners.pop(key)
        if key not in self._jobs and key not in self._action_tasks:
            self._log_paths.pop(key, None)
            self._cancel_reasons.pop(key, None)
            self._started_jobs.discard(key)
        if not task.cancelled() and (exc := task.exception()) is not None:
            LOG.error("Node task failed for job %s attempt %s", *key, exc_info=exc)

    async def _run_job(self, offer: OfferedJob) -> None:
        key = (offer.job_id, offer.attempt_id)
        with contextlib.ExitStack() as scope:
            try:
                scope.enter_context(workspace_lock(self._resolve(offer.log_path).parent))
            except (OSError, ValueError) as exc:
                LOG.exception("Could not lock workspace for job %s attempt %s", *key)
                self._queue_message(
                    Finished(
                        job_id=offer.job_id,
                        attempt_id=offer.attempt_id,
                        kind=offer.kind,
                        result="failed",
                        error=str(exc),
                        log_path=offer.log_path,
                    ).model_dump(mode="json"),
                    offer.log_path,
                )
                self._jobs.pop(key, None)
                if key not in self._action_tasks:
                    self._log_paths.pop(key, None)
                    self._cancel_reasons.pop(key, None)
                return
            await self._run_locked_job(offer)

    async def _run_locked_job(self, offer: OfferedJob) -> None:
        key = (offer.job_id, offer.attempt_id)
        self._started_jobs.add(key)
        self._log_paths[key] = offer.log_path
        LOG.info(
            "Started job %s attempt %s on node %s",
            offer.job_id,
            offer.attempt_id,
            self._config.node_id,
            extra={
                "operation": "job.started",
                "node_id": self._config.node_id,
                "job_id": offer.job_id,
                "attempt_id": offer.attempt_id,
                "stage": offer.kind,
            },
        )
        try:
            message = await self._execute_job(offer)
        except asyncio.CancelledError:
            reason = self._cancel_reasons.get(key)
            if reason is None or self._closed:
                raise
            self._queue_message(
                Finished(
                    job_id=offer.job_id,
                    attempt_id=offer.attempt_id,
                    kind=offer.kind,
                    result="stalled"
                    if reason == "stalled"
                    else "failed"
                    if reason == _RECONNECT_ERROR
                    else "cancelled",
                    error=reason,
                    log_path=offer.log_path,
                ).model_dump(mode="json"),
                offer.log_path,
            )
        except ChildNotReapedError:
            await self._disconnect_without_result()
            if self._cancel_reasons.get(key) == _REVOKED_ERROR:
                raise
        except Exception as exc:
            LOG.exception(
                "Could not run job %s attempt %s for workspace file %s on node %s",
                offer.job_id,
                offer.attempt_id,
                offer.source_path,
                self._config.node_id,
                extra={"job_id": offer.job_id, "attempt_id": offer.attempt_id},
            )
            self._queue_message(
                Finished(
                    job_id=offer.job_id,
                    attempt_id=offer.attempt_id,
                    kind=offer.kind,
                    result="failed",
                    error=str(exc),
                    log_path=offer.log_path,
                ).model_dump(mode="json"),
                offer.log_path,
            )
        else:
            LOG.log(
                logging.WARNING if message["result"] in {"failed", "aborted", "stalled"} else logging.INFO,
                "Job %s attempt %s for workspace file %s on node %s finished: %s.\nExecution log: %s",
                offer.job_id,
                offer.attempt_id,
                offer.source_path,
                self._config.node_id,
                message["result"],
                offer.log_path,
                extra={
                    "operation": "job.finished",
                    "node_id": self._config.node_id,
                    "job_id": offer.job_id,
                    "attempt_id": offer.attempt_id,
                    "outcome": message["result"],
                    "error": message.get("error"),
                },
            )
            self._queue_message(message, offer.log_path)
        finally:
            if self._log_handler is not None:
                self._log_handler.flush()
            self._jobs.pop(key, None)
            self._started_jobs.discard(key)
            if key not in self._action_tasks:
                self._log_paths.pop(key, None)
                self._cancel_reasons.pop(key, None)

    async def _execute_job(self, offer: OfferedJob) -> dict:
        key = (offer.job_id, offer.attempt_id)
        if key in self._cancel_reasons:
            raise asyncio.CancelledError
        if offer.kind != "transcode":
            return await self._execute_auxiliary(offer)
        async with self._transcode_slot(), contextlib.AsyncExitStack() as preparation:
            if key in self._cancel_reasons:
                raise asyncio.CancelledError
            spec = self._build_spec(offer)
            log = preparation.enter_context(open_log(Path(spec.log)))

            def write_log(text: str) -> None:
                log.write(text.encode("utf-8", errors="replace"))
                log.flush()

            prepared = await preparation.enter_async_context(
                prepare_input(
                    Path(spec.src),
                    spec.probe,
                    Path(spec.dst).with_name(f"repair-{Path(spec.dst).stem}"),
                    tools=self._config.mkv_tools,
                    workspace_dir=Path(self._config.workspace_dir).resolve(),
                    ffmpeg_bin=self._config.ffmpeg_bin,
                    ffprobe_bin=self._config.ffprobe_bin,
                    log=write_log,
                    min_free_gb=spec.min_free_gb,
                    timeout_s=spec.stall_timeout_s,
                )
            )
            action_results = await self._run_pre_actions(
                spec.model_copy(update={"src": str(prepared.path), "probe": prepared.probe}), offer.actions
            )
            if key in self._cancel_reasons:
                raise asyncio.CancelledError
            error = extraction_error(offer.actions, spec.probe, action_results)
            if error is not None:
                return Finished(
                    job_id=offer.job_id,
                    attempt_id=offer.attempt_id,
                    result="failed",
                    error=error,
                    action_results=action_results,
                    log_path=offer.log_path,
                ).model_dump(mode="json")
            result = await execute(
                spec,
                self._config.ffmpeg_bin,
                self._config.ffprobe_bin,
                self._progress_sink(offer.job_id, offer.attempt_id, offer.log_path),
                prepared=prepared,
            )
            if self._closed:
                raise asyncio.CancelledError
            message = self._finished_msg(offer.job_id, offer.attempt_id, result, action_results, offer.log_path)
            reason = self._cancel_reasons.get(key)
            if reason is not None:
                message["result"] = (
                    "stalled" if reason == "stalled" else "failed" if reason == _RECONNECT_ERROR else "cancelled"
                )
                message["error"] = reason
                message["output_path"] = None
                message["output_probe"] = None
            return message

    @asynccontextmanager
    async def _lane_slot(self, kind: str):
        condition = self._slots[kind]
        async with condition:
            await condition.wait_for(
                lambda: self._active_lanes[kind] < self._lane_limits[kind] and not self._disconnecting
            )
            if self._closed:
                raise asyncio.CancelledError
            self._active_lanes[kind] += 1
        try:
            yield
        finally:
            async with condition:
                self._active_lanes[kind] -= 1
                condition.notify_all()

    async def _execute_auxiliary(self, offer: OfferedJob) -> dict:
        async with self._lane_slot(offer.kind):
            with process_policy(
                create=partial(create_tool, workspace=self._resolve(offer.log_path).parent), stop=stop_tool
            ):
                return await self._run_auxiliary(offer)

    async def _run_auxiliary(self, offer: OfferedJob) -> dict:
        source = self._resolve(offer.source_path)
        path = self._resolve(offer.log_path)
        with open_log(path) as log:

            def write(text: str) -> None:
                log.write(text.encode(errors="replace"))
                log.flush()

            async def progress(payload: dict[str, object]) -> None:
                percent = payload.get("percent")
                self._queue_message(
                    Progress(
                        job_id=offer.job_id,
                        attempt_id=offer.attempt_id,
                        phase=str(payload.get("stage", offer.kind)),
                        percent=float(percent) if isinstance(percent, (int, float)) else None,
                    ).model_dump(mode="json"),
                    offer.log_path,
                )

            await progress({"stage": "probe"})
            if offer.kind == "healthcheck":

                class Log:
                    def append(self, text: str, *, sync: bool = False) -> None:
                        write(text)
                        if sync:
                            os.fsync(log.fileno())

                try:
                    result = await _decode_healthcheck(
                        str(source),
                        self._config.ffmpeg_bin,
                        self._config.ffprobe_bin,
                        Log(),
                        hwaccel=offer.healthcheck_hwaccel,
                        on_progress=progress,
                    )
                except HealthcheckExitError as exc:
                    raise ChildNotReapedError(str(exc)) from exc
                return Finished(
                    job_id=offer.job_id,
                    attempt_id=offer.attempt_id,
                    kind=offer.kind,
                    result="failed" if result.failed else "succeeded",
                    error=result.error,
                    health=None
                    if result.failed
                    else HealthResult(
                        healthy=result.healthy, error_count=result.error_count, samples=result.samples or []
                    ),
                    raw_probe=result.probe,
                    source_size=offer.probe.size_bytes,
                    log_path=offer.log_path,
                ).model_dump(mode="json")
            tool = self._config.mkv_tools.mkvpropedit
            if tool is None:
                raise ValueError("mkvpropedit is unavailable on this node.")
            output = self._resolve(offer.output_path)
            check_free_space(output.parent, max(offer.min_free_gb, self._config.min_free_gb))
            artifact = create_output(output)
            try:
                # Edit a copy so the staged source stays unchanged.
                copy = await spawn_tool(
                    sys.executable,
                    "-c",
                    "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])",
                    str(source),
                    str(output),
                    workspace=path.parent,
                )
                try:
                    if await copy.wait() != 0:
                        raise OSError("Statistics workspace copy failed.")  # noqa: TRY301 -- Wait for the copy process to exit before reporting failure.
                except BaseException:
                    await stop_tool(copy, force=True)
                    raise

                async def tool_result(payload: dict) -> None:
                    write(json.dumps(payload) + "\n")

                async def stats_progress(percent: int) -> None:
                    await progress({"stage": "mkvpropedit", "percent": percent})

                if offer.statistics_edit:
                    await run_mkvpropedit(
                        tool, output, argv_prefix=low_priority_argv(), on_progress=stats_progress, on_result=tool_result
                    )
                raw = await run_ffprobe(
                    self._config.ffprobe_bin, output, argv_prefix=low_priority_argv(), on_result=tool_result
                )
                probe = probe_from_raw(output, output.stat().st_size, raw).model_copy(
                    update={"file_path": offer.output_path}
                )
                plan = FFmpegPlan()
                expected = render(plan, offer.probe, offer.source_path, offer.output_path).stream_log
                validate_duration(offer.probe, probe, plan, expected)
                validate_streams(offer.probe, probe, expected)
                limit = (
                    min(offer.hard_size_ceiling, self._config.hard_size_ceiling, plan.expected.size_ceiling_ratio)
                    * offer.probe.size_bytes
                )
                if probe.size_bytes > limit:
                    message = f"Statistics output size {probe.size_bytes} bytes exceeds the allowed {limit:g} bytes."
                    raise ValueError(message)
                artifact.retained = True
                return Finished(
                    job_id=offer.job_id,
                    attempt_id=offer.attempt_id,
                    kind=offer.kind,
                    result="succeeded",
                    output_path=offer.output_path,
                    output_probe=probe,
                    raw_probe=raw,
                    source_size=offer.probe.size_bytes,
                    output_size=probe.size_bytes,
                    log_path=offer.log_path,
                ).model_dump(mode="json")

            except ChildNotReapedError:
                artifact.retained = True
                raise
            finally:
                artifact.discard()

    async def _run_pre_actions(self, spec: JobSpec, actions: list[Action]) -> list[ActionOutcome]:
        if not actions:
            return []
        return await run_actions(
            ActionsSpec(
                job_id=int(spec.job_id),
                path=Path(spec.src),
                probe=spec.probe,
                output_path=Path(spec.dst),
                actions=actions,
                workspace_dir=Path(self._config.workspace_dir).resolve(),
                log_path=Path(spec.log),
            ),
            self._config.ffmpeg_bin,
        )

    async def _run_action_task(self, task: ActionTask) -> None:
        key = (task.job_id, task.attempt_id)
        output = Path(task.path)
        log_name = (
            f"ffmpeg{output.stem.removeprefix('output')}.log" if output.stem.startswith("output-") else "ffmpeg.log"
        )
        log_path = str(output.with_name(log_name))
        self._log_paths[key] = log_path
        with contextlib.ExitStack() as scope:
            try:
                scope.enter_context(workspace_lock(self._resolve(log_path).parent))
            except (OSError, ValueError) as exc:
                LOG.exception("Could not lock workspace for action task %s attempt %s", *key)
                self._queue_message(
                    ActionReport(
                        job_id=task.job_id,
                        attempt_id=task.attempt_id,
                        results=[ActionOutcome(kind=action.kind, ok=False, error=str(exc)) for action in task.actions],
                    ).model_dump(mode="json"),
                    log_path,
                )
                self._action_tasks.pop(key, None)
                if key not in self._jobs:
                    self._log_paths.pop(key, None)
                    self._cancel_reasons.pop(key, None)
                return
            await self._run_locked_actions(task, log_path)

    async def _run_locked_actions(self, task: ActionTask, log_path: str) -> None:
        key = (task.job_id, task.attempt_id)
        try:
            try:
                spec = ActionsSpec(
                    job_id=task.job_id,
                    path=self._resolve(task.path),
                    probe=task.probe,
                    actions=task.actions,
                    workspace_dir=Path(self._config.workspace_dir).resolve(),
                    log_path=self._resolve(log_path),
                )
                async with contextlib.AsyncExitStack() as preparation:
                    if spec.probe is not None:
                        log = preparation.enter_context(open_log(spec.log_path))

                        def write_log(text: str) -> None:
                            log.write(text.encode("utf-8", errors="replace"))
                            log.flush()

                        prepared = await preparation.enter_async_context(
                            prepare_input(
                                spec.path,
                                spec.probe,
                                spec.path.with_name(f"repair-actions-{spec.path.stem}"),
                                tools=self._config.mkv_tools,
                                workspace_dir=Path(self._config.workspace_dir).resolve(),
                                ffmpeg_bin=self._config.ffmpeg_bin,
                                ffprobe_bin=self._config.ffprobe_bin,
                                log=write_log,
                                min_free_gb=self._config.min_free_gb,
                                timeout_s=self._config.stall_timeout_s,
                            )
                        )
                        spec.output_path = spec.path
                        spec.path = prepared.path
                        spec.probe = prepared.probe
                    results = await run_actions(spec, self._config.ffmpeg_bin)
            except asyncio.CancelledError:
                if self._closed:
                    raise
                results = [
                    ActionOutcome(kind=action.kind, ok=False, error=self._cancel_reasons.get(key, "action cancelled"))
                    for action in task.actions
                ]
            except ChildNotReapedError:
                await self._disconnect_without_result()
                if self._cancel_reasons.get(key) == _REVOKED_ERROR:
                    raise
                return
            except Exception as exc:
                LOG.exception("action task %s failed", task.job_id)
                results = [ActionOutcome(kind=action.kind, ok=False, error=str(exc)) for action in task.actions]
            if key in self._cancel_reasons:
                results = [
                    ActionOutcome(kind=action.kind, ok=False, error=self._cancel_reasons[key])
                    for action in task.actions
                ]
            if not self._closed:
                self._queue_message(
                    ActionReport(job_id=task.job_id, attempt_id=task.attempt_id, results=results).model_dump(
                        mode="json"
                    ),
                    log_path,
                )
        finally:
            if self._log_handler is not None:
                self._log_handler.flush()
            self._action_tasks.pop(key, None)
            if key not in self._jobs:
                self._log_paths.pop(key, None)
                self._cancel_reasons.pop(key, None)

    async def _disconnect_without_result(self) -> None:
        self._disconnecting = True
        LOG.exception(
            "Couldn't confirm that the subprocess exited. The node will disconnect and won't report completion."
        )
        if self._conn is not None:
            try:
                await asyncio.wait_for(self._conn.close(), _HANDSHAKE_TIMEOUT_S)
            except Exception:
                LOG.exception("could not close hub connection")

    def _build_spec(self, offer: OfferedJob) -> JobSpec:
        plan = offer.plan
        probe = offer.probe
        if plan is None or probe is None:
            msg = f"job {offer.job_id} requires a transcode plan and probe"
            raise ValueError(msg)
        return JobSpec(
            job_id=str(offer.job_id),
            attempt_id=offer.attempt_id,
            src=str(self._resolve(offer.source_path)),
            dst=str(self._resolve(offer.output_path)),
            log=str(self._resolve(offer.log_path)),
            subtitle_path=str(self._resolve(offer.subtitle_path)) if offer.subtitle_path is not None else None,
            plan=plan,
            probe=probe,
            min_free_gb=max(offer.min_free_gb, self._config.min_free_gb),
            hard_size_ceiling=min(offer.hard_size_ceiling, self._config.hard_size_ceiling),
            stall_timeout_s=min(offer.stall_timeout_s, self._config.stall_timeout_s),
        )

    def _progress_sink(self, job_id: int, attempt_id: int, log_path: str) -> Callable[[ProgressUpdate], None]:
        def sink(update: ProgressUpdate) -> None:
            self._queue_message(
                Progress(
                    job_id=job_id,
                    attempt_id=attempt_id,
                    percent=update.percent,
                    fps=update.fps,
                    out_time_us=update.out_time_us,
                    total_size=update.total_size,
                    speed=update.speed,
                ).model_dump(mode="json"),
                log_path,
            )

        return sink

    def _queue_message(self, message: dict, log_path: str) -> None:
        try:
            with workspace_lock(self._resolve(log_path).parent):
                append_message(Path(self._config.workspace_dir), log_path, message)
        except OSError, ValueError:
            LOG.exception("Could not record job message in %s", log_path, extra={"_hub_log_skip": True})
        if self._outbox.full():
            if message["type"] not in {"finished", "action_report"}:
                return
            self._outbox.get_nowait()
        self._outbox.put_nowait(message)

    def _finished_msg(
        self,
        job_id: int,
        attempt_id: int,
        result: JobResult,
        action_results: list[ActionOutcome],
        log_path: str,
    ) -> dict:
        root = Path(self._config.workspace_dir).resolve()
        output_path = str(Path(result.output_path).resolve().relative_to(root)) if result.output_path else None
        output_probe = result.output_probe
        if output_probe is not None:
            output_probe = output_probe.model_copy(
                update={"file_path": str(Path(output_probe.file_path).resolve().relative_to(root))}
            )
        return Finished(
            job_id=job_id,
            attempt_id=attempt_id,
            result=result.status,
            error=result.error,
            output_path=None if result.status is not ExecutionResult.SUCCEEDED else output_path,
            output_size=result.output_size,
            source_size=result.source_size,
            argv=[],
            argv_hash=hashlib.sha256("\0".join(result.argv).encode()).hexdigest(),
            output_probe=output_probe,
            action_results=action_results,
            log_path=log_path,
        ).model_dump(mode="json")

    async def _writer(self, conn: HubConnection) -> None:
        try:
            while True:
                msg = await self._outbox.get()
                await conn.send_json(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("send to hub failed", extra={"_hub_log_skip": True})
            raise

    def _queue_logs(self, message: dict) -> None:
        entry = message["entries"][0]
        job_id, attempt_id = entry.get("job_id"), entry.get("attempt_id")
        if isinstance(job_id, int) and isinstance(attempt_id, int):
            log_path = self._log_paths.get((job_id, attempt_id))
            if log_path is not None:
                self._queue_message(message, log_path)
                return
        if not self._outbox.full():
            self._outbox.put_nowait(message)

    async def _heartbeat_loop(self, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                cpu, memory, gpus, self._cpu_sample = await resource_sample(self._cpu_sample)
                if not self._outbox.full():
                    self._outbox.put_nowait(
                        Heartbeat(cpu_percent=cpu, memory_percent=memory, gpus=gpus).model_dump(mode="json")
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOG.error(
                "Node heartbeat failed.",
                extra={
                    "operation": "node.heartbeat.failed",
                    "node_id": self._config.node_id,
                    "error": error_message(exc, secrets=(self._config.token, self._config.enrollment_token or "")),
                    "traceback": error_traceback(
                        exc, secrets=(self._config.token, self._config.enrollment_token or "")
                    ),
                },
            )
            raise

    @asynccontextmanager
    async def _transcode_slot(self):
        condition = self._slots["transcode"]
        async with condition:
            await condition.wait_for(
                lambda: self._active_transcodes < self._transcode_limit and not self._disconnecting
            )
            if self._closed:
                raise asyncio.CancelledError
            self._active_transcodes += 1
        try:
            yield
        except ChildNotReapedError:
            self._disconnecting = True
            raise
        finally:
            async with condition:
                self._active_transcodes -= 1
                condition.notify_all()

    def _resolve(self, value: str) -> Path:
        root = Path(self._config.workspace_dir).resolve()
        resolve_workspace_path(root, value)
        path = root.joinpath(*value.split("/"))
        for component in (path, *path.parents):
            if component == root:
                break
            if component.is_symlink():
                msg = "workspace path contains a symlink"
                raise ValueError(msg)
        return path

    def _workspace_response(self, challenge: WorkspaceChallenge) -> dict:
        root = Path(self._config.workspace_dir)
        challenge_path = root / challenge.challenge_path
        try:
            root = root.resolve()
            challenge_path = root / challenge.challenge_path
            challenge_path = self._resolve(challenge.challenge_path)
            digest = hashlib.sha256(challenge_path.read_bytes()).hexdigest() + "\n"
            response = self._resolve(challenge.response_path)
            # The server reads only after our reply, so this temporary file needs no atomic publication.
            response.write_text(digest, encoding="ascii")
            return WorkspaceResponse(
                challenge_id=challenge.challenge_id,
                response_path=challenge.response_path,
                workspace_ready=True,
                workspace_free_bytes=shutil.disk_usage(root).free,
            ).model_dump(mode="json")
        except (OSError, ValueError) as exc:
            secrets = (self._config.token, self._config.enrollment_token or "")
            cause = error_message(exc, secrets=secrets)
            LOG.warning(
                "Node workspace verification failed. Resolved workspace: %s. Challenge: %s. "
                "Check server workspace_dir/MW_WORKSPACE_DIR and node workspace_dir/MW_NODE_WORKSPACE_DIR. "
                "Both must access the same shared files. Paths can differ between hosts.",
                redact(str(root), secrets=secrets),
                redact(str(challenge_path), secrets=secrets),
                extra={"operation": "node.workspace.failed", "node_id": self._config.node_id, "error": cause},
            )
            return WorkspaceResponse(
                challenge_id=challenge.challenge_id,
                response_path=challenge.response_path,
                workspace_ready=False,
                workspace_free_bytes=0,
                error=cause,
            ).model_dump(mode="json")


__all__ = ["Agent", "AgentConfig", "HubConnection"]
