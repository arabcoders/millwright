from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import requests
from fastapi import FastAPI
from server.api.notifications import router
from server.core.config import ServerConfig
from tests.helpers import database_context


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with database_context("notifications-api", tables=("notification_targets",)) as factory:
        app = FastAPI()
        app.state.db = factory
        app.include_router(router)
        app.state.auth = SimpleNamespace(config=ServerConfig(api_key="test-key"), sessionmaker=factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer test-key"},
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_target_crud(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    first = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    second = datetime(2026, 1, 2, 3, 5, 6, tzinfo=UTC)
    monkeypatch.setattr("server.models.datetime", SimpleNamespace(now=lambda _: first))
    url = "discord://webhook-secret/token"
    created = await client.put("/api/notifications/alerts", json={"url": url})
    assert created.status_code == 200
    assert created.json() == {
        "name": "alerts",
        "url": url,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T03:04:05Z",
    }

    listing = await client.get("/api/notifications")
    assert listing.json() == [created.json()]

    monkeypatch.setattr("server.models.datetime", SimpleNamespace(now=lambda _: second))
    updated = await client.put("/api/notifications/alerts", json={"url": "json://example.test/notify"})
    assert updated.json() == {
        "name": "alerts",
        "url": "json://example.test/notify",
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T03:05:06Z",
    }
    assert (await client.delete("/api/notifications/alerts")).status_code == 204


@pytest.mark.asyncio
async def test_target_rejects_unsupported(client: httpx.AsyncClient) -> None:
    response = await client.put("/api/notifications/alerts", json={"url": "file:///secret/path"})
    assert response.status_code == 422
    assert "secret" not in response.text
    response = await client.put("/api/notifications/alerts", json={"url": "json://one\njson://two"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sends_message(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "json://example.test/notify"
    await client.put("/api/notifications/alerts", json={"url": url})
    calls: list[tuple[str, str, dict]] = []

    def send(method: str, target_url: str, *, data: str, **_kwargs: object) -> requests.Response:
        calls.append((method, target_url, json.loads(data)))
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr("apprise.plugins.custom_json.requests.request", send)
    response = await client.post("/api/notifications/alerts/test")

    assert response.status_code == 204
    assert len(calls) == 1
    assert calls[0][:2] == ("POST", "http://example.test/notify")
    assert calls[0][2]["title"]
    assert calls[0][2]["message"]


@pytest.mark.asyncio
async def test_target_reports_provider(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await client.put("/api/notifications/alerts", json={"url": "json://example.test/notify"})

    def fail(*_args: object, **_kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 401
        response._content = b"Unauthorized"
        return response

    monkeypatch.setattr("apprise.plugins.custom_json.requests.request", fail)
    response = await client.post("/api/notifications/alerts/test")

    assert response.status_code == 502
    assert "401" in response.json()["detail"]
    record = next(
        record
        for record in caplog.records
        if record.name == "server.api.notifications" and record.levelno == logging.WARNING
    )
    assert response.json()["detail"] == getattr(record, "error")


@pytest.mark.asyncio
async def test_reports_exception(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    await client.put("/api/notifications/alerts", json={"url": "json://example.test/notify"})
    failure = RuntimeError("connection refused")

    def send(*_args: object, **_kwargs: object) -> requests.Response:
        raise failure

    monkeypatch.setattr("apprise.plugins.custom_json.requests.request", send)
    response = await client.post("/api/notifications/alerts/test")

    assert response.status_code == 502
    assert str(failure) in response.json()["detail"]
