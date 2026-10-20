from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.models import KeyValue
from tests.helpers import database_context


@pytest_asyncio.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    trash = tmp_path / "trash"
    trash.mkdir()
    async with database_context("background-startup") as factory:
        app = create_app(factory, config=ServerConfig(workspace_dir=str(tmp_path / "workspace"), trash_dir=str(trash)))
        for name in (
            "snapshot_interrupted_logs",
            "recover_healthcheck_logs",
            "recover_incomplete",
            "requeue_startup",
            "cleanup_workspace",
            "recover_trash_restores",
            "refresh_trash_records",
            "purge_trash",
        ):
            monkeypatch.setattr(f"server.api.app.{name}", AsyncMock())
        monkeypatch.setattr("server.api.app.plugins.sync_states", AsyncMock())
        monkeypatch.setattr(app.state.hub, "recover_startup", AsyncMock())
        for name in ("staging", "healthchecks", "hub", "scanner", "scheduler", "notifications"):
            monkeypatch.setattr(getattr(app.state, name), "start", Mock())
        monkeypatch.setattr(app.state.plugin_host, "start_watching", Mock())
        for name in ("probes", "flow_enqueue", "library_deletion"):
            monkeypatch.setattr(getattr(app.state, name), "start", AsyncMock())
        yield app


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_activity_restores_pause(app: FastAPI, paused: bool) -> None:
    async with app.state.db() as session:
        session.add(KeyValue(key="activity_paused", value=paused))
        await session.commit()
    app.state.scanner.set_paused(paused=not paused)
    async with asyncio.timeout(5), app.router.lifespan_context(app):
        await asyncio.shield(app.state.startup_task)
        assert app.state.scanner.paused is paused
        assert app.state.hub.activity_paused() is paused
        assert app.state.healthchecks.activity_paused() is paused
        assert app.state.services_running


@pytest.mark.asyncio
async def test_recovery_serves_reads(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def recover(_sessions) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr("server.api.app.recover_incomplete", recover)
    async with asyncio.timeout(5), app.router.lifespan_context(app):
        try:
            await entered.wait()
            assert not app.state.services_running
            app.state.hub.start.assert_not_called()
            app.state.healthchecks.start.assert_not_called()
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for path in ("/health", "/api/jobs", "/api/logs"):
                    assert (await client.get(path)).status_code == 200
                assert (await client.get("/api/system")).status_code == 200
                assert (await client.post("/api/auth/ws-ticket")).status_code == 200
                for path, body in (
                    ("/api/jobs/bulk", {"action": "cancel", "job_ids": [1]}),
                    ("/api/backups/reset", {"keep": []}),
                ):
                    response = await client.post(path, json=body)
                    assert response.status_code == 503
                receive = AsyncMock(return_value={"type": "websocket.connect"})
                send = AsyncMock()
                await app(
                    {
                        "type": "websocket",
                        "path": "/api/hub",
                        "headers": [],
                        "query_string": b"",
                        "scheme": "ws",
                        "extensions": {"websocket.http.response": {}},
                    },
                    receive,
                    send,
                )
                response, body = [call.args[0] for call in send.call_args_list]
                assert response["type"] == "websocket.http.response.start" and response["status"] == 503
                assert (b"retry-after", b"1") in response["headers"]
                assert body["type"] == "websocket.http.response.body"
                release.set()
                await asyncio.shield(app.state.startup_task)
                assert app.state.services_running
                app.state.hub.start.assert_called_once()
                assert not app.state.recovering
                response = await client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": [1]})
                assert response.status_code == 200
        finally:
            release.set()
    assert app.state.startup_task.done()


@pytest.mark.asyncio
async def test_recovery_failure_visible(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = RuntimeError("Cannot recover /workspace/jobs/43.")

    async def recover(_sessions) -> None:
        raise failure

    monkeypatch.setattr("server.api.app.recover_incomplete", recover)
    auth_client = AsyncMock()
    app.state.auth.http_client = auth_client
    app.state.auth.owns_client = True
    async with asyncio.timeout(5), app.router.lifespan_context(app):
        with pytest.raises(RuntimeError) as error:
            await asyncio.shield(app.state.startup_task)
        assert error.value is failure
        assert not app.state.services_running
        auth_client.aclose.assert_not_awaited()
        app.state.hub.start.assert_not_called()
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/jobs")).status_code == 200
            assert (await client.get("/api/system")).status_code == 200
            assert app.state.recovering
            response = await client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": [1]})
            assert response.status_code == 503
            assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
    auth_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_drains_recovery(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def recover(_sessions) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()

    monkeypatch.setattr("server.api.app.recover_incomplete", recover)
    context = app.router.lifespan_context(app)
    await asyncio.wait_for(context.__aenter__(), 2)
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.wait_for(draining.wait(), 2)
        assert not shutdown.done()
        assert not app.state.startup_task.done()
        app.state.hub.start.assert_not_called()
        shutdown.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 2)
        assert app.state.startup_task.cancelled()
        assert not app.state.services_running
    finally:
        release.set()
        if shutdown is None:
            await asyncio.wait_for(context.__aexit__(None, None, None), 2)
        else:
            await asyncio.wait_for(asyncio.gather(shutdown, return_exceptions=True), 2)
