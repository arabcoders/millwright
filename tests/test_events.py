from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import select
from tests.helpers import create_test_app as create_app
from server.api.events import _watch_session
from server.core.auth import Principal, revoke_session
from server.core.config import AuthConfig, OidcConfig, ServerConfig
from server.events import Event, EventBus, Subscription, publish_file_status_session, publish_job_status
from server.models import Job, Library, MediaFile
from tests.helpers import database_context
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosedError

ISSUER = "https://sso.example.test/realms/main"


async def pop_event(sub: Subscription, timeout_s: float = 1.0) -> Event:
    event = await asyncio.wait_for(sub.next(), timeout=timeout_s)
    assert event is not None
    return event


@pytest_asyncio.fixture
async def db():
    async with database_context("events") as factory:
        yield factory


def oidc_config() -> ServerConfig:
    oidc = OidcConfig(issuer=ISSUER, client_id="transcode", secret="shh", redirect_uri="http://ui.example.test/cb")
    return ServerConfig(
        auth=AuthConfig(oidc=oidc),
    )


class StartedServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, started: asyncio.Event) -> None:
        super().__init__(config)
        self._started = started

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets)
        self._started.set()


async def start_server(app) -> tuple[StartedServer, asyncio.Task, int]:
    started = asyncio.Event()
    server = StartedServer(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, log_level=None), started)
    task = asyncio.create_task(server.serve())
    try:
        # These tests own the event feed. Background maintenance must not race
        # their explicit publications with unrelated statistics updates.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(app.state.scheduler, "start", lambda: None)
            await asyncio.wait_for(started.wait(), timeout=10)
            await asyncio.wait_for(asyncio.shield(app.state.startup_task), 5)
        port = server.servers[0].sockets[0].getsockname()[1]
        return server, task, port
    except BaseException:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        raise


async def stop_server(server: StartedServer, task: asyncio.Task) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)


async def events_url(app, port: int) -> str:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/auth/ws-ticket", headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        return f"ws://127.0.0.1:{port}/api/events?ticket={response.json()['ticket']}"


async def recv_json(ws, timeout_s: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout_s))


def test_subscription_coalesces_latest() -> None:
    async def run() -> list[dict]:
        bus = EventBus(min_interval_s=0)
        sub = bus.subscribe()
        bus.publish("node.status", {"name": "n1", "state": "starting"}, key="node.status:n1")
        bus.publish("node.status", {"name": "n1", "state": "online"}, key="node.status:n1")
        bus.publish("node.status", {"name": "n1", "state": "online"}, key="node.status:n1")
        bus.publish("stats", {"queue_depth": {}}, key="stats")
        bus.publish("stats", {"queue_depth": {"queued": 1}}, key="stats")
        assert sub.backlog == 2

        events = []
        while sub.backlog:
            event = await pop_event(sub)
            events.append({"type": event.type, "payload": event.payload})
        await bus.aclose()
        return events

    assert asyncio.run(run()) == [
        {"type": "node.status", "payload": {"name": "n1", "state": "online"}},
        {"type": "stats", "payload": {"queue_depth": {"queued": 1}}},
    ]


def test_subscription_drain() -> None:
    async def run() -> tuple[Event, Event | None]:
        bus = EventBus(min_interval_s=0)
        sub = bus.subscribe()
        bus.publish("node.status", {"name": "n1", "state": "online"}, key="node.status:n1")
        sub.close()
        first = await pop_event(sub)
        second = await sub.next()
        return first, second

    first, second = asyncio.run(run())
    assert first.payload == {"name": "n1", "state": "online"}
    assert second is None


def test_gate_supersedes() -> None:
    async def run() -> list[dict]:
        now = [0.0]
        bus = EventBus(min_interval_s=1.0, clock=lambda: now[0])
        sub = bus.subscribe()
        bus.publish("node.status", {"name": "n1", "state": "starting"}, key="node.status:n1")

        now[0] = 0.5
        bus.publish("node.status", {"name": "n1", "state": "busy"}, key="node.status:n1")
        assert bus.staged_count() == 1

        # Past the gate interval, a fresh publish supersedes the staged one.
        now[0] = 1.5
        bus.publish("node.status", {"name": "n1", "state": "online"}, key="node.status:n1")
        assert bus.staged_count() == 0

        events = []
        while sub.backlog:
            event = await pop_event(sub)
            events.append(event.payload)
        await bus.aclose()
        return events

    assert asyncio.run(run()) == [{"name": "n1", "state": "online"}]


def test_gate_flushes_trailing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> list[dict]:
        now = [0.0]
        bus = EventBus(min_interval_s=1.0, clock=lambda: now[0])
        loop = asyncio.get_running_loop()
        call_later = loop.call_later
        timers: list[asyncio.TimerHandle] = []

        def schedule(delay, callback, *args, context=None):
            if callback == bus._flush:
                assert delay == 1.0
                handle = asyncio.TimerHandle(delay, callback, args, loop, context=context)
                timers.append(handle)
                return handle
            return call_later(delay, callback, *args, context=context)

        monkeypatch.setattr(loop, "call_later", schedule)
        sub = bus.subscribe()
        try:
            bus.publish("stats", {"n": 1})
            bus.publish("stats", {"n": 2})
            assert bus.staged_count() == 1
            first = await pop_event(sub)
            assert len(timers) == 1 and sub.backlog == 0
            now[0] = 1.0
            timers[0]._run()
            assert bus.staged_count() == 0
            second = await pop_event(sub)
            return [first.payload, second.payload]
        finally:
            for timer in timers:
                timer.cancel()
            await bus.aclose()

    assert asyncio.run(run()) == [{"n": 1}, {"n": 2}]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pruned", "deleted"])
@pytest.mark.parametrize("connected", [False, True])
async def test_removal_stays_terminal(action: str, connected: bool) -> None:
    bus = EventBus()
    sub = bus.subscribe() if connected else None
    try:
        bus.publish("file.status", {"action": action, "media_file_id": 1}, key="file.status:file:1")
        bus.flush_bulk()
        if sub is not None:
            removal = await pop_event(sub)
            assert removal.payload["events"][0]["payload"]["action"] == action
        else:
            sub = bus.subscribe()
        bus.publish(
            "file.status",
            {"action": "updated", "media_file_id": 1, "file": {"id": 1}},
            key="file.status:file:1",
        )
        # Rediscovering the same path creates a new catalog ID and must still update clients.
        replacement = {"action": "updated", "media_file_id": 2, "file": {"id": 2}}
        bus.publish("file.status", replacement, key="file.status:file:2")
        bus.flush_bulk()
        update = await pop_event(sub)
        assert update.payload["events"] == [{"type": "file.status", "payload": replacement}]
    finally:
        if sub is not None:
            bus.unsubscribe(sub)
        await bus.aclose()


@pytest.mark.asyncio
async def test_events_open(db) -> None:
    app = create_app(db)
    server, task, port = await start_server(app)
    try:
        async with ws_connect(await events_url(app, port), open_timeout=5) as ws:
            payload = {"job_id": 7, "status": "running", "error": None, "finished_at": None}
            app.state.events.publish("node.status", payload)
            assert await recv_json(ws) == {"type": "node.status", "payload": payload}
    finally:
        await stop_server(server, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ["none", "pending", "inflight"])
@pytest.mark.parametrize("scoped", [False, True])
async def test_scan_pruning_events(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str, scoped: bool
) -> None:
    # Flush explicitly so a delayed publisher cannot depend on the batch timer's timing.
    if publication != "none":
        monkeypatch.setattr("server.events.EVENT_BATCH_INTERVAL", 60)
    root = tmp_path / "library"
    root.mkdir()
    paths = [root / f"movie-{index}.mkv" for index in range(6)]
    for path in paths:
        path.write_bytes(b"media")
    app = create_app(db)
    # Discovery and event delivery remain real. Automatic probing must not race the controlled publisher.
    monkeypatch.setattr(app.state.probes, "start", AsyncMock())
    server, task, port = await start_server(app)
    entered, release = asyncio.Event(), asyncio.Event()
    publisher: asyncio.Task | None = None
    try:
        async with db() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.commit()
        initial = await asyncio.wait_for(app.state.scanner.scan_now(library.id), 5)
        assert initial is not None and initial.found == initial.added == 6
        async with db() as session:
            media = await session.scalar(select(MediaFile).where(MediaFile.path == str(paths[-1])))
            assert media is not None
            job = Job(library_id=library.id, media_file_id=media.id, kind="transcode", status="failed")
            session.add(job)
            await session.commit()
        bus = app.state.events
        assert app.state.scanner.events is app.state.staging.events is app.state.hub.events is bus
        async with ws_connect(await events_url(app, port), open_timeout=5) as ws:
            if publication == "pending":
                await publish_job_status(bus, db, job.id)
            elif publication == "inflight":

                async def delayed_publish() -> None:
                    async with db() as session:
                        get = session.get

                        async def delayed_get(model, *args, **kwargs):
                            value = await get(model, *args, **kwargs)
                            if model is MediaFile:
                                entered.set()
                                await asyncio.wait_for(release.wait(), 5)
                            return value

                        monkeypatch.setattr(session, "get", delayed_get)
                        await publish_file_status_session(bus, session, media.id)

                publisher = asyncio.create_task(delayed_publish())
                await asyncio.wait_for(entered.wait(), 5)
            paths[-1].unlink()
            if scoped:
                assert app.state.scanner.queue_scan(library.id, "watch", target_paths={paths[-1]}) == "queued"
                report = await asyncio.wait_for(asyncio.shield(app.state.scanner._scan_tasks[library.id]), 5)
            else:
                report = await asyncio.wait_for(app.state.scanner.scan_now(library.id), 5)
            assert report is not None and (report.found, report.pruned) == (0 if scoped else 5, 1)
            async with db() as session:
                assert await session.get(MediaFile, media.id) is None
                assert await session.get(Job, job.id) is None
            release.set()
            if publisher is not None:
                await asyncio.wait_for(publisher, 5)
            if publication != "none":
                bus.flush_bulk()
            frames = []
            async with asyncio.timeout(5):
                while True:
                    frame = await recv_json(ws)
                    frames.append(frame)
                    if frame["type"] == "events.batch":
                        break
            assert frames[-1] == {
                "type": "events.batch",
                "payload": {
                    "events": [
                        {
                            "type": "file.status",
                            "payload": {"action": "pruned", "media_file_id": media.id, "library_id": library.id},
                        }
                    ]
                },
            }
            assert {"type": "job.deleted", "payload": {"job_id": job.id}} in frames
    finally:
        release.set()
        if publisher is not None:
            publisher.cancel()
            await asyncio.wait_for(asyncio.gather(publisher, return_exceptions=True), 5)
        await stop_server(server, task)


@pytest.mark.asyncio
async def test_events_missing_ticket(db) -> None:
    app = create_app(db, config=oidc_config(), http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        async with ws_connect(f"ws://127.0.0.1:{port}/api/events", open_timeout=5) as ws:
            with pytest.raises(ConnectionClosedError) as exc_info:
                await asyncio.wait_for(ws.recv(), timeout=5)
            assert exc_info.value.rcvd is not None
            assert exc_info.value.rcvd.code == 1008
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()


@pytest.mark.asyncio
async def test_events_ticket_once(db) -> None:
    app = create_app(db, config=oidc_config(), http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        ticket = app.state.auth.ws_tickets.issue(Principal(user_id=None, username="API key"))
        url = f"ws://127.0.0.1:{port}/api/events?ticket={ticket}"
        async with ws_connect(url, open_timeout=5) as ws:
            app.state.events.publish("node.status", {"name": "n1", "state": "online"})
            assert await recv_json(ws) == {"type": "node.status", "payload": {"name": "n1", "state": "online"}}

        async with ws_connect(url, open_timeout=5) as ws:
            with pytest.raises(ConnectionClosedError) as exc_info:
                await asyncio.wait_for(ws.recv(), timeout=5)
            assert exc_info.value.rcvd is not None
            assert exc_info.value.rcvd.code == 1008
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()


async def session_headers(db) -> dict[str, str]:
    async with db() as session:
        _, token = await create_user_session(session, "user-1", 7)
        await session.commit()
    return {"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"}


@pytest.mark.asyncio
async def test_logout_closes_events(db) -> None:
    app = create_app(db, config=oidc_config(), http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        headers = await session_headers(db)
        other_headers = await session_headers(db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ui:
            ticket = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            idle_ticket = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            pending = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            other_ticket = (await ui.post("/api/auth/ws-ticket", headers=other_headers)).json()["ticket"]
            async with (
                ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={ticket}", open_timeout=5) as ws,
                ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={idle_ticket}", open_timeout=5) as idle_ws,
                ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={other_ticket}", open_timeout=5) as other_ws,
            ):
                app.state.events.publish("node.status", {"path": "before-logout"})
                assert (await recv_json(ws))["payload"] == {"path": "before-logout"}
                await recv_json(idle_ws)
                await recv_json(other_ws)
                assert (await ui.post("/api/auth/logout", headers=headers)).status_code == 204
                assert (await ui.get("/api/auth/me", headers=headers)).status_code == 401
                assert (await ui.post("/api/auth/ws-ticket", headers=headers)).status_code == 401
                with pytest.raises(ConnectionClosedError) as exc_info:
                    await asyncio.wait_for(idle_ws.recv(), timeout=5)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1008
                app.state.events.publish("stats", {"path": "after-logout"})
                with pytest.raises(ConnectionClosedError) as exc_info:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1008
                assert (await recv_json(other_ws))["payload"] == {"path": "after-logout"}
                assert (await ui.get("/api/auth/me", headers=other_headers)).status_code == 200
            async with ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={pending}", open_timeout=5) as ws:
                with pytest.raises(ConnectionClosedError) as exc_info:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1008
            assert not app.state.auth.ws_tickets._sessions
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()


@pytest.mark.asyncio
async def test_expiry_closes_events(db, monkeypatch) -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0])
    monkeypatch.setattr("server.api.auth.utcnow", lambda: now[0])
    monkeypatch.setattr("server.api.events.utcnow", lambda: now[0])
    app = create_app(db, config=oidc_config(), http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        headers = await session_headers(db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ui:
            ticket = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            pending = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            async with ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={ticket}", open_timeout=5) as ws:
                app.state.events.publish("node.status", {"path": "before-expiry"})
                assert (await recv_json(ws))["payload"] == {"path": "before-expiry"}
                now[0] += timedelta(days=7)
                assert (await ui.get("/api/auth/me", headers=headers)).status_code == 401
                app.state.events.publish("stats", {"path": "after-expiry"})
                with pytest.raises(ConnectionClosedError) as exc_info:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1008
            # Keep the ticket TTL valid to isolate the owning session's expiry check.
            monkeypatch.setattr("server.core.auth.utcnow", lambda: now[0] - timedelta(days=7))
            async with ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={pending}", open_timeout=5) as ws:
                with pytest.raises(ConnectionClosedError) as exc_info:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1008
            assert not app.state.auth.ws_tickets._sessions
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()


@pytest.mark.asyncio
async def test_expiry_wakes_idle(monkeypatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("server.api.events.utcnow", lambda: now)
    revoked = asyncio.Event()
    sub = Subscription()
    task = asyncio.create_task(sub.next())
    try:
        await asyncio.wait_for(_watch_session(revoked, now, sub), timeout=1)
        assert revoked.is_set()
        assert await asyncio.wait_for(task, timeout=1) is None
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_events_api_key(db) -> None:
    config = oidc_config().model_copy(update={"api_key": "trusted-key"})
    app = create_app(db, config=config, http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        headers = {"Authorization": "Bearer trusted-key"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ui:
            ticket = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            async with ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={ticket}", open_timeout=5) as ws:
                assert (await ui.post("/api/auth/logout", headers=headers)).status_code == 400
                app.state.events.publish("node.status", {"path": "trusted"})
                assert (await recv_json(ws))["payload"] == {"path": "trusted"}
            assert not app.state.auth.ws_tickets._sessions
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()


@pytest.mark.asyncio
async def test_revoked_ticket_rejected(db) -> None:
    app = create_app(db, config=oidc_config(), http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        headers = await session_headers(db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ui:
            ticket = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            # Retain the pending ticket to exercise database validation independently
            # of logout's in-memory invalidation (including a concurrent issuance).
            async with db() as session:
                assert await revoke_session(session, headers["Cookie"].removeprefix("mw_session="))
                await session.commit()
            assert (await ui.get("/api/auth/me", headers=headers)).status_code == 401
            async with ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={ticket}", open_timeout=5) as ws:
                with pytest.raises(ConnectionClosedError) as exc_info:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                assert exc_info.value.rcvd is not None
                assert exc_info.value.rcvd.code == 1008
            assert app.state.auth.ws_tickets.consume(ticket) is None
            assert not app.state.auth.ws_tickets._sessions
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()


@pytest.mark.asyncio
async def test_failed_logout_retains(db, monkeypatch) -> None:
    async def fail_revoke(session, token):
        await revoke_session(session, token)
        raise RuntimeError("revocation failed")

    monkeypatch.setattr("server.api.auth.revoke_session", fail_revoke)
    app = create_app(db, config=oidc_config(), http_client=httpx.AsyncClient())
    server, task, port = await start_server(app)
    try:
        headers = await session_headers(db)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as ui:
            ticket = (await ui.post("/api/auth/ws-ticket", headers=headers)).json()["ticket"]
            async with ws_connect(f"ws://127.0.0.1:{port}/api/events?ticket={ticket}", open_timeout=5) as ws:
                assert (await ui.post("/api/auth/logout", headers=headers)).status_code == 500
                assert (await ui.get("/api/auth/me", headers=headers)).status_code == 200
                app.state.events.publish("node.status", {"path": "retained"})
                assert (await recv_json(ws))["payload"] == {"path": "retained"}
    finally:
        await stop_server(server, task)
        await app.state.auth.http_client.aclose()
