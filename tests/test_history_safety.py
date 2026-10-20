from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import aclosing
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.requests import Request
from starlette.websockets import WebSocket

from common.enums import JobStatus
from server.api.jobs import attempt_log
from server.core.log_history import install_log_file, query_logs
from server.models import Job, JobAttempt, JobEvent, MediaFile, ProbeRow
from server.workspace import cleanup_workspace
from tests.test_api import ApiEnv, api_env
from tests.hub_helpers import FakeConn
from tests.test_hub import Env, env, file_job, finished, hello, hub, materialize_offer, serve
from tests.test_staging import add_job
from tests.helpers import database_context


@pytest.mark.parametrize("replacement", ["leaf", "parent", "hardlink"])
def test_log_sink_confined(tmp_path, replacement: str) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "millwright.log"
    original.write_bytes(b"unrelated personal data")
    directory = state / "logs"
    if replacement == "parent":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        if replacement == "hardlink":
            os.link(original, directory / "millwright.log")
        else:
            (directory / "millwright.log").symlink_to(original)
    handler = None
    try:
        try:
            handler = install_log_file(state)
            handler.handle(logging.LogRecord("server.test", logging.INFO, "", 0, "operational event", (), None))
        except OSError, ValueError:
            pass
        assert original.read_bytes() == b"unrelated personal data", (
            "operational logging appended to an unrelated file through an aliased log sink"
        )
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_log_history_confined(tmp_path) -> None:
    from tests.test_logs_stream import log_file

    outside = tmp_path / "outside"
    outside.mkdir()
    log_file(outside, [1])
    state = tmp_path / "state"
    state.mkdir()
    (state / "logs").symlink_to(outside / "logs", target_is_directory=True)
    try:
        entries = query_logs(state)["entries"]
    except OSError, ValueError:
        entries = []
    assert entries == [], "operational log paging read records through a parent symlink outside its state root"


@pytest.mark.asyncio
async def test_log_stream_confined(tmp_path, monkeypatch) -> None:
    from server.api.logs import stream_logs
    from tests.test_logs_stream import log_file, request

    outside = tmp_path / "outside"
    outside.mkdir()
    log = log_file(outside, [1])
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "millwright.log").symlink_to(log)
    appended = False
    hold = asyncio.Event()

    async def poll(delay):
        nonlocal appended
        if not appended:
            appended = True
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": 2, "level": "info", "logger": "private", "message": "outside"}) + "\n")
            return
        await hold.wait()

    monkeypatch.setattr("server.api.logs.asyncio.sleep", poll)
    response = await stream_logs(request(state))
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    async with aclosing(body):
        try:
            event = await asyncio.wait_for(anext(body), 3)
        except OSError, ValueError, StopAsyncIteration, TimeoutError:
            event = None
        assert event is None, "operational log streaming read an unrelated file through a symlink"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["leaf", "parent"])
async def test_download_rejects_redirection(api_env: ApiEnv, tmp_path, replacement: str) -> None:
    job_id = await add_job(api_env.factory, None, status=JobStatus.RUNNING)
    root = api_env.app.state.workspace_dir
    relative = f"jobs/{job_id}/attempts/1/ffmpeg.log"
    log = root / relative
    log.parent.mkdir(parents=True)
    log.write_bytes(b"owned")
    outside = tmp_path / "unrelated.txt"
    outside.write_bytes(b"private unrelated data")
    async with api_env.factory() as session:
        attempt = JobAttempt(job_id=job_id, attempt_number=1, workload="transcode", status="running", log_path=relative)
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id
    request = Request({"type": "http", "headers": [], "app": api_env.app})
    response = await attempt_log(job_id, attempt_id, request)
    # Validation has returned, but streaming has not opened its source yet.
    if replacement == "parent":
        outside.rename(tmp_path / "ffmpeg.log")
        outside = tmp_path / "ffmpeg.log"
        log.parent.rename(log.parent.with_name("retained"))
        log.parent.symlink_to(tmp_path, target_is_directory=True)
    else:
        log.unlink()
        log.symlink_to(outside)
    downloaded = b""
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    async with aclosing(body):
        try:
            async for chunk in body:
                downloaded += chunk.encode() if isinstance(chunk, str) else chunk
        except OSError, ValueError:
            pass
    assert downloaded in {b"", b"owned"}, "log download read an unrelated file after pathname replacement"
    assert outside.read_bytes() == b"private unrelated data"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["finished", "post_actions"])
async def test_log_failure_cleans(env: Env, monkeypatch, phase: str) -> None:
    from tests.test_hub import post_action_case, resolve_action

    def unread(*args):
        raise OSError("injected read failure")

    if phase == "post_actions":
        async with post_action_case(env) as case:
            materialize_offer(case.hub, case.offer)
            async with env.factory() as session:
                attempt = await session.get(JobAttempt, case.offer["attempt_id"])
                assert attempt is not None
                attempt.log_data = b"encoding log"
                await session.commit()
            monkeypatch.setattr("server.nodehub.hub.read_attempt_log", unread)
            await resolve_action(case)
            job_id, attempt_id, relative = case.jid, case.offer["attempt_id"], case.offer["log_path"]
            assert not (env.workspace / relative).exists()
    else:
        job_id = await file_job(env)
        h, conn = hub(env), FakeConn([hello()])
        task = await serve(h, conn)
        try:
            assert await h.dispatch_once() == 1
            offer = conn.messages("job")[0]["job"]
            materialize_offer(h, offer)
            relative, attempt_id = offer["log_path"], offer["attempt_id"]
            monkeypatch.setattr("server.nodehub.hub.read_attempt_log", unread)
            await h._handle_finished(h.entries["node-a"], finished(job_id, attempt_id, relative, "failed"))
            assert not (env.workspace / relative).exists()
        finally:
            conn.disconnect()
            await asyncio.wait_for(task, 3)
    async with env.factory() as session:
        attempt = await session.get(JobAttempt, attempt_id)
        assert attempt is not None and attempt.log_data == (b"encoding log" if phase == "post_actions" else b"log")
        assert (
            await session.scalar(
                select(JobEvent.id).where(JobEvent.job_id == job_id, JobEvent.type == "execution_log_unavailable")
            )
            is not None
        )
    await cleanup_workspace(env.factory, env.workspace)
    assert not (env.workspace / relative).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["list", "event", "detail", "export"])
async def test_invalid_probe_isolated(api_env: ApiEnv, surface: str) -> None:
    from server.events import publish_job_status

    job_id = await add_job(api_env.factory, None, status=JobStatus.FAILED)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        bad_probe = {"file_path": "recorded.mkv", "duration_s": "unsupported", "size_bytes": 1, "streams": []}
        session.add(JobAttempt(job_id=job_id, attempt_number=1, status="failed", input_probe=bad_probe))
        if surface in {"detail", "export"}:
            media = await session.get(MediaFile, job.media_file_id)
            assert media is not None
            probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps({"format": {"duration": "nan"}}))
            session.add(probe)
            await session.flush()
            media.probe_id = probe.id
        await session.commit()
    if surface == "event":
        subscription = api_env.app.state.events.subscribe()
        try:
            await publish_job_status(api_env.app.state.events, api_env.factory, job_id)
            api_env.app.state.events.flush_bulk()
            assert subscription.backlog > 0
        finally:
            api_env.app.state.events.unsubscribe(subscription)
            await api_env.app.state.events.aclose()
    else:
        path = "/api/jobs" if surface == "list" else f"/api/jobs/{job_id}"
        if surface == "export":
            path += "/export.json"
        response = await api_env.client.get(path)
        assert response.status_code == 200, "one unreadable probe prevented inspection of retained job report"
        if surface in {"detail", "export"}:
            assert response.json()["attempts"][0]["input_probe"] == bad_probe


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["logout", "disconnect", "close"])
async def test_blocked_events_closed(reason: str) -> None:
    from server.api.auth import AuthStack
    from server.api.events import events_socket
    from server.core.auth import PendingStates, Principal, WsTickets, revoke_session
    from server.core.config import ServerConfig
    from server.events import EventBus

    bus = EventBus(min_interval_s=0)
    accepted, sending, release, closed, disconnected = (asyncio.Event() for _ in range(5))
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if received == 1:
            return {"type": "websocket.connect"}
        await disconnected.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        if message["type"] == "websocket.accept":
            accepted.set()
        elif message["type"] == "websocket.send":
            sending.set()
            await release.wait()
        elif message["type"] == "websocket.close":
            closed.set()

    async with database_context("blocked-events") as factory:
        async with factory() as session:
            row, token = await create_user_session(session, "operator", 1)
            await session.commit()
        tickets = WsTickets()
        stack = AuthStack(
            config=ServerConfig(api_key="fake-key"),
            sessionmaker=factory,
            ws_tickets=tickets,
            pending=PendingStates(),
        )
        ticket = tickets.issue(
            Principal(
                user_id=1,
                username="operator",
                session_hash=row.token_hash,
                session_expires_at=row.expires_at,
            )
        )
        ws = WebSocket(
            {"type": "websocket", "app": SimpleNamespace(state=SimpleNamespace(auth=stack, events=bus))}, receive, send
        )
        task = asyncio.create_task(events_socket(ws, ticket))
        try:
            await asyncio.wait_for(accepted.wait(), 3)
            bus.publish("node.status", {"state": "online"})
            await asyncio.wait_for(sending.wait(), 3)
            if reason == "logout":
                async with factory() as session:
                    assert await revoke_session(session, token)
                    await session.commit()
                tickets.revoke_session(row.token_hash)
            elif reason == "disconnect":
                disconnected.set()
            else:
                await bus.aclose()
            await asyncio.wait_for(asyncio.shield(task), 3)
            if reason == "logout":
                assert closed.is_set()
            assert not tickets._sessions
        finally:
            release.set()
            disconnected.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            await bus.aclose()


@pytest.mark.asyncio
async def test_blocked_logs_revoked(tmp_path) -> None:
    from server.api.auth import AuthStack
    from server.api.logs import stream_logs
    from server.core.auth import PendingStates, WsTickets, revoke_session
    from server.core.config import ServerConfig
    from tests.test_logs_stream import log_file

    log_file(tmp_path, [1])
    sending, release = asyncio.Event(), asyncio.Event()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            sending.set()
            await release.wait()

    async with database_context("blocked-log-session") as factory:
        async with factory() as session:
            row, token = await create_user_session(session, "operator", 1)
            await session.commit()
        stack = AuthStack(
            config=ServerConfig(api_key="fake-key"),
            sessionmaker=factory,
            ws_tickets=WsTickets(),
            pending=PendingStates(),
        )
        scope = {
            "type": "http",
            "method": "GET",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "headers": [(b"cookie", f"mw_session={token}".encode())],
            "app": SimpleNamespace(
                state=SimpleNamespace(auth=stack, log_state_dir=tmp_path, shutdown_requested=asyncio.Event())
            ),
        }
        response = await stream_logs(Request(scope, receive))
        task = asyncio.create_task(response(scope, receive, send))
        try:
            await asyncio.wait_for(sending.wait(), 3)
            async with factory() as session:
                assert await revoke_session(session, token)
                await session.commit()
            stack.ws_tickets.revoke_session(row.token_hash)
            await asyncio.wait_for(asyncio.shield(task), 3)
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            body = response.body_iterator
            assert isinstance(body, AsyncGenerator)
            await body.aclose()


@pytest.mark.asyncio
async def test_foreign_report_retained(env: Env) -> None:
    job_id = await file_job(env)
    unrelated = env.library / "unrelated.mkv"
    unrelated.write_bytes(b"unrelated")
    unrelated_id = await add_job(env.factory, unrelated, status=JobStatus.FAILED)
    h, conn = hub(env), FakeConn([hello()])
    task = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
        await h._handle_finished(
            h.entries["node-a"], finished(unrelated_id, offer["attempt_id"], offer["log_path"], "failed")
        )
        async with env.factory() as session:
            report = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == unrelated_id, JobEvent.type == "foreign_finished")
            )
            assert report is not None
            assert report.payload["attempt_id"] == offer["attempt_id"]
            assert report.attempt_id is None, "a rejected report linked another job's attempt into its report graph"
        await h._handle_finished(
            h.entries["node-a"], finished(job_id, offer["attempt_id"], offer["log_path"], "failed")
        )
        from server.retention import delete_eligible_job

        async with env.factory() as session:
            assert await delete_eligible_job(session, job_id)
            await session.commit()
            assert await session.get(Job, unrelated_id) is not None
            assert await session.get(JobEvent, report.id) is not None
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, 3)
