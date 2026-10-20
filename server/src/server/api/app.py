from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import structlog
from common.diagnostics import error_message, error_traceback, redact
from common.enums import ExecutionLocation
from fastapi import FastAPI, Request, WebSocket
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from common import version
from server.api import (
    background,
    backups,
    directories,
    files,
    flows,
    jobs,
    libraries,
    logs,
    metrics,
    nodes,
    notifications,
    playback,
    plugins,
    settings,
    stats,
    webhooks,
    workspaces,
)
from server.api import trash as trash_api
from server.api.auth import AuthStack, OidcService, auth_router
from server.api.documentation import documentation_router
from server.api.events import router as events_router
from server.cache import JsonCache
from server.core.auth import PendingStates, WsTickets
from server.core.config import ServerConfig, is_oidc_ready
from server.core.log_history import install_log_file
from server.core.paths import (
    cache_dir,
    config_dir,
    config_file,
    healthcheck_dir,
    playback_dir,
    state_dir,
    trash_dir,
    uploaded_plugins_dir,
    validate_runtime_roots,
    workspace_dir,
)
from server.events import EventBus
from server.finalization import recover_incomplete
from server.flows.batch import BatchDryRuns
from server.healthcheck import HealthcheckService
from server.healthlogs import recover_healthcheck_logs
from server.jobs import JobService
from server.kv import KeyValueStore
from server.library_deletion import LibraryDeletionService
from server.nodehub.hub import NodeHub
from server.notifications import NotificationService
from server.playback.service import PlaybackService
from server.plugins.host import PluginHost
from server.plugins.store_migrations import migrate_saved_plugins
from server.probing import ProbeService
from server.queue.enqueue import FlowEnqueueService
from server.queue.store import requeue_startup
from server.retention import prune_jobs
from server.scanner.service import ScannerService
from server.scheduler.service import Scheduler
from server.settings import OperationalSettings, SettingsService
from server.stats import STATS_INTERVAL_S, StatsService
from server.trash import cleanup_trash, purge_trash, recover_trash_restores, refresh_trash_records
from server.workspace import cleanup_workspace, snapshot_interrupted_logs
from server.workspaces import WorkspaceService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from starlette.types import ASGIApp, Receive, Scope, Send

    from server.nodehub.hub import NodeConnection


ACCESS_LOG = logging.getLogger("server.access")
LOG = logging.getLogger("server.api.app")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_REPO_ROOT = Path(__file__).resolve().parents[4]


class ResetGuardMiddleware:
    """Start mutations in the caller's task so cancellation can wait for cleanup."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and not scope["path"].startswith("/api/auth/")
            and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
            and scope["app"].state.recovering
        ):
            await JSONResponse({"detail": "Server recovery is not complete. Check the server logs."}, status_code=503)(
                scope, receive, send
            )
            return
        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
            and scope["path"] != "/api/backups/reset"
            and scope["app"].state.reset_lock.locked()
        ):
            await JSONResponse({"detail": "reset in progress"}, status_code=409)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class AccessLogMiddleware:
    """Request context and optional summaries. Replaces uvicorn's access log."""

    def __init__(self, app: ASGIApp, *, access_log: bool = True, secrets: tuple[str, ...] = ()) -> None:
        self.app = app
        self.access_log = access_log
        self.secrets = secrets

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        response_started = False
        started = time.perf_counter()
        request_id = next(
            (
                value
                for key, raw in scope.get("headers", [])
                if key.lower() == b"x-request-id"
                for value in [raw.decode("latin-1")]
                if _REQUEST_ID.fullmatch(value)
            ),
            uuid.uuid4().hex,
        )
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_wrapper(message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-request-id" for key, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                detail = error_message(exc, secrets=self.secrets)
                LOG.error(
                    "Unhandled %s %s request",
                    scope["method"],
                    scope.get("path", ""),
                    extra={"error": detail, "traceback": error_traceback(exc, secrets=self.secrets)},
                )
                if response_started:
                    raise
                await JSONResponse({"detail": detail}, status_code=500)(scope, receive, send_wrapper)
        finally:
            try:
                path = scope.get("path", "").rstrip("/")
                if self.access_log and path not in {"/logs", "/api/logs"} and not path.startswith("/api/logs/"):
                    duration_ms = round((time.perf_counter() - started) * 1000, 2)
                    ACCESS_LOG.info(
                        "%s %s completed with HTTP %s in %.2f ms",
                        scope["method"],
                        path,
                        status,
                        duration_ms,
                        extra={
                            "method": scope["method"],
                            "route": path,
                            "status": status,
                            "duration_ms": duration_ms,
                            "request_id": request_id,
                        },
                    )
            finally:
                structlog.contextvars.reset_contextvars(**tokens)


class WebSocketConnection:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_json(self, data: dict) -> None:
        await self._ws.send_json(data)

    async def recv_json(self) -> dict | None:
        try:
            return await self._ws.receive_json()
        except WebSocketDisconnect as exc:
            LOG.info("Node hub WebSocket closed. Code: %s. Reason: %s", exc.code, exc.reason or "No reason provided.")
            return None

    async def close(self, code: int = 1000) -> None:
        # The client may have already completed the closing handshake.
        with contextlib.suppress(Exception):
            await self._ws.close(code=code)


def default_plugin_host(config: ServerConfig) -> PluginHost:
    bundled = (
        _REPO_ROOT / "plugins"
        if (_REPO_ROOT / "pyproject.toml").is_file()
        else Path(sys.prefix) / "share" / "millwright" / "plugins"
    )
    return PluginHost(
        [bundled, config_dir() / "plugins"],
        uploaded_dir=uploaded_plugins_dir(config),
    )


def create_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    hub: NodeHub | None = None,
    scanner: ScannerService | None = None,
    config: ServerConfig | None = None,
    config_path: Path | None = None,
    http_client: httpx.AsyncClient | None = None,
    ffprobe_bin: str = "ffprobe",
    ffmpeg_bin: str = "ffmpeg",
    mkvpropedit_bin: str = "mkvpropedit",
    events: EventBus | None = None,
    plugin_host: PluginHost | None = None,
    playback_root: Path | None = None,
    cache_root: Path | None = None,
    **hub_kwargs,
) -> FastAPI:
    if hub is not None and hub_kwargs:
        msg = "pass either hub or hub_kwargs, not both"
        raise ValueError(msg)
    events = events if events is not None else EventBus()
    config = config if config is not None else ServerConfig()
    operational = OperationalSettings()
    events.timezone = ZoneInfo(config.tz)
    hub_kwargs.setdefault("ffprobe_bin", ffprobe_bin)
    hub_kwargs.setdefault("mkvpropedit_bin", mkvpropedit_bin)
    hub_kwargs.setdefault("workspace_dir", workspace_dir(config))
    configured_trash = config.trash_dir is not None
    hub_kwargs.setdefault("trash_dir", trash_dir(config))
    workspace = hub.workspace_dir if hub is not None else hub_kwargs["workspace_dir"]
    root = hub.trash_dir if hub is not None else hub_kwargs["trash_dir"]
    if root is None:
        root = workspace.resolve().parent / "trash"
    if root.is_symlink():
        raise ValueError("trash directory must not be a symlink")
    if plugin_host is None:
        plugin_host = hub.plugin_host if hub is not None else hub_kwargs.get("plugin_host")
    uploads = plugin_host.uploaded_dir if plugin_host is not None else uploaded_plugins_dir(config)
    with sessionmaker().sync_session as session:
        database_url = session.get_bind().engine.url
    validate_runtime_roots(
        workspace=workspace,
        trash=root,
        uploads=uploads,
        config_path=config_path or config_file(),
    )
    workspace = workspace.expanduser().resolve()
    root = root.expanduser().resolve()
    sessionmaker.configure(info={"library_excluded_roots": (workspace, root, healthcheck_dir().resolve())})
    config = config.model_copy(
        update={
            "workspace_dir": str(workspace),
            "trash_dir": str(root),
            "uploaded_plugins_dir": str(uploads.expanduser().resolve()) if uploads is not None else None,
        }
    )
    hub_kwargs["workspace_dir"] = workspace
    hub_kwargs["trash_dir"] = root
    if hub is None and not configured_trash:
        root.mkdir(parents=True, exist_ok=True)
    hub_kwargs.setdefault("timezone", ZoneInfo(config.tz))
    hub_kwargs.setdefault("min_free_gb", operational.min_free_gb)
    if config.node_enrollment_token is not None:
        hub_kwargs.setdefault("enrollment_token", config.node_enrollment_token)
    hub = hub if hub is not None else NodeHub(sessionmaker, events=events, **hub_kwargs)
    if hub.events is None:
        hub.events = events
    plugin_host = plugin_host if plugin_host is not None else default_plugin_host(config)
    if hub.plugin_host is None:
        hub.plugin_host = plugin_host
    staging = hub.staging
    staging.events = events
    scanner = (
        scanner
        if scanner is not None
        else ScannerService(
            sessionmaker,
            staging=staging,
            scanner=operational.scanner,
            events=events,
        )
    )
    scanner.staging = staging
    scanner.trash_dir = root.resolve()
    store = KeyValueStore(sessionmaker)
    hub.activity_paused = lambda: scanner.paused
    notification_service = NotificationService(sessionmaker)
    workspace_service = WorkspaceService(sessionmaker, workspace_dir(config), staging=staging)
    flow_enqueue = FlowEnqueueService(
        sessionmaker, plugin_host, events=events, hub_wake=hub.wake, staging_wake=staging.wake
    )

    def wake_jobs() -> None:
        flow_enqueue.wake()
        job_service.wake()

    healthchecks = HealthcheckService(
        sessionmaker,
        healthcheck_dir(),
        ffmpeg_bin,
        ffprobe_bin,
        max_concurrency=operational.max_healthchecks,
        hwaccel=operational.healthcheck_hwaccel,
        events=events,
        on_healthy=wake_jobs,
        activity_paused=lambda: scanner.paused,
    )
    flow_enqueue.healthcheck_wake = healthchecks.wake
    probes = ProbeService(
        sessionmaker,
        ffprobe_bin,
        mkvpropedit_bin,
        events=events,
        healthcheck_wake=healthchecks.wake,
        on_healthy=wake_jobs,
        max_concurrency=operational.max_probes,
        max_statistics=operational.max_mkv_stats,
        workspace_dir=workspace_dir(config),
        scanner_paused=lambda: scanner.paused,
        busy_jobs=staging.busy_jobs,
    )
    healthchecks.on_finished = probes.wake
    hub.healthcheck_service = healthchecks
    hub.probe_service = probes

    def wake_local_jobs() -> None:
        probes.wake()
        healthchecks.wake()
        job_service.wake()

    hub.healthcheck_wake = wake_local_jobs
    staging.healthcheck_wake = wake_local_jobs
    previous_scan_callback = scanner.on_scan_complete

    def wake_followups(report) -> None:
        try:
            if previous_scan_callback is not None:
                previous_scan_callback(report)
        finally:
            probes.wake()
            flow_enqueue.wake()

    scanner.on_scan_complete = wake_followups
    scanner.on_files_pending = probes.files_pending

    stack = AuthStack(
        config=config,
        sessionmaker=sessionmaker,
        ws_tickets=WsTickets(),
        pending=PendingStates(),
    )
    dry_runs = BatchDryRuns(sessionmaker, plugin_host, stack.ws_tickets)
    playback_service = PlaybackService(
        operational.playback,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        files_dir=playback_root if playback_root is not None else playback_dir(),
        forbidden_roots=[workspace, root, healthcheck_dir()],
    )
    reset_lock = asyncio.Lock()
    job_service = JobService(
        sessionmaker,
        staging=staging,
        healthchecks=healthchecks,
        probes=probes,
        hub=hub,
        host=plugin_host,
        events=events,
        ffprobe_bin=ffprobe_bin,
        mkvpropedit_bin=mkvpropedit_bin,
        workspace_dir=workspace,
    )
    library_deletion = LibraryDeletionService(
        sessionmaker,
        scanner=scanner,
        probes=probes,
        hub=hub,
        healthchecks=healthchecks,
        staging=staging,
        events=events,
        reset_lock=reset_lock,
        jobs=job_service,
    )
    oidc = config.auth.oidc
    if oidc is not None and is_oidc_ready(config):
        client = http_client if http_client is not None else httpx.AsyncClient()
        stack.oidc = OidcService(oidc, client)
        stack.http_client = client
        stack.owns_client = http_client is None

    cache = JsonCache(cache_root if cache_root is not None else cache_dir())
    stats_service = StatsService(
        sessionmaker, cache, events, address=("stats", database_url.render_as_string(hide_password=True))
    )
    scheduler = Scheduler()
    scheduler.add(stats_service.refresh, every=STATS_INTERVAL_S, name="stats")
    scheduler.add(cache.cleanup, every=3600, name="cache")
    scheduler.add(scanner.check_intervals, every=60, name="scanner intervals")
    scheduler.add(stack.cleanup, every=60, name="auth")
    scheduler.add(workspace_service.cleanup, every=60, name="workspaces")
    scheduler.add(partial(recover_healthcheck_logs, sessionmaker, healthcheck_dir()), every=60, name="healthcheck logs")
    scheduler.add(playback_service.purge, every=60, name="playback")
    scheduler.add(dry_runs.purge, every=60, name="dry runs")

    async def prune_history() -> None:
        await prune_jobs(sessionmaker, settings_service.current.job_retention_days, events=events)

    scheduler.add(prune_history, every=3600, name="history")
    scheduler.add(partial(cleanup_trash, sessionmaker, root), every=3600, name="trash")

    def apply_settings(value: OperationalSettings) -> None:
        healthchecks.configure(
            max_concurrency=value.max_healthchecks,
            hwaccel=value.healthcheck_hwaccel,
            location=value.healthcheck_location,
        )
        probes.configure(
            max_concurrency=value.max_probes,
            max_statistics=value.max_mkv_stats,
            statistics_location=value.mkv_stats_location,
        )
        staging.min_free_gb = value.min_free_gb
        scanner.scanner = value.scanner
        playback_service.configure(value.playback)
        events.healthcheck_slots = value.max_healthchecks if value.healthcheck_location != ExecutionLocation.NODE else 0
        events.healthchecks_on_nodes = value.healthcheck_location != ExecutionLocation.LOCAL
        events.mkv_stats_on_nodes = value.mkv_stats_location != ExecutionLocation.LOCAL
        hub.wake()

    settings_service = SettingsService(store, apply=apply_settings)
    apply_settings(settings_service.current)

    async def migrate_plugins() -> None:
        await migrate_saved_plugins(plugin_host, sessionmaker)

    plugin_host.on_change = migrate_plugins

    @contextlib.asynccontextmanager
    async def service_lifespan(_app: FastAPI):
        started = {"plugin host", "events", "playback", "dry runs", "workspaces"}
        stopping = False
        startup_task: asyncio.Task[None] | None = None

        async def stop_services(*, close_http: bool = True) -> None:
            # Reset waits for service restarts. Block new requests immediately,
            # then let the reset request finish before closing services.
            _app.state.services_running = False
            if "hub" in started:
                hub.begin_shutdown()
            if "library deletion" in started:
                try:
                    await library_deletion.close()
                finally:
                    started.discard("library deletion")
            async with _app.state.reset_lock:
                await drain_services(close_http=close_http)

        async def drain_services(*, close_http: bool) -> None:
            closers = (
                ("scheduler", scheduler.aclose),
                ("jobs", job_service.close),
                ("dry runs", dry_runs.aclose),
                ("scanner", scanner.aclose),
                ("workspaces", workspace_service.aclose),
                ("probes", probes.close),
                ("flow enqueue", flow_enqueue.close),
                ("healthchecks", healthchecks.close),
                ("staging", staging.aclose),
                ("notifications", notification_service.aclose),
                ("playback", playback_service.aclose),
                ("hub", hub.aclose),
                ("plugin host", plugin_host.aclose),
                ("events", events.aclose),
            )
            first_error: BaseException | None = None
            try:
                for name, close in closers:
                    if name not in started:
                        continue
                    try:
                        async with asyncio.timeout(30):
                            await close()
                    except (Exception, asyncio.CancelledError) as exc:
                        if first_error is None:
                            first_error = exc
                        LOG.exception("Server service %s shutdown failed", name)
                    finally:
                        started.discard(name)
            finally:
                _app.state.services_running = False
                if close_http and stack.owns_client and stack.http_client is not None:
                    try:
                        async with asyncio.timeout(5):
                            await stack.http_client.aclose()
                    except Exception as exc:
                        if first_error is None:
                            first_error = exc
                        LOG.exception("Server HTTP client shutdown failed")
                    finally:
                        stack.owns_client = False
            if first_error is not None:
                raise first_error

        async def startup() -> None:
            try:
                LOG.info("Recovering interrupted jobs and publication")
                await migrate_saved_plugins(plugin_host, sessionmaker)
                await plugins.sync_states(plugin_host, sessionmaker)
                async with sessionmaker() as session:
                    await snapshot_interrupted_logs(session, workspace_dir(config))
                    await session.commit()
                await recover_healthcheck_logs(sessionmaker, healthcheck_dir(), startup=True)
                await recover_incomplete(sessionmaker)
                LOG.info("Requeuing interrupted server jobs")
                async with sessionmaker() as session:
                    await requeue_startup(session, datetime.now(UTC))
                    await session.commit()
                await recover_healthcheck_logs(sessionmaker, healthcheck_dir(), startup=True)
                await hub.recover_startup()
                LOG.info("Cleaning workspace files and updating trash records")
                await cleanup_workspace(sessionmaker, workspace_dir(config))
                if root.is_dir() and os.access(root, os.R_OK | os.W_OK):
                    await recover_trash_restores(sessionmaker, root)
                    await refresh_trash_records(sessionmaker, root)
                    await purge_trash(sessionmaker, root)
                else:
                    LOG.error("trash root unavailable during startup: %s", root)
                for name, start in (
                    ("staging", staging.start),
                    ("healthchecks", healthchecks.start),
                    ("hub", hub.start),
                    ("plugin host", plugin_host.start_watching),
                    ("scanner", scanner.start),
                ):
                    started.add(name)
                    start()
                started.add("probes")
                await probes.start()
                started.add("flow enqueue")
                await flow_enqueue.start()
                started.add("jobs")
                await job_service.start()
                for name, start in (
                    ("scheduler", scheduler.start),
                    ("notifications", notification_service.start),
                ):
                    started.add(name)
                    start()
                _app.state.services_running = True
                started.add("library deletion")
                await library_deletion.start()
            except BaseException:
                if not stopping:
                    LOG.exception("Background startup failed. Processing remains paused.")
                await stop_services(close_http=False)
                raise
            _app.state.recovering = False
            LOG.info("Server services ready")

        async def shutdown() -> None:
            nonlocal stopping
            stopping = True
            _app.state.recovering = True
            _app.state.services_running = False
            if startup_task is not None:
                if not startup_task.done():
                    startup_task.cancel()
                try:
                    await startup_task
                except Exception, asyncio.CancelledError:
                    pass
            await stop_services()

        try:
            await settings_service.load()
            paused = await store.get("activity_paused", default=False)
            scanner.set_paused(paused=paused is True)
            if config.disable_jobs:
                LOG.info("Jobs disabled. Skipping startup recovery and background services.")
            else:
                _app.state.recovering = True
                startup_task = asyncio.create_task(startup(), name="server-startup")
                _app.state.startup_task = startup_task
            yield
        finally:
            LOG.info("Stopping server services")
            cleanup = asyncio.create_task(shutdown())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            await cleanup
            if cancelled:
                raise asyncio.CancelledError
            LOG.info("Server services stopped")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        log_state = state_dir()
        handler = install_log_file(log_state)
        _app.state.log_state_dir = log_state
        try:
            LOG.info("Starting server services")
            async with service_lifespan(_app):
                yield
        finally:
            root_logger = logging.getLogger()
            root_logger.removeHandler(handler)
            handler.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Millwright",
        version=version.APP_VERSION,
        docs_url="/api/docs" if config.enable_docs else None,
        swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect" if config.enable_docs else None,
        redoc_url="/api/redoc" if config.enable_docs else None,
        openapi_url="/api/openapi.json" if config.enable_docs else None,
        description=(
            "Manage media libraries, flows, jobs, and nodes. Nodes connect through /api/hub. "
            "Browser clients receive live updates through /api/events."
        ),
        openapi_tags=[
            {"name": "auth", "description": "Account setup, password and OIDC login, and session management."},
            {"name": "events", "description": "WebSocket stream of live job, node, and stats events."},
            {"name": "libraries", "description": "Monitored media library folders and their schedules."},
            {"name": "files", "description": "Media files, probes, and per-file actions."},
            {
                "name": "playback",
                "description": "Browser playback grants, media streaming, and on-demand HLS conversion.",
            },
            {"name": "jobs", "description": "Transcode and healthcheck jobs."},
            {"name": "workspaces", "description": "Job workspace inspection and cleanup."},
            {"name": "nodes", "description": "Transcode node registration and state."},
            {"name": "plugins", "description": "Installed flow plugins and their declared inputs."},
            {"name": "notifications", "description": "Server-managed notification targets."},
            {"name": "webhooks", "description": "Incoming file and folder updates that trigger library scans."},
            {"name": "flows", "description": "Reusable decision flows that map files to transcode plans."},
            {"name": "backups", "description": "Encrypted configuration backup and selective restore."},
            {"name": "settings", "description": "Server settings."},
            {
                "name": "stats",
                "description": "Aggregate statistics, server build info, health, and Prometheus metrics.",
            },
        ],
    )
    app.add_middleware(
        GZipMiddleware, exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/octet-stream")
    )
    secrets = tuple(
        value
        for value in (
            config.api_key,
            config.node_enrollment_token,
            config.auth.oidc.secret if config.auth.oidc is not None else None,
        )
        if value
    )
    app.add_middleware(AccessLogMiddleware, access_log=config.access_log, secrets=secrets)
    app.add_middleware(ResetGuardMiddleware)

    @app.exception_handler(HTTPException)
    async def handled_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code >= 500:
            exc.detail = redact(str(exc.detail), secrets=secrets)
            LOG.error(
                "%s %s returned HTTP %s",
                request.method,
                request.url.path,
                exc.status_code,
                extra={"error": exc.detail},
            )
        return await http_exception_handler(request, exc)

    app.state.auth = stack
    app.state.db = sessionmaker
    app.state.timezone = config.tz
    app.state.healthcheck_dir = healthcheck_dir()
    app.state.config_path = config_path or config_file()
    app.state.store = store
    app.state.settings = settings_service
    app.state.hub = hub
    app.state.plugin_host = plugin_host
    app.state.scheduler = scheduler
    app.state.scanner = scanner
    app.state.notifications = notification_service
    app.state.trash_dir = root
    app.state.staging = staging
    app.state.workspaces = workspace_service
    app.state.flow_enqueue = flow_enqueue
    app.state.healthchecks = healthchecks
    app.state.probes = probes
    app.state.jobs = job_service
    app.state.ffmpeg_bin = ffmpeg_bin
    app.state.ffprobe_bin = ffprobe_bin
    app.state.mkvpropedit_bin = mkvpropedit_bin
    app.state.playback = playback_service
    app.state.dry_runs = dry_runs
    app.state.library_deletion = library_deletion
    app.state.events = events
    app.state.stats = stats_service
    app.state.cache = cache
    app.state.background_image = None
    app.state.reset_lock = reset_lock
    app.state.services_running = False
    app.state.recovering = False
    app.state.startup_task = None
    app.state.shutdown_requested = asyncio.Event()
    app.state.log_state_dir = state_dir()
    app.state.workspace_dir = workspace_dir(config)
    app.include_router(auth_router(stack))
    app.include_router(background.router)
    app.include_router(events_router)
    app.include_router(libraries.router)
    app.include_router(directories.router)
    app.include_router(files.router)
    app.include_router(playback.router)
    app.include_router(jobs.router)
    app.include_router(nodes.router)
    app.include_router(plugins.router)
    app.include_router(notifications.router)
    app.include_router(webhooks.router)
    app.include_router(webhooks.receiver)
    app.include_router(flows.router)
    app.include_router(backups.router)
    app.include_router(settings.router)
    app.include_router(stats.router)
    app.include_router(metrics.router)
    app.include_router(logs.router)
    app.include_router(trash_api.router)
    app.include_router(workspaces.router)

    @app.get("/health", summary="Liveness probe", tags=["stats"])
    async def health() -> dict:
        return {"status": "ok", "version": version.APP_VERSION}

    @app.websocket("/api/hub")
    async def hub_endpoint(ws: WebSocket) -> None:
        if config.disable_jobs or app.state.recovering:
            detail = "Jobs are disabled." if config.disable_jobs else "Server recovery is not ready."
            await ws.send_denial_response(
                JSONResponse({"detail": detail}, status_code=503, headers={"Retry-After": "1"})
            )
            return
        await ws.accept()
        conn: NodeConnection = WebSocketConnection(ws)
        await hub.serve_connection(conn)

    default_ui = (
        _REPO_ROOT / "ui" / "exported"
        if (_REPO_ROOT / "pyproject.toml").is_file()
        else Path(sys.prefix) / "share" / "millwright" / "ui"
    )
    ui_path = Path(os.environ.get("MW_UI_PATH", default_ui)).expanduser()
    docs_path = (
        _REPO_ROOT
        if (_REPO_ROOT / "pyproject.toml").is_file()
        else Path(sys.prefix) / "share" / "millwright" / "documentation"
    )
    app.include_router(documentation_router(docs_path))
    if (ui_path / "index.html").is_file():

        @app.api_route(
            "/api", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False
        )
        @app.api_route(
            "/api/{path:path}",
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            include_in_schema=False,
        )
        async def api_not_found() -> None:
            raise HTTPException(status_code=404)

        app.mount("/assets", StaticFiles(directory=ui_path / "assets"), name="frontend-assets")
        static_files = StaticFiles(directory=ui_path)

        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        async def frontend(path: str, request: Request) -> Response:
            try:
                response = await static_files.get_response(path, request.scope)
            except HTTPException as exc:
                if exc.status_code != 404 or Path(path).suffix:
                    raise
                response = await static_files.get_response("index.html", request.scope)
            if response.headers.get("content-type", "").startswith("text/html"):
                response.headers["cache-control"] = "no-cache"
            return response

    return app
