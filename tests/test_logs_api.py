from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from server.api.logs import router
from server.core.config import ServerConfig
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_logs_auth(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.log_state_dir = tmp_path
    app.include_router(router)
    async with database_context("logs-api", tables=()) as sessions:
        app.state.auth = SimpleNamespace(config=ServerConfig(api_key="test-key"), sessionmaker=sessions)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            unauthorized = await client.get("/api/logs")
            empty = await client.get("/api/logs", headers={"Authorization": "Bearer test-key"})
            conflict = await client.get("/api/logs?before=2&after=1", headers={"Authorization": "Bearer test-key"})
    assert unauthorized.status_code == 401
    assert empty.status_code == 200
    assert empty.json()["entries"] == []
    assert conflict.status_code == 400
