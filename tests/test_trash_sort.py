from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.models import TrashEntry
from tests.helpers import database_context


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    async with database_context(
        "trash-sort", tables=("trash_entries", "media_files", "probes", "libraries", "flows")
    ) as sessions:
        async with sessions() as session:
            for number, library_id, name, size, trashed_day, purge_day in (
                (3, 1, "alpha.mkv", 30, 0, 2),
                (2, 1, "beta.mkv", 10, 2, 0),
                (4, 1, "gamma.mkv", 20, 1, 1),
                (1, 1, "beta.mkv", 10, 2, 0),
                (5, 2, "beta.mkv", 99, 3, 3),
            ):
                session.add(
                    TrashEntry(
                        id=f"{number:016x}",
                        library_id=library_id,
                        original_path=str(tmp_path / name),
                        library_root=str(tmp_path),
                        suffix=".mkv",
                        size_bytes=size,
                        mtime_ns=1,
                        trashed_at=stamp + timedelta(days=trashed_day),
                        purge_at=stamp + timedelta(days=purge_day),
                    )
                )
            await session.commit()
        app = create_app(
            sessions,
            config=ServerConfig(trash_dir=str(tmp_path / "trash"), workspace_dir=str(tmp_path / "workspace")),
        )
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            yield api


@pytest.mark.asyncio
async def test_sort_fields(client: httpx.AsyncClient) -> None:
    for sort_by, ascending in (
        ("trashed_at", [3, 4, 1, 2]),
        ("purge_at", [1, 2, 4, 3]),
        ("size_bytes", [1, 2, 4, 3]),
        ("original_path", [3, 1, 2, 4]),
    ):
        for sort_dir in ("asc", "desc"):
            response = await client.get(
                "/api/trash", params={"library_id": 1, "sort_by": sort_by, "sort_dir": sort_dir}
            )
            assert response.status_code == 200
            expected = ascending if sort_dir == "asc" else list(reversed(ascending))
            assert [item["id"] for item in response.json()["items"]] == [f"{number:016x}" for number in expected]


@pytest.mark.asyncio
async def test_default_newest(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/trash")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [f"{number:016x}" for number in [5, 2, 1, 4, 3]]
    assert response.json()["pagination"] == {"limit": 50, "offset": 0, "total": 5}
    assert response.json()["total_size_bytes"] == 169


@pytest.mark.asyncio
async def test_scoped_sort_pagination(client: httpx.AsyncClient) -> None:
    for sort_dir in ("asc", "desc"):
        params = {"library_id": 1, "query": " BETA.MKV ", "sort_by": "size_bytes", "sort_dir": sort_dir, "limit": 1}
        expected = [1, 2] if sort_dir == "asc" else [2, 1]
        for offset in range(3):
            response = await client.get("/api/trash", params={**params, "offset": offset})
            assert response.status_code == 200
            data = response.json()
            assert [item["id"] for item in data["items"]] == [
                f"{number:016x}" for number in expected[offset : offset + 1]
            ]
            assert data["pagination"] == {"limit": 1, "offset": offset, "total": 2}
            assert data["total_size_bytes"] == 20


@pytest.mark.asyncio
async def test_invalid_sort(client: httpx.AsyncClient) -> None:
    for params in ({"sort_by": "id"}, {"sort_dir": "ascending"}):
        response = await client.get("/api/trash", params=params)
        assert response.status_code == 422
