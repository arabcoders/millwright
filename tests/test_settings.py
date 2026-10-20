from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.settings import SETTINGS_KEY


@pytest.mark.asyncio
async def test_settings_persist(database_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    config = ServerConfig(api_key="test-key", disable_jobs=True)
    app = create_app(database_factory, config=config, cache_root=tmp_path / "cache")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/settings")).status_code == 401
            assert (await client.patch("/api/settings", json={"max_probes": 2})).status_code == 401
            client.headers["Authorization"] = "Bearer test-key"
            original = (await client.get("/api/settings")).json()
            response = await client.patch(
                "/api/settings",
                json={"max_probes": 2, "healthcheck_location": "node", "scanner": {"ignore_dirs": ["Extras"]}},
            )
            assert response.status_code == 200, response.text
            updated = response.json()
            assert updated["max_probes"] == 2 and updated["healthcheck_location"] == "node"
            assert updated["scanner"] == {**original["scanner"], "ignore_dirs": ["Extras"]}
            assert app.state.scanner.scanner.ignore_dirs == ["Extras"]
            assert app.state.events.healthcheck_slots == 0
            assert app.state.events.healthchecks_on_nodes
            assert await app.state.store.get(SETTINGS_KEY) == updated
    restarted = create_app(database_factory, config=config, cache_root=tmp_path / "restarted")
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.settings.current.model_dump(mode="json") == updated
        assert restarted.state.scanner.scanner.ignore_dirs == ["Extras"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"max_probes": 2, "playback": {"grant_ttl_s": 1}},
        {"scanner": {"unknown": True}},
        {"healthcheck_location": "elsewhere"},
        {"min_free_gb": None},
    ],
)
async def test_settings_rejected(
    database_factory: async_sessionmaker[AsyncSession], tmp_path: Path, changes: dict
) -> None:
    app = create_app(database_factory, config=ServerConfig(disable_jobs=True), cache_root=tmp_path / "cache")
    async with app.router.lifespan_context(app):
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            before = (await client.get("/api/settings")).json()
            response = await client.patch("/api/settings", json=changes)
            assert response.status_code == 422
            assert (await client.get("/api/settings")).json() == before
            assert await app.state.store.get(SETTINGS_KEY) is None
