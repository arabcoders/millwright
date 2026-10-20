import asyncio
from contextlib import asynccontextmanager
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from server import __main__
from server.core.db import create_engine_and_session
from server.api.logs import stream_logs
from tests.test_logs_stream import log_file, request


@pytest_asyncio.fixture
async def engine_fixture(database_url) -> AsyncIterator[tuple[AsyncEngine, Any]]:
    value, _ = create_engine_and_session(database_url)
    try:
        yield value, _
    finally:
        await asyncio.wait_for(value.dispose(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["shutdown", "cancel"])
async def test_shutdown_finishes_logs(
    monkeypatch: pytest.MonkeyPatch, engine_fixture, tmp_path: Path, cancel: bool
) -> None:
    engine, _factory = engine_fixture
    reading, finished, stop, stopped = (asyncio.Event() for _ in range(4))
    log_file(tmp_path, [1])

    @asynccontextmanager
    async def lifespan(_app):
        yield
        assert finished.is_set()
        stopped.set()

    app = FastAPI(lifespan=lifespan)
    req = request(tmp_path)
    app.state.auth = req.app.state.auth
    app.state.log_state_dir = tmp_path
    req.scope["app"] = app
    reader = None

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            if message.get("more_body", False):
                reading.set()
            else:
                finished.set()

    async def serve(server):
        nonlocal reader
        server.lifespan = server.config.lifespan_class(server.config)
        await server.lifespan.startup()
        response = await stream_logs(req)
        reader = asyncio.create_task(response({**req.scope, "asgi": {"spec_version": "2.4"}}, receive, send))
        server.server_state.tasks.add(reader)
        reader.add_done_callback(server.server_state.tasks.discard)
        await stop.wait()
        await server.shutdown()

    async def drain(server):
        assert app.state.shutdown_requested.is_set()
        await asyncio.gather(*tuple(server.server_state.tasks))

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    monkeypatch.setattr(uvicorn.Server, "_wait_tasks_to_complete", drain)
    task = asyncio.create_task(__main__._serve(app, engine, host="127.0.0.1", port=8000))
    try:
        await asyncio.wait_for(reading.wait(), 2)
        assert reader is not None and not reader.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            stop.set()
            await asyncio.wait_for(task, 3)
        assert reader.done() and finished.is_set() and stopped.is_set()
    finally:
        stop.set()
        task.cancel()
        if reader is not None:
            reader.cancel()
        await asyncio.wait_for(
            asyncio.gather(task, *([reader] if reader is not None else []), return_exceptions=True), 3
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["shutdown", "cancel-cleanup"])
async def test_shutdown_drains_workers(
    monkeypatch: pytest.MonkeyPatch, engine_fixture, tmp_path: Path, cancel: bool
) -> None:
    engine, _factory = engine_fixture
    reading, finished, worker_started, draining, release, completed = (asyncio.Event() for _ in range(6))
    workers = []
    readers = []
    servers = []
    disposed = []
    log_file(tmp_path, [1])

    async def worker():
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            completed.set()

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(worker())
        workers.append(task)
        try:
            await worker_started.wait()
            yield
        finally:
            assert finished.is_set(), "Request streams must finish before worker cleanup."
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)
    req = request(tmp_path)
    app.state.auth = req.app.state.auth
    app.state.log_state_dir = tmp_path
    req.scope["app"] = app

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            (reading if message.get("more_body", False) else finished).set()

    async def serve(server):
        servers.append(server)
        server.lifespan = server.config.lifespan_class(server.config)
        await server.lifespan.startup()
        response = await stream_logs(req)
        reader = asyncio.create_task(response({**req.scope, "asgi": {"spec_version": "2.4"}}, receive, send))
        readers.append(reader)
        server.server_state.tasks.add(reader)
        reader.add_done_callback(server.server_state.tasks.discard)
        await app.state.shutdown_requested.wait()
        assert server.should_exit and not server.force_exit
        await server.shutdown()

    async def drain(server):
        assert app.state.shutdown_requested.is_set()
        await asyncio.gather(*tuple(server.server_state.tasks))

    original_dispose = engine.dispose

    async def dispose(_engine):
        assert finished.is_set() and completed.is_set()
        assert all(task.done() for task in (*readers, *workers))
        await original_dispose()
        disposed.append(True)

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    monkeypatch.setattr(uvicorn.Server, "_wait_tasks_to_complete", drain)
    monkeypatch.setattr(type(engine), "dispose", dispose)
    serving = asyncio.create_task(__main__._serve(app, engine, host="127.0.0.1", port=8000))
    try:
        await asyncio.wait_for(reading.wait(), 2)
        servers[0].should_exit = True
        app.state.shutdown_requested.set()
        await asyncio.wait_for(draining.wait(), 2)
        assert finished.is_set() and readers[0].done()
        assert not serving.done() and disposed == []
        if cancel:
            serving.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            serving.cancel()
            assert not serving.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 3)
        else:
            await asyncio.wait_for(serving, 3)
        assert disposed == [True] and completed.is_set()
        assert servers[0].lifespan.task.done()
    finally:
        release.set()
        if not serving.done():
            serving.cancel()
        for task in (*readers, *workers):
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(serving, *readers, *workers, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_disposes_on_failure(monkeypatch: pytest.MonkeyPatch, engine_fixture: tuple[AsyncEngine, Any]) -> None:
    engine, _ = engine_fixture
    connections = []

    class Server:
        def __init__(self, _config: uvicorn.Config) -> None:
            pass

        async def serve(self) -> None:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                raw = await connection.get_raw_connection()
                driver = raw.driver_connection
                assert driver is not None
                connections.append(driver)
            assert not connections[0].closed
            raise RuntimeError("startup failed")

    monkeypatch.setattr(__main__.uvicorn, "Server", Server)
    with pytest.raises(RuntimeError):
        await __main__._serve(FastAPI(), engine, host="127.0.0.1", port=8000)

    assert len(connections) == 1 and connections[0].closed


@pytest.mark.asyncio
async def test_failed_start_cleans(monkeypatch: pytest.MonkeyPatch, engine_fixture) -> None:
    engine, _ = engine_fixture
    lifespans = []

    @asynccontextmanager
    async def lifespan(_app):
        raise RuntimeError("Startup failed before serving.")
        yield

    async def serve(server):
        server.lifespan = server.config.lifespan_class(server.config)
        lifespans.append(server.lifespan)
        await server.lifespan.startup()
        assert server.lifespan.startup_failed

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    await __main__._serve(FastAPI(lifespan=lifespan), engine, host="127.0.0.1", port=8000)
    assert lifespans[0].task.done()


@pytest.mark.asyncio
async def test_cancellation_disposes_engine(
    monkeypatch: pytest.MonkeyPatch, engine_fixture: tuple[AsyncEngine, Any]
) -> None:
    engine, sessionmaker = engine_fixture
    started = asyncio.Event()
    stop = asyncio.Event()
    disposed = AsyncMock(wraps=engine.dispose)
    monkeypatch.setattr(type(engine), "dispose", disposed)

    class Server:
        def __init__(self, _config: uvicorn.Config) -> None:
            pass

        async def serve(self) -> None:
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
                started.set()
                await asyncio.wait_for(stop.wait(), timeout=1)

    monkeypatch.setattr(__main__.uvicorn, "Server", Server)
    task = asyncio.create_task(__main__._serve(FastAPI(), engine, host="127.0.0.1", port=8000))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        disposed.assert_awaited_once()
        pool = engine.sync_engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)
        assert pool.checkedout() == 0
    finally:
        stop.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_cancel_during_dispose(monkeypatch: pytest.MonkeyPatch, engine_fixture: tuple[AsyncEngine, Any]) -> None:
    engine, _ = engine_fixture
    dispose_started = asyncio.Event()
    release_dispose = asyncio.Event()
    dispose_done = asyncio.Event()
    original_dispose = engine.dispose

    async def dispose(_engine: AsyncEngine) -> None:
        dispose_started.set()
        await asyncio.wait_for(release_dispose.wait(), timeout=1)
        await original_dispose()
        dispose_done.set()

    monkeypatch.setattr(type(engine), "dispose", dispose)

    class Server:
        def __init__(self, _config: uvicorn.Config) -> None:
            pass

        async def serve(self) -> None:
            return

    monkeypatch.setattr(__main__.uvicorn, "Server", Server)
    task = asyncio.create_task(__main__._serve(FastAPI(), engine, host="127.0.0.1", port=8000))
    try:
        await asyncio.wait_for(dispose_started.wait(), timeout=1)
        task.cancel()
        release_dispose.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert dispose_done.is_set()
    finally:
        release_dispose.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_dev_server_detaches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | dict[str, Any]] = []

    monkeypatch.setattr(__main__.os, "setsid", lambda: calls.append("setsid"))
    monkeypatch.setattr(__main__, "start", lambda **kwargs: calls.append(kwargs))
    __main__._run_dev_server(None, None, "127.0.0.1", 8000, True)

    assert calls == [
        "setsid",
        {"config_path": None, "database_url": None, "host": "127.0.0.1", "port": 8000, "no_auto_migration": True},
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX crash signals only")
def test_server_crash_trace(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import resource
import signal
from pathlib import Path
from server import __main__ as server
from server.core.config import ServerConfig

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
server.ensure_config = lambda path: None
server.load_config = lambda path: ServerConfig()

def crash(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGSEGV)

server.create_engine_and_session = crash
server.start(config_path=Path("config.toml"), database_url="postgresql://localhost/test")
""",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == -signal.SIGSEGV
    assert "Fatal Python error: Segmentation fault" in result.stderr
    assert "in crash" in result.stderr
    assert "server/__main__.py" in result.stderr


@pytest.mark.parametrize("mode", ["stop", "idle", "reload", "error"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_dev_stop_reaps(monkeypatch: pytest.MonkeyPatch, mode: str, signum: signal.Signals) -> None:
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    handlers = original_handlers.copy()
    children = []
    forwarded = []

    class Child:
        def __init__(self, **_kwargs):
            self.alive = False
            self.closed = False
            self.started = False
            children.append(self)

        @property
        def pid(self):
            if self.closed:
                raise ValueError("Process is closed.")
            return os.getpid() if self.started else None

        def start(self):
            self.started = True
            self.alive = True

        def is_alive(self):
            return self.alive

        @property
        def exitcode(self):
            return None if self.alive else 0

        def join(self):
            for request in (signum, signal.SIGINT, signal.SIGTERM):
                handler = handlers[request]
                assert callable(handler)
                handler(request, None)
                assert self.alive and not self.closed
            self.alive = False

        def close(self):
            assert not self.alive
            self.closed = True

    def watch(*_args, **kwargs):
        if mode == "error":
            raise RuntimeError("Watcher failed.")
        if mode == "reload":
            yield {(2, str(__main__.REPO_ROOT / "server" / "src" / "server" / "__main__.py"))}
        else:
            if mode == "idle":
                yield set()
                assert len(children) == 1 and children[0].alive and not children[0].closed
            handler = handlers[signum]
            assert callable(handler)
            handler(signum, None)
            assert kwargs["stop_event"].is_set()

    monkeypatch.setattr("multiprocessing.get_context", lambda _method: SimpleNamespace(Process=Child))
    monkeypatch.setattr("watchfiles.watch", watch)
    monkeypatch.setattr(signal, "getsignal", lambda sig: handlers[sig])
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(__main__.os, "kill", lambda _pid, sig: forwarded.append(sig))
    monkeypatch.setattr(__main__, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(__main__, "ensure_config", lambda _path: None)
    monkeypatch.setattr(__main__, "load_config", lambda _path: SimpleNamespace(tz="Asia/Kuwait"))
    monkeypatch.setattr(__main__.faulthandler, "enable", lambda **_kwargs: None)

    if mode == "error":
        with pytest.raises(RuntimeError, match="Watcher failed"):
            __main__.run_dev(config_path=None, database_url=None, host="127.0.0.1", port=8000)
    else:
        __main__.run_dev(config_path=None, database_url=None, host="127.0.0.1", port=8000)

    assert children and all(child.closed and not child.alive for child in children)
    assert len(children) == 1
    assert forwarded == [signal.SIGINT]
    assert handlers == original_handlers


@pytest.mark.parametrize("exitcode, expected", [(-signal.SIGSEGV, 139), (-signal.SIGABRT, 134), (1, 1), (0, 1)])
def test_dev_reports_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exitcode, expected
) -> None:
    children = []
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    class Child:
        pid = 12345

        def __init__(self, **_kwargs):
            self.exitcode = None
            self.joined = False
            self.closed = False
            children.append(self)

        def start(self):
            self.exitcode = exitcode

        def is_alive(self):
            return False

        def join(self):
            self.joined = True

        def close(self):
            assert self.joined
            self.closed = True

    def watch(*_args, **kwargs):
        assert kwargs["yield_on_timeout"] is True
        assert kwargs["rust_timeout"] == 1000
        yield set()
        pytest.fail("Dead server left the watcher running.")

    def kill(*_args):
        pytest.fail("Dead server received a shutdown signal.")

    monkeypatch.setattr("multiprocessing.get_context", lambda _method: SimpleNamespace(Process=Child))
    monkeypatch.setattr("watchfiles.watch", watch)
    monkeypatch.setattr(__main__.os, "kill", kill)
    monkeypatch.setattr(__main__, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(__main__, "ensure_config", lambda _path: None)
    monkeypatch.setattr(__main__, "load_config", lambda _path: SimpleNamespace(tz="Asia/Kuwait"))
    monkeypatch.setattr(__main__.faulthandler, "enable", lambda **_kwargs: None)

    with pytest.raises(SystemExit) as error:
        __main__.run_dev(config_path=None, database_url=None, host="127.0.0.1", port=8000)

    assert error.value.code == expected
    assert len(children) == 1 and children[0].joined and children[0].closed
    records = [record for record in caplog.records if record.name == "server"]
    assert records and all(record.levelno == logging.ERROR for record in records)
    assert {sig: signal.getsignal(sig) for sig in original_handlers} == original_handlers
