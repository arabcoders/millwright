from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from server.api import flows as flows_api
from server.api.flows import router
from server.core.config import ServerConfig


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(router)
    app.state.auth = SimpleNamespace(config=ServerConfig(api_key="test-key"), sessionmaker=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-key"},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_lists_shipped_presets(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/flows/presets")

    assert resp.status_code == 200
    presets = resp.json()
    ids = [preset["id"] for preset in presets]
    assert ids and all(ids)
    assert ids == sorted(ids)
    for preset in presets:
        assert preset["name"]
        assert preset["description"]
        assert preset["route"]
        assert preset["concepts"]


@pytest.mark.asyncio
async def test_broken_entry_fails(client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "broken.flow.json").write_text('{"name": "broken"}', encoding="utf-8")
    monkeypatch.setattr(flows_api, "PRESETS_DIR", tmp_path)

    resp = await client.get("/api/flows/presets")

    assert resp.status_code == 500
    assert "broken.flow.json" in resp.json()["detail"]
