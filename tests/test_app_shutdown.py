from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI

from tests.helpers import create_test_app as create_app
from tests.helpers import database_context


@pytest_asyncio.fixture
async def app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    async with database_context("app-shutdown") as factory:
        app = create_app(factory)
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
        for name in ("staging", "healthchecks", "hub", "scanner", "scheduler", "notifications"):
            monkeypatch.setattr(getattr(app.state, name), "start", lambda: None)
        monkeypatch.setattr(app.state.hub, "recover_startup", AsyncMock())
        monkeypatch.setattr(app.state.plugin_host, "start_watching", lambda: None)
        monkeypatch.setattr(app.state.probes, "start", AsyncMock())
        monkeypatch.setattr(app.state.flow_enqueue, "start", AsyncMock())
        monkeypatch.setattr(app.state.library_deletion, "start", AsyncMock())
        monkeypatch.setattr(app.state.library_deletion, "close", AsyncMock())
        yield app


NAMES = (
    "scheduler",
    "dry_runs",
    "scanner",
    "workspaces",
    "probes",
    "flow_enqueue",
    "healthchecks",
    "staging",
    "notifications",
    "playback",
    "hub",
    "plugin_host",
    "events",
)


def record_closers(app: FastAPI, monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    for name in NAMES:
        service = getattr(app.state, name)
        method = "close" if name in {"probes", "flow_enqueue", "healthchecks"} else "aclose"

        async def close(name: str = name) -> None:
            calls.append(name)

        monkeypatch.setattr(service, method, close)


@pytest.mark.asyncio
async def test_shutdown_cancellation(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    record_closers(app, monkeypatch, calls)

    async def blocked() -> None:
        calls.append("scanner")
        started.set()
        await asyncio.wait_for(release.wait(), 1)

    monkeypatch.setattr(app.state.scanner, "aclose", blocked)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 1)
    await asyncio.wait_for(asyncio.shield(app.state.startup_task), 1)
    assert app.state.services_running is True
    task = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert sorted(calls) == sorted(NAMES)
        assert app.state.services_running is False
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_shutdown_error(app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []
    record_closers(app, monkeypatch, calls)
    client = AsyncMock()
    app.state.auth.owns_client = True
    app.state.auth.http_client = client

    async def fail() -> None:
        calls.append("scanner")
        raise RuntimeError("close failed")

    monkeypatch.setattr(app.state.scanner, "aclose", fail)
    with pytest.raises(RuntimeError):
        async with asyncio.timeout(2), app.router.lifespan_context(app):
            await asyncio.wait_for(asyncio.shield(app.state.startup_task), 1)
    assert sorted(calls) == sorted(NAMES)
    assert app.state.services_running is False
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_startup_client(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    app.state.auth.owns_client = True
    app.state.auth.http_client = client

    async def fail() -> None:
        raise RuntimeError("probe startup failed")

    monkeypatch.setattr(app.state.probes, "start", fail)
    async with asyncio.timeout(2), app.router.lifespan_context(app):
        assert isinstance(app.state.startup_task, asyncio.Task)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(asyncio.shield(app.state.startup_task), 1)
        assert app.state.services_running is False
        client.aclose.assert_not_awaited()
    assert app.state.services_running is False
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["scheduler", "hub"])
async def test_shutdown_dependency_order(app: FastAPI, monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow() -> None:
        calls.append(owner)
        started.set()
        await release.wait()

    calls: list[str] = []
    record_closers(app, monkeypatch, calls)
    monkeypatch.setattr(getattr(app.state, owner), "aclose", slow)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 2)
    await asyncio.wait_for(asyncio.shield(app.state.startup_task), 2)
    shutdown = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert owner in calls
        assert "plugin_host" not in calls and "events" not in calls
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert sorted(calls) == sorted(NAMES)
        assert calls.index(owner) < calls.index("plugin_host") < calls.index("events")
        assert app.state.services_running is False
    finally:
        release.set()
        await asyncio.wait_for(shutdown, 2)
