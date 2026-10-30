from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import signal
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from common.dates import Dates
from fastapi import FastAPI
from uvicorn import Config, Server

import run
from node import cli
from node.agent import AgentConfig
from server import __main__ as server_main
from tests.helpers import create_test_app as create_app
from server.api.backups import ResetBody, reset_backup
from server.core.config import ServerConfig
from server.core.db import create_engine_and_session
from server.scheduler.service import Scheduler
from starlette.requests import Request
from tests.helpers import database_context
from tests.test_run import FakeProc


SERVICES = (
    "dry_runs",
    "scheduler",
    "scanner",
    "workspaces",
    "probes",
    "flow_enqueue",
    "healthchecks",
    "staging",
    "jobs",
    "notifications",
    "hub",
    "plugin_host",
    "events",
)


@pytest.fixture(scope="module", autouse=True)
def collect_resources() -> Iterator[None]:
    yield
    # Surface resource warnings without rescanning the entire heap after every case.
    gc.collect()


@pytest_asyncio.fixture
async def lifecycle_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[FastAPI]:
    (tmp_path / "trash").mkdir()
    config = ServerConfig(workspace_dir=str(tmp_path / "workspace"), trash_dir=str(tmp_path / "trash"))
    async with database_context("lifecycle-safety") as db:
        app = create_app(db, config=config)
        for name in (
            "recover_incomplete",
            "snapshot_interrupted_logs",
            "recover_healthcheck_logs",
            "requeue_startup",
            "cleanup_workspace",
            "recover_trash_restores",
            "refresh_trash_records",
            "purge_trash",
        ):
            monkeypatch.setattr(f"server.api.app.{name}", AsyncMock())
        monkeypatch.setattr("server.api.app.plugins.sync_states", AsyncMock())
        monkeypatch.setattr(app.state.hub, "recover_startup", AsyncMock())
        for name in SERVICES:
            service = getattr(app.state, name)
            if name not in {"dry_runs", "events", "plugin_host", "workspaces"}:
                monkeypatch.setattr(
                    service,
                    "start",
                    AsyncMock() if name in {"probes", "flow_enqueue", "jobs"} else lambda: None,
                )
            close = "close" if name in {"probes", "flow_enqueue", "healthchecks", "jobs"} else "aclose"
            monkeypatch.setattr(service, close, AsyncMock())
        monkeypatch.setattr(app.state.plugin_host, "start_watching", lambda: None)
        monkeypatch.setattr(app.state.library_deletion, "start", AsyncMock())
        monkeypatch.setattr(app.state.library_deletion, "close", AsyncMock())
        yield app


@pytest.mark.asyncio
async def test_shutdown_deadline_cleanup(lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    app = lifecycle_app
    entered, release = asyncio.Event(), asyncio.Event()
    budgets: list[asyncio.Timeout] = []
    timeout = asyncio.timeout

    def controlled_timeout(_delay: float | None) -> asyncio.Timeout:
        budget = timeout(None)
        budgets.append(budget)
        return budget

    async def close_maintenance() -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(app.state.scheduler, "aclose", close_maintenance)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 2)
    await asyncio.wait_for(asyncio.shield(app.state.startup_task), 2)
    monkeypatch.setattr("server.api.app.asyncio.timeout", controlled_timeout)
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        budgets[-1].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(closing, 2)
        for name in SERVICES:
            if name == "scheduler":
                continue
            service = getattr(app.state, name)
            close = service.close if name in {"probes", "flow_enqueue", "healthchecks", "jobs"} else service.aclose
            assert close.await_count == 1, name
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_shutdown_stops_dispatch(lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    app = lifecycle_app
    entered, release, dispatch = (asyncio.Event() for _ in range(3))
    offered = asyncio.Event()

    async def dispatch_once() -> int:
        offered.set()
        return 0

    async def wait_dispatch() -> None:
        await dispatch.wait()
        await app.state.hub.dispatch_once()
        await asyncio.Event().wait()

    async def close_maintenance() -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(app.state.hub, "_dispatch_loop", wait_dispatch)
    monkeypatch.setattr(app.state.hub, "dispatch_once", dispatch_once)
    monkeypatch.setattr(app.state.hub, "_sweep_loop", lambda: asyncio.Event().wait())
    monkeypatch.setattr(app.state.scheduler, "aclose", close_maintenance)
    # Restore the real hub lifetime, but keep dispatch effects isolated.
    monkeypatch.setattr(app.state.hub, "start", type(app.state.hub).start.__get__(app.state.hub))
    monkeypatch.setattr(app.state.hub, "aclose", type(app.state.hub).aclose.__get__(app.state.hub))
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 2)
    await asyncio.wait_for(asyncio.shield(app.state.startup_task), 2)
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        dispatch.set()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert offered.is_set() is False
    finally:
        release.set()
        await asyncio.wait_for(closing, 2)
        await asyncio.wait_for(app.state.hub.aclose(), 2)


@pytest.mark.asyncio
async def test_flow_close_drain(lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    service = lifecycle_app.state.flow_enqueue
    started, stopping, release, completed = (asyncio.Event() for _ in range(4))

    async def blocked_pass() -> int:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()
            completed.set()
        return 0

    monkeypatch.setattr(service, "_pass", blocked_pass)
    monkeypatch.setattr(service, "start", type(service).start.__get__(service))
    monkeypatch.setattr(service, "close", type(service).close.__get__(service))
    first = second = None
    try:
        await service.start()
        await asyncio.wait_for(started.wait(), 2)
        first = asyncio.create_task(service.close())
        await asyncio.wait_for(stopping.wait(), 2)
        second = asyncio.create_task(service.close())
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert second.done() is False
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert completed.is_set()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*(task for task in (first, second) if task is not None)), 2)
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_node_cancellation_drains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started, release, completed = (asyncio.Event() for _ in range(3))
    tasks: list[asyncio.Task] = []

    class BlockedAgent:
        def __init__(self, _config: AgentConfig, *, dates: Dates) -> None:
            pass

        async def run(self) -> None:
            task = asyncio.current_task()
            assert task is not None
            tasks.append(task)
            started.set()
            try:
                await release.wait()
            finally:
                completed.set()

        async def aclose(self) -> None:
            release.set()
            await asyncio.gather(*tasks)

    config = AgentConfig(token="test", server_url="ws://hub", workspace_dir=str(tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli, "probe_capabilities", AsyncMock(return_value=SimpleNamespace(encoders=frozenset())))
    monkeypatch.setattr(cli, "system_facts", AsyncMock())
    monkeypatch.setattr(cli, "verify_encoders", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(cli, "_build_capabilities", lambda *_args: config.capabilities)
    monkeypatch.setattr(cli, "Agent", BlockedAgent)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    main = asyncio.create_task(cli._amain(argparse.Namespace(config=tmp_path / "node.toml"), Dates("UTC")))
    try:
        await asyncio.wait_for(started.wait(), 2)
        main.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(main, 2)
        assert completed.is_set()
    finally:
        release.set()
        if not main.done():
            main.cancel()
        await asyncio.wait_for(asyncio.gather(main, *tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_serve_drains_lifespan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url) -> None:
    ready, closing, release, disposed = (asyncio.Event() for _ in range(4))
    live = False
    disposed_live: list[bool] = []
    servers: list[Server] = []
    lifespan_tasks: list[asyncio.Task] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal live
        task = asyncio.current_task()
        assert task is not None
        lifespan_tasks.append(task)
        live = True
        try:
            yield
        finally:
            closing.set()
            await release.wait()
            live = False

    class NoSocketServer(Server):
        def __init__(self, config: Config) -> None:
            super().__init__(config)
            servers.append(self)

        async def startup(self, sockets=None) -> None:
            await self.lifespan.startup()
            self.started = True

        async def main_loop(self) -> None:
            ready.set()
            await asyncio.Event().wait()

        async def shutdown(self, sockets=None) -> None:
            await self.lifespan.shutdown()

    async def dispose(_engine) -> None:
        disposed_live.append(live)
        disposed.set()

    monkeypatch.setattr(server_main.uvicorn, "Server", NoSocketServer)
    engine, _ = create_engine_and_session(database_url)
    original_dispose = engine.dispose
    monkeypatch.setattr(type(engine), "dispose", dispose)
    serving = asyncio.create_task(server_main._serve(FastAPI(lifespan=lifespan), engine, host="127.0.0.1", port=8000))
    observers: list[asyncio.Task] = []
    try:
        await asyncio.wait_for(ready.wait(), 2)
        serving.cancel()
        observers = [asyncio.create_task(closing.wait()), asyncio.create_task(disposed.wait())]
        await asyncio.wait_for(asyncio.wait(observers, return_when=asyncio.FIRST_COMPLETED), 2)
        assert disposed_live != [True]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 2)
        assert disposed_live == [False]
    finally:
        release.set()
        if lifespan_tasks and not lifespan_tasks[0].done() and not closing.is_set():
            await asyncio.wait_for(servers[0].lifespan.shutdown(), 2)
        if not serving.done():
            serving.cancel()
        for task in observers:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(serving, *lifespan_tasks, *observers, return_exceptions=True), 2)
        await asyncio.wait_for(original_dispose(), 2)


@pytest.mark.asyncio
async def test_maintenance_restart_check() -> None:
    started, stopping, release = (asyncio.Event() for _ in range(3))
    runners: list[asyncio.Task] = []

    async def sleep(_delay: float) -> None:
        task = asyncio.current_task()
        assert task is not None
        runners.append(task)
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()

    async def callback() -> None:
        started.set()
        await release.wait()

    scheduler = Scheduler(sleep=sleep)
    scheduler.add(callback)
    scheduler.start()
    closing = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        closing = asyncio.create_task(scheduler.aclose())
        await asyncio.wait_for(stopping.wait(), 2)
        scheduler.start()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        release.set()
        await asyncio.wait_for(closing, 2)
        unexpected = [task for task in runners if not task.done() and task is not scheduler._task]
        assert unexpected == []
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 2)
        for task in runners:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*runners, return_exceptions=True), 2)
        await asyncio.wait_for(scheduler.aclose(), 2)


@pytest.mark.asyncio
async def test_workspace_close_drains(lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = lifecycle_app.state.workspaces
    started, release, completed = (asyncio.Event() for _ in range(3))
    original_wait = asyncio.wait_for

    async def expired_wait(future, timeout):
        if timeout == 5 and isinstance(future, asyncio.Future):
            future.cancel()
            raise TimeoutError
        return await original_wait(future, timeout)

    async def run_scan() -> None:
        with (tmp_path / "scan-input").open("rb"):
            started.set()
            await release.wait()
        completed.set()

    (tmp_path / "scan-input").write_bytes(b"scanner input")
    worker = asyncio.create_task(run_scan())
    service._scan_task = worker
    monkeypatch.setattr(service, "aclose", type(service).aclose.__get__(service))
    monkeypatch.setattr("server.workspaces.asyncio.wait_for", expired_wait)
    closing = None
    try:
        await original_wait(started.wait(), 2)
        closing = asyncio.create_task(service.aclose())
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await original_wait(checkpoint.wait(), 2)
        assert closing.done() is False
        assert completed.is_set() is False
        release.set()
        await original_wait(closing, 2)
        assert completed.is_set()
    finally:
        release.set()
        try:
            await original_wait(
                asyncio.gather(worker, *(task for task in (closing,) if task is not None), return_exceptions=True), 2
            )
        finally:
            monkeypatch.setattr("server.workspaces.asyncio.wait_for", original_wait)


@pytest.mark.asyncio
async def test_pause_cancellation_releases(lifecycle_app: FastAPI) -> None:
    hub = lifecycle_app.state.hub
    entered = asyncio.Event()

    async def paused() -> None:
        async with hub.pause_for_reset():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(paused())
    held = False
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await hub.dispatch_lock.acquire()
        held = True
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done()
        task.cancel()
        hub.dispatch_lock.release()
        held = False
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert hub._reset_active is False
        assert hub._reset_done.is_set()
    finally:
        if held:
            hub.dispatch_lock.release()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        hub._reset_active = False
        hub._reset_done.set()


def test_launcher_stop_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ExitedServer(FakeProc):
        def send_signal(self, sig: int) -> None:
            self.returncode = 0
            raise ProcessLookupError("child exited during signal delivery")

    class Node(FakeProc):
        waited = False

        def send_signal(self, sig: int) -> None:
            self.signals.append(sig)

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            self.returncode = 0
            return super().wait(timeout)

    server, node = ExitedServer("server"), Node("node")
    children = iter((server, node))
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    (tmp_path / "config.toml").write_text('api_key = "test-key"\n')
    monkeypatch.setattr(run, "resolve_bin", lambda name: name)
    monkeypatch.setattr(run.subprocess, "Popen", lambda *_args, **_kwargs: next(children))
    monkeypatch.setattr(run, "wait_healthy", lambda _port, *, stopped=None: True)
    monkeypatch.setattr(run, "supervise", lambda *_args, **_kwargs: 0)
    try:
        with contextlib.suppress(ProcessLookupError):
            run.run_all()
        assert node.signals == [signal.SIGTERM]
        assert node.waited
    finally:
        # Fake children must also finish when the regression fails.
        node.terminate()
        node.wait()


def test_launcher_startup_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handlers = []
    server = FakeProc("server")
    spawned: list[str] = []

    def spawn(argv, **_kwargs):
        spawned.append(argv[0])
        assert len(spawned) == 1
        return server

    def wait_healthy(_port: int, *, stopped=None) -> bool:
        assert handlers, "SIGTERM must be handled during the startup health wait"
        handlers[0](signal.SIGTERM, None)
        assert stopped is not None and stopped()
        return False

    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(run, "resolve_bin", lambda name: name)
    monkeypatch.setattr(run.subprocess, "Popen", spawn)
    monkeypatch.setattr(run, "_install_signals", handlers.append)
    monkeypatch.setattr(run, "wait_healthy", wait_healthy)
    try:
        assert run.run_all() == 1
        assert spawned == [run.SERVER_BIN]
        assert server.signals == [signal.SIGTERM]
        assert server.poll() is not None
    finally:
        server.terminate()
        server.wait()


@pytest.mark.asyncio
async def test_reset_restart_checked(lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    app = lifecycle_app
    app.state.services_running = True
    monkeypatch.setattr("server.api.backups.reset_data", AsyncMock())

    async def restart_probes() -> None:
        assert app.state.hub._reset_active
        assert not app.state.hub._reset_done.is_set()

    monkeypatch.setattr(app.state.probes, "start", restart_probes)
    request = Request({"type": "http", "app": app})
    try:
        await asyncio.wait_for(
            reset_backup(ResetBody(keep=["nodes", "libraries", "flows", "notifications", "plugins"]), request), 2
        )
        assert not app.state.hub._reset_active
        assert app.state.hub._reset_done.is_set()
    finally:
        app.state.services_running = False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError], ids=["error", "cancel"])
@pytest.mark.parametrize("failed", [name for name in SERVICES if name not in {"dry_runs", "events", "workspaces"}])
async def test_startup_handoff_rollback(
    lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch, failed: str, failure: type[BaseException]
) -> None:
    app = lifecycle_app
    acquired: list[str] = []
    for name in SERVICES:
        if name in {"dry_runs", "events", "workspaces"}:
            continue
        service = getattr(app.state, name)

        def acquire(name: str = name) -> None:
            acquired.append(name)
            if name == failed:
                raise failure("injected startup failure")

        if name in {"probes", "flow_enqueue", "jobs"}:

            async def start(name: str = name) -> None:
                acquired.append(name)
                if name == failed:
                    raise failure("injected startup failure")

            monkeypatch.setattr(service, "start", start)
        else:
            monkeypatch.setattr(service, "start_watching" if name == "plugin_host" else "start", acquire)
    # Bound the lifecycle without imposing a one-second startup latency requirement.
    async with asyncio.timeout(10), app.router.lifespan_context(app):
        assert isinstance(app.state.startup_task, asyncio.Task)
        with pytest.raises(failure):
            await asyncio.shield(app.state.startup_task)
        assert acquired[-1] == failed
        assert len(acquired) == len(set(acquired))
        assert not app.state.services_running
        for name in SERVICES:
            service = getattr(app.state, name)
            close = service.close if name in {"probes", "flow_enqueue", "healthchecks", "jobs"} else service.aclose
            assert close.await_count == int(
                name in acquired or name in {"dry_runs", "events", "plugin_host", "workspaces"}
            ), name
    for name in SERVICES:
        service = getattr(app.state, name)
        close = service.close if name in {"probes", "flow_enqueue", "healthchecks", "jobs"} else service.aclose
        assert close.await_count == int(
            name in acquired or name in {"dry_runs", "events", "plugin_host", "workspaces"}
        ), name
    assert not app.state.services_running


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_shutdown", [False, True], ids=["normal", "cancelled"])
async def test_startup_shutdown_drains(
    lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch, cancel_shutdown: bool
) -> None:
    app = lifecycle_app
    entered, stopping, release, completed = (asyncio.Event() for _ in range(4))

    async def start_probes() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()
            completed.set()

    monkeypatch.setattr(app.state.probes, "start", start_probes)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 2)
    startup = app.state.startup_task
    closing = None
    try:
        assert isinstance(startup, asyncio.Task)
        await asyncio.wait_for(entered.wait(), 2)
        assert not startup.done()
        assert not app.state.services_running
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.wait_for(stopping.wait(), 2)
        assert not closing.done()
        app.state.probes.close.assert_not_awaited()
        if cancel_shutdown:
            closing.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not closing.done()
            assert not completed.is_set()
        release.set()
        if cancel_shutdown:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 2)
        else:
            await asyncio.wait_for(closing, 2)
        assert completed.is_set()
        assert startup.cancelled()
        assert not app.state.services_running
        app.state.probes.close.assert_awaited_once()
    finally:
        release.set()
        if closing is None:
            closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.wait_for(asyncio.gather(closing, startup, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError], ids=["error", "cancel"])
async def test_plugin_close_continues(
    lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    host = lifecycle_app.state.plugin_host
    states = list(host._plugins.values())[:2]
    assert len(states) == 2
    host._plugins = {state.manifest.id: state for state in states}
    releases = [asyncio.Event(), asyncio.Event()]
    completed = [asyncio.Event(), asyncio.Event()]

    async def runner(index: int) -> None:
        try:
            await releases[index].wait()
        finally:
            completed[index].set()

    workers = [asyncio.create_task(runner(index)) for index in range(2)]

    async def stop_runner(state) -> None:
        if state is states[0]:
            if failure is asyncio.CancelledError:
                releases[0].set()
                await workers[0]
            raise failure("first runner exit unconfirmed")
        releases[1].set()
        await workers[1]

    monkeypatch.setattr(host, "_stop_runner", stop_runner)
    monkeypatch.setattr(host, "aclose", type(host).aclose.__get__(host))
    try:
        with pytest.raises(failure):
            await asyncio.wait_for(host.aclose(), 2)
        assert completed[1].is_set()
    finally:
        for release in releases:
            release.set()
        await asyncio.wait_for(asyncio.gather(*workers), 2)


@pytest.mark.asyncio
async def test_reset_restart_stops(lifecycle_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    app = lifecycle_app
    monkeypatch.setattr("server.api.backups.reset_data", AsyncMock())
    restarting, release, services_closed = (asyncio.Event() for _ in range(3))
    workers: dict[str, list[asyncio.Task]] = {name: [] for name in ("flow_enqueue", "scheduler", "notifications")}

    async def restart_probes() -> None:
        assert app.state.hub._reset_active
        restarting.set()
        await release.wait()

    async def worker() -> None:
        await asyncio.Event().wait()

    for name in workers:

        async def close(name: str = name) -> None:
            for task in workers[name]:
                task.cancel()
            if workers[name]:
                await asyncio.gather(*workers[name], return_exceptions=True)

        service = getattr(app.state, name)
        monkeypatch.setattr(service, "close" if name == "flow_enqueue" else "aclose", close)

    async def close_events() -> None:
        services_closed.set()

    monkeypatch.setattr(app.state.events, "aclose", close_events)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 2)
    await asyncio.wait_for(asyncio.shield(app.state.startup_task), 2)
    monkeypatch.setattr(app.state.probes, "start", restart_probes)

    async def restart_flow() -> None:
        workers["flow_enqueue"].append(asyncio.create_task(worker()))

    monkeypatch.setattr(app.state.flow_enqueue, "start", restart_flow)
    for name in ("scheduler", "notifications"):
        monkeypatch.setattr(
            getattr(app.state, name), "start", lambda name=name: workers[name].append(asyncio.create_task(worker()))
        )
    request = Request({"type": "http", "app": app})
    resetting = asyncio.create_task(
        reset_backup(ResetBody(keep=["nodes", "libraries", "flows", "notifications", "plugins"]), request)
    )
    closing = None
    try:
        await asyncio.wait_for(restarting.wait(), 2)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        # All unblocked fake closers run without yielding. A lifecycle check may instead
        # hold shutdown here until this request's awaited restart finishes.
        if services_closed.is_set():
            await asyncio.wait_for(closing, 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(resetting, closing), 2)
        pending = [name for name, tasks in workers.items() if any(not task.done() for task in tasks)]
        assert pending == []
        assert app.state.services_running is False
        assert not app.state.hub._reset_active
        assert app.state.hub._reset_done.is_set()
    finally:
        release.set()
        if closing is None:
            closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.wait_for(asyncio.gather(resetting, closing, return_exceptions=True), 2)
        for tasks in workers.values():
            for task in tasks:
                if not task.done():
                    task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*(task for tasks in workers.values() for task in tasks), return_exceptions=True), 2
        )
        app.state.services_running = False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "forced"])
async def test_serve_waits_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, database_url) -> None:
    ready, release, draining, completed = (asyncio.Event() for _ in range(4))
    live = False
    drivers: list[asyncio.Task] = []
    tails: list[asyncio.Task] = []
    dispositions: list[bool] = []
    input_path = tmp_path / "peer-input"
    input_path.write_bytes(b"fake network peer")
    listener = input_path.open("rb")

    class Listener:
        waited = False

        def close(self) -> None:
            listener.close()

        async def wait_closed(self) -> None:
            assert listener.closed
            self.waited = True

    worker_listener = Listener()

    class Peer:
        shutdown_requested = False

        def shutdown(self) -> None:
            self.shutdown_requested = True
            draining.set()

    peer = Peer()

    async def tail() -> None:
        with input_path.open("rb"):
            ready.set()
            await release.wait()
        completed.set()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal live
        task = asyncio.current_task()
        assert task is not None
        drivers.append(task)
        live = True
        try:
            yield
        finally:
            assert completed.is_set(), "the ASGI tail must finish before its application resources"
            live = False

    class NoSocketServer(Server):
        async def startup(self, sockets=None) -> None:
            await self.lifespan.startup()
            monkeypatch.setattr(self, "servers", [worker_listener])
            monkeypatch.setattr(self.server_state, "connections", {peer})
            task = asyncio.create_task(tail())
            tails.append(task)
            self.server_state.tasks.add(task)
            task.add_done_callback(self.server_state.tasks.discard)
            await ready.wait()
            self.started = True

        async def main_loop(self) -> None:
            if mode == "failure":
                raise RuntimeError("serve failed")
            self.force_exit = True

        async def shutdown(self, sockets=None) -> None:
            if mode == "failure":
                raise RuntimeError("network shutdown failed")
            assert self.force_exit

    engine, _ = create_engine_and_session(database_url)
    original_dispose = engine.dispose

    async def dispose(_engine) -> None:
        dispositions.append(not live and completed.is_set() and listener.closed and worker_listener.waited)
        await original_dispose()

    monkeypatch.setattr(server_main.uvicorn, "Server", NoSocketServer)
    monkeypatch.setattr(type(engine), "dispose", dispose)
    serving = asyncio.create_task(server_main._serve(FastAPI(lifespan=lifespan), engine, host="127.0.0.1", port=8000))
    try:
        await asyncio.wait_for(draining.wait(), 2)
        assert peer.shutdown_requested and listener.closed
        assert not serving.done() and dispositions == [] and live
        release.set()
        if mode == "failure":
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(serving, 2)
        else:
            await asyncio.wait_for(serving, 2)
        assert dispositions == [True]
        assert all(task.done() for task in (*tails, *drivers))
    finally:
        release.set()
        listener.close()
        await asyncio.wait_for(asyncio.gather(serving, *tails, *drivers, return_exceptions=True), 2)
        await asyncio.wait_for(original_dispose(), 2)
