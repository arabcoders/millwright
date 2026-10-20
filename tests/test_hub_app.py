from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import connect as ws_connect

from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.settings import ScannerConfig
from server.events import EventBus
from server.models import Library
from server.models import PluginState
from server.models import TrashEntry
from common.protocol import NodeCapabilities
from server.nodehub.hub import NodeEntry, NodeHub
from server.nodehub.store import get_node
from server.nodehub.store import register_node
from server.plugins.host import PluginHost
from server.scanner import service as scanner_service
from server.scanner.scan import ScanReport
from server.staging import StagingService
from tests.helpers import database_context, make_test_temp_dir

FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Lifespan teardown closes the engine before the persistence assertions.
    async with database_context("hub-app") as factory:
        yield factory


class StartedServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, started: asyncio.Event) -> None:
        super().__init__(config)
        self._started = started

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets)
        self._started.set()


@pytest.mark.asyncio
async def test_startup_order(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(db)
    calls: list[str] = []

    async def recover(_sessionmaker) -> None:
        calls.append("recover")

    async def requeue(_session, _now) -> list:
        calls.append("requeue")
        return []

    async def workspace(_sessionmaker, _root) -> dict[str, int]:
        calls.append("workspace")
        return {}

    async def journals() -> None:
        calls.append("journals")

    async def refresh_trash(sessionmaker, _root) -> int:
        assert sessionmaker is db
        calls.append("trash-refresh")
        return 0

    async def trash_purge(sessionmaker, _root) -> int:
        assert sessionmaker is db
        calls.append("trash-purge")
        return 0

    monkeypatch.setattr("server.api.app.recover_incomplete", recover)
    monkeypatch.setattr("server.api.app.requeue_startup", requeue)
    monkeypatch.setattr(app.state.hub, "recover_startup", journals)
    monkeypatch.setattr("server.api.app.cleanup_workspace", workspace)
    monkeypatch.setattr("server.api.app.refresh_trash_records", refresh_trash)
    monkeypatch.setattr("server.api.app.purge_trash", trash_purge)
    for service in (
        app.state.staging,
        app.state.healthchecks,
        app.state.hub,
        app.state.scanner,
        app.state.scheduler,
    ):
        monkeypatch.setattr(service, "start", lambda service=service: calls.append(type(service).__name__))
    monkeypatch.setattr(app.state.plugin_host, "start_watching", lambda: calls.append("plugins"))

    async def probes_start() -> None:
        calls.append("probes")

    async def enqueue_start() -> None:
        calls.append("enqueue")

    monkeypatch.setattr(app.state.probes, "start", probes_start)
    monkeypatch.setattr(app.state.flow_enqueue, "start", enqueue_start)
    async with asyncio.timeout(10):
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(asyncio.shield(app.state.startup_task), 5)
            # Replay journals before workspace cleanup can discard their files.
            assert calls.index("recover") < calls.index("journals") < calls.index("workspace")
            assert calls.index("requeue") < calls.index("workspace")
            assert calls.index("recover") < calls.index("trash-refresh") < calls.index("trash-purge")
            for service in (
                "StagingService",
                "HealthcheckService",
                "NodeHub",
                "ScannerService",
                "Scheduler",
                "plugins",
                "probes",
                "enqueue",
            ):
                assert calls.index("workspace") < calls.index(service)
                assert calls.index("trash-purge") < calls.index(service)


@pytest.mark.asyncio
async def test_disabled_startup(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uploaded = tmp_path / "plugins"
    shutil.copytree(Path(__file__).parent / "fakes" / "plugins" / "filter.echo", uploaded / "filter.echo")
    host = PluginHost([], uploaded_dir=uploaded)
    async with db() as session:
        session.add(PluginState(plugin_id="filter.echo", enabled=False))
        await session.commit()
    app = create_app(db, plugin_host=host)
    calls: list[str] = []

    async def enqueue_start() -> None:
        outcome = await host.evaluate("filter.echo", {}, {})
        assert outcome.type == "error" and outcome.error is not None
        calls.append("disabled")

    async def spawn(_state):
        pytest.fail("persisted disabled plugin must not execute during startup")

    monkeypatch.setattr(host, "_spawn", spawn)
    monkeypatch.setattr(host, "start_watching", lambda: None)
    for name in ("staging", "healthchecks", "hub", "scanner", "scheduler", "notifications"):
        monkeypatch.setattr(getattr(app.state, name), "start", lambda: None)
    monkeypatch.setattr(app.state.flow_enqueue, "start", enqueue_start)
    async with asyncio.timeout(10), app.router.lifespan_context(app):
        await asyncio.wait_for(asyncio.shield(app.state.startup_task), 5)
        assert calls == ["disabled"]
        assert next(item for item in host.plugins() if item.manifest.id == "filter.echo").state == "disabled"


@pytest.mark.asyncio
async def test_missing_trash_preserved(db: async_sessionmaker[AsyncSession]) -> None:
    root = make_test_temp_dir("missing-trash").joinpath("custom")
    async with db() as session:
        session.add(
            TrashEntry(
                id="entry1",
                library_id=1,
                original_path="/library/file.mkv",
                library_root="/library",
                suffix=".mkv",
                size_bytes=1,
                mtime_ns=1,
                purge_at=T0,
            )
        )
        await session.commit()
    app = create_app(db, config=ServerConfig(trash_dir=str(root)))
    async with asyncio.timeout(10):
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(asyncio.shield(app.state.startup_task), 5)
            assert not root.exists()
    async with db() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == "entry1")) is not None


@pytest.mark.asyncio
async def test_scan_publishes(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    lib_dir = make_test_temp_dir("hub-app-scan")
    async with db() as session:
        library = Library(name="lib", roots=[str(lib_dir)], scan_on_start=True)
        session.add(library)
        await session.commit()
        library_id = library.id

    scans = 0
    real_scan = scanner_service.scan_library

    async def counting_scan(
        session: AsyncSession,
        library: Library,
        *,
        config: ScannerConfig,
        staging: StagingService,
        events: EventBus | None = None,
        trash_dir: Path | None = None,
        on_progress=None,
        on_files_pending=None,
        stopped=None,
        target_paths: set[Path] | None = None,
    ) -> ScanReport:
        nonlocal scans
        scans += 1
        return await real_scan(
            session,
            library,
            config=config,
            staging=staging,
            events=events,
            trash_dir=trash_dir,
            on_progress=on_progress,
            on_files_pending=on_files_pending,
            stopped=stopped,
            target_paths=target_paths,
        )

    monkeypatch.setattr(scanner_service, "scan_library", counting_scan)

    app = create_app(db, ffprobe_bin=str(FAKE_FFPROBE))
    sub = app.state.events.subscribe()
    try:
        async with asyncio.timeout(10):
            async with app.router.lifespan_context(app):
                event = await asyncio.wait_for(sub.next(), timeout=5)
                while event.type != "file.status":
                    event = await asyncio.wait_for(sub.next(), timeout=5)
                await asyncio.wait_for(asyncio.shield(app.state.startup_task), 5)
                scanner = app.state.scanner
                assert scanner.running
                task = scanner._startup_task
                assert task is not None
                await asyncio.wait_for(asyncio.shield(task), 2)
                assert app.state.scheduler._task is not None
                assert not app.state.scheduler._task.done()
    finally:
        app.state.events.unsubscribe(sub)

    assert not scanner.running
    assert scanner._watch_tasks == {}
    assert scanner._debounce_tasks == {}
    assert app.state.scheduler._task is None
    assert scans == 1
    assert event.key == f"file.status:{library_id}"
    assert event.payload == {
        "action": "scanned",
        "library_id": library_id,
        "added": 0,
        "updated": 0,
        "pruned": 0,
    }


@pytest.mark.asyncio
async def test_websocket_handshake_lifecycle(db: async_sessionmaker[AsyncSession]) -> None:
    workspace_dir = make_test_temp_dir("hub-workspace")
    trash_dir = make_test_temp_dir("hub-trash")
    async with db() as session:
        await register_node(session, "node-x", "tok")
        await session.commit()
    hub = NodeHub(db, workspace_dir=workspace_dir, trash_dir=trash_dir, min_free_gb=1)
    app = create_app(db, hub=hub)
    subscription = app.state.events.subscribe()
    started = asyncio.Event()
    server = StartedServer(
        uvicorn.Config(app, host="127.0.0.1", port=0, ws="wsproto", log_config=None, log_level=None), started
    )
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.wait_for(asyncio.shield(app.state.startup_task), 5)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with ws_connect(f"ws://127.0.0.1:{port}/api/hub", open_timeout=5) as ws:
            hello = {"v": 1, "type": "hello", "node_id": "node-x", "token": "tok", "active_attempts": []}
            await ws.send(json.dumps(hello))
            challenge = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert challenge["type"] == "workspace_challenge"
            challenge_bytes = (workspace_dir / challenge["challenge_path"]).read_bytes()
            response_path = workspace_dir / challenge["response_path"]
            response_path.write_text(hashlib.sha256(challenge_bytes).hexdigest() + "\n")
            await ws.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "workspace_response",
                        "challenge_id": challenge["challenge_id"],
                        "response_path": challenge["response_path"],
                        "workspace_ready": True,
                        "workspace_free_bytes": 1,
                    }
                )
            )
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert reply["type"] == "welcome"
            assert reply["heartbeat_s"] == 15.0
            assert reply["lease_ttl_s"] == 300.0
            await hub.wait_node_state("node-x", "online")
            await ws.send(json.dumps({"v": 1, "type": "heartbeat", "cpu_percent": 42}))
            async with asyncio.timeout(5):
                event = await subscription.next()
                while event.type != "node.usage":
                    event = await subscription.next()
            assert event.payload["usage"]["cpu_percent"] == 42
            async with hub.pause_for_reset():
                await hub.disconnect_for_reset(timeout_s=5)
                assert not hub.entries

        await hub.wait_node_state("node-x", "offline", timeout_s=10)
        async with db() as session:
            node = await get_node(session, "node-x")
            assert node is not None
            assert node.state == "offline"
            assert node.usage is not None
            assert node.usage["cpu_percent"] == 42
    finally:
        app.state.events.unsubscribe(subscription)
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


class ResetConnection:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        _ = data
        return None

    async def recv_json(self) -> dict | None:
        return None

    async def close(self, code: int = 1000) -> None:
        _ = code
        self.closed.set()


def make_reset_entry(conn: ResetConnection, *, paused: bool = False) -> NodeEntry:
    return NodeEntry(
        name="reset-node",
        node_db_id=1,
        capabilities=NodeCapabilities(),
        workspace_ready=True,
        conn=conn,
        paused=paused,
    )


@pytest.mark.asyncio
async def test_reset_rejects_busy(db: async_sessionmaker[AsyncSession]) -> None:
    hub = NodeHub(db, workspace_dir=make_test_temp_dir("busy-reset-workspace"), min_free_gb=1)
    entry = make_reset_entry(ResetConnection())
    entry.in_flight[1] = "transcode"
    hub.entries[entry.name] = entry

    with pytest.raises(ValueError):
        async with hub.pause_for_reset():
            pass


@pytest.mark.asyncio
async def test_reset_rejects_nested(db: async_sessionmaker[AsyncSession]) -> None:
    hub = NodeHub(db, workspace_dir=make_test_temp_dir("nested-reset-workspace"), min_free_gb=1)

    async with hub.pause_for_reset():
        with pytest.raises(ValueError):
            async with hub.pause_for_reset():
                pass
        assert hub._reset_active

    assert not hub._reset_active
