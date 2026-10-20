from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import insert

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.models import Job, Library, MediaFile, utcnow
from tests.helpers import database_context


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with database_context("api-pagination") as sessions:
        app = create_app(
            sessions,
            config=ServerConfig(trash_dir=str(tmp_path / "trash"), workspace_dir=str(tmp_path / "workspace")),
        )
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            yield api


@pytest.mark.asyncio
async def test_full_file_page(tmp_path: Path) -> None:
    async with database_context("full-file-page") as sessions:
        async with sessions() as session:
            library = Library(name="movies", roots=["/media"])
            session.add(library)
            await session.flush()
            session.add_all(
                MediaFile(
                    library_id=library.id,
                    path=f"/media/movie-{index}.mkv",
                    size=1,
                    mtime_ns=1,
                    container="matroska",
                    medium="video",
                    last_scanned_at=utcnow(),
                )
                for index in range(1000)
            )
            await session.commit()
        app = create_app(
            sessions,
            config=ServerConfig(trash_dir=str(tmp_path / "trash"), workspace_dir=str(tmp_path / "workspace")),
        )
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            for query in (None, "movie"):
                params = {"limit": "999", "sort_by": "id", "sort_dir": "asc"}
                if query is not None:
                    params["query"] = query
                response = await api.get("/api/files", params=params)
                assert response.status_code == 200
                page = response.json()
                assert page["pagination"] == {"limit": 999, "offset": 0, "total": 1000}
                assert len(page["items"]) == 999
                assert [file["id"] for file in page["items"]] == list(range(1, 1000))
                response = await api.get("/api/files", params={**params, "offset": "999"})
                assert response.status_code == 200
                assert [file["id"] for file in response.json()["items"]] == [1000]


@pytest.mark.asyncio
async def test_page_limits(client: httpx.AsyncClient) -> None:
    for endpoint in ("/api/files", "/api/trash", "/api/jobs", "/api/jobs/queue"):
        params = {"limit": "999"}
        if endpoint == "/api/jobs/queue":
            params["kind"] = "transcode"
        response = await client.get(endpoint, params=params)
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["pagination"] == {"limit": 999, "offset": 0, "total": 0}
        response = await client.get(endpoint, params={**params, "limit": "1000"})
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["healthcheck", "transcode"])
async def test_full_queue_page(tmp_path: Path, kind: str) -> None:
    async with database_context("full-queue-page") as sessions:
        async with sessions() as session:
            library = Library(name="movies", roots=["/media"])
            session.add(library)
            await session.flush()
            identifiers = list(
                await session.scalars(
                    insert(MediaFile).returning(MediaFile.id),
                    [
                        {
                            "library_id": library.id,
                            "path": f"/media/{index}.mkv",
                            "size": 1,
                            "mtime_ns": 1,
                            "container": "matroska",
                            "medium": "video",
                            "last_scanned_at": utcnow(),
                        }
                        for index in range(1000)
                    ],
                )
            )
            created = utcnow()
            await session.execute(
                insert(Job),
                [
                    {
                        "library_id": library.id,
                        "media_file_id": identifier,
                        "kind": kind,
                        "status": "queued",
                        "created_at": created,
                    }
                    for identifier in identifiers
                ],
            )
            await session.commit()
        app = create_app(
            sessions,
            config=ServerConfig(trash_dir=str(tmp_path / "trash"), workspace_dir=str(tmp_path / "workspace")),
        )
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.get("/api/jobs/queue", params={"kind": kind, "limit": "999"})
        assert response.status_code == 200
        page = response.json()
        assert page["pagination"] == {"limit": 999, "offset": 0, "total": 1000}
        assert [row["id"] for row in page["items"]] == list(range(1, 1000))
        assert {row["queue_state"] for row in page["items"]} == {"ready" if kind == "healthcheck" else "blocked"}


@pytest.mark.asyncio
async def test_bulk_page_limit(client: httpx.AsyncClient) -> None:
    for count in (999, 1000):
        response = await client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": list(range(1, count + 1))})
        if count == 999:
            assert response.status_code == 200
            assert response.json()["succeeded"] == []
            assert len(response.json()["failed"]) == count
        else:
            assert response.status_code == 422
            assert response.json()["detail"][0]["loc"] == ["body", "job_ids"]
