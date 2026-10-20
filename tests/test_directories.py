from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from tests.helpers import database_context, make_test_disk_path, make_test_temp_dir

if TYPE_CHECKING:
    from fastapi import FastAPI

FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with database_context("directories") as factory:
        app: FastAPI = create_app(factory, ffprobe_bin=str(FAKE_FFPROBE))
        transport = httpx.ASGITransport(app=app)
        async with api_client(transport=transport, base_url="http://testserver") as client:
            yield client


def make_tree() -> Path:
    root = make_test_temp_dir("directories")
    (root / "series").mkdir()
    (root / "movies").mkdir()
    (root / "notes.txt").write_bytes(b"x")
    dot_dir = root / ".hidden"
    dot_dir.mkdir()
    (dot_dir / "nested").mkdir()
    return root


@pytest.mark.asyncio
async def test_lists_directories_sorted(client: httpx.AsyncClient) -> None:
    root = make_tree()
    resp = await client.get("/api/directories", params={"path": str(root)})

    assert resp.status_code == 200
    listing = resp.json()
    assert listing["path"] == str(root.resolve())
    assert [item["name"] for item in listing["items"]] == ["movies", "series"]
    assert listing["items"][0]["path"] == str(root / "movies")
    child = root / "series" / "season-01"
    child.mkdir()
    second = (await client.get("/api/directories", params={"path": listing["items"][1]["path"]})).json()
    assert second["path"] == str((root / "series").resolve())
    assert [item["name"] for item in second["items"]] == ["season-01"]
    dotted = await client.get("/api/directories", params={"path": f"{root}/series/../series/season-01"})
    assert dotted.json()["path"] == str(child.resolve())


@pytest.mark.asyncio
async def test_missing_path_errors(client: httpx.AsyncClient) -> None:
    missing = make_test_disk_path("directories", f"missing-{uuid.uuid4().hex}")
    file_path = make_test_disk_path("directories", f"plain-{uuid.uuid4().hex}.txt")
    file_path.write_bytes(b"x")

    missing_resp = await client.get("/api/directories", params={"path": str(missing)})
    file_resp = await client.get("/api/directories", params={"path": str(file_path)})

    assert missing_resp.status_code == 404
    assert file_resp.status_code == 404
