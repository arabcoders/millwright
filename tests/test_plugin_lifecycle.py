from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import io
import os
import shutil
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.media.probe import ProbeData, ProbeStream
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app

from server.core.config import AuthConfig, OidcConfig, ServerConfig
from server.flows.definition import FlowDefinition
from server.flows.dryrun import dry_run
from server.plugins.host import PluginHost
from server.plugins.states import disabled_ids
from server.scanner.normalize import normalize
from tests.helpers import database_context, graph_flow, make_test_temp_dir
from tests.test_dry_run import H264_PROBE

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER_PLUGINS = REPO_ROOT / "plugins"
FAKE_PLUGINS = Path(__file__).parent / "fakes" / "plugins"

MANIFEST_TOML = b'id = "filter.uploaded"\nkind = "filter"\nname = "Uploaded"\ndescription = "Uploaded test plugin"\nversion = "1.0.0"\nauthor = "Test"\nurl = "https://example.com/plugins"\n'
PLUGIN_PY = b"""
from __future__ import annotations

from typing import Any

from common.ctx import PluginContext
from pydantic import BaseModel


class Inputs(BaseModel):
    level: int = 3


INPUTS = Inputs


def decide(_ctx: PluginContext, _inputs: Any) -> dict[str, Any]:
    return {"type": "skip", "reason": "uploaded plugin ran"}
"""


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def upload_entries(manifest: bytes = MANIFEST_TOML, plugin: bytes = PLUGIN_PY) -> dict[str, bytes]:
    return {"plugin.toml": manifest, "plugin.py": plugin}


def video(index: int = 0, codec: str = "h264") -> dict[str, Any]:
    return {"index": index, "codec": codec, "codec_type": "video"}


def ctx_dict(*, size_bytes: int = 4 * 1024**3) -> dict[str, Any]:
    probe = ProbeData(
        file_path="/media/fixture.mp4",
        container="mp4",
        duration_s=3000.0,
        size_bytes=size_bytes,
        streams=[ProbeStream(**video())],
    )
    return {"probe": probe.model_dump(mode="json"), "original_filename": "movie.mkv"}


def stage_all(root: Path, *names: str) -> None:
    for name in names:
        shutil.copytree(FAKE_PLUGINS / name, root / name)


async def evaluated(host: PluginHost, plugin_id: str, *, size_bytes: int = 4 * 1024**3) -> Any:
    return await host.evaluate(plugin_id, ctx_dict(size_bytes=size_bytes), {})


@dataclass
class ApiEnv:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    host: PluginHost


@pytest_asyncio.fixture
async def api_env() -> AsyncIterator[ApiEnv]:
    async with database_context("plugins", tables=("flows", "plugin_states", "users", "sessions")) as factory:
        bundled = make_test_temp_dir("plugins-api-bundled")
        stage_all(bundled, "filter.echo")
        host = PluginHost([bundled], uploaded_dir=make_test_temp_dir("plugins-api-uploads"))
        app = create_app(factory, plugin_host=host)
        try:
            transport = httpx.ASGITransport(app=app)
            async with api_client(transport=transport, base_url="http://testserver") as client:
                yield ApiEnv(factory=factory, client=client, host=host)
        finally:
            await host.aclose()


@pytest.mark.asyncio
async def test_disable_respawns() -> None:
    root = make_test_temp_dir("plugins-lifecycle-toggle")
    stage_all(root, "filter.echo")
    host = PluginHost([root])
    try:
        assert (await evaluated(host, "filter.echo")).facets == {"mode": "x"}
        # Runner processes are host-private. Observe teardown through the PID.
        old = host._plugins["filter.echo"].runner
        assert old is not None and old.returncode is None

        await host.set_enabled("filter.echo", False)
        await asyncio.wait_for(old.wait(), 10)
        assert host._plugins["filter.echo"].runner is None
        assert host.plugins()[0].state == "disabled"
        decision = await evaluated(host, "filter.echo")
        assert decision.type == "error"
        assert decision.error is not None
        assert await host.describe_inputs("filter.echo") is None

        await host.set_enabled("filter.echo", True)
        assert host.plugins()[0].state == "ready"
        decision = await evaluated(host, "filter.echo")
        assert decision.facets == {"mode": "x"}
        new = host._plugins["filter.echo"].runner
        assert new is not None and new.pid != old.pid
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_restart_respawns() -> None:
    root = make_test_temp_dir("plugins-lifecycle-restart")
    stage_all(root, "filter.echo")
    host = PluginHost([root])
    try:
        await evaluated(host, "filter.echo")
        old = host._plugins["filter.echo"].runner
        assert old is not None

        await host.restart("filter.echo")
        assert host.plugins()[0].state == "ready"
        await asyncio.wait_for(old.wait(), 10)
        decision = await evaluated(host, "filter.echo")
        assert decision.facets == {"mode": "x"}
        new = host._plugins["filter.echo"].runner
        assert new is not None and new.pid != old.pid
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_stop_until_restart() -> None:
    root = make_test_temp_dir("plugins-lifecycle-stop")
    stage_all(root, "filter.echo")
    host = PluginHost([root])
    try:
        await evaluated(host, "filter.echo")
        await host.stop("filter.echo")
        assert host.plugins()[0].state == "stopped"
        decision = await evaluated(host, "filter.echo")
        assert decision.type == "error"
        assert decision.error is not None
        # Describe must not respawn a stopped runner.
        assert await host.describe_inputs("filter.echo") is None
        assert host._plugins["filter.echo"].runner is None

        await host.restart("filter.echo")
        assert host.plugins()[0].state == "ready"
        assert (await evaluated(host, "filter.echo")).facets == {"mode": "x"}
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_patch_persists_disabled(api_env: ApiEnv) -> None:
    resp = await api_env.client.patch("/api/plugins/filter.echo", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "disabled" and body["source"] == "bundled"
    async with api_env.factory() as session:
        assert await disabled_ids(session) == {"filter.echo"}

    listed = (await api_env.client.get("/api/plugins")).json()
    assert listed[0]["state"] == "disabled"

    unknown = await api_env.client.patch("/api/plugins/filter.nope", json={"enabled": False})
    assert unknown.status_code == 404
    enabled = await api_env.client.patch("/api/plugins/filter.echo", json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json()["state"] == "ready"
    async with api_env.factory() as session:
        assert await disabled_ids(session) == set()


@pytest.mark.asyncio
async def test_stop_restart(api_env: ApiEnv) -> None:
    await evaluated(api_env.host, "filter.echo")
    stopped = await api_env.client.post("/api/plugins/filter.echo/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"
    listed = (await api_env.client.get("/api/plugins")).json()
    assert listed[0]["state"] == "stopped"

    disabled_block = await api_env.client.post("/api/plugins/filter.echo/restart")
    assert disabled_block.status_code == 200
    assert disabled_block.json()["state"] == "ready"

    await api_env.client.patch("/api/plugins/filter.echo", json={"enabled": False})
    blocked = await api_env.client.post("/api/plugins/filter.echo/stop")
    assert blocked.status_code == 409
    unknown = await api_env.client.post("/api/plugins/filter.nope/restart")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_save_rejects_disabled(api_env: ApiEnv) -> None:
    await api_env.host.hot_load(STARTER_PLUGINS / "transcode.video")
    flow = graph_flow("filter.echo", "transcode.video")
    valid = await api_env.client.put("/api/flows/enabled", json={"definition": flow})
    assert valid.status_code == 200, valid.text

    await api_env.host.set_enabled("filter.echo", False)
    resp = await api_env.client.put("/api/flows/broken", json={"definition": flow})
    assert resp.status_code == 422

    unknown_flow = graph_flow("filter.nope", "transcode.video")
    resp = await api_env.client.put("/api/flows/broken2", json={"definition": unknown_flow})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_dry_run_disabled() -> None:
    host = PluginHost([STARTER_PLUGINS])
    try:
        await host.set_enabled("transcode.video", False)
        flow = FlowDefinition.model_validate(graph_flow("transcode.video"))
        result = await dry_run(flow, _probe(4 * 1024**3), host, "/media/input.mkv")
        assert result.flow.steps[0].decision == "error"
        assert result.flow.steps[0].error is not None
    finally:
        await host.aclose()


def _probe(size: int) -> Any:
    stat = os.stat_result((0, 0, 0, 0, 0, 0, size, 0, 0, 0))
    return normalize(Path("/lib/movie.mkv"), stat, H264_PROBE).probe


async def auth_client(env: ApiEnv) -> AsyncIterator[httpx.AsyncClient]:
    oidc = OidcConfig(issuer="https://sso.example.test/realm", client_id="t", secret="s", redirect_uri="https://ui/cb")
    app = create_app(
        env.factory,
        plugin_host=env.host,
        config=ServerConfig(
            auth=AuthConfig(oidc=oidc),
        ),
    )
    async with env.factory() as session:
        _, token = await create_user_session(session, "tester", 1)
        await session.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_upload_auth(api_env: ApiEnv) -> None:
    resp = await api_env.client.post(
        "/api/plugins",
        headers={"Authorization": ""},
        files={"file": ("echo.zip", zip_bytes(upload_entries()), "application/zip")},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_hot_loads(api_env: ApiEnv) -> None:
    async for client in auth_client(api_env):
        resp = await client.post(
            "/api/plugins", files={"file": ("uploaded.zip", zip_bytes(upload_entries()), "application/zip")}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest"]["id"] == "filter.uploaded"
    assert body["state"] == "ready" and body["source"] == "uploaded"
    assert [field["name"] for field in body["inputs"]] == ["level"]

    listed = (await api_env.client.get("/api/plugins")).json()
    sources = {row["manifest"]["id"]: row["source"] for row in listed}
    assert sources == {"filter.echo": "bundled", "filter.uploaded": "uploaded"}
    decision = await evaluated(api_env.host, "filter.uploaded")
    assert decision.type == "skip" and decision.reason == "uploaded plugin ran"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(
            {"../evil.py": b"x", **upload_entries()},
            id="zip-slip",
        ),
        pytest.param(
            upload_entries(
                manifest=b'id = "filter.media"\nkind = "filter"\nname = "M"\ndescription = "Reserved namespace test"\nversion = "1.0.0"\nauthor = "Test"\nurl = "https://example.com/plugins"\n'
            ),
            id="reserved-namespace",
        ),
        pytest.param(
            {"a/plugin.toml": MANIFEST_TOML, "b/plugin.toml": MANIFEST_TOML},
            id="multi-root",
        ),
        pytest.param({"notes.txt": b"no manifest here"}, id="missing-manifest"),
        pytest.param(upload_entries(manifest=b"not toml ["), id="bad-toml"),
        pytest.param(
            upload_entries(manifest=b'id = 3\nauthor = "Test"\nurl = "https://example.com/plugins"\n'),
            id="bad-manifest-type",
        ),
    ],
)
async def test_upload_bad_archives(api_env: ApiEnv, entries: dict[str, bytes]) -> None:
    async for client in auth_client(api_env):
        resp = await client.post("/api/plugins", files={"file": ("bad.zip", zip_bytes(entries), "application/zip")})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_collision(api_env: ApiEnv) -> None:
    manifest = b'id = "filter.echo"\nkind = "filter"\nname = "Echo"\ndescription = "Echo test plugin"\nversion = "1.0.0"\nauthor = "Test"\nurl = "https://example.com/plugins"\n'
    async for client in auth_client(api_env):
        resp = await client.post(
            "/api/plugins",
            files={"file": ("echo.zip", zip_bytes(upload_entries(manifest=manifest)), "application/zip")},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_rejects_oversize(api_env: ApiEnv) -> None:
    big = b"0" * (10 * 1024 * 1024 + 1)
    async for client in auth_client(api_env):
        resp = await client.post(
            "/api/plugins", files={"file": ("big.zip", zip_bytes({"blob": big}), "application/zip")}
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(upload_entries(plugin=b"0" * (64 * 1024 + 1)), id="single-entry"),
        pytest.param(
            upload_entries() | {f"blob{i}": b"0" * (16 * 1024) for i in range(8)},
            id="entry-total",
        ),
    ],
)
async def test_upload_rejects_bomb(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, entries: dict[str, bytes]) -> None:
    # Shrink the cap so the streaming check trips without allocating 100 MiB.
    monkeypatch.setattr("server.plugins.upload.MAX_DECOMPRESSED_BYTES", 64 * 1024)
    async for client in auth_client(api_env):
        resp = await client.post("/api/plugins", files={"file": ("bomb.zip", zip_bytes(entries), "application/zip")})
    assert resp.status_code == 413
