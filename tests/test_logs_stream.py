from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from server.api.logs import stream_logs
from server.core import log_history
from server.core.config import ServerConfig


@pytest.mark.asyncio
async def test_logout_closes_stream(tmp_path: Path) -> None:
    from server.api.auth import AuthStack, auth_router, current_principal
    from server.api.logs import router
    from server.core.auth import PendingStates, WsTickets
    from server.core.config import ServerConfig
    from tests.helpers import database_context

    file = log_file(tmp_path, [1])
    async with database_context("log-revocation", tables=("users", "sessions")) as factory:
        async with factory() as session:
            _, token = await create_user_session(session, "operator", 1)
            await session.commit()
        app = FastAPI()
        stack = AuthStack(
            config=ServerConfig(api_key="test-key"),
            sessionmaker=factory,
            ws_tickets=WsTickets(),
            pending=PendingStates(),
        )
        app.state.auth = stack
        app.state.shutdown_requested = asyncio.Event()
        app.state.log_state_dir = tmp_path
        app.include_router(auth_router(stack))
        app.include_router(router)
        req = request(tmp_path, [(b"cookie", f"mw_session={token}".encode())])
        req.scope["app"] = app
        assert (await current_principal(req)).session_hash is not None
        response = await stream_logs(req)
        body = response.body_iterator
        assert isinstance(body, AsyncGenerator)
        async with aclosing(body):
            assert (await next_event(body)).startswith("id: 1\n")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"},
            ) as client:
                assert (await client.post("/api/auth/logout")).status_code == 204
                assert (await client.get("/api/auth/me")).status_code == 401
            with file.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"id": 2, "logger": "server.test", "message": "after logout", "level": "info"}) + "\n"
                )
            event = None
            try:
                event = await next_event(body)
            except StopAsyncIteration:
                pass
            assert event is None, "a revoked session still receives new server logs"


@pytest.mark.asyncio
async def test_expiry_closes_stream(tmp_path: Path, monkeypatch) -> None:
    from server.api.auth import AuthStack
    from server.core.auth import PendingStates, WsTickets
    from server.models import utcnow
    from tests.helpers import database_context

    now = utcnow()
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now)
    log_file(tmp_path, [1])
    async with database_context("log-expiry", tables=("users", "sessions")) as factory:
        async with factory() as session:
            row, token = await create_user_session(session, "operator", 1)
            await session.commit()
        app = FastAPI()
        app.state.auth = AuthStack(
            config=ServerConfig(api_key="test-key"),
            sessionmaker=factory,
            ws_tickets=WsTickets(),
            pending=PendingStates(),
        )
        app.state.shutdown_requested = asyncio.Event()
        app.state.log_state_dir = tmp_path
        req = request(tmp_path, [(b"cookie", f"mw_session={token}".encode())])
        req.scope["app"] = app
        response = await stream_logs(req)
        body = response.body_iterator
        assert isinstance(body, AsyncGenerator)
        async with aclosing(body):
            assert (await next_event(body)).startswith("id: 1\n")
            now = row.expires_at + timedelta(seconds=1)
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(body), timeout=5)


def request(path: Path, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    headers = headers or []
    if not any(key in {b"authorization", b"cookie"} for key, _ in headers):
        headers = [(b"authorization", b"Bearer test-key"), *headers]
    scope = {
        "type": "http",
        "headers": headers,
        "method": "GET",
        "app": SimpleNamespace(
            state=SimpleNamespace(
                auth=SimpleNamespace(config=ServerConfig(api_key="test-key"), sessionmaker=None),
                log_state_dir=path,
                shutdown_requested=asyncio.Event(),
            )
        ),
    }

    async def receive():
        return {"type": "http.request"}

    return Request(scope, receive)


def log_file(path: Path, ids: list[int]) -> Path:
    directory = path / "logs"
    directory.mkdir(exist_ok=True)
    file = directory / "millwright.log"
    file.write_text(
        "".join(
            json.dumps({"id": id_, "logger": "server.test", "message": "restored", "level": "info"}) + "\n"
            for id_ in ids
        ),
        encoding="utf-8",
    )
    return file


async def next_event(body: AsyncGenerator[object, None]) -> str:
    event = await asyncio.wait_for(anext(body), timeout=5)
    assert isinstance(event, str)
    return event


@pytest.mark.asyncio
async def test_stream_pages(tmp_path: Path, monkeypatch) -> None:
    log_file(tmp_path, list(range(1, 1002)))
    parsed = 0
    original = log_history._parse

    def parse(line):
        nonlocal parsed
        parsed += 1
        return original(line)

    monkeypatch.setattr(log_history, "_parse", parse)
    response = await stream_logs(request(tmp_path), after=1)
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    async with aclosing(body):
        for id_ in range(2, 1002):
            event = await next_event(body)
            assert event.startswith(f"id: {id_}\nevent: log_lines\n")
    assert parsed < 1100


@pytest.mark.asyncio
async def test_stream_rotation(tmp_path: Path) -> None:
    file = log_file(tmp_path, [1])
    response = await stream_logs(request(tmp_path))
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    async with aclosing(body):
        assert (await next_event(body)).startswith("id: 1\n")
        file.replace(file.with_name("millwright.log.1"))
        log_file(tmp_path, [2, 3])
        assert (await next_event(body)).startswith("id: 2\n")
        assert (await next_event(body)).startswith("id: 3\n")


@pytest.mark.asyncio
async def test_stream_resume(tmp_path: Path) -> None:
    log_file(tmp_path, [1, 2, 3])
    response = await stream_logs(request(tmp_path, [(b"last-event-id", b"2")]))
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    async with aclosing(body):
        assert (await next_event(body)).startswith("id: 3\n")


@pytest.mark.asyncio
async def test_stream_rejects_id(tmp_path: Path) -> None:
    with pytest.raises(HTTPException):
        await stream_logs(request(tmp_path, [(b"last-event-id", b"nope")]))


@pytest.mark.asyncio
async def test_shutdown_drains_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.api import logs
    from server.api.auth import AuthStack
    from server.core.auth import PendingStates, WsTickets
    from tests.helpers import database_context

    log_file(tmp_path, [1])
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    read_tail = logs._read_tail

    def blocked_read(state_dir, identity, offset):
        if offset is not None:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
        return read_tail(state_dir, identity, offset)

    monkeypatch.setattr(logs, "_read_tail", blocked_read)
    async with database_context("log-shutdown") as factory:
        async with factory() as session:
            _, token = await create_user_session(session, "operator", 1)
            await session.commit()
        req = request(tmp_path, [(b"cookie", f"mw_session={token}".encode())])
        req.app.state.auth = AuthStack(
            config=ServerConfig(api_key="test-key"),
            sessionmaker=factory,
            ws_tickets=WsTickets(),
            pending=PendingStates(),
        )
        response = await stream_logs(req)
        disconnected = asyncio.Event()

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished.set()

        task = asyncio.create_task(response({**req.scope, "asgi": {"spec_version": "2.4"}}, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            req.app.state.shutdown_requested.set()
            assert not task.done() and not finished.is_set()
            release.set()
            await asyncio.wait_for(task, 3)
            assert finished.is_set() and not disconnected.is_set()
            assert not req.app.state.auth.ws_tickets._sessions
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
