from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import uuid
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.media.probe import probe_from_raw
from common.plan.models import FFmpegPlan, VideoTarget
from common.protocol import NodeCapabilities
from common.enums import JobStatus
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import AuthConfig, OidcConfig, ServerConfig
from server.core.log_history import install_log_file, query_logs
from server.flows.definition import FlowDefinition
from server.flows.engine import FlowResult, StepOutcome
from server.healthcheck import HealthcheckService
from server.healthlogs import create_healthcheck_log
from server.models import (
    FinalizationJournal,
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    KeyValue,
    MediaFile,
    NodeRow,
    NotificationDelivery,
    ProbeRow,
    TrashEntry,
    utcnow,
)
from server.queue.store import enqueue_file_job, enqueue_healthcheck_job, finish, get_job
from server.nodehub.store import register_node
from server.nodehub.hub import NodeEntry
from server.serialize import iso_utc
from server.staging import StagingService
from common.fs.identity import source_identity
from tests.helpers import database_context, graph_flow, make_test_temp_dir

if TYPE_CHECKING:
    from fastapi import FastAPI

FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
UI_BASE = "http://testserver"

PROBE_RAW = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "disposition": {},
            "tags": {},
        },
        {
            "index": 1,
            "codec_name": "ac3",
            "codec_type": "audio",
            "channels": 6,
            "disposition": {},
            "tags": {},
        },
    ],
    "format": {"duration": "600.0", "size": "2048", "bit_rate": "27000"},
}


def plan() -> FFmpegPlan:
    return FFmpegPlan(video=VideoTarget(encoder="libx264"))


@dataclass
class ApiEnv:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    app: FastAPI
    lib_dir: Path


@asynccontextmanager
async def api_context(monkeypatch: pytest.MonkeyPatch, *, background: bool = False) -> AsyncGenerator[ApiEnv]:
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _: SimpleNamespace(free=20_000_000_000))
    lib_dir = make_test_temp_dir("api-lib")
    async with database_context("api") as factory:
        app = create_app(
            factory,
            ffprobe_bin=str(FAKE_FFPROBE),
            cache_root=make_test_temp_dir("api-cache"),
        )
        await app.state.settings.update({"min_free_gb": 1})
        app.state.workspace_dir = make_test_temp_dir("api-workspace")
        app.state.staging.workspace_dir = app.state.workspace_dir
        app.state.jobs.workspace_dir = app.state.workspace_dir
        if background:
            await app.state.jobs.start()
        transport = httpx.ASGITransport(app=app)
        try:
            async with api_client(transport=transport, base_url=UI_BASE) as client:
                yield ApiEnv(factory=factory, client=client, app=app, lib_dir=lib_dir)
        finally:

            async def drain_services() -> None:
                first_error = None
                try:
                    await asyncio.wait_for(app.state.jobs.close(), 5)
                except BaseException as exc:
                    first_error = exc
                scans = tuple(app.state.scanner._scan_tasks.values())
                if scans:
                    try:
                        results = await asyncio.wait_for(asyncio.gather(*scans, return_exceptions=True), 5)
                        for result in results:
                            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                                raise result
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                for close in (
                    app.state.library_deletion.close,
                    app.state.scheduler.aclose,
                    app.state.dry_runs.aclose,
                    app.state.scanner.aclose,
                    app.state.workspaces.aclose,
                    app.state.probes.close,
                    app.state.flow_enqueue.close,
                    app.state.healthchecks.close,
                    app.state.staging.aclose,
                    app.state.notifications.aclose,
                    app.state.playback.aclose,
                    app.state.hub.aclose,
                    app.state.plugin_host.aclose,
                    app.state.events.aclose,
                ):
                    try:
                        await asyncio.wait_for(close(), 5)
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                if first_error is not None:
                    raise first_error

            cancelled = False
            closing = asyncio.create_task(drain_services())
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
            if cancelled:
                raise asyncio.CancelledError


@pytest_asyncio.fixture
async def api_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ApiEnv]:
    # Only background integration tests start service loops.
    async with api_context(monkeypatch, background=getattr(request, "param", False)) as env:
        yield env


async def delete_requested(env: ApiEnv, ids: list[int], *, bulk: bool) -> tuple[httpx.Response, dict[int, dict]]:
    events = env.app.state.events
    sub = events.subscribe()
    outcomes = {}
    try:
        async with asyncio.timeout(5):
            response = (
                await env.client.post("/api/jobs/bulk", json={"action": "delete", "job_ids": ids})
                if bulk
                else await env.client.delete(f"/api/jobs/{ids[0]}")
            )
            accepted = response.json()["accepted"] if bulk else ids if response.status_code == 202 else []
            while len(outcomes) < len(accepted):
                event = await sub.next()
                assert event is not None
                if event.type == "job.deletion" and event.payload["job_id"] in accepted:
                    outcomes[event.payload["job_id"]] = event.payload
            return response, outcomes
    finally:
        events.unsubscribe(sub)


async def requeue_outcome(env: ApiEnv, job_id: int, *, event_type: str = "requeue") -> dict:
    bus = env.app.state.events
    sub = bus.subscribe()
    try:
        async with asyncio.timeout(5):
            async with env.factory() as session:
                request_id = await session.scalar(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id, JobEvent.type == "requested")
                    .order_by(JobEvent.id.desc())
                    .limit(1)
                )
                outcome = await session.scalar(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id, JobEvent.type == event_type, JobEvent.id > request_id)
                    .order_by(JobEvent.id.desc())
                    .limit(1)
                )
                if outcome is not None:
                    return outcome.payload
            while True:
                event = await sub.next()
                assert event is not None
                if event.type == f"job.{event_type}" and event.payload["job_id"] == job_id:
                    return event.payload
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "failure", "cancel"])
async def test_cleanup_joins_scans(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    context = api_context(monkeypatch)
    env = await context.__aenter__()
    started, closing_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order: list[str] = []
    failure = RuntimeError("scan failed")

    async def scan(_library_id: int, _reason: str) -> None:
        started.set()
        try:
            await asyncio.wait_for(release.wait(), 2)
            if outcome == "failure":
                raise failure
        finally:
            async with env.factory() as session:
                assert await session.scalar(select(func.count(Library.id))) == 0
            order.append("scan")

    async def close() -> None:
        closing_started.set()
        try:
            await context.__aexit__(None, None, None)
        finally:
            order.append("context")

    monkeypatch.setattr(env.app.state.scanner, "_requested_scan", scan)
    env.app.state.scanner.queue_scan(1, "manual")
    task = env.app.state.scanner._scan_tasks[1]
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        closing = asyncio.create_task(close())
        await asyncio.wait_for(closing_started.wait(), 1)
        assert not closing.done()
        if outcome == "cancel":
            closing.cancel()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 3)
        if outcome == "failure":
            assert results == [failure]
        elif outcome == "cancel":
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results == [None]
        assert task.done()
        assert order == ["scan", "context"]
    finally:
        release.set()
        if closing is None:
            closing = asyncio.create_task(close())
        await asyncio.wait_for(asyncio.gather(closing, task, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_sort_validation(api_env: ApiEnv) -> None:
    for resource in ("jobs", "files"):
        for params in ({"sort_by": "unknown"}, {"sort_dir": "sideways"}):
            response = await api_env.client.get(f"/api/{resource}", params=params)
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_file_date_contract(api_env: ApiEnv) -> None:
    first, _ = await seed_media(api_env, "a.mkv")
    second, _ = await seed_media(api_env, "b.mkv")
    async with api_env.factory() as session:
        for file_id, days in ((first, 1), (second, 2)):
            file = await session.get(MediaFile, file_id)
            assert file is not None
            file.created_at = T0 + timedelta(days=days)
            file.updated_at = T0 + timedelta(days=3 - days)
        await session.commit()
    for field in ("created_at", "updated_at"):
        response = await api_env.client.get("/api/files", params={"sort_by": field, "sort_dir": "desc", "limit": 1})
        assert response.status_code == 200
        row = response.json()["items"][0]
        assert row["id"] == (second if field == "created_at" else first)
        detail = await api_env.client.get(f"/api/files/{row['id']}")
        assert row["created_at"] == detail.json()["created_at"]
        assert row["updated_at"] == detail.json()["updated_at"]
        assert row[field].endswith("Z")


async def seed_media(env: ApiEnv, name: str = "movie.avi", *, video_codec: str | None = "h264") -> tuple[int, int]:
    path = env.lib_dir / name
    path.write_bytes(b"x" * 2048)
    identity = source_identity(path.stat())
    async with env.factory() as session:
        library = Library(name=f"lib-{uuid.uuid4().hex}", roots=[str(env.lib_dir)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=identity["size"],
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=identity,
            container="avi",
            video_codec=video_codec,
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        ids = (library.id, media.id)
        await session.commit()
        return ids


async def seed_node(env: ApiEnv, name: str = "node-a") -> int:
    async with env.factory() as session:
        node = await register_node(session, name, "test-token")
        await session.commit()
        return node.id


async def seed_queue(
    env: ApiEnv,
    name: str,
    *,
    library_id: int | None = None,
    kind: str = "transcode",
    status: str = "queued",
    priority: int = 0,
    created_at: datetime = T0,
) -> tuple[int, int]:
    async with env.factory() as session:
        if library_id is None:
            library = Library(name=name, roots=[str(env.lib_dir)])
            session.add(library)
            await session.flush()
            library_id = library.id
        media = MediaFile(
            library_id=library_id,
            path=str(env.lib_dir / name),
            size=2048,
            mtime_ns=1,
            medium="movie",
            container="avi",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(
            library_id=library_id,
            media_file_id=media.id,
            kind=kind,
            status=status,
            priority=priority,
            created_at=created_at,
            plan=plan().model_dump(mode="json") if kind == "transcode" else None,
        )
        session.add(job)
        await session.flush()
        if kind == "transcode":
            probe = probe_from_raw(Path(media.path), media.size, PROBE_RAW)
            session.add(
                JobEvent(
                    job_id=job.id,
                    type="enqueued",
                    payload={
                        "source": {"path": media.path, "size": media.size},
                        "probe": probe.model_dump(mode="json"),
                    },
                )
            )
        await session.commit()
        return job.id, library_id


@pytest.mark.asyncio
async def test_queue_rotation(api_env: ApiEnv, monkeypatch) -> None:
    monkeypatch.setattr("server.api.jobs._QUEUE_BATCH", 1)
    a1, library_a = await seed_queue(api_env, "a1", priority=10)
    a2, _ = await seed_queue(api_env, "a2", library_id=library_a, priority=10)
    b1, library_b = await seed_queue(api_env, "b1", priority=10)
    b2, _ = await seed_queue(api_env, "b2", library_id=library_b, priority=10, created_at=T0 + timedelta(seconds=1))
    high, _ = await seed_queue(api_env, "high", priority=11)
    low, _ = await seed_queue(api_env, "low", priority=9)
    staged_old, _ = await seed_queue(api_env, "staged-old", status="staging", priority=100)
    staged_tie, _ = await seed_queue(api_env, "staged-tie", status="staging", priority=100)
    staged_low, _ = await seed_queue(api_env, "staged-low", status="staging", priority=50)
    async with api_env.factory() as session:
        staged = await session.get(Job, staged_old)
        queued = await session.get(Job, high)
        assert staged is not None and queued is not None
        staged.warning = "Staging waiting. Workspace needs more free bytes."
        queued.warning = "Old node warning."
        await session.commit()
    h = api_env.app.state.hub
    h._library_cursors[10] = library_a

    blocked = (await api_env.client.get("/api/jobs/queue?kind=transcode&limit=200")).json()
    assert [row["id"] for row in blocked["items"]] == [staged_old, staged_tie, staged_low, high, b1, a1, a2, b2, low]
    assert [row["queue_state"] for row in blocked["items"]] == ["staging"] * 3 + ["blocked"] * 6
    assert all(row["cooldown_started_at"] is None for row in blocked["items"])
    assert blocked["items"][0]["queue_reason"] == "Staging waiting. Workspace needs more free bytes."

    node_id = await seed_node(api_env)
    conn = AsyncMock()
    entry = NodeEntry(
        name="node-a",
        node_db_id=node_id,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=conn,
    )
    h.entries[entry.name] = entry
    async with api_env.factory() as session:
        before_jobs = [dict(row) for row in (await session.execute(select(Job.__table__).order_by(Job.id))).mappings()]
        before_node = dict((await session.execute(select(NodeRow.__table__))).mappings().one())
        before_events = await session.scalar(select(func.count(JobEvent.id)))
    h._wake.clear()
    h.staging._wake.clear()
    statements: list[str] = []

    def track(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = api_env.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", track)
    try:
        response = await api_env.client.get("/api/jobs/queue?kind=transcode&limit=4")
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert response.status_code == 200
    preview = response.json()
    assert preview["pagination"] == {"limit": 4, "offset": 0, "total": 9}
    assert [row["id"] for row in preview["items"]] == [high, b1, a1, a2]
    assert all(row["queue_state"] == "ready" and row["queue_reason"] is None for row in preview["items"])
    assert all(row["cooldown_started_at"] is None for row in preview["items"])
    assert preview["items"][0]["warning"] == "Old node warning."
    assert h._library_cursors == {10: library_a}
    assert not entry.in_flight and not entry.offered_at and not conn.send_json.called
    assert not h._wake.is_set() and not h.staging._wake.is_set()
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in statements
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
    async with api_env.factory() as session:
        assert [
            dict(row) for row in (await session.execute(select(Job.__table__).order_by(Job.id))).mappings()
        ] == before_jobs
        assert dict((await session.execute(select(NodeRow.__table__))).mappings().one()) == before_node
        assert await session.scalar(select(func.count(JobEvent.id))) == before_events


@pytest.mark.asyncio
async def test_queue_healthchecks(api_env: ApiEnv) -> None:
    blocked, suspended = await seed_queue(api_env, "suspended", kind="healthcheck", priority=100)
    first, library_id = await seed_queue(api_env, "first", kind="healthcheck", priority=5)
    tie, _ = await seed_queue(api_env, "tie", kind="healthcheck", library_id=library_id, priority=5)
    later, _ = await seed_queue(api_env, "later", kind="healthcheck", priority=5, created_at=T0 + timedelta(seconds=1))
    low, _ = await seed_queue(api_env, "low", kind="healthcheck", priority=1)
    await seed_queue(api_env, "running", kind="healthcheck", status="running")
    await seed_queue(api_env, "staging", status="staging")
    await seed_queue(api_env, "queued")
    async with api_env.factory() as session:
        library = await session.get(Library, suspended)
        assert library is not None
        library.suspended = True
        library = await session.get(Library, library_id)
        assert library is not None
        library.schedule = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
        await session.commit()
    response = await api_env.client.get("/api/jobs/queue?kind=healthcheck")
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"] == {"limit": 6, "offset": 0, "total": 5}
    assert [row["id"] for row in body["items"]] == [first, tie, later, low, blocked]
    assert [row["queue_state"] for row in body["items"]] == ["ready"] * 4 + ["blocked"]
    assert all(row["kind"] == "healthcheck" for row in body["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    ["schedule", "suspended", "paused", "slots", "workspace", "encoder", "tags", "source", "probe", "plan", "actions"],
)
async def test_queue_blockers(api_env: ApiEnv, blocker: str) -> None:
    job_id, library_id = await seed_queue(api_env, "blocked", priority=100)
    staged, _ = await seed_queue(api_env, "staged", status="staging")
    h = api_env.app.state.hub
    entry = NodeEntry(
        name="node-a",
        node_db_id=1,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    h.entries[entry.name] = entry
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        library = await session.get(Library, library_id)
        assert job is not None and library is not None
        job.warning = "Old node warning."
        if blocker == "schedule":
            library.schedule = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
        elif blocker == "suspended":
            library.suspended = True
        elif blocker == "paused":
            entry.paused = True
        elif blocker == "slots":
            entry.in_flight[999] = "transcode"
        elif blocker == "workspace":
            entry.workspace_ready = False
        elif blocker == "encoder":
            entry.capabilities = entry.capabilities.model_copy(update={"encoders": []})
        elif blocker == "tags":
            job.requires = ["gpu"]
        elif blocker in {"source", "probe"}:
            captured = await session.scalar(select(JobEvent).where(JobEvent.job_id == job_id))
            assert captured is not None
            captured.payload = {key: value for key, value in captured.payload.items() if key != blocker}
        elif blocker == "plan":
            job.plan = None
        elif blocker == "actions":
            owner = Job(library_id=library_id, media_file_id=job.media_file_id, status="succeeded")
            session.add(owner)
            await session.flush()
            attempt = JobAttempt(job_id=owner.id, attempt_number=1, status="succeeded")
            session.add(attempt)
            await session.flush()
            session.add(JobEvent(job_id=owner.id, attempt_id=attempt.id, type="actions_pending"))
        await session.commit()
    response = await api_env.client.get("/api/jobs/queue?kind=transcode&limit=1")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == staged
    assert response.json()["items"][0]["queue_state"] == "staging"
    rows = (await api_env.client.get("/api/jobs/queue?kind=transcode&limit=200")).json()["items"]
    blocked = next(row for row in rows if row["id"] == job_id)
    assert blocked["queue_state"] == "blocked"
    assert blocked["warning"] == "Old node warning."
    assert blocked["latest_attempt"] is None


@pytest.mark.asyncio
async def test_queue_validation(api_env: ApiEnv) -> None:
    for query in ("", "?kind=other", "?kind=healthcheck&limit=0", "?kind=transcode&limit=1000"):
        assert (await api_env.client.get(f"/api/jobs/queue{query}")).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_library_crud_and(api_env: ApiEnv) -> None:
    (api_env.lib_dir / "sample.avi").write_bytes(b"x" * 2048)
    body = {
        "name": "movies",
        "roots": [str(api_env.lib_dir)],
        "cooldown_s": 120,
        "watch": True,
        "mkv_stats": True,
        "trash_mode": "all",
        "trash_retention_days": 14,
        "schedule": {"type": "grid", "cells": [[hour == 3 for hour in range(24)] for _ in range(7)]},
    }
    resp = await api_env.client.post("/api/libraries", json=body)
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "movies" and created["flow_id"] is None
    assert created["roots"] == [str(api_env.lib_dir)] and created["cooldown_s"] == 120
    assert created["trash_mode"] == "all" and created["trash_retention_days"] == 14
    assert created["mkv_stats"] is True
    assert created["healthcheck_enabled"] is False

    resp = await api_env.client.get("/api/libraries")
    assert [lib["name"] for lib in resp.json()] == ["movies"]

    resp = await api_env.client.patch(
        f"/api/libraries/{created['id']}",
        json={
            "watch": False,
            "schedule": None,
            "mkv_stats": False,
            "healthcheck_enabled": True,
            "trash_retention_days": 7,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["watch"] is False and resp.json()["schedule"] is None
    assert resp.json()["mkv_stats"] is False
    assert resp.json()["healthcheck_enabled"] is True
    assert resp.json()["trash_retention_days"] == 7

    resp = await api_env.client.get(f"/api/libraries/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["mkv_stats"] is False
    assert resp.json()["healthcheck_enabled"] is True
    resp = await api_env.client.get("/api/libraries")
    assert next(item for item in resp.json() if item["id"] == created["id"])["healthcheck_enabled"] is True

    resp = await api_env.client.post(f"/api/libraries/{created['id']}/scan")
    assert resp.status_code == 202
    assert resp.json() == {"library_id": created["id"], "status": "queued"}


@pytest.mark.asyncio
async def test_library_roots_validated(api_env: ApiEnv) -> None:
    resp = await api_env.client.post("/api/libraries", json={"name": "empty", "roots": []})
    assert resp.status_code == 422
    resp = await api_env.client.post("/api/libraries", json={"name": "blank", "roots": ["  "]})
    assert resp.status_code == 422
    resp = await api_env.client.post("/api/libraries", json={"name": "negative", "roots": ["/x"], "cooldown_s": -1})
    assert resp.status_code == 422
    resp = await api_env.client.post(
        "/api/libraries", json={"name": "trash", "roots": ["/x"], "trash_retention_days": 0}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("service_name", ["hub", "healthchecks"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_activity_pause_check(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, service_name: str) -> None:
    service = getattr(api_env.app.state, service_name)
    api_env.app.state.hub._wake.clear()
    api_env.app.state.healthchecks._wake.clear()
    lock = service.dispatch_lock
    waiting = asyncio.Event()

    @asynccontextmanager
    async def queue():
        waiting.set()
        async with lock:
            yield

    monkeypatch.setattr(service, "dispatch_lock", queue())
    operation = None
    try:
        async with lock:
            operation = asyncio.create_task(api_env.client.post("/api/libraries/scanner/pause"))
            await asyncio.wait_for(waiting.wait(), 2)
            assert not operation.done()
            assert not api_env.app.state.scanner.paused
        response = await asyncio.wait_for(operation, 2)
        assert response.status_code == 200 and response.json()["paused"]
        assert api_env.app.state.hub.activity_paused()
        assert api_env.app.state.healthchecks.activity_paused()
        assert api_env.app.state.hub._wake.is_set()
        assert api_env.app.state.healthchecks._wake.is_set()
        async with api_env.factory() as session:
            assert await session.scalar(select(KeyValue.value).where(KeyValue.key == "activity_paused")) is True
    finally:
        if operation is not None:
            await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), 2)
    monkeypatch.setattr(service, "dispatch_lock", lock)
    response = await api_env.client.post("/api/libraries/scanner/resume")
    assert response.status_code == 200 and not response.json()["paused"]
    assert not api_env.app.state.hub.activity_paused()
    assert not api_env.app.state.healthchecks.activity_paused()
    async with api_env.factory() as session:
        state = (await session.scalars(select(KeyValue).where(KeyValue.key == "activity_paused"))).one()
        assert state.key == "activity_paused" and state.value is False


@pytest.mark.asyncio
async def test_activity_write_failure(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    execute = AsyncSession.execute

    async def fail(session, statement, *args, **kwargs):
        if statement.is_insert and statement.table.name == "key_values":
            raise RuntimeError("Cannot persist activity pause.")
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail)
    response = await api_env.client.post("/api/libraries/scanner/pause")
    assert response.status_code == 500
    assert not api_env.app.state.scanner.paused
    async with api_env.factory() as session:
        assert await session.scalar(select(KeyValue.id).where(KeyValue.key == "activity_paused")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_activity_cancel_drains(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    commit = AsyncSession.commit
    execute = AsyncSession.execute

    async def mark_write(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if statement.is_insert and statement.table.name == "key_values":
            session.info["activity_write"] = True
        return result

    async def blocked(session):
        if session.info.get("activity_write"):
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", blocked)
    monkeypatch.setattr(AsyncSession, "execute", mark_write)
    operation = asyncio.create_task(api_env.client.post("/api/libraries/scanner/pause"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        operation.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
        assert api_env.app.state.scanner.paused
        async with api_env.factory() as session:
            assert await session.scalar(select(KeyValue.value).where(KeyValue.key == "activity_paused")) is True
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_activity_queues_healthcheck(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    async with api_env.factory() as session:
        queued = await enqueue_healthcheck_job(session, media_id, library_id)
        job_id = queued.id
        await session.commit()
    response = await api_env.client.post("/api/libraries/scanner/pause")
    assert response.status_code == 200
    service = api_env.app.state.healthchecks
    assert await service.claim_next() is None
    async with api_env.factory() as session:
        queued = await session.get(Job, job_id)
        assert queued is not None and queued.status == JobStatus.QUEUED
    await api_env.client.post("/api/libraries/scanner/resume")
    claimed = await service.claim_next()
    assert claimed is not None and claimed[0] == job_id
    await service._cancel_attempt(*claimed)


@pytest.mark.asyncio
async def test_batch_scan_outcomes(api_env: ApiEnv) -> None:
    source = api_env.lib_dir / "sample.avi"
    source.write_bytes(b"x" * 2048)
    first = (
        await api_env.client.post("/api/libraries", json={"name": "first", "roots": [str(api_env.lib_dir)]})
    ).json()["id"]
    suspended = (
        await api_env.client.post(
            "/api/libraries", json={"name": "suspended", "roots": [str(api_env.lib_dir)], "suspended": True}
        )
    ).json()["id"]
    response = await api_env.client.post("/api/libraries/scan", json={"library_ids": [first, suspended, 999, first]})
    assert response.status_code == 202
    assert [(outcome["library_id"], outcome["status"]) for outcome in response.json()["outcomes"]] == [
        (first, "queued"),
        (suspended, "suspended"),
        (999, "missing"),
    ]
    scanner = api_env.app.state.scanner
    await asyncio.wait_for(asyncio.gather(*scanner._scan_tasks.values()), 3)
    async with api_env.factory() as session:
        files = list(await session.scalars(select(MediaFile)))
        assert [(file.library_id, file.path) for file in files] == [(first, str(source))]
    source.unlink()
    api_env.app.state.scanner.set_paused(paused=True)
    assert (await api_env.client.post("/api/libraries/scan", json={"library_ids": [first]})).status_code == 409
    assert not scanner._scan_tasks
    async with api_env.factory() as session:
        assert await session.scalar(select(MediaFile.path).where(MediaFile.library_id == first)) == str(source)


@pytest.mark.asyncio
async def test_batch_scan_validation(api_env: ApiEnv) -> None:
    for ids in ([], [0], [-1], [True], ["1"]):
        assert (await api_env.client.post("/api/libraries/scan", json={"library_ids": ids})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["probe_failed", "healthy"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_probe_request(api_env: ApiEnv, health: str) -> None:
    library_id, media_id = await seed_probed(api_env)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.probe_id is not None
        media.health_status = health
        await session.commit()
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    assert response.status_code == 202
    assert response.json() == {
        "file_id": media_id,
        "job_id": response.json()["job_id"],
        "kind": "probe",
        "status": "queued",
    }
    assert api_env.app.state.probes._wake.is_set()
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.probe_id is None and media.health_status == "pending"
        assert await session.scalar(select(ProbeRow).where(ProbeRow.media_file_id == media_id)) is None
    service = api_env.app.state.probes
    try:
        await service._fill()
        assert list(service._tasks) == [response.json()["job_id"]]
        await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.probe_id is not None and media.health_status == "healthy"
    finally:
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["paused", "suspended", "active", "recovery", "draining", "failed"])
async def test_probe_request_guards(api_env: ApiEnv, blocked: str) -> None:
    library_id, media_id = await seed_probed(api_env)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        probe_id = media.probe_id
        media.health_status = "healthy"
        if blocked in {"active", "recovery", "draining"}:
            job = await enqueue_file_job(session, media_id, library_id, plan())
            if blocked != "active":
                job.status = "failed"
            if blocked == "recovery":
                attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
                session.add(attempt)
                await session.flush()
                session.add(
                    FinalizationJournal(
                        attempt_id=attempt.id,
                        source_path=media.path,
                        destination_path="destination",
                        partial_path="partial",
                        trash_path="trash",
                        state="prepared",
                    )
                )
            if blocked == "draining":
                api_env.app.state.staging.active_jobs.add(job.id)
        if blocked == "suspended":
            library = await session.get(Library, library_id)
            assert library is not None
            library.suspended = True
        await session.commit()
    if blocked == "paused":
        api_env.app.state.scanner.set_paused(paused=True)
    if blocked == "failed":
        api_env.app.state.probes.failed = True
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    assert response.status_code == 409
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.health_status == "healthy"
        assert media.probe_id == probe_id and await session.get(ProbeRow, probe_id) is not None
    api_env.app.state.staging.active_jobs.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["scanner", "probing"])
async def test_service_failure_state(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, service: str, caplog) -> None:
    state = api_env.app.state
    state.events.min_interval_s = 0
    subscription = state.events.subscribe()
    failure = OSError("Controlled service failure.")

    async def fail() -> None:
        raise failure

    try:
        if service == "scanner":
            monkeypatch.setattr(state.scanner, "_scan_on_start", fail)
            state.scanner.start()
        else:
            monkeypatch.setattr(state.probes, "_run", fail)
            await state.probes.start()
        async with asyncio.timeout(2):
            while event := await subscription.next():
                if event.type == f"{service}.state" and event.payload["failed"]:
                    break
            else:
                pytest.fail("Service did not publish its failure.")
        response = await api_env.client.get("/api/libraries/progress")
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()[service]["failed"] is True
        assert any(record.exc_info is not None and record.exc_info[1] is failure for record in caplog.records)
    finally:
        await asyncio.wait_for(state.scanner.aclose(), 2)
        await asyncio.wait_for(state.probes.close(), 2)
        state.events.unsubscribe(subscription)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_library_binding_create(api_env: ApiEnv) -> None:
    source = api_env.lib_dir / "movie.avi"
    source.write_bytes(b"x" * 2048)
    assert (await api_env.client.put("/api/flows/movies", json={"definition": hevc_flow()})).status_code == 200
    flow_id = (await api_env.client.get("/api/flows/movies")).json()["id"]
    resp = await api_env.client.post(
        "/api/libraries", json={"name": "lib", "roots": [str(api_env.lib_dir)], "flow_id": flow_id}
    )
    assert resp.status_code == 201
    library_id = resp.json()["id"]
    await api_env.app.state.scanner.scan_now(library_id)
    probes = api_env.app.state.probes
    await probes._fill()
    await asyncio.wait_for(asyncio.gather(*probes._tasks.values()), 3)
    async with api_env.factory() as session:
        media_id = await session.scalar(select(MediaFile.id).where(MediaFile.library_id == library_id))
        assert media_id is not None
    response = await api_env.client.post(f"/api/files/{media_id}/transcode")
    assert response.status_code == 202
    outcome = await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result")
    assert outcome["outcome"] == "queued" and outcome["decision"]["status"] == "matched"
    async with api_env.factory() as session:
        job = await session.get(Job, response.json()["job_id"])
        assert job is not None and job.flow_snapshot is not None and job.plan is not None
        assert job.flow_snapshot["id"] == flow_id
        assert job.plan["video"]["encoder"] == "libx265"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_binding_changes_queue(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    assert (await api_env.client.post(f"/api/files/{media_id}/transcode")).status_code == 422
    resp = await api_env.client.put("/api/flows/movies", json={"definition": hevc_flow(quality=19)})
    assert resp.status_code == 200
    flow_id = resp.json()["id"]
    resp = await api_env.client.patch(f"/api/libraries/{library_id}", json={"flow_id": flow_id})
    assert resp.status_code == 200
    response = await api_env.client.post(f"/api/files/{media_id}/transcode")
    assert response.status_code == 202
    outcome = await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result")
    assert outcome["outcome"] == "queued" and outcome["decision"]["status"] == "matched"
    async with api_env.factory() as session:
        job = await session.get(Job, response.json()["job_id"])
        assert job is not None and job.flow_snapshot is not None and job.plan is not None
        assert job.flow_snapshot["id"] == flow_id
        assert job.plan["video"]["rate"]["quality"] == 19


@pytest.mark.asyncio
async def test_detail_probe_raw(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    raw = {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "profile": "High",
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "24000/1001",
                "width": 1920,
                "height": 1080,
                "disposition": {"default": 1},
                "tags": {},
            },
            {
                "index": 1,
                "codec_name": "ac3",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 6,
                "channel_layout": "5.1",
                "disposition": {"forced": 1},
                "tags": {"title": "Surround"},
            },
            {
                "index": 2,
                "codec_name": "ttf",
                "codec_type": "attachment",
                "tags": {"filename": "Subtitle.ttf", "mimetype": "application/x-truetype-font"},
            },
            {
                "index": 3,
                "codec_name": "mjpeg",
                "codec_type": "video",
                "tags": {"filename": "cover.jpg", "mimetype": "image/jpeg"},
            },
        ],
        "format": {"duration": "600.0", "size": "2048", "bit_rate": "27000"},
    }
    async with api_env.factory() as session:
        probe = ProbeRow(media_file_id=media_id, raw_json=json.dumps(raw))
        session.add(probe)
        await session.flush()
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.probe_id = probe.id
        await session.commit()

    listing = (await api_env.client.get("/api/files", params={"library_id": library_id})).json()["items"]
    assert all("probe" not in row for row in listing)

    detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
    assert detail["probe"]["raw"] == raw

    video, audio, font, artwork = detail["probe"]["streams"]
    assert video["title"] is None
    assert audio["title"] == "Surround"
    assert video["fps"] == pytest.approx(23.976, abs=1e-3)
    assert (video["pix_fmt"], video["profile"]) == ("yuv420p", "High")
    assert video["default"] is True and video["forced"] is False
    assert (audio["sample_rate"], audio["channels"], audio["channel_layout"]) == (48000, 6, "5.1")
    assert audio["forced"] is True
    assert (font["filename"], font["mimetype"]) == ("Subtitle.ttf", "application/x-truetype-font")
    assert (artwork["filename"], artwork["mimetype"]) == ("cover.jpg", "image/jpeg")
    assert artwork["disposition_attached_pic"] is True


@pytest.mark.asyncio
async def test_files_list_detail(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        probe = ProbeRow(media_file_id=media_id, raw_json=json.dumps(PROBE_RAW))
        session.add(probe)
        await session.flush()
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.probe_id = probe.id
        media.mtime_ns = 1_767_225_600_000_000_000
        await session.commit()

    resp = await api_env.client.get("/api/files", params={"library_id": library_id})
    assert [f["path"] for f in resp.json()["items"]] == [str(api_env.lib_dir / "movie.avi")]

    resp = await api_env.client.get("/api/files", params={"query": "movie"})
    assert len(resp.json()["items"]) == 1
    resp = await api_env.client.get(
        "/api/files", params={"query": "movie", "library_id": library_id, "limit": 1, "offset": 1}
    )
    assert resp.json() == {"items": [], "pagination": {"limit": 1, "offset": 1, "total": 1}}
    resp = await api_env.client.get("/api/files", params={"transcode_status": "transcoded"})
    assert resp.json()["items"] == []
    assert resp.json()["pagination"]["total"] == 0

    resp = await api_env.client.get(f"/api/files/{media_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["probe"]["duration_s"] == 600.0
    assert detail["probe"]["streams"][0]["codec"] == "h264"
    video, audio = detail["probe"]["streams"]
    assert (video["width"], video["height"]) == (1920, 1080)
    assert audio["width"] is None and audio["height"] is None
    assert detail["jobs"] == []
    assert detail["modified_at"] == "2026-01-01T00:00:00Z"
    assert detail["cooldown_started_at"] is None
    assert detail["cooldown_until"] is None
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        library.cooldown_s = 300
        media.first_seen = T0
        await session.commit()
    listing = (await api_env.client.get("/api/files", params={"library_id": library_id})).json()["items"]
    detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
    for row in (listing[0], detail):
        assert row["active_job_id"] is None
        assert row["cooldown_started_at"] == "2026-01-01T12:00:00Z"
        assert row["cooldown_until"] == "2026-01-01T12:05:00Z"

    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()
        job_id = job.id

    resp = await api_env.client.get(f"/api/files/{media_id}")
    assert [entry["id"] for entry in resp.json()["jobs"]] == [job_id]
    assert resp.json()["transcode_status"] == "staging"
    assert resp.json()["active_job_kind"] == "transcode"
    assert resp.json()["active_job_status"] == "staging"
    assert resp.json()["active_job_id"] == job_id
    assert resp.json()["active_job_progress"] is None
    assert resp.json()["active_job_updated_at"] is not None

    async with api_env.factory() as session:
        job = await get_job(session, job_id)
        assert job is not None
        job.status = JobStatus.HOLD.value
        job.hold_until = T0 + timedelta(minutes=5)
        await session.commit()

    listing = (await api_env.client.get("/api/files", params={"library_id": library_id})).json()["items"]
    detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
    for row in (listing[0], detail):
        assert row["modified_at"] == "2026-01-01T00:00:00Z"
        assert row["active_job_status"] == "hold"
        assert row["cooldown_started_at"] == "2026-01-01T12:00:00Z"
        assert row["cooldown_until"] == "2026-01-01T12:05:00Z"

    # The healthcheck would otherwise collide with the still-active transcode job.
    async with api_env.factory() as session:
        await finish(session, job_id, JobStatus.FAILED, error="done", now=T0)
        await session.commit()

    wakes = {name: 0 for name in ("healthchecks", "staging", "hub")}
    for name in wakes:
        monkeypatch.setattr(
            getattr(api_env.app.state, name), "wake", lambda name=name: wakes.__setitem__(name, wakes[name] + 1)
        )
    resp = await api_env.client.post(f"/api/files/{media_id}/healthcheck")
    assert resp.status_code == 202
    health_job_id = resp.json()["job_id"]
    async with api_env.factory() as session:
        health_job = await get_job(session, health_job_id)
        assert health_job is not None and health_job.kind == "healthcheck" and health_job.status == "queued"
        events = list(
            (
                await session.execute(
                    select(JobEvent.type).where(JobEvent.job_id == health_job_id).order_by(JobEvent.id)
                )
            ).scalars()
        )
    assert events == ["enqueued", "queued", "requested"]
    assert wakes == {"healthchecks": 1, "staging": 0, "hub": 0}

    listing = (await api_env.client.get("/api/files", params={"library_id": library_id})).json()["items"]
    assert listing[0]["active_job_kind"] == "healthcheck"
    assert listing[0]["active_job_status"] == "queued"
    assert listing[0]["active_job_id"] == health_job_id
    assert listing[0]["active_job_progress"] is None
    assert listing[0]["active_job_updated_at"] is not None

    async with api_env.factory() as session:
        health_job = await get_job(session, health_job_id)
        assert health_job is not None
        health_job.status = JobStatus.RUNNING.value
        health_job.progress = {"percent": 42, "attempt_id": 1}
        health_job.updated_at = T0
        await session.commit()

    listing = (await api_env.client.get("/api/files", params={"library_id": library_id})).json()["items"]
    detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
    for row in (listing[0], detail):
        assert row["active_job_status"] == "running"
        assert row["active_job_id"] == health_job_id
        assert row["active_job_progress"] == {"percent": 42, "attempt_id": 1}
        assert row["active_job_updated_at"] == "2026-01-01T12:00:00Z"


@pytest.mark.asyncio
async def test_files_list_last(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    other_library, other_media = await seed_media(api_env, "other.avi")

    def probe_json(size: int, subtitles: int) -> dict:
        streams = [
            {"index": 0, "codec": "h264", "codec_type": "video", "width": 1920, "height": 1080},
            {"index": 1, "codec": "ac3", "codec_type": "audio"},
        ]
        streams.extend({"index": 2 + n, "codec": "subrip", "codec_type": "subtitle"} for n in range(subtitles))
        return {
            "file_path": str(api_env.lib_dir / "movie.avi"),
            "container": "avi",
            "duration_s": 600.0,
            "size_bytes": size,
            "streams": streams,
            "raw": {"padding": "p" * 100_000},
        }

    async with api_env.factory() as session:
        job = Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="succeeded")
        newer = Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="failed")
        healthcheck = Job(media_file_id=media_id, library_id=library_id, kind="healthcheck", status="succeeded")
        other = Job(media_file_id=other_media, library_id=other_library, kind="transcode", status="succeeded")
        session.add_all([job, newer, healthcheck, other])
        await session.flush()
        for job_id, number, result, size in (
            (job.id, 1, "succeeded", 900),
            (newer.id, 1, "succeeded", 800),
            (job.id, 2, "succeeded", 500),
            (job.id, 3, "failed", 400),
            (healthcheck.id, 1, "succeeded", 300),
            (other.id, 1, "succeeded", 200),
        ):
            session.add(
                JobAttempt(
                    job_id=job_id,
                    attempt_number=number,
                    status=result,
                    result=result,
                    source_size=1000,
                    output_size=size,
                    input_probe=probe_json(1000, 3),
                    output_probe=probe_json(size, 1),
                    log_data=b"execution log",
                    created_at=T0,
                )
            )
            await session.flush()
        await session.commit()
        job_id, other_id = job.id, other.id

    loaded: list[JobAttempt] = []

    def track(attempt: JobAttempt, _context: object) -> None:
        loaded.append(attempt)

    event.listen(JobAttempt, "load", track)
    try:
        response = await api_env.client.get("/api/files")
        detail = await api_env.client.get(f"/api/files/{media_id}")
    finally:
        event.remove(JobAttempt, "load", track)
    assert len(loaded) == 6
    for attempt in loaded:
        assert "log_data" in inspect(attempt).unloaded
        assert attempt.input_probe is not None and attempt.output_probe is not None
        assert "raw" not in attempt.input_probe and "raw" not in attempt.output_probe
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["items"]}
    assert set(rows) == {media_id, other_media}
    assert rows[other_media]["last_change"]["job_id"] == other_id
    assert rows[other_media]["last_change"]["after"]["size"] == 200
    change = rows[media_id]["last_change"]
    assert change["job_id"] == job_id
    assert change["attempt_number"] == 2
    assert change["before"]["size"] == 1000
    assert change["after"]["size"] == 500
    assert change["before"]["subtitle"] == 3
    assert change["after"]["subtitle"] == 1
    assert change["before"]["video_codec"] == "h264"

    assert detail.status_code == 200
    last = detail.json()["last_transcode"]
    assert (last["job_id"], last["attempt_number"]) == (job_id, 2)
    assert (last["source_size"], last["output_size"]) == (1000, 500)
    assert last["input_probe"]["size_bytes"] == 1000
    assert last["output_probe"]["size_bytes"] == 500
    assert [row["id"] for row in detail.json()["jobs"]] == [healthcheck.id, newer.id, job_id]
    snapshots = detail.json()["media_snapshots"]
    assert [(row["job_id"], row["attempt_number"], row["kind"]) for row in snapshots] == [
        (newer.id, 1, "original"),
        (newer.id, 1, "result"),
        (job_id, 3, "original"),
        (job_id, 3, "result"),
        (job_id, 2, "original"),
        (job_id, 2, "result"),
        (job_id, 1, "original"),
        (job_id, 1, "result"),
    ]
    assert all("raw" not in row["probe"] for row in snapshots)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["input_probe", "output_probe"])
async def test_files_missing_probes(api_env: ApiEnv, missing: str) -> None:
    library_id, media_id = await seed_media(api_env)
    probe = {
        "file_path": str(api_env.lib_dir / "movie.avi"),
        "container": "avi",
        "duration_s": 600.0,
        "size_bytes": 500,
        "streams": [],
        "raw": {"padding": "p" * 100_000},
    }
    async with api_env.factory() as session:
        job = Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="succeeded")
        session.add(job)
        await session.flush()
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=1,
                status="succeeded",
                result="succeeded",
                input_probe=None if missing == "input_probe" else probe,
                output_probe=None if missing == "output_probe" else probe,
            )
        )
        await session.commit()

    response = await api_env.client.get("/api/files")
    assert response.status_code == 200
    assert "last_change" not in response.json()["items"][0]
    response = await api_env.client.get(f"/api/files/{media_id}")
    assert response.status_code == 200
    last = response.json()["last_transcode"]
    for field in ("input_probe", "output_probe"):
        if missing == field:
            assert last[field] is None
        else:
            assert last[field]["size_bytes"] == 500
            assert "raw" not in last[field]
    snapshots = response.json()["media_snapshots"]
    assert [row["kind"] for row in snapshots] == ["result" if missing == "input_probe" else "original"]
    assert snapshots[0]["probe"]["size_bytes"] == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_file_stage_snapshots(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    current = (await api_env.client.get(f"/api/files/{media_id}")).json()
    assert current["media_snapshots"] == []

    def probe(size: int, codec: str) -> dict:
        return {
            "file_path": "workspace/source.mkv",
            "container": "mkv",
            "duration_s": 90,
            "size_bytes": size,
            "streams": [{"index": 0, "codec_type": "video", "codec": codec}],
            "raw": {"padding": "p" * 100_000},
        }

    async with api_env.factory() as session:
        job = Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="failed")
        session.add(job)
        await session.flush()
        complete = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            status="succeeded",
            result="succeeded",
            input_probe=probe(100, "h264"),
            source_size=5_000_000_000,
            output_probe={**probe(200, "hevc"), "file_path": "library/result.mkv"},
            output_size=4_000_000_000,
            created_at=T0,
            finished_at=T0 + timedelta(minutes=2),
        )
        rejected = JobAttempt(
            job_id=job.id,
            attempt_number=2,
            status="failed",
            result="failed",
            input_probe=None,
            created_at=T0 + timedelta(minutes=3),
        )
        failed = JobAttempt(
            job_id=job.id,
            attempt_number=3,
            status="failed",
            result="failed",
            input_probe=probe(70, "av1"),
            output_probe=None,
            created_at=T0 + timedelta(minutes=4),
        )
        session.add_all([complete, rejected, failed])
        await session.flush()
        stages = []
        for attempt, number, input_probe, output_probe, size, result in (
            (complete, 1, complete.input_probe, probe(80, "h264"), None, "succeeded"),
            (complete, 2, probe(80, "h264"), probe(300, "hevc"), 4_000_000_000, "succeeded"),
            (rejected, 1, probe(200, "hevc"), probe(40, "vp9"), None, "failed"),
            (rejected, 2, probe(40, "vp9"), None, None, None),
        ):
            stage = JobEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                type="stage",
                ts=attempt.created_at + timedelta(seconds=number),
                payload={
                    "number": number,
                    "probe": input_probe,
                    "output_probe": output_probe,
                    "output_size": size,
                    "result": result,
                    "log": "log line\n" * 20_000,
                    "plan": {"unrelated": "payload"},
                },
            )
            session.add(stage)
            stages.append(stage)
        await session.commit()

    detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
    snapshots = detail["media_snapshots"]
    assert [row["id"] for row in snapshots] == [
        f"attempt-{failed.id}-original",
        f"stage-{stages[2].id}",
        f"attempt-{complete.id}-original",
        f"stage-{stages[0].id}",
        f"stage-{stages[1].id}",
    ]
    assert [row["probe"]["size_bytes"] for row in snapshots] == [70, 40, 5_000_000_000, 80, 4_000_000_000]
    assert [row["probe"]["streams"][0]["codec"] for row in snapshots] == ["av1", "vp9", "h264", "h264", "hevc"]
    assert [(row["attempt_number"], row["stage_number"], row["kind"], row["result"]) for row in snapshots] == [
        (3, None, "original", "failed"),
        (2, 1, "stage", "failed"),
        (1, None, "original", "succeeded"),
        (1, 1, "stage", "succeeded"),
        (1, 2, "stage", "succeeded"),
    ]
    assert snapshots[1]["created_at"] == iso_utc(stages[2].ts)
    assert snapshots[2]["created_at"] == iso_utc(complete.created_at)
    assert detail["probe"] == current["probe"] and "raw" in detail["probe"]
    assert all(set(row["probe"]) == {"container", "duration_s", "size_bytes", "streams"} for row in snapshots)
    assert len(json.dumps(snapshots)) < 10_000

    response, outcomes = await delete_requested(api_env, [job.id], bulk=False)
    assert response.status_code == 202 and outcomes[job.id]["outcome"] == "deleted"
    deleted = (await api_env.client.get(f"/api/files/{media_id}")).json()
    assert deleted["media_snapshots"] == []
    assert deleted["probe"] == current["probe"]


@pytest.mark.asyncio
async def test_timestamps_are_utc(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()
        job_id = job.id

    detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
    created = detail["jobs"][0]["created_at"]
    assert created.endswith("Z")
    assert datetime.fromisoformat(created) == T0

    listed = (await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})).json()
    for value in (listed["created_at"], listed["events"][0]["ts"]):
        assert value.endswith("Z")
        assert datetime.fromisoformat(value) == T0


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_scan_publishes_status(api_env: ApiEnv) -> None:
    (api_env.lib_dir / "sample.avi").write_bytes(b"x" * 2048)
    body = {"name": "movies", "roots": [str(api_env.lib_dir)]}
    library_id = (await api_env.client.post("/api/libraries", json=body)).json()["id"]

    bus = api_env.app.state.events
    sub = bus.subscribe()
    try:
        for added, errors in ((1, 0), (0, 1)):
            if not added:
                (api_env.lib_dir / "sample.avi").unlink()
            resp = await api_env.client.post(f"/api/libraries/{library_id}/scan")
            assert resp.status_code == 202
            received = {}
            async with asyncio.timeout(3):
                while not {"file.status", "library.scan"}.issubset(received):
                    event = await sub.next()
                    assert event is not None
                    if event.type in {"file.status", "library.scan"}:
                        received[event.type] = event
            event = received["file.status"]
            assert event.key == f"file.status:{library_id}"
            assert event.payload == {
                "action": "scanned",
                "library_id": library_id,
                "added": added,
                "updated": 0,
                "pruned": 0,
            }
            event = received["library.scan"]
            assert event.payload["status"] == "succeeded"
            assert event.payload["library_id"] == library_id
            assert event.payload["error_count"] == errors
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_rich_job_detail(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env, video_codec="")
    async with api_env.factory() as session:
        probe = (await session.execute(select(ProbeRow).where(ProbeRow.media_file_id == media_id))).scalar_one()
        raw = json.loads(probe.raw_json)
        raw["streams"] = [
            {
                **raw["streams"][0],
                "codec_name": "h264",
                "avg_frame_rate": "24000/1001",
                "bit_rate": "5000000",
                "disposition": {"default": 1},
            },
            {
                **raw["streams"][1],
                "codec_name": "ac3",
                "channel_layout": "5.1",
                "bit_rate": "640000",
                "disposition": {"default": 1},
            },
        ]
        probe.raw_json = json.dumps(raw)
        job = await enqueue_file_job(
            session,
            media_id,
            library_id,
            plan(),
            decision_log=[{"step": "inspector.audio_languages", "outcome": "match"}],
            now=T0,
        )
        job.progress = {"percent": 42.0, "fps": 30.0}
        await session.commit()
        job_id = job.id

    detail = (await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})).json()
    assert detail["decision_log"] == [{"step": "inspector.audio_languages", "outcome": "match"}]
    assert detail["progress"] == {"percent": 42.0, "fps": 30.0}
    assert detail["file"]["video_codec"] == "h264"
    assert detail["file"]["video_fps"] == pytest.approx(24000 / 1001)
    assert detail["file"]["video_bit_rate"] == 5_000_000
    assert detail["file"]["audio_codec"] == "ac3"
    assert detail["file"]["audio_channels"] == 6
    assert detail["file"]["audio_channel_layout"] == "5.1"
    assert detail["file"]["audio_bit_rate"] == 640_000
    row = (await api_env.client.get("/api/jobs")).json()["items"][0]
    assert row["progress"] == {"percent": 42.0, "fps": 30.0}
    listed = row["file"]
    assert set(listed) == {
        "id",
        "library_id",
        "path",
        "size",
        "container",
        "video_codec",
        "width",
        "height",
        "duration_s",
    }
    assert listed["video_codec"] == ""


@pytest.mark.asyncio
async def test_jobs_attempt_payloads(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    probe = {
        "file_path": str(api_env.lib_dir / "movie.avi"),
        "container": "avi",
        "duration_s": 600.0,
        "size_bytes": 2048,
        "streams": [{"index": 0, "codec": "h264", "codec_type": "video", "width": 1920, "height": 1080}],
        "raw": {"padding": "p" * 100_000},
    }
    log = b"log line\n" * 20_000
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        for number in (1, 2):
            attempt = JobAttempt(
                job_id=job.id,
                attempt_number=number,
                node_name=f"node-{number}",
                status="succeeded" if number == 2 else "failed",
                result="succeeded" if number == 2 else "failed",
                input_probe=probe,
                output_probe=probe,
                log_data=log,
            )
            session.add(attempt)
        await session.commit()
        job_id, latest_id = job.id, attempt.id

    loaded: list[JobAttempt] = []

    def track(attempt: JobAttempt, _context: object) -> None:
        loaded.append(attempt)

    event.listen(JobAttempt, "load", track)
    try:
        response = await api_env.client.get("/api/jobs", headers={"Accept-Encoding": "gzip"})
    finally:
        event.remove(JobAttempt, "load", track)
    assert response.status_code == 200
    assert [attempt.id for attempt in loaded] == [latest_id]
    assert "log_data" in inspect(loaded[0]).unloaded
    assert loaded[0].input_probe is not None and loaded[0].output_probe is not None
    assert "raw" not in loaded[0].input_probe
    assert "raw" not in loaded[0].output_probe
    row = response.json()["items"][0]
    summary = row["latest_attempt"]
    assert row["node_name"] == summary["node_name"] == "node-2"
    assert summary["id"] == latest_id and summary["result"] == "succeeded"
    assert "log_data" not in summary
    assert "raw" not in summary["input_probe"]
    assert summary["input_probe"]["size_bytes"] == 2048
    assert summary["output_probe"]["streams"][0]["width"] == 1920
    assert len(response.content) < 10_000
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]
    assert int(response.headers["content-length"]) < len(response.content)
    identity = await api_env.client.get("/api/jobs", headers={"Accept-Encoding": "identity"})
    assert identity.json() == response.json()
    assert "content-encoding" not in identity.headers

    for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/export.json"):
        detail = (await api_env.client.get(path)).json()
        assert len(detail["attempts"]) == 2
        for attempt in detail["attempts"]:
            assert attempt["log_data"] == log.decode()
            assert attempt["input_probe"] == attempt["output_probe"] == probe
    download = await api_env.client.get(f"/api/jobs/{job_id}/attempts/{latest_id}/log")
    assert download.content == log


@pytest.mark.asyncio
async def test_jobs_unfiltered_count(api_env: ApiEnv) -> None:
    ids = []
    for number in range(2):
        library_id, media_id = await seed_media(api_env, f"count-{number}.avi")
        async with api_env.factory() as session:
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
            await session.commit()
            ids.append(job.id)
    response = await api_env.client.get("/api/jobs", params={"limit": 999})
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["items"]} == set(ids)
    assert response.json()["pagination"] == {"limit": 999, "offset": 0, "total": 2}
    response = await api_env.client.get("/api/jobs", params={"limit": 1, "offset": 1})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["items"]] == ids[:1]
    assert response.json()["pagination"] == {"limit": 1, "offset": 1, "total": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_jobs_lifecycle_endpoints(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()
        job_id = job.id

    resp = await api_env.client.get("/api/jobs")
    assert [entry["id"] for entry in resp.json()["items"]] == [job_id]
    assert resp.json()["pagination"]["total"] == 1

    resp = await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})
    assert [event["type"] for event in resp.json()["events"]] == ["enqueued"]
    before_retry = resp.json()

    bus = api_env.app.state.events
    sub = bus.subscribe()
    try:
        resp = await api_env.client.post(f"/api/jobs/{job_id}/bump", json={"priority": 5})
        assert resp.status_code == 200 and resp.json()["priority"] == 5
        bus.flush_bulk()
        received = [await asyncio.wait_for(sub.next(), timeout=1) for _ in range(sub.backlog)]
        event = next(event for event in received if event is not None and event.type == "events.batch")
        inner = next(inner for inner in event.payload["events"] if inner["type"] == "job.status")
        assert inner["payload"]["job_id"] == job_id and inner["payload"]["priority"] == 5
    finally:
        bus.unsubscribe(sub)

    resp = await api_env.client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    async with api_env.factory() as session:
        cancelled = await get_job(session, job_id)
        assert cancelled is not None and cancelled.status == JobStatus.CANCELLED.value

    resp = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert resp.status_code == 202 and resp.json()["status"] == "pending"
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    async with api_env.factory() as session:
        stored_events = list((await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))).scalars())
        for event in stored_events:
            event.ts = T0
        retry_event = (
            await session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id.desc()).limit(1)
            )
        ).scalar_one()
        retry_event.ts = T0 + timedelta(seconds=1)
        await session.commit()
    after_retry = (await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})).json()
    assert after_retry["created_at"] == before_retry["created_at"]
    async with api_env.factory() as session:
        retried = await get_job(session, job_id)
    assert retried is not None
    assert after_retry["updated_at"] == iso_utc(retried.updated_at)
    listed_after_retry = (await api_env.client.get("/api/jobs")).json()["items"][0]
    assert listed_after_retry["updated_at"] == after_retry["updated_at"]
    assert retried is not None and retried.attempts == 0 and retried.finished_at is None


@pytest.mark.asyncio
async def test_job_state(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()
        job_id = job.id
    response = await api_env.client.get(f"/api/jobs/{job_id}/state")
    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "status",
        "priority",
        "progress",
        "error",
        "warning",
        "started_at",
        "finished_at",
    }
    assert response.json()["status"] == "staging"
    cancelled = await api_env.client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    response = await api_env.client.get(f"/api/jobs/{job_id}/state")
    assert response.json()["status"] == "cancelled"
    assert response.json()["finished_at"] is not None
    assert (await api_env.client.get("/api/jobs/999999/state")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_deletes_job_graph(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await finish(session, job.id, JobStatus.SUCCEEDED, now=T0)
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="succeeded")
        session.add(attempt)
        await session.flush()
        event = JobEvent(job_id=job.id, type="done")
        session.add(event)
        await session.flush()
        session.add_all(
            [
                NotificationDelivery(job_id=job.id, job_event_id=event.id, target_name="alerts", status="sent"),
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path="a",
                    destination_path="d",
                    partial_path="b",
                    trash_path="c",
                    state="completed",
                ),
            ]
        )
        await session.commit()
        job_id = job.id

    response, outcomes = await delete_requested(api_env, [job_id], bulk=False)
    assert response.status_code == 202 and outcomes[job_id]["outcome"] == "deleted"
    assert (await api_env.client.get(f"/api/jobs/{job_id}")).status_code == 404
    assert (await api_env.client.delete(f"/api/jobs/{job_id}")).status_code == 404
    async with api_env.factory() as session:
        assert await session.get(MediaFile, media_id) is not None
        assert not (await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))).all()
        assert not (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).all()
        assert not (
            await session.scalars(select(NotificationDelivery).where(NotificationDelivery.job_id == job_id))
        ).all()
        assert not (await session.scalars(select(FinalizationJournal))).all()
    assert (api_env.lib_dir / "movie.avi").exists()


@pytest.mark.asyncio
async def test_delete_rejects_processing(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        job.status = JobStatus.CLAIMED.value
        await session.commit()
        job_id = job.id

    assert (await api_env.client.delete(f"/api/jobs/{job_id}")).status_code == 409
    async with api_env.factory() as session:
        retained = await session.get(Job, job_id)
        assert retained is not None and retained.status == JobStatus.CLAIMED.value
        assert (await session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_bulk_deletes_eligible(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    queued_library, queued_media = await seed_media(api_env, "queued.avi")
    skipped_library, skipped_media = await seed_media(api_env, "skipped.avi")
    active_library, active_media = await seed_media(api_env, "active.avi")
    async with api_env.factory() as session:
        held = await enqueue_file_job(session, media_id, library_id, plan(), now=T0, hold_until=T0)
        queued = await enqueue_healthcheck_job(session, queued_media, queued_library, now=T0)
        skipped = await enqueue_healthcheck_job(session, skipped_media, skipped_library, now=T0)
        skipped.status = JobStatus.SKIPPED.value
        active = await enqueue_healthcheck_job(session, active_media, active_library, now=T0)
        active.status = JobStatus.RUNNING.value
        await session.commit()
        ids = [held.id, queued.id, skipped.id, active.id]

    response, outcomes = await delete_requested(api_env, [*ids, 9999], bulk=True)
    assert response.status_code == 202
    result = response.json()
    assert result["accepted"] == ids[:3]
    assert all(outcome["outcome"] == "deleted" for outcome in outcomes.values())
    assert [entry["id"] for entry in result["failed"]] == [ids[3], 9999]
    async with api_env.factory() as session:
        assert [await session.get(Job, job_id) for job_id in ids[:3]] == [None] * 3
        assert await session.get(Job, ids[3]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_bulk_wakes_kind(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, health_media = await seed_media(api_env, "health.avi")
    transcode_library, transcode_media = await seed_media(api_env, "transcode.avi")
    async with api_env.factory() as session:
        health = await enqueue_healthcheck_job(session, health_media, library_id, now=T0)
        transcode = await enqueue_file_job(session, transcode_media, transcode_library, plan(), now=T0)
        await finish(session, health.id, JobStatus.FAILED, now=T0)
        await finish(session, transcode.id, JobStatus.FAILED, now=T0)
        await session.commit()
    wakes = {name: 0 for name in ("healthchecks", "staging", "hub")}
    for name in wakes:
        monkeypatch.setattr(
            getattr(api_env.app.state, name), "wake", lambda name=name: wakes.__setitem__(name, wakes[name] + 1)
        )
    response = await api_env.client.post(
        "/api/jobs/bulk", json={"action": "retry", "job_ids": [health.id, transcode.id]}
    )
    assert response.status_code == 202 and response.json()["accepted"] == [health.id, transcode.id]
    await requeue_outcome(api_env, health.id)
    await requeue_outcome(api_env, transcode.id)
    async with api_env.factory() as session:
        health_job = await get_job(session, health.id)
        transcode_job = await get_job(session, transcode.id)
        assert health_job is not None and health_job.status == JobStatus.QUEUED.value
        assert transcode_job is not None and transcode_job.status == JobStatus.STAGING.value
    assert wakes == {"healthchecks": 1, "staging": 1, "hub": 1}


@pytest.mark.asyncio
async def test_healthcheck_cancel(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        job.status = JobStatus.RUNNING.value
        await session.commit()
    cancelled: list[int] = []
    monkeypatch.setattr(api_env.app.state.healthchecks, "cancel_job", lambda job_id: cancelled.append(job_id) or True)
    monkeypatch.setattr(api_env.app.state.hub, "cancel_job", lambda _job_id: pytest.fail("hub cancellation called"))
    bus = api_env.app.state.events
    sub = bus.subscribe()
    try:
        response = await api_env.client.post(f"/api/jobs/{job.id}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelling"
        event = await asyncio.wait_for(sub.next(), 2)
        assert event is not None and event.type == "job.cancel_requested"
        assert event.payload == {"job_id": job.id}
    finally:
        bus.unsubscribe(sub)
    assert cancelled == [job.id]
    async with api_env.factory() as session:
        stored = await get_job(session, job.id)
        assert stored is not None and stored.status == JobStatus.RUNNING.value


@pytest.mark.asyncio
async def test_cancel_requires_task(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        job.status = JobStatus.RUNNING.value
        await session.commit()
    monkeypatch.setattr(api_env.app.state.healthchecks, "cancel_job", lambda _job_id: False)
    monkeypatch.setattr(api_env.app.state.hub, "cancel_job", lambda _job_id: pytest.fail("hub cancellation called"))
    response = await api_env.client.post(f"/api/jobs/{job.id}/cancel")
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["healthcheck", "mkv_stats"])
@pytest.mark.parametrize("bulk", [False, True])
async def test_node_inspection_cancel(
    api_env: ApiEnv,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: Literal["healthcheck", "mkv_stats"],
    bulk: bool,
) -> None:
    from common.protocol import Finished
    from common.enums import ExecutionLocation
    from server.metadata_jobs import enqueue_metadata_job
    from tests.hub_helpers import Clock, FakeConn
    from tests.test_hub import Env, hello, hub, serve, wait_for

    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        if kind == "healthcheck":
            job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        else:
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            assert media is not None and library is not None
            job = await enqueue_metadata_job(session, media, library, kind="mkv_stats", priority=1)
        job_id = job.id
        await register_node(session, "node-a", "tok")
        await session.commit()
    env = Env(api_env.factory, api_env.lib_dir, tmp_path / "workspace", tmp_path / "trash", Clock())
    h = hub(env)
    h.healthcheck_service = api_env.app.state.healthchecks
    h.probe_service = api_env.app.state.probes
    h.healthcheck_service.configure(max_concurrency=1, hwaccel=False, location=ExecutionLocation.NODE)
    h.probe_service.configure(max_concurrency=1, max_statistics=1, statistics_location=ExecutionLocation.NODE)
    monkeypatch.setattr(api_env.app.state, "hub", h)
    message = hello()
    message["capabilities"]["mkvpropedit"] = True
    conn = FakeConn([message])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        await wait_for(lambda: bool(conn.messages("job")), conn.changed)
        offer = conn.messages("job")[0]["job"]
        assert offer["job_id"] == job_id and offer["kind"] == kind
        log = h.workspace_dir / offer["log_path"]
        log.write_text("Inspection cancelled by operator.\n")
        if bulk:
            response = await api_env.client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": [job_id]})
            assert response.status_code == 200 and response.json() == {"succeeded": [job_id], "failed": []}
        else:
            response = await api_env.client.post(f"/api/jobs/{job_id}/cancel")
            assert response.status_code == 200 and response.json()["status"] == "cancelling"
        cancellation = conn.messages("cancel")
        assert len(cancellation) == 1
        assert cancellation[0]["attempt_id"] == offer["attempt_id"]
        async with api_env.factory() as session:
            current = await session.get(Job, job_id)
            assert current is not None and current.node_id == h.entries["node-a"].node_db_id
            assert current.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
        await h._handle_finished(
            h.entries["node-a"],
            Finished(
                job_id=job_id, attempt_id=offer["attempt_id"], kind=kind, result="cancelled", log_path=offer["log_path"]
            ),
        )
        async with api_env.factory() as session:
            current = await session.get(Job, job_id)
            attempt = await session.get(JobAttempt, offer["attempt_id"])
            assert current is not None and current.status == JobStatus.CANCELLED
            assert attempt is not None and attempt.status == JobStatus.CANCELLED
            assert attempt.log_data == b"Inspection cancelled by operator.\n"
        assert job_id not in h.entries["node-a"].in_flight
        assert (api_env.lib_dir / "movie.avi").read_bytes() == b"x" * 2048
    finally:
        conn.disconnect()
        await asyncio.wait_for(asyncio.gather(connection, h.aclose()), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transcode", "healthcheck"])
async def test_job_cooldown_bounds(api_env: ApiEnv, kind: str) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        if kind == "transcode":
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        else:
            job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        job.status = JobStatus.HOLD.value
        await session.commit()
        job_id = job.id

    for seen, cooldown_s, deadline in (
        (T0 - timedelta(minutes=2), 300, T0 + timedelta(minutes=3)),
        (T0 + timedelta(minutes=1), 300, T0 + timedelta(minutes=6)),
        (T0 + timedelta(minutes=1), 600, T0 + timedelta(minutes=11)),
    ):
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            job = await session.get(Job, job_id)
            assert media is not None and library is not None and job is not None
            media.first_seen = seen
            library.cooldown_s = cooldown_s
            job.hold_until = deadline
            await session.commit()

        listing = (await api_env.client.get("/api/jobs")).json()["items"]
        detail = (await api_env.client.get(f"/api/jobs/{job_id}")).json()
        export = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
        for row in (listing[0], detail, export):
            assert row["cooldown_started_at"] == iso_utc(seen)
            assert row["hold_until"] == iso_utc(deadline)
            assert row["created_at"] == iso_utc(T0)

        files = (await api_env.client.get("/api/files")).json()["items"]
        file_detail = (await api_env.client.get(f"/api/files/{media_id}")).json()
        for row in (files[0], file_detail):
            assert row["cooldown_started_at"] == iso_utc(seen)
            assert row["cooldown_until"] == iso_utc(deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, has_media", [("hold", False), ("staging", True)])
async def test_job_cooldown_nulls(api_env: ApiEnv, status: str, has_media: bool) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.first_seen = T0 - timedelta(minutes=2)
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        job.status = status
        job.hold_until = T0 + timedelta(minutes=3)
        if not has_media:
            await session.delete(media)
        await session.commit()
        job_id = job.id

    listing = (await api_env.client.get("/api/jobs")).json()["items"]
    detail = (await api_env.client.get(f"/api/jobs/{job_id}")).json()
    export = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    for row in (listing[0], detail, export):
        assert row["cooldown_started_at"] is None
        assert row["hold_until"] == "2026-01-01T12:03:00Z"


@pytest.mark.asyncio
async def test_cancel_held_job(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(
            session, media_id, library_id, plan(), now=T0, hold_until=T0 + timedelta(seconds=300)
        )
        await session.commit()

    listed = (await api_env.client.get("/api/jobs", params={"limit": 1})).json()["items"][0]
    assert listed["status"] == "hold"
    assert listed["hold_until"] is not None and listed["hold_until"].endswith("Z")

    response = await api_env.client.post(f"/api/jobs/{job.id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_claim_race(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()

    async def lose_pending(session, job_id: int, now=None):
        current = await get_job(session, job_id)
        assert current is not None
        current.status = JobStatus.CLAIMED.value
        await session.flush()
        return None

    async def accept_hub(_job_id: int) -> bool:
        return True

    monkeypatch.setattr("server.api.jobs.cancel_pending", lose_pending)
    monkeypatch.setattr(api_env.app.state.hub, "cancel_job", accept_hub)
    response = await api_env.client.post(f"/api/jobs/{job.id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelling"


@pytest.mark.asyncio
async def test_cancel_terminal_race(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()

    async def lose_pending(session, job_id: int, now=None):
        current = await get_job(session, job_id)
        assert current is not None
        current.status = JobStatus.SUCCEEDED.value
        await session.flush()
        return None

    monkeypatch.setattr("server.api.jobs.cancel_pending", lose_pending)
    response = await api_env.client.post(f"/api/jobs/{job.id}/cancel")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_bulk_cancel_races(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    terminal_library, terminal_media_id = await seed_media(api_env, "bulk-terminal.avi")
    async with api_env.factory() as session:
        claimed = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        terminal = await enqueue_file_job(session, terminal_media_id, terminal_library, plan(), now=T0)
        await session.commit()

    async def lose_pending(session, job_id: int, now=None):
        current = await get_job(session, job_id)
        assert current is not None
        current.status = JobStatus.CLAIMED.value if job_id == claimed.id else JobStatus.SUCCEEDED.value
        await session.flush()
        return None

    async def accept_hub(_job_id: int) -> bool:
        return True

    monkeypatch.setattr("server.api.jobs.cancel_pending", lose_pending)
    monkeypatch.setattr(api_env.app.state.hub, "cancel_job", accept_hub)
    bus = api_env.app.state.events
    sub = bus.subscribe()
    try:
        response = await api_env.client.post(
            "/api/jobs/bulk", json={"action": "cancel", "job_ids": [claimed.id, terminal.id]}
        )
        event = await asyncio.wait_for(sub.next(), 2)
        assert event is not None and event.type == "job.cancel_requested"
        assert event.payload == {"job_id": claimed.id}
    finally:
        bus.unsubscribe(sub)
    result = response.json()
    assert result["succeeded"] == [claimed.id]
    assert [entry["id"] for entry in result["failed"]] == [terminal.id]
    async with api_env.factory() as session:
        stored = await get_job(session, claimed.id)
        assert stored is not None and stored.status == JobStatus.CLAIMED.value


@pytest.mark.asyncio
async def test_jobs_filter_and(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        ids = []
        for name in ("batch-a.mkv", "batch-b.mkv", "batch-c.mkv", "show.mkv"):
            path = api_env.lib_dir / name
            path.write_bytes(b"x")
            identity = source_identity(path.stat())
            media = MediaFile(
                library_id=library_id,
                path=str(path),
                size=identity["size"],
                mtime_ns=path.stat().st_mtime_ns,
                source_identity=identity,
                container="mkv",
                medium="video",
                first_seen=T0,
                last_scanned_at=T0,
            )
            session.add(media)
            await session.flush()
            job = await enqueue_file_job(session, media.id, library_id, plan(), now=T0)
            ids.append(job.id)
        for name in ("check-a.mkv", "check-b.mkv"):
            media = MediaFile(
                library_id=library_id,
                path=str(api_env.lib_dir / name),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                first_seen=T0,
                last_scanned_at=T0,
            )
            session.add(media)
            await session.flush()
            await enqueue_healthcheck_job(session, media.id, library_id, now=T0)
        await session.commit()

    resp = await api_env.client.get("/api/jobs", params={"status": "staging"})
    body = resp.json()
    assert body["pagination"]["total"] == 4
    assert [row["id"] for row in body["items"]] == sorted(ids, reverse=True)

    resp = await api_env.client.get("/api/jobs", params={"kind": "healthcheck"})
    body = resp.json()
    assert body["pagination"]["total"] == 2
    assert {row["kind"] for row in body["items"]} == {"healthcheck"}

    resp = await api_env.client.get("/api/jobs", params={"query": "SHOW.MKV"})
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert body["items"][0]["file"]["path"].endswith("show.mkv")

    resp = await api_env.client.get("/api/jobs", params={"status": "staging", "limit": 2, "offset": 2})
    body = resp.json()
    assert body["pagination"] == {"limit": 2, "offset": 2, "total": 4}
    assert [row["id"] for row in body["items"]] == sorted(ids, reverse=True)[2:4]


@pytest.mark.asyncio
async def test_jobs_include_file(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()

    expected = {
        "id": media_id,
        "library_id": library_id,
        "path": str(api_env.lib_dir / "movie.avi"),
        "size": 2048,
        "container": "avi",
        "video_codec": "h264",
        "width": None,
        "height": None,
        "duration_s": None,
    }
    listed = (await api_env.client.get("/api/jobs")).json()["items"][0]
    detailed = (await api_env.client.get(f"/api/jobs/{job.id}")).json()
    assert listed["media_file_id"] == media_id and listed["file"] == expected
    assert detailed["media_file_id"] == media_id and detailed["file"] == expected | {
        "video_fps": None,
        "video_bit_rate": None,
        "audio_codec": None,
        "audio_channels": None,
        "audio_channel_layout": None,
        "audio_bit_rate": None,
    }


@pytest.mark.asyncio
async def test_empty_bump_goes(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    path = api_env.lib_dir / "other.avi"
    path.write_bytes(b"x" * 2048)
    identity = source_identity(path.stat())
    async with api_env.factory() as session:
        other = MediaFile(
            library_id=library_id,
            path=str(path),
            size=identity["size"],
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=identity,
            container="avi",
            video_codec="h264",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(other)
        await session.flush()
        target = await enqueue_file_job(session, media_id, library_id, plan(), priority=0, now=T0)
        await enqueue_file_job(session, other.id, library_id, plan(), priority=50, now=T0)
        await session.commit()

    response = await api_env.client.post(f"/api/jobs/{target.id}/bump", json={})
    assert response.status_code == 200 and response.json()["priority"] == 51


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["single", "bulk"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_cancel_precedes_cleanup(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    ids = []
    for number in range(2 if mode == "bulk" else 1):
        library_id, media_id = await seed_media(api_env, f"cancel-{number}.avi")
        async with api_env.factory() as session:
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
            await session.commit()
            ids.append(job.id)
    pruning, release = asyncio.Event(), asyncio.Event()

    async def prune_job(job_id: int, *, attempt_number: int) -> None:
        pruning.set()
        await release.wait()

    monkeypatch.setattr(api_env.app.state.staging, "prune_job", prune_job)
    bus = api_env.app.state.events
    sub = bus.subscribe()
    operation = asyncio.create_task(
        api_env.client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": ids})
        if mode == "bulk"
        else api_env.client.post(f"/api/jobs/{ids[0]}/cancel")
    )
    try:
        await asyncio.wait_for(pruning.wait(), 3)
        assert not operation.done()
        bus.flush_bulk()
        received = [await asyncio.wait_for(sub.next(), 1) for _ in range(sub.backlog)]
        statuses = [
            inner["payload"]
            for event in received
            if event is not None and event.type == "events.batch"
            for inner in event.payload["events"]
            if inner["type"] == "job.status"
        ]
        assert {payload["job_id"] for payload in statuses} == set(ids)
        assert all(payload["status"] == "cancelled" for payload in statuses)
        async with api_env.factory() as session:
            for job_id in ids:
                current = await get_job(session, job_id)
                assert current is not None and current.status == JobStatus.CANCELLED
        release.set()
        response = await asyncio.wait_for(operation, 3)
        assert response.status_code == 200
        if mode == "bulk":
            assert response.json() == {"succeeded": ids, "failed": []}
        else:
            assert response.json() == {"id": ids[0], "status": "cancelled"}
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), 3)
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_bulk_cancel_mixed(api_env: ApiEnv) -> None:
    library_id, first_media = await seed_media(api_env, "first.avi")
    second_library, second_media = await seed_media(api_env, "second.avi")
    async with api_env.factory() as session:
        queued = await enqueue_file_job(session, first_media, library_id, plan(), now=T0)
        terminal = await enqueue_file_job(session, second_media, second_library, plan(), now=T0)
        await finish(session, terminal.id, JobStatus.SUCCEEDED, now=T0)
        await session.commit()
        queued_id, terminal_id = queued.id, terminal.id

    response = await api_env.client.post(
        "/api/jobs/bulk", json={"action": "cancel", "job_ids": [queued_id, terminal_id, 9999]}
    )
    result = response.json()
    assert result["succeeded"] == [queued_id]
    assert [entry["id"] for entry in result["failed"]] == [terminal_id, 9999]
    async with api_env.factory() as session:
        cancelled = await get_job(session, queued_id)
        assert cancelled is not None and cancelled.status == JobStatus.CANCELLED.value
        events = await session.scalars(select(JobEvent.type).where(JobEvent.job_id == queued_id))
        assert "cancelled" in list(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_bulk_retry_states(api_env: ApiEnv) -> None:
    ids = []
    media = [
        (status, *(await seed_media(api_env, f"{status.value}.avi")))
        for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED)
    ]
    active_library, active_media = await seed_media(api_env, "active.avi")
    async with api_env.factory() as session:
        for status, library_id, media_id in media:
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
            await finish(session, job.id, status, now=T0)
            ids.append(job.id)
        active = await enqueue_file_job(session, active_media, active_library, plan(), now=T0)
        ids.append(active.id)
        await session.commit()

    response = await api_env.client.post("/api/jobs/bulk", json={"action": "retry", "job_ids": ids})
    body = response.json()
    assert response.status_code == 202 and body["accepted"] == ids[:3]
    assert [entry["id"] for entry in body["failed"]] == ids[3:]
    assert (await requeue_outcome(api_env, ids[0]))["outcome"] == "failed"
    for job_id in ids[1:3]:
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    async with api_env.factory() as session:
        for job_id in ids[1:3]:
            job = await get_job(session, job_id)
            assert job is not None and job.status == JobStatus.STAGING.value
        original = await get_job(session, ids[0])
        assert original is not None and original.status == JobStatus.SUCCEEDED.value


async def seed_published(env: ApiEnv) -> tuple[int, int]:
    library_id, media_id = await seed_media(env)
    async with env.factory() as session:
        flow = Flow(name="current-flow", version=3, definition=json.dumps(graph_flow("transcode.current")))
        session.add(flow)
        await session.flush()
        library = await session.get(Library, library_id)
        assert library is not None
        library.flow_id = flow.id
        job = await enqueue_file_job(
            session, media_id, library_id, plan(), flow_snapshot={"name": "original-flow"}, now=T0
        )
        job.attempts = 1
        session.add(
            JobAttempt(
                job_id=job.id, attempt_number=1, status="succeeded", result="succeeded", log_data=b"original log"
            )
        )
        await finish(session, job.id, JobStatus.SUCCEEDED, now=T0)
        await session.commit()
        return job.id, media_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_retry_preserves_publication(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    job_id, _ = await seed_published(api_env)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = status
        await session.commit()
    before = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()

    async def evaluate(*_args):
        return FlowResult(status="matched", plan=FFmpegPlan(video=VideoTarget(encoder="libx265")))

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202, response.text
    assert response.json() == {"id": job_id, "status": "pending"}
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    after = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert after["attempts"] == before["attempts"]
    assert after["events"][: len(before["events"])] == before["events"]
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.attempts == 1
        assert job.plan is not None and job.plan["video"]["encoder"] == "libx265"
        assert job.flow_snapshot is not None and job.flow_snapshot["name"] == "current-flow"
        retried = await session.scalar(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "retried"))
        assert retried is not None and retried.payload["previous"]["flow_snapshot"] == {"name": "original-flow"}
        assert retried.payload["previous"]["plan"] == plan().model_dump(mode="json")
        assert await session.scalar(select(func.count()).select_from(Job).where(Job.kind == "transcode")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_unavailable_source(api_env: ApiEnv) -> None:
    job_id, _ = await seed_published(api_env)
    (api_env.lib_dir / "movie.avi").unlink()
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_probe_changed(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    path = api_env.lib_dir / "movie.avi"
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan())
        await finish(session, job.id, JobStatus.FAILED)
        job.attempts = 1
        flow = Flow(name="current-flow", definition=json.dumps(graph_flow("transcode.current")))
        session.add(flow)
        await session.flush()
        library = await session.get(Library, library_id)
        assert library is not None
        library.flow_id = flow.id
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=1,
                status="failed",
                input_probe=probe_from_raw(path, path.stat().st_size, PROBE_RAW).model_dump(mode="json"),
            )
        )
        raw = json.loads(json.dumps(PROBE_RAW))
        raw["streams"][0]["codec_name"] = "hevc"
        probe = ProbeRow(media_file_id=media_id, raw_json=json.dumps(raw))
        session.add(probe)
        await session.flush()
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.probe_id = probe.id
        await session.commit()

    async def evaluate(_definition, ctx, _host):
        assert ctx.probe.real_video[0].codec == "h264"
        return FlowResult(status="matched", plan=FFmpegPlan(video=VideoTarget(encoder="libx265")))

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 202, response.text
    assert response.json() == {"id": job.id, "status": "pending"}
    assert (await requeue_outcome(api_env, job.id))["outcome"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_refreshes_catalog(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library_id, media_id = await seed_media(api_env)
    path = api_env.lib_dir / "movie.avi"
    original_size = path.stat().st_size
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await finish(session, job.id, JobStatus.FAILED, now=T0)
        flow = Flow(name="current-flow", definition=json.dumps(graph_flow("transcode.current")))
        session.add(flow)
        await session.flush()
        library = await session.get(Library, library_id)
        assert library is not None
        library.flow_id = flow.id
        await session.commit()
    path.write_bytes(b"changed")
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.size = path.stat().st_size
        media.mtime_ns = path.stat().st_mtime_ns
        await session.commit()

    async def evaluate(_definition, ctx, _host):
        assert ctx.probe.size_bytes == len(b"changed")
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job.id}/retry")
    assert response.status_code == 202, response.text
    assert response.json()["id"] == job.id
    assert (await requeue_outcome(api_env, job.id))["outcome"] == "queued"
    async with api_env.factory() as session:
        stored = await session.get(Job, job.id)
        assert stored is not None and stored.status == "staging"
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        captures = list(
            (
                await session.scalars(
                    select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.type == "enqueued").order_by(JobEvent.id)
                )
            ).all()
        )
        assert [event.payload["source"]["size"] for event in captures] == [original_size, len(b"changed")]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cancel_local", "cancel_node", "failure"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_manual_metadata_prerequisite(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    from common.media.probe import ProbeData
    from common.protocol import Finished, HealthResult
    from server.plugins.host import EvaluatedDecision
    from tests.fakes.ffprobe import DEFAULT_PROBE
    from tests.hub_helpers import FakeConn
    from tests.test_hub import hello, serve, wait_for

    library_id, media_id = await seed_media(api_env, "movie.mkv")
    cancelled = outcome.startswith("cancel")
    async with api_env.factory() as session:
        flow = Flow(name="manual", definition=json.dumps(graph_flow(("transcode.video", {"quality": 19}))))
        default = Flow(name="default", definition=json.dumps(hevc_flow(quality=30)))
        session.add_all([flow, default])
        await session.flush()
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        library.flow_id, library.mkv_stats, library.healthcheck_enabled = default.id, True, cancelled
        library.cooldown_s = 0
        media.container = "mkv"
        await register_node(session, "node-a", "tok")
        await session.commit()
    await api_env.app.state.settings.update({"mkv_stats_location": "node"})
    healthchecks = api_env.app.state.healthchecks
    healthchecks.ffmpeg_bin = str(FAKE_FFPROBE.with_name("ffmpeg.py"))
    await api_env.app.state.settings.update({"healthcheck_location": "node" if outcome == "cancel_node" else "local"})
    evaluated: list[ProbeData] = []
    original_evaluate = api_env.app.state.plugin_host.evaluate

    async def decide(_plugin, ctx, _inputs):
        assert _inputs["quality"] == 19
        evaluated.append(ProbeData.model_validate(ctx["probe"]))
        return EvaluatedDecision(type="plan", plan=plan().model_dump(mode="json"))

    monkeypatch.setattr(api_env.app.state.plugin_host, "evaluate", decide)
    h = api_env.app.state.hub
    h.workspace_dir = api_env.app.state.workspace_dir
    conn = FakeConn([hello(mkvpropedit=True)])
    connection = await serve(h, conn)
    try:
        # The selected request must survive its own refresh observing a new size.
        (api_env.lib_dir / "movie.mkv").write_bytes(b"updated input" * 100)
        response = await api_env.client.post(
            f"/api/files/{media_id}/transcode",
            params={"refresh": True},
            json={"flow_id": flow.id, "expected_version": flow.version},
        )
        assert response.status_code == 202, response.text
        requested_id = response.json()["job_id"]
        await asyncio.wait_for(api_env.app.state.jobs._scan(), 5)
        waiting = (await api_env.client.get(f"/api/jobs/{requested_id}")).json()
        assert waiting["status"] == "pending" and waiting["error"] is None
        assert waiting["warning"] == "Waiting for MKV statistics."
        assert evaluated == []
        await api_env.app.state.jobs.close()
        async with api_env.factory() as session:
            selected = await session.get(Flow, flow.id)
            library = await session.get(Library, library_id)
            assert selected is not None and library is not None
            selected.name, selected.definition, selected.version = "edited", json.dumps(hevc_flow(quality=35)), 2
            library.flow_id = None
            await session.commit()
            await session.delete(selected)
            await session.commit()
        await api_env.app.state.jobs.start()
        await asyncio.wait_for(api_env.app.state.jobs._scan(), 5)
        assert await h.dispatch_once() == 1
        await wait_for(lambda: bool(conn.messages("job")), conn.changed)
        offer = conn.messages("job")[0]["job"]
        assert offer["kind"] == "mkv_stats" and offer["job_id"] != requested_id
        assert evaluated == []
        if cancelled:
            cancel_response = await api_env.client.post(f"/api/jobs/{requested_id}/cancel")
            assert cancel_response.status_code == 202, cancel_response.text
            assert (await requeue_outcome(api_env, requested_id, event_type="request_result"))["outcome"] == "cancelled"

        (h.workspace_dir / offer["log_path"]).write_text("MKV statistics completed.\n")
        if outcome == "failure":
            report = Finished(
                job_id=offer["job_id"],
                attempt_id=offer["attempt_id"],
                kind="mkv_stats",
                result="failed",
                error="Statistics edit failed.",
                log_path=offer["log_path"],
            )
        else:
            output = h.workspace_dir / offer["output_path"]
            output.write_bytes(b"statistics output")
            raw = json.loads(json.dumps(DEFAULT_PROBE))
            raw["streams"][0]["bit_rate"] = "4000000"
            Path(f"{output}.probe.json").write_text(json.dumps(raw))
            # The fake probe reads path sidecars instead of the published Matroska tags.
            (api_env.lib_dir / "movie.mkv.probe.json").write_text(json.dumps(raw))
            reported = ProbeData.model_validate(offer["probe"]).model_copy(
                update={"file_path": offer["output_path"], "size_bytes": output.stat().st_size}
            )
            report = Finished(
                job_id=offer["job_id"],
                attempt_id=offer["attempt_id"],
                kind="mkv_stats",
                result="succeeded",
                output_path=offer["output_path"],
                output_probe=reported,
                raw_probe=raw,
                log_path=offer["log_path"],
            )
        await asyncio.wait_for(h._handle_finished(h.entries["node-a"], report), 5)
        async with api_env.factory() as session:
            prerequisite = await session.get(Job, offer["job_id"])
            assert prerequisite is not None
            assert prerequisite.status == ("failed" if outcome == "failure" else "succeeded"), prerequisite.error
            media = await session.get(MediaFile, media_id)
            assert media is not None
            if outcome != "failure":
                assert media.probe_id is not None
        if not cancelled:
            completed = await requeue_outcome(api_env, requested_id, event_type="request_result")
            assert completed["outcome"] == ("queued" if outcome == "success" else "failed")
        else:
            await api_env.app.state.jobs._scan()
        detail = (await api_env.client.get(f"/api/jobs/{requested_id}")).json()
        assert detail["id"] == requested_id
        assert detail["status"] == ("cancelled" if cancelled else "staging" if outcome == "success" else "failed")
        if outcome == "success":
            assert len(evaluated) == 1 and evaluated[0].real_video[0].bit_rate == 4000000
        else:
            assert evaluated == []
            if outcome == "failure":
                assert "Statistics edit failed." in detail["error"]
        async with api_env.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Job).where(Job.kind == "transcode")) == 1
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            assert media is not None and library is not None and media.transcode_decided
            library.flow_id = default.id
            await session.commit()
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(JobEvent)
                    .where(JobEvent.job_id == requested_id, JobEvent.type == "request_result")
                )
                == 1
            )
        if cancelled:
            if outcome == "cancel_local":
                claimed = await healthchecks.claim_next()
                assert claimed is not None
                await asyncio.wait_for(healthchecks.run(*claimed), 5)
            else:
                assert await h.dispatch_once() == 1
                await wait_for(lambda: len(conn.messages("job")) == 2, conn.changed)
                check = conn.messages("job")[1]["job"]
                assert check["kind"] == "healthcheck"
                (h.workspace_dir / check["log_path"]).write_text("Healthy.\n")
                await asyncio.wait_for(
                    h._handle_finished(
                        h.entries["node-a"],
                        Finished(
                            job_id=check["job_id"],
                            attempt_id=check["attempt_id"],
                            kind="healthcheck",
                            result="succeeded",
                            health=HealthResult(healthy=True),
                            raw_probe=raw,
                            log_path=check["log_path"],
                        ),
                    ),
                    5,
                )
            async with api_env.factory() as session:
                media = await session.get(MediaFile, media_id)
                assert media is not None and media.health_status == "healthy" and media.transcode_decided
        assert await api_env.app.state.flow_enqueue._pass() == 0
        if outcome == "cancel_local":
            # An operator's retry is a new request, unlike the automatic handoff.
            response = await api_env.client.post(f"/api/jobs/{claimed[0]}/retry")
            assert response.status_code == 202, response.text
            assert (await requeue_outcome(api_env, claimed[0]))["outcome"] == "queued"
            retried = await healthchecks.claim_next()
            assert retried is not None and retried[0] == claimed[0]
            await asyncio.wait_for(healthchecks.run(*retried), 5)
            monkeypatch.setattr(api_env.app.state.plugin_host, "evaluate", original_evaluate)
            assert await api_env.app.state.flow_enqueue._pass() == 1
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh", "decision"),
    [(False, "matched"), (True, "matched"), (True, "skipped"), (True, "vetoed"), (True, "error")],
)
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_evaluation_catalog(
    api_env: ApiEnv,
    monkeypatch: pytest.MonkeyPatch,
    refresh: bool,
    decision: str,
) -> None:
    from server.fs.library import LibraryFS

    job_id, media_id = await seed_published(api_env)
    path = api_env.lib_dir / "movie.avi"
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        library = await session.get(Library, media.library_id)
        assert library is not None
        await LibraryFS(
            library.id,
            sessionmaker=api_env.factory,
            ffprobe_bin=FAKE_FFPROBE,
            mkvpropedit_bin="unused",
            workspace_dir=api_env.app.state.workspace_dir,
        ).refresh_current(media_id)

    async def evaluate(*args):
        path.write_bytes(b"replacement")
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None
            library = await session.get(Library, media.library_id)
            assert library is not None
            await LibraryFS(
                library.id,
                sessionmaker=api_env.factory,
                ffprobe_bin=FAKE_FFPROBE,
                mkvpropedit_bin="unused",
                workspace_dir=api_env.app.state.workspace_dir,
            ).refresh_current(media_id)
        return FlowResult(status=decision, plan=plan() if decision == "matched" else None)

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/files/{media_id}/transcode", params={"refresh": refresh})
    assert response.status_code == 202
    new_id = response.json()["job_id"]
    assert (await requeue_outcome(api_env, new_id, event_type="request_result"))["outcome"] == "failed"
    async with api_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 2
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["matched", "skipped", "vetoed", "error"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_retry_current_flow(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, decision: str, enabled: bool
) -> None:
    job_id, media_id = await seed_published(api_env)
    healthchecks = HealthcheckService(
        api_env.factory,
        tmp_path / "health-workspace",
        str(FAKE_FFPROBE.with_name("ffmpeg.py")),
        str(FAKE_FFPROBE),
    )
    monkeypatch.setattr(api_env.app.state, "healthchecks", healthchecks)
    monkeypatch.setattr(api_env.app.state.jobs, "healthchecks", healthchecks)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.health_status = "healthy"
        library = await session.get(Library, media.library_id)
        assert library is not None
        library.healthcheck_enabled = enabled
        await session.commit()
    before = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    path = api_env.lib_dir / "movie.avi"
    path.write_bytes(b"current file")
    probe_log = tmp_path / "probes.log"
    monkeypatch.setenv("FAKE_FFPROBE_LOG", str(probe_log))
    seen = []
    decision_reason = "fixture reason"

    async def current_flow(definition, ctx, _host):
        seen.append(ctx.probe)
        assert definition.nodes[0].plugin == "transcode.current"
        assert ctx.probe.size_bytes == len(b"current file")
        assert ctx.probe.real_video[0].codec == "h264"
        return FlowResult(
            status=decision,
            reason="" if decision == "skipped" else decision_reason,
            steps=[StepOutcome(node="n0", decision="skip", reason=decision_reason)] if decision == "skipped" else [],
            plan=FFmpegPlan(video=VideoTarget(encoder="libx265")) if decision == "matched" else None,
        )

    monkeypatch.setattr("server.flows.evaluation.evaluate", current_flow)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202, response.text
    outcome = await requeue_outcome(api_env, job_id)
    assert outcome["job_id"] == job_id
    assert outcome["decision"]["status"] == decision
    assert outcome["decision"]["reason"] == decision_reason
    assert probe_log.read_text().splitlines() == [str(path)]
    assert len(seen) == 1
    after = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert after["attempts"] == before["attempts"]
    assert after["events"][: len(before["events"])] == before["events"]
    async with api_env.factory() as session:
        original = await session.get(Job, job_id)
        assert original is not None and original.media_file_id == media_id
        assert original.status == ("staging" if decision == "matched" else "succeeded")
        assert original.attempts == 1
        if decision == "matched":
            assert original.flow_snapshot is not None and original.flow_snapshot["name"] == "current-flow"
            assert original.flow_snapshot["version"] == 3
            assert original.plan is not None and original.plan["video"]["encoder"] == "libx265"
        else:
            assert original.flow_snapshot == {"name": "original-flow"}
            evaluated = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "requeue_evaluated")
            )
            assert evaluated is not None and evaluated.payload["status"] == decision
            assert evaluated.payload["flow_snapshot"]["name"] == "current-flow"
        assert await session.scalar(select(func.count()).select_from(Job).where(Job.kind == "transcode")) == 1
        health_jobs = list((await session.scalars(select(Job).where(Job.kind == "healthcheck"))).all())
        assert len(health_jobs) == int(enabled and decision != "matched")
        media = await session.get(MediaFile, media_id)
        assert (
            media is not None
            and media.health_status == ("pending" if enabled else "healthy")
            and media.probe_id is not None
        )
    assert healthchecks._wake.is_set() is (enabled and decision != "matched")
    claimed = await healthchecks.claim_next()
    if not enabled or decision == "matched":
        assert claimed is None
    else:
        assert claimed is not None and claimed[0] == health_jobs[0].id
        await asyncio.wait_for(healthchecks.run(*claimed), 3)
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.health_status == "healthy"
            assert media.transcode_decided == (decision == "skipped")
            completed = await session.get(Job, claimed[0])
            assert completed is not None and completed.status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_without_plan(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id, media_id = await seed_published(api_env)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        library = await session.get(Library, media.library_id)
        assert library is not None
        library.healthcheck_enabled = True
        await session.commit()
    (api_env.lib_dir / "movie.avi").write_bytes(b"changed source")

    async def evaluate(*_args):
        return FlowResult(status="matched", reason="no transcode requested")

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    outcome = await requeue_outcome(api_env, job_id)
    assert outcome["outcome"] == "not_queued" and outcome["decision"]["status"] == "matched"
    service = api_env.app.state.healthchecks
    assert service._wake.is_set()
    claimed = await service.claim_next()
    assert claimed is not None
    async with api_env.factory() as session:
        healthcheck = await session.get(Job, claimed[0])
        assert healthcheck is not None and healthcheck.kind == "healthcheck"
        assert healthcheck.media_file_id == media_id
        assert await session.scalar(select(func.count()).select_from(Job)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_fresh_attempt(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.hub_helpers import Clock, FakeConn
    from tests.test_hub import Env, finished, hello, hub, materialize_offer, serve

    job_id, _ = await seed_published(api_env)
    before = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()["attempts"]
    path = api_env.lib_dir / "movie.avi"
    path.write_bytes(b"current input")
    evaluations = []

    async def evaluate(_definition, ctx, _host):
        evaluations.append(ctx.probe)
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    env = Env(api_env.factory, api_env.lib_dir, tmp_path / "workspace", tmp_path / "trash", Clock())
    h = hub(env, max_attempts=3)
    monkeypatch.setattr(api_env.app.state, "hub", h)
    monkeypatch.setattr(api_env.app.state, "staging", h.staging)
    monkeypatch.setattr(api_env.app.state, "workspace_dir", h.workspace_dir)
    monkeypatch.setattr(api_env.app.state.jobs, "hub", h)
    monkeypatch.setattr(api_env.app.state.jobs, "staging", h.staging)
    monkeypatch.setattr(api_env.app.state.jobs, "workspace_dir", h.workspace_dir)
    async with api_env.factory() as session:
        await register_node(session, "node-a", "tok")
        await session.commit()
    conn = FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 202 and response.json()["id"] == job_id
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
        assert len(evaluations) == 1
        assert (await api_env.client.post(f"/api/jobs/{job_id}/cancel")).status_code == 200
        response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 202 and response.json() == {"id": job_id, "status": "pending"}
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
        assert len(evaluations) == 1
        for number in (2, 3, 4):
            assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
            await asyncio.wait_for(h.staging.wait_idle(), 3)
            assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
            offer = conn.messages("job")[-1]["job"]
            assert offer["job_id"] == job_id
            assert (h.workspace_dir / offer["source_path"]).read_bytes() == b"current input"
            assert offer["probe"]["size_bytes"] == len(b"current input")
            materialize_offer(h, offer)
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"], finished(job_id, offer["attempt_id"], offer["log_path"], "failed")
                ),
                3,
            )
            async with api_env.factory() as session:
                current = await session.get(Job, job_id)
                assert current is not None and current.attempts == number
                assert current.status == ("failed" if number == 4 else "staging")
        after = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()["attempts"]
        assert after[:1] == before
        assert [attempt["attempt_number"] for attempt in after] == [1, 2, 3, 4]
        async with api_env.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Job).where(Job.kind == "transcode")) == 1
    finally:
        conn.disconnect()
        await asyncio.wait_for(asyncio.gather(connection, h.aclose()), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["matched", "skipped"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_retry_job_changed(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, decision: str) -> None:
    job_id, _ = await seed_published(api_env)

    async def evaluate(*_args):
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.status = JobStatus.FAILED
            await session.commit()
        return FlowResult(status=decision, plan=plan() if decision == "matched" else None)

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "failed"
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.FAILED
        assert job.flow_snapshot == {"name": "original-flow"}
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["healthy", "corrupt"])
@pytest.mark.parametrize("decision", ["matched", "skipped", "vetoed", "error"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_keeps_health(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, health: str, decision: str) -> None:
    api_env.app.state.healthchecks._wake.clear()
    job_id, media_id = await seed_published(api_env)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.first_seen = T0
        media.health_status = health
        await session.commit()

    async def evaluate(*_args):
        return FlowResult(status=decision, reason="current rules", plan=plan() if decision == "matched" else None)

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    assert (await requeue_outcome(api_env, job_id))["decision"]["status"] == decision
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.health_status == health
        assert not list((await session.scalars(select(Job).where(Job.kind == "healthcheck"))).all())
    assert not api_env.app.state.healthchecks._wake.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["skipped", "vetoed", "error"])
@pytest.mark.parametrize("blocker", ["active", "suspended"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_health_checked(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, decision: str, blocker: str
) -> None:
    api_env.app.state.healthchecks._wake.clear()
    job_id, media_id = await seed_published(api_env)
    (api_env.lib_dir / "movie.avi").write_bytes(b"changed source")

    async def evaluate(*_args):
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None
            library = await session.get(Library, media.library_id)
            assert library is not None
            if blocker == "active":
                await enqueue_healthcheck_job(session, media.id, library.id)
            else:
                library.suspended = True
            await session.commit()
        return FlowResult(status=decision, reason="current rules")

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "failed"
    assert not api_env.app.state.healthchecks._wake.is_set()
    async with api_env.factory() as session:
        health_jobs = list((await session.scalars(select(Job).where(Job.kind == "healthcheck"))).all())
        assert len(health_jobs) == 0
        assert await session.scalar(select(func.count()).select_from(Job)) == 1 + len(health_jobs)
        original = await session.get(Job, job_id)
        assert original is not None and original.status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_failed_probe(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    api_env.app.state.healthchecks._wake.clear()
    job_id, media_id = await seed_published(api_env)
    path = api_env.lib_dir / "movie.avi"
    path.write_bytes(b"changed source")
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", path.name)
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "failed"
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.health_status == "probe_failed"
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
    assert not api_env.app.state.healthchecks._wake.is_set()
    assert await api_env.app.state.healthchecks.claim_next() is None
    await api_env.app.state.probes._fill()
    assert not api_env.app.state.probes._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker", ["missing", "flow", "probe", "active", "recovery", "suspended", "outside", "symlink"]
)
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_retry_prechecks(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blocker: str) -> None:
    job_id, media_id = await seed_published(api_env)
    path = api_env.lib_dir / "movie.avi"
    if blocker == "missing":
        path.unlink()
    elif blocker == "probe":
        monkeypatch.setenv("FAKE_FFPROBE_FAIL", path.name)
    elif blocker == "symlink":
        target = tmp_path / "target.avi"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        library = await session.get(Library, media.library_id)
        assert library is not None
        if blocker == "flow":
            library.flow_id = None
        elif blocker == "suspended":
            library.suspended = True
        elif blocker == "outside":
            library.roots = [str(tmp_path)]
        elif blocker == "active":
            await enqueue_healthcheck_job(session, media_id, library.id)
        elif blocker == "recovery":
            attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
            assert attempt is not None
            session.add(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path=str(path),
                    destination_path=str(path),
                    partial_path=str(path) + ".partial",
                    trash_path=str(tmp_path / "trash"),
                    state="prepared",
                )
            )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(Job))
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    if blocker in {"active", "recovery", "suspended"}:
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 202, response.text
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "failed"
    async with api_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == count
        original = await session.get(Job, job_id)
        assert original is not None and original.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.usefixtures("mock_ffprobe")
async def test_bulk_refreshes_retry(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id, _ = await seed_published(api_env)
    library_id, media_id = await seed_media(api_env, "fresh.avi")
    async with api_env.factory() as session:
        fresh = await enqueue_file_job(session, media_id, library_id, plan())
        await finish(session, fresh.id, JobStatus.FAILED)
        await session.commit()

    async def skip(*_args):
        return FlowResult(status="skipped", reason="already meets rules")

    monkeypatch.setattr("server.flows.evaluation.evaluate", skip)
    response = await api_env.client.post(
        "/api/jobs/bulk", json={"action": "retry", "job_ids": [job_id, fresh.id, 9999]}
    )
    result = response.json()
    assert response.status_code == 202 and result["accepted"] == [job_id, fresh.id]
    assert [entry["id"] for entry in result["failed"]] == [9999]
    assert result["failed"][0]["detail"]
    assert (await requeue_outcome(api_env, job_id))["decision"]["status"] == "skipped"
    assert (await requeue_outcome(api_env, fresh.id))["outcome"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_retry_file_conflict(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library, media = await seed_media(api_env, "retry-conflict.avi")
    second_library, second_media = await seed_media(api_env, "retry-valid.avi")
    async with api_env.factory() as session:
        old = await enqueue_file_job(session, media, library, plan(), now=T0)
        await finish(session, old.id, JobStatus.FAILED, now=T0)
        health = await enqueue_healthcheck_job(session, media, library, now=T0)
        valid = await enqueue_file_job(session, second_media, second_library, plan(), now=T0)
        await finish(session, valid.id, JobStatus.FAILED, now=T0)
        await session.commit()

    async def miss(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.queue.store.active_job_for_file", miss)
    response = await api_env.client.post(f"/api/jobs/{old.id}/retry")
    assert response.status_code == 409
    response = await api_env.client.post("/api/jobs/bulk", json={"action": "retry", "job_ids": [old.id, valid.id]})
    assert response.status_code == 202
    assert response.json()["accepted"] == [valid.id]
    assert [entry["id"] for entry in response.json()["failed"]] == [old.id]
    assert (await requeue_outcome(api_env, valid.id))["outcome"] == "queued"
    async with api_env.factory() as session:
        for identifier, status in (
            (old.id, JobStatus.FAILED),
            (health.id, JobStatus.QUEUED),
            (valid.id, JobStatus.STAGING),
        ):
            stored = await get_job(session, identifier)
            assert stored is not None and stored.status == status


@pytest.mark.asyncio
async def test_bulk_duplicates_order(api_env: ApiEnv) -> None:
    first_library, first_media = await seed_media(api_env, "first.avi")
    second_library, second_media = await seed_media(api_env, "second.avi")
    async with api_env.factory() as session:
        first = await enqueue_file_job(session, first_media, first_library, plan(), now=T0)
        second = await enqueue_file_job(session, second_media, second_library, plan(), now=T0)
        await session.commit()
        ids = [first.id, second.id]
    response = await api_env.client.post(
        "/api/jobs/bulk", json={"action": "cancel", "job_ids": [ids[1], ids[1], ids[0]]}
    )
    assert response.json()["succeeded"] == [ids[1], ids[0]]


@pytest.mark.asyncio
async def test_bulk_bump_order(api_env: ApiEnv) -> None:
    jobs = []
    for index, priority in enumerate((5, 3, 1)):
        library_id, media_id = await seed_media(api_env, f"bump-{index}.avi")
        async with api_env.factory() as session:
            job = await enqueue_file_job(session, media_id, library_id, plan(), priority=priority, now=T0)
            await session.commit()
            jobs.append(job.id)
    library_id, media_id = await seed_media(api_env, "done.avi")
    async with api_env.factory() as session:
        done = await enqueue_file_job(session, media_id, library_id, plan(), priority=20, now=T0)
        await finish(session, done.id, JobStatus.SUCCEEDED, now=T0)
        await session.commit()
    response = await api_env.client.post(
        "/api/jobs/bulk", json={"action": "bump", "job_ids": [jobs[2], jobs[0], 9999, done.id]}
    )
    result = response.json()
    assert result["succeeded"] == [jobs[2], jobs[0]]
    assert [entry["id"] for entry in result["failed"]] == [9999, done.id]
    async with api_env.factory() as session:
        rows = list((await session.execute(select(Job).where(Job.id.in_(jobs)))).scalars())
        priorities = {row.id: row.priority for row in rows}
        assert priorities[jobs[0]] > priorities[jobs[2]] > 5


@pytest.mark.asyncio
async def test_bulk_validation(api_env: ApiEnv) -> None:
    for job_ids in ([], [1] * 1000, [0]):
        response = await api_env.client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": job_ids})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_job_stderr_detail(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await finish(session, job.id, JobStatus.SUCCEEDED, stderr_tail="decode glitch tail", now=T0)
        await session.commit()
        job_id = job.id

    response = await api_env.client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["stderr_tail"] == "decode glitch tail"


@pytest.mark.asyncio
async def test_healthcheck_logs(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        await finish(session, job.id, JobStatus.FAILED, error="decode errors", now=T0)
        data = b"healthcheck: result failed\n"
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            status=JobStatus.FAILED.value,
            result=JobStatus.FAILED.value,
            log_path="1.log",
            log_data=data,
        )
        session.add(attempt)
        await session.flush()
        await session.commit()

    response = await api_env.client.get(f"/api/jobs/{job.id}/attempts/{attempt.id}/log")
    assert response.status_code == 200
    assert response.content == data


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.FAILED])
async def test_running_healthcheck_log(api_env: ApiEnv, status: JobStatus) -> None:
    library_id, media_id = await seed_media(api_env)
    data = b"healthcheck: running\n"
    async with api_env.factory() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        job.status = status.value
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            workload="healthcheck",
            status=status.value,
        )
        session.add(attempt)
        await session.flush()
        log = create_healthcheck_log(api_env.app.state.healthcheck_dir, attempt.id)
        log.append(data.decode())
        attempt.log_path = log.path.name
        session.add(
            JobEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                type="healthcheck_log_created",
                payload={"log_path": log.path.name},
            )
        )
        await session.commit()
        job_id, attempt_id = job.id, attempt.id
    log_path = log.path

    response = await api_env.client.get(f"/api/jobs/{job_id}/attempts/{attempt_id}/log", headers={"Range": "bytes=2-8"})
    assert response.status_code == 206
    assert response.content == data[2:9]
    assert response.headers["content-range"] == f"bytes 2-8/{len(data)}"
    detail = (await api_env.client.get(f"/api/jobs/{job_id}")).json()
    exported = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert detail["attempts"][0]["log_data"] == data.decode()
    assert exported["attempts"][0]["log_data"] == data.decode()

    log_path.unlink()
    assert (await api_env.client.get(f"/api/jobs/{job_id}/attempts/{attempt_id}/log")).status_code == 404


@pytest.mark.asyncio
async def test_healthlog_download_symlink(api_env: ApiEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    from server.api.jobs import attempt_log

    root = tmp_path / "healthchecks"
    monkeypatch.setattr(api_env.app.state, "healthcheck_dir", root)
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        job.status = "running"
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="running", workload="healthcheck")
        session.add(attempt)
        await session.flush()
        log = create_healthcheck_log(root, attempt.id)
        log.append("diagnostics\n")
        attempt.log_path = log.path.name
        session.add(
            JobEvent(
                job_id=job.id,
                attempt_id=attempt.id,
                type="healthcheck_log_created",
                payload={"log_path": log.path.name},
            )
        )
        await session.commit()
    request = Request({"type": "http", "app": api_env.app, "headers": []})
    response = await attempt_log(job.id, attempt.id, request)
    detached = tmp_path / "detached"
    root.rename(detached)
    root.symlink_to(detached, target_is_directory=True)
    with pytest.raises(OSError):
        await anext(aiter(response.body_iterator))
    assert (detached / log.path.name).read_text() == "diagnostics\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, staged", [("transcode", False), ("transcode", True), ("healthcheck", False), ("mkv_stats", False)]
)
async def test_running_node_log(api_env: ApiEnv, kind: str, staged: bool) -> None:
    from server.metadata_jobs import enqueue_metadata_job

    library_id, media_id = await seed_media(api_env)
    data = b"phase: ffmpeg\nprogress\n"
    async with api_env.factory() as session:
        node = await register_node(session, "node-logs", "token")
        if kind == "transcode":
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        elif kind == "healthcheck":
            job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        else:
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            assert media is not None and library is not None
            job = await enqueue_metadata_job(session, media, library, kind="mkv_stats", priority=1)
        job.status = JobStatus.RUNNING.value
        job.node_id = node.id
        job.attempts = 1
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            node_id=node.id,
            workload=kind,
            capabilities={"ffmpeg_version": "fake"},
            status=JobStatus.RUNNING.value,
            log_path=None,
        )
        session.add(attempt)
        await session.flush()
        parent = f"jobs/{job.id}/attempts/1"
        attempt.log_path = f"{parent}/{'ffmpeg-2.log' if staged else 'ffmpeg.log'}"
        if staged:
            session.add_all(
                [
                    JobEvent(
                        job_id=job.id,
                        attempt_id=attempt.id,
                        type="stage",
                        payload={"log_path": f"{parent}/ffmpeg.log", "log": "older snapshot"},
                    ),
                    JobEvent(
                        job_id=job.id, attempt_id=attempt.id, type="stage", payload={"log_path": attempt.log_path}
                    ),
                ]
            )
        await session.commit()
        job_id, attempt_id = job.id, attempt.id
    log_path = api_env.app.state.workspace_dir / attempt.log_path
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(data)
    prior = b"cleanup: complete\n"
    if staged:
        log_path.with_name("ffmpeg.log").write_bytes(prior)
        data = prior + b"\n" + data

    response = await api_env.client.get(f"/api/jobs/{job_id}/attempts/{attempt_id}/log")
    assert response.status_code == 200
    assert response.content == data
    start = len(prior) - 2 if staged else 2
    response = await api_env.client.get(
        f"/api/jobs/{job_id}/attempts/{attempt_id}/log", headers={"Range": f"bytes={start}-{start + 5}"}
    )
    assert response.status_code == 206
    assert response.content == data[start : start + 6]
    assert response.headers["content-range"] == f"bytes {start}-{start + 5}/{len(data)}"
    detail = (await api_env.client.get(f"/api/jobs/{job_id}")).json()
    exported = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert detail["attempts"][0]["log_data"] == data.decode()
    assert exported["attempts"][0]["log_data"] == data.decode()


@pytest.mark.asyncio
async def test_large_log_ranges(api_env: ApiEnv, monkeypatch) -> None:
    from contextlib import contextmanager

    from server.api import jobs
    from tests.helpers import fd_path

    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        job.status = JobStatus.RUNNING.value
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            workload="transcode",
            status=JobStatus.RUNNING.value,
            log_path=f"jobs/{job.id}/attempts/1/ffmpeg.log",
        )
        session.add(attempt)
        await session.commit()
        job_id, attempt_id = job.id, attempt.id
    data = ("first café\n" * 100000).encode() + b"last line\n"
    path = api_env.app.state.workspace_dir / attempt.log_path
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    read_sizes = []
    original = jobs._file

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def fileno(self):
            return self.handle.fileno()

        def seek(self, offset):
            return self.handle.seek(offset)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    @contextmanager
    def open_file(directory, name):
        with original(directory, name) as handle:
            yield Reader(handle) if fd_path(name, directory) == path else handle

    monkeypatch.setattr(jobs, "_file", open_file)
    url = f"/api/jobs/{job_id}/attempts/{attempt_id}/log"
    for header, expected in (("bytes=100000-100010", data[100000:100011]), ("bytes=-10", data[-10:])):
        read_sizes.clear()
        response = await api_env.client.get(url, headers={"Range": header})
        assert response.status_code == 206
        assert response.content == expected
        assert read_sizes == [len(expected)]
    read_sizes.clear()
    response = await api_env.client.get(url)
    assert response.content == data
    assert max(read_sizes) <= 65536
    detail = (await api_env.client.get(f"/api/jobs/{job_id}")).json()
    exported = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert detail["attempts"][0]["log_data"] == data.decode()
    assert exported["attempts"][0]["log_data"] == data.decode()


@pytest.mark.asyncio
async def test_attempt_history_and(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    first_data = b"first attempt\nline with ```\n"
    second_data = b"0123456789"
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        job.decision_log = [{"decision": "keep", "raw": "```"}]
        job.flow_snapshot = {
            "id": 12,
            "name": "Main flow",
            "version": 3,
            "definition": {
                "entry": "encode",
                "nodes": [
                    {"id": "encode", "type": "plugin", "plugin": "transcode.hevc", "inputs": {"crf": 20}},
                    {"id": "done", "type": "terminal", "outcome": "done"},
                ],
                "edges": [{"src": "encode", "port": "plan", "dst": "done"}],
            },
        }
        await session.flush()
        session.add_all(
            [
                JobAttempt(
                    job_id=job.id,
                    attempt_number=1,
                    node_id=None,
                    node_name="old-node",
                    capabilities={"encoders": ["h264"]},
                    workload="gpu",
                    status="failed",
                    result="failed",
                    error="old error",
                    source_path="media/source.avi",
                    output_path="media/output.mkv",
                    log_path="jobs/1/attempts/1/run.log",
                    argv=["ffmpeg", "old"],
                    argv_hash="old-hash",
                    source_size=10,
                    output_size=4,
                    health={"healthy": False},
                    action_results=[{"ok": False}],
                    input_probe={"file_path": "old", "duration_s": 1, "size_bytes": 10, "streams": []},
                    output_probe={"file_path": "new", "duration_s": 1, "size_bytes": 4, "streams": []},
                    claimed_at=T0,
                    started_at=T0,
                    finished_at=T0,
                    log_data=first_data,
                ),
                JobAttempt(
                    job_id=job.id,
                    attempt_number=2,
                    node_id=None,
                    node_name="new-node",
                    capabilities={"encoders": ["hevc"]},
                    workload="cpu",
                    status="succeeded",
                    result="succeeded",
                    source_path="media/source.avi",
                    output_path="media/output.mkv",
                    log_path="jobs/1/attempts/2/run.log",
                    argv=["ffmpeg", "new"],
                    argv_hash="new-hash",
                    source_size=10,
                    output_size=8,
                    health={"healthy": True},
                    action_results=[{"ok": True}],
                    claimed_at=T0,
                    started_at=T0,
                    finished_at=T0,
                    log_data=second_data,
                ),
                JobAttempt(
                    job_id=job.id,
                    attempt_number=3,
                    node_name="new-node",
                    status="failed",
                    result="failed",
                    argv=["ffmpeg", "arg with `tick` and\nnewline"],
                ),
            ]
        )
        await session.flush()
        attempts = list(
            (
                await session.execute(
                    select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_number)
                )
            ).scalars()
        )
        session.add(JobEvent(job_id=job.id, attempt_id=attempts[1].id, type="finished", payload={}))
        await session.commit()
        job_id, first_id, second_id, third_id = job.id, attempts[0].id, attempts[1].id, attempts[2].id

    detail = (await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})).json()
    assert [row["attempt_number"] for row in detail["attempts"]] == [1, 2, 3]
    assert detail["attempts"][0]["argv"] == ["ffmpeg", "old"]
    assert detail["attempts"][0]["health"] == {"healthy": False}
    assert detail["attempts"][0]["action_results"] == [{"ok": False}]
    assert detail["attempts"][1]["argv"] == ["ffmpeg", "new"]
    assert detail["attempts"][1]["health"] == {"healthy": True}
    assert detail["attempts"][1]["action_results"] == [{"ok": True}]
    assert detail["attempts"][1]["node_name"] == "new-node"
    assert detail["attempts"][1]["output_probe"] is None
    assert detail["events"][0]["attempt_id"] is None
    assert detail["events"][1]["attempt_id"] == second_id
    assert detail["attempts"][0]["log_data"] == first_data.decode()
    assert detail["flow_snapshot"] == job.flow_snapshot

    export = await api_env.client.get(f"/api/jobs/{job_id}/export.json")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("application/json")
    assert export.headers["content-disposition"] == f'attachment; filename="job-{job_id}.json"'
    exported = export.json()
    assert exported == detail
    assert exported["id"] == job_id
    assert exported["attempt_count"] == job.attempts
    assert [row["attempt_number"] for row in exported["attempts"]] == [1, 2, 3]
    assert exported["attempts"][0]["input_probe"]["streams"] == []
    assert exported["events"][1]["attempt_id"] == second_id
    assert exported["attempts"][2]["argv"] == ["ffmpeg", "arg with `tick` and\nnewline"]
    assert (await api_env.client.get(f"/api/jobs/{job_id + 1}/export.json")).status_code == 404

    listing = (await api_env.client.get("/api/jobs")).json()["items"][0]
    assert listing["node_name"] == "new-node"
    assert listing["latest_attempt"]["id"] == third_id

    response = await api_env.client.get(
        f"/api/jobs/{job_id}/attempts/{second_id}/log", headers={"Accept-Encoding": "identity"}
    )
    assert response.status_code == 200
    assert response.text == "0123456789"
    assert response.headers["content-length"] == "10"
    assert response.headers["accept-ranges"] == "bytes"
    assert (
        await api_env.client.get(f"/api/jobs/{job_id}/attempts/{second_id}/log", headers={"Range": "bytes=2-5"})
    ).text == "2345"
    assert (
        await api_env.client.get(f"/api/jobs/{job_id}/attempts/{second_id}/log", headers={"Range": "bytes=3-"})
    ).text == "3456789"
    assert (
        await api_env.client.get(f"/api/jobs/{job_id}/attempts/{second_id}/log", headers={"Range": "bytes=-3"})
    ).text == "789"
    assert (
        await api_env.client.get(f"/api/jobs/{job_id}/attempts/{second_id}/log", headers={"Range": "bytes=99-"})
    ).status_code == 416
    assert (await api_env.client.get(f"/api/jobs/{job_id}/attempts/{first_id}/log")).content == first_data
    assert (await api_env.client.get(f"/api/jobs/{job_id + 1}/attempts/{second_id}/log")).status_code == 404

    async with api_env.factory() as session:
        attempt = await session.get(JobAttempt, second_id)
        assert attempt is not None
        attempt.log_data = None
        await session.commit()
    assert (await api_env.client.get(f"/api/jobs/{job_id}/attempts/{second_id}/log")).status_code == 404

    async with api_env.factory() as session:
        attempt = await session.get(JobAttempt, second_id)
        assert attempt is not None
        attempt.log_path = "jobs/1/attempts/2/run.log"
        await session.commit()
    assert (await api_env.client.get(f"/api/jobs/{job_id}/attempts/{second_id}/log")).status_code == 404


@pytest.mark.asyncio
async def test_nodes_list(api_env: ApiEnv) -> None:
    await seed_node(api_env)
    resp = await api_env.client.get("/api/nodes")
    nodes = resp.json()
    assert len(nodes) == 1
    assert nodes[0]["name"] == "node-a"
    assert nodes[0]["display_name"] == "node-a"
    assert "max_concurrency" not in nodes[0]
    assert nodes[0]["capabilities"]["max_transcodes"] == 1
    assert nodes[0]["max_transcodes"] == 1
    assert nodes[0]["active_jobs"] == 0
    assert nodes[0]["active_job_ids"] == []
    assert nodes[0]["usage"] is None
    assert "token_hash" not in nodes[0]
    sub = api_env.app.state.events.subscribe()
    try:
        api_env.app.state.hub._publish_node("node-a", "online")
        event = await asyncio.wait_for(sub.next(), 2)
        assert event is not None and event.payload["active_job_ids"] == []
    finally:
        api_env.app.state.events.unsubscribe(sub)


@pytest.mark.asyncio
async def test_node_limit_persists(api_env: ApiEnv) -> None:
    node_id = await seed_node(api_env)
    entry = NodeEntry(
        name="node-a",
        node_db_id=node_id,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
        in_flight={41: "transcode", 42: "healthcheck", 43: "healthcheck", 44: "mkv_stats"},
    )
    api_env.app.state.hub.entries[entry.name] = entry

    response = await api_env.client.patch(
        f"/api/nodes/{node_id}",
        json={"display_name": "Living Room", "max_transcodes": 3, "max_healthchecks": 4, "max_mkv_stats": 2},
    )
    assert response.status_code == 200
    assert response.json()["max_transcodes"] == 3
    assert response.json()["display_name"] == "Living Room"
    assert response.json()["name"] == "node-a"
    assert entry.capabilities.max_transcodes == 3
    assert entry.capabilities.max_healthchecks == 4
    assert entry.capabilities.max_mkv_stats == 2
    listed = (await api_env.client.get("/api/nodes")).json()[0]
    assert (listed["max_transcodes"], listed["max_healthchecks"], listed["max_mkv_stats"]) == (3, 4, 2)
    assert (
        listed["active_jobs"],
        listed["active_transcodes"],
        listed["active_healthchecks"],
        listed["active_mkv_stats"],
    ) == (4, 1, 2, 1)
    assert listed["active_job_ids"] == [41, 42, 43, 44]
    disabled = await api_env.client.patch(f"/api/nodes/{node_id}", json={"max_healthchecks": 0, "max_mkv_stats": 0})
    assert disabled.status_code == 200
    assert disabled.json()["max_transcodes"] == 3
    assert (entry.capabilities.max_healthchecks, entry.capabilities.max_mkv_stats) == (0, 0)
    assert disabled.json()["active_jobs"] == 4
    for field in ("max_healthchecks", "max_mkv_stats"):
        assert (await api_env.client.patch(f"/api/nodes/{node_id}", json={field: -1})).status_code == 422
    assert (await api_env.client.patch(f"/api/nodes/{node_id}", json={"max_transcodes": 0})).status_code == 422
    assert (await api_env.client.patch("/api/nodes/99999", json={"max_transcodes": 2})).status_code == 404


@pytest.mark.asyncio
async def test_node_pause_persists(api_env: ApiEnv) -> None:
    node_id = await seed_node(api_env)
    entry = NodeEntry(
        name="node-a",
        node_db_id=node_id,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    api_env.app.state.hub.entries[entry.name] = entry

    assert (await api_env.client.get("/api/nodes")).json()[0]["paused"] is False

    response = await api_env.client.patch(f"/api/nodes/{node_id}", json={"paused": True})
    assert response.status_code == 200
    assert response.json()["paused"] is True
    assert entry.paused
    assert (await api_env.client.get("/api/nodes")).json()[0]["paused"] is True

    response = await api_env.client.patch(f"/api/nodes/{node_id}", json={"paused": False})
    assert response.status_code == 200
    assert response.json()["paused"] is False
    assert not entry.paused


@pytest.mark.asyncio
async def test_remove_offline_node(api_env: ApiEnv) -> None:
    node_id = await seed_node(api_env)
    response = await api_env.client.delete(f"/api/nodes/{node_id}")
    assert response.status_code == 204
    assert (await api_env.client.get("/api/nodes")).json() == []
    assert (await api_env.client.delete("/api/nodes/99999")).status_code == 404


@pytest.mark.asyncio
async def test_reject_online_node(api_env: ApiEnv) -> None:
    await seed_node(api_env)
    async with api_env.factory() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "node-a"))
        assert node is not None
        node.state = "online"
        await session.commit()
        node_id = node.id
    response = await api_env.client.delete(f"/api/nodes/{node_id}")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_reject_active_node(api_env: ApiEnv) -> None:
    await seed_node(api_env)
    async with api_env.factory() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "node-a"))
        assert node is not None
        media_id, library_id = await seed_media(api_env)
        job = Job(media_file_id=media_id, library_id=library_id, status=JobStatus.QUEUED.value, node_id=node.id)
        session.add(job)
        await session.commit()
        node_id = node.id
    assert (await api_env.client.delete(f"/api/nodes/{node_id}")).status_code == 409


@pytest.mark.asyncio
async def test_retain_node_history(api_env: ApiEnv) -> None:
    await seed_node(api_env)
    async with api_env.factory() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "node-a"))
        assert node is not None
        media_id, library_id = await seed_media(api_env)
        job = Job(media_file_id=media_id, library_id=library_id, status=JobStatus.SUCCEEDED.value, node_id=node.id)
        session.add(job)
        await session.flush()
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            node_id=node.id,
            node_name="node-a",
            capabilities={"encoders": ["h264"]},
            workload="transcode",
            status=JobStatus.SUCCEEDED.value,
            result="succeeded",
        )
        session.add(attempt)
        await session.flush()
        session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type="finished", payload={"details": "kept"}))
        await session.commit()
        node_id, job_id, attempt_id = node.id, job.id, attempt.id
    assert (await api_env.client.delete(f"/api/nodes/{node_id}")).status_code == 204
    async with api_env.factory() as session:
        assert await session.get(NodeRow, node_id) is None
        retained_job = await session.get(Job, job_id)
        retained_attempt = await session.get(JobAttempt, attempt_id)
        assert retained_job is not None and retained_job.node_id is None
        assert retained_attempt is not None and retained_attempt.node_id is None
        assert retained_attempt.node_name == "node-a"
        assert retained_attempt.capabilities is not None
        assert retained_attempt.capabilities["encoders"] == ["h264"]
        payload = await session.scalar(select(JobEvent.payload).where(JobEvent.attempt_id == attempt_id))
        assert payload is not None
        assert payload["details"] == "kept"


@pytest.mark.asyncio
async def test_system_timezone(api_env: ApiEnv, tmp_path: Path) -> None:
    app = create_app(api_env.factory, config=ServerConfig(tz="Europe/Paris"), cache_root=tmp_path / "cache")
    async with api_client(transport=httpx.ASGITransport(app=app), base_url=UI_BASE) as client:
        response = await client.get("/api/system")
    assert response.status_code == 200 and response.json()["timezone"] == "Europe/Paris"


@pytest.mark.asyncio
async def test_stats_overview_scope(api_env: ApiEnv) -> None:
    now = T0
    api_env.app.state.stats.clock = lambda: now
    api_env.app.state.cache.clock = lambda: now
    async with api_env.factory() as session:
        libraries = [Library(name=f"overview-{uuid.uuid4().hex}", roots=[str(api_env.lib_dir)]) for _ in range(3)]
        session.add_all(libraries)
        await session.flush()
        media = [
            MediaFile(
                library_id=libraries[0].id,
                path=str(api_env.lib_dir / "one.mkv"),
                size=100,
                mtime_ns=1,
                container="mkv",
                video_codec="h264",
                resolution_bucket="1080p",
                health_status="healthy",
                transcode_status="done",
                medium="video",
                last_scanned_at=now,
            ),
            MediaFile(
                library_id=libraries[1].id,
                path=str(api_env.lib_dir / "two.mp4"),
                size=200,
                mtime_ns=1,
                container="mp4",
                video_codec="hevc",
                resolution_bucket="4k",
                health_status="failed",
                transcode_status="pending",
                medium="video",
                last_scanned_at=now,
            ),
            MediaFile(
                library_id=libraries[0].id,
                path=str(api_env.lib_dir / "audio.mp3"),
                size=50,
                mtime_ns=1,
                container="mp3",
                video_codec=None,
                resolution_bucket=None,
                health_status="healthy",
                transcode_status="pending",
                medium="audio",
                last_scanned_at=now,
            ),
        ]
        session.add_all(media)
        await session.flush()
        jobs = []
        for item, library, status, kind, started, source, output in (
            (media[0], libraries[0], "succeeded", "transcode", now - timedelta(seconds=10), 1000, 600),
            (media[0], libraries[0], "failed", "healthcheck", now - timedelta(seconds=20), None, None),
            (media[1], libraries[1], "cancelled", "transcode", now - timedelta(seconds=5), None, None),
        ):
            job = Job(
                media_file_id=item.id,
                library_id=library.id,
                kind=kind,
                status=status,
                started_at=started,
                finished_at=now,
            )
            session.add(job)
            await session.flush()
            jobs.append(job)
            if source is not None:
                session.add(
                    JobAttempt(
                        job_id=job.id,
                        attempt_number=1,
                        status="succeeded",
                        result="succeeded",
                        source_size=source,
                        output_size=output,
                        argv=["-i", item.path, "-c", "copy", "-c:v:0", "libx265", "output.mkv"],
                    )
                )
        await session.commit()

    await api_env.app.state.stats.refresh()
    all_stats = (await api_env.client.get("/api/stats/overview", params={"days": 1})).json()
    selected = (
        await api_env.client.get("/api/stats/overview", params={"days": 1, "library_id": libraries[0].id})
    ).json()
    assert all_stats["inventory"]["files"] == 3
    assert all_stats["inventory"]["bytes"] == 350
    assert all_stats["inventory"]["video_codecs"] == {"h264": 1, "hevc": 1}
    assert all_stats["inventory"]["audio_codecs"] == {}
    assert all_stats["inventory"]["subtitle_codecs"] == {}
    assert all_stats["inventory"]["containers"] == {"mkv": 1, "mp3": 1, "mp4": 1}
    assert all_stats["inventory"]["resolutions"] == {"1080p": 1, "4k": 1}
    assert all_stats["inventory"]["transcode_status"] == {"done": 1, "pending": 2}
    assert all_stats["jobs"]["completed"] == 3
    assert all_stats["jobs"]["succeeded"] == 1
    assert all_stats["jobs"]["failed"] == 1
    assert all_stats["jobs"]["cancelled"] == 1
    assert all_stats["jobs"]["by_kind"]["transcode"]["succeeded"] == 1
    assert all_stats["jobs"]["avg_duration_s"] == 11.667
    assert selected["inventory"]["files"] == 2
    assert selected["inventory"]["containers"] == {"mkv": 1, "mp3": 1}
    assert selected["inventory"]["video_codecs"] == {"h264": 1}
    assert selected["inventory"]["resolutions"] == {"1080p": 1}
    assert selected["jobs"]["completed"] == 2
    assert selected["savings"]["totals"]["saved_bytes"] == 400
    assert all_stats["savings"]["totals"]["saved_bytes"] == 400
    assert sum(point["saved_bytes"] for point in selected["savings"]["series"]) == 400
    summary = (await api_env.client.get("/api/stats")).json()
    assert summary["space_saved_by_library"] == {str(libraries[0].id): 400}
    assert selected["libraries"] == all_stats["libraries"]
    assert [row["name"] for row in all_stats["libraries"]] == sorted(row["name"] for row in all_stats["libraries"])
    by_id = {row["id"]: row for row in all_stats["libraries"]}
    assert by_id[libraries[0].id]["files"] == 2
    assert by_id[libraries[0].id]["bytes"] == 150
    assert by_id[libraries[0].id]["successful_transcodes"] == 1
    assert by_id[libraries[0].id]["source_bytes"] == 1000
    assert by_id[libraries[0].id]["saved_bytes"] == 400
    assert by_id[libraries[0].id]["avg_reduction_pct"] == 40.0
    assert by_id[libraries[1].id]["files"] == 1
    assert by_id[libraries[1].id]["successful_transcodes"] == 0
    assert by_id[libraries[2].id] == {
        "id": libraries[2].id,
        "name": by_id[libraries[2].id]["name"],
        "files": 0,
        "bytes": 0,
        "successful_transcodes": 0,
        "successful_remuxes": 0,
        "successful_unclassified": 0,
        "transcode_saved_bytes": 0,
        "remux_saved_bytes": 0,
        "unclassified_saved_bytes": 0,
        "source_bytes": 0,
        "saved_bytes": 0,
        "avg_reduction_pct": 0.0,
    }
    for params in ({"days": 0}, {"days": 366}, {"library_id": 0}):
        assert (await api_env.client.get("/api/stats/overview", params=params)).status_code == 422


@pytest.mark.asyncio
async def test_stats_track_codecs(api_env: ApiEnv) -> None:
    async with api_env.factory() as session:
        libraries = [Library(name=f"tracks-{index}", roots=[str(api_env.lib_dir)]) for index in range(3)]
        session.add_all(libraries)
        await session.flush()
        for library, name, medium, streams in (
            (
                libraries[0],
                "mixed.mkv",
                "video",
                [
                    {"codec_type": "video", "codec_name": "h264"},
                    {"codec_type": "audio", "codec_name": " AAC ", "tags": {"language": "en"}},
                    {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
                    {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "jap", "title": "Japanese"}},
                    {"codec_type": "subtitle", "codec_name": " SUBRIP ", "tags": {"language": "fre"}},
                    {"codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "fr"}},
                    {"codec_type": "subtitle", "tags": {"language": None, "title": "French"}},
                    {"codec_type": "subtitle", "codec_name": " ", "tags": {"language": "und", "title": "Japanese"}},
                ],
            ),
            (
                libraries[0],
                "audio.flac",
                "audio",
                [
                    {"codec_type": "audio", "codec_name": "flac", "tags": {"title": "English"}},
                    {"codec_type": "audio", "codec_name": None, "tags": {"language": " ", "title": "Japanese"}},
                ],
            ),
            (
                libraries[1],
                "other.mkv",
                "video",
                [
                    {"codec_type": "audio", "codec_name": "opus", "tags": {"language": "ja", "title": "English"}},
                    {
                        "codec_type": "subtitle",
                        "codec_name": "hdmv_pgs_subtitle",
                        "tags": {"language": "x-private", "title": "Japanese commentary"},
                    },
                    {
                        "codec_type": "subtitle",
                        "codec_name": "hdmv_pgs_subtitle",
                        "tags": {"language": "jap", "title": "Japanese"},
                    },
                ],
            ),
        ):
            media = MediaFile(
                library_id=library.id,
                path=str(api_env.lib_dir / name),
                size=100,
                mtime_ns=1,
                container=Path(name).suffix.lstrip("."),
                medium=medium,
                last_scanned_at=T0,
            )
            session.add(media)
            await session.flush()
            probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps({"streams": streams}))
            session.add(probe)
            await session.flush()
            media.probe_id = probe.id
        await session.commit()

    await api_env.app.state.stats.refresh()
    all_stats = (await api_env.client.get("/api/stats/overview", params={"days": 1})).json()
    assert all_stats["inventory"]["audio_codecs"] == {"aac": 2, "ac3": 1, "flac": 1, "opus": 1, "unknown": 1}
    assert all_stats["inventory"]["subtitle_codecs"] == {"subrip": 1, "ass": 1, "hdmv_pgs_subtitle": 2, "unknown": 2}
    assert all_stats["inventory"]["audio_languages"] == {"eng": 2, "jpn": 2, "und": 2}
    assert all_stats["inventory"]["subtitle_languages"] == {"fra": 2, "und": 2, "x-private": 1, "jpn": 1}
    selected = (
        await api_env.client.get("/api/stats/overview", params={"days": 1, "library_id": libraries[0].id})
    ).json()
    assert selected["inventory"]["audio_codecs"] == {"aac": 2, "ac3": 1, "flac": 1, "unknown": 1}
    assert selected["inventory"]["subtitle_codecs"] == {"subrip": 1, "ass": 1, "unknown": 2}
    assert selected["inventory"]["audio_languages"] == {"eng": 2, "jpn": 1, "und": 2}
    assert selected["inventory"]["subtitle_languages"] == {"fra": 2, "und": 2}
    longer = (
        await api_env.client.get("/api/stats/overview", params={"days": 365, "library_id": libraries[0].id})
    ).json()
    assert longer["inventory"] == selected["inventory"]
    empty = (await api_env.client.get("/api/stats/overview", params={"library_id": libraries[2].id})).json()
    assert empty["inventory"]["audio_codecs"] == {}
    assert empty["inventory"]["subtitle_codecs"] == {}
    assert empty["inventory"]["audio_languages"] == {}
    assert empty["inventory"]["subtitle_languages"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        None,
        "{invalid",
        '{"streams": ["bad", null, 42]}',
        '{"streams": "bad"}',
    ],
)
async def test_stats_tracks_absent(api_env: ApiEnv, raw: str | None) -> None:
    library_id, media_id = await seed_media(api_env)
    if raw is not None:
        async with api_env.factory() as session:
            probe = ProbeRow(media_file_id=media_id, raw_json=raw)
            session.add(probe)
            await session.flush()
            media = await session.get(MediaFile, media_id)
            assert media is not None
            media.probe_id = probe.id
            await session.commit()
    await api_env.app.state.stats.refresh()
    response = await api_env.client.get("/api/stats/overview", params={"library_id": library_id})
    assert response.status_code == 200
    inventory = response.json()["inventory"]
    assert inventory["files"] == 1
    assert inventory["audio_codecs"] == {}
    assert inventory["subtitle_codecs"] == {}
    assert inventory["audio_languages"] == {}
    assert inventory["subtitle_languages"] == {}


def metric_sample(body: str, name: str) -> float:
    for line in body.splitlines():
        if line.startswith(name):
            return float(line.rsplit(" ", 1)[1])
    # A labeled counter series is absent until first use.
    if "{" in name:
        return 0.0
    msg = f"metric {name} not exposed"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_metrics_endpoint(api_env: ApiEnv) -> None:
    resp = await api_env.client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE millwright_nodes_connected gauge" in resp.text
    assert "# TYPE millwright_jobs_finished_total counter" in resp.text
    assert "# TYPE millwright_events_sent_total counter" in resp.text

    # Counters accumulate process-wide across tests, so assert on the delta.
    sample = 'millwright_events_sent_total{type="node.status"}'
    before = metric_sample(resp.text, sample)
    api_env.app.state.hub._publish("node.status", {"name": "n", "state": "online"}, key="node.status:n")
    resp = await api_env.client.get("/metrics")
    assert metric_sample(resp.text, sample) == before + 1

    # Routers publish directly on the bus. The counter catches those too.
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()
        job_id = job.id
    status_sample = 'millwright_events_sent_total{type="job.status"}'
    resp = await api_env.client.get("/metrics")
    before = metric_sample(resp.text, status_sample)
    resp = await api_env.client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    resp = await api_env.client.get("/metrics")
    assert metric_sample(resp.text, status_sample) == before + 1


@pytest.mark.asyncio
async def test_auth_gate(api_env: ApiEnv) -> None:
    resp = await api_env.client.get("/api/libraries")
    assert resp.status_code == 200

    oidc = OidcConfig(issuer="https://sso.example.test/realm", client_id="t", secret="s", redirect_uri="https://ui/cb")
    locked = create_app(
        api_env.factory,
        config=ServerConfig(
            auth=AuthConfig(oidc=oidc),
        ),
        ffprobe_bin=str(FAKE_FFPROBE),
    )
    transport = httpx.ASGITransport(app=locked)
    async with httpx.AsyncClient(transport=transport, base_url=UI_BASE) as client:
        resp = await client.get("/api/libraries")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health_bypasses_auth(api_env: ApiEnv) -> None:
    oidc = OidcConfig(issuer="https://sso.example.test/realm", client_id="t", secret="s", redirect_uri="https://ui/cb")
    locked = create_app(
        api_env.factory,
        config=ServerConfig(
            auth=AuthConfig(oidc=oidc),
        ),
        ffprobe_bin=str(FAKE_FFPROBE),
    )
    transport = httpx.ASGITransport(app=locked)
    async with api_client(transport=transport, base_url=UI_BASE) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["version"]


@pytest.mark.asyncio
async def test_access_log_combined(api_env: ApiEnv, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="server.access"):
        resp = await api_env.client.get("/health", headers={"user-agent": "probe/1.0"})
    assert resp.status_code == 200
    record = next(record for record in reversed(caplog.records) if record.name == "server.access")
    assert record.__dict__["method"] == "GET"
    assert record.__dict__["route"] == "/health"
    assert record.__dict__["status"] == 200
    assert record.__dict__["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_configured_access_logs(enabled: bool, caplog: pytest.LogCaptureFixture) -> None:
    async with database_context("access-log") as factory:
        app = create_app(factory, config=ServerConfig(access_log=enabled))
        try:
            transport = httpx.ASGITransport(app=app)
            async with api_client(transport=transport, base_url=UI_BASE) as client:
                with caplog.at_level(logging.INFO, logger="server.access"):
                    response = await client.get("/health", headers={"x-request-id": "request-fixture"})
        finally:
            await asyncio.wait_for(app.state.plugin_host.aclose(), 5)
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-fixture"
    records = [record for record in caplog.records if record.name == "server.access"]
    assert len(records) == int(enabled)
    if enabled:
        assert records[0].__dict__["request_id"] == "request-fixture"


def hevc_flow(quality: int = 21) -> dict:
    return {
        "entry": "gate",
        "nodes": [
            {"type": "condition", "id": "gate", "when": {"media.file.size_bytes": {"gte": 0}}},
            {"type": "plugin", "id": "enc", "plugin": "transcode.video", "inputs": {"quality": quality}},
            {"type": "plugin", "id": "subs", "plugin": "action.extract_subs", "inputs": {}},
            {"type": "terminal", "id": "skip", "outcome": "skip"},
            {"type": "terminal", "id": "done", "outcome": "done"},
        ],
        "edges": [
            {"src": "gate", "port": "true", "dst": "enc"},
            {"src": "gate", "port": "false", "dst": "skip"},
            {"src": "enc", "port": "plan", "dst": "subs"},
            {"src": "subs", "port": "next", "dst": "done"},
        ],
    }


async def seed_probed(env: ApiEnv, *, video_codec: str | None = "h264") -> tuple[int, int]:
    library_id, media_id = await seed_media(env, video_codec=video_codec)
    async with env.factory() as session:
        probe = ProbeRow(media_file_id=media_id, raw_json=json.dumps(PROBE_RAW))
        session.add(probe)
        await session.flush()
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.probe_id = probe.id
        await session.commit()
    return library_id, media_id


async def seed_flow_library(env: ApiEnv, library_id: int, definition: dict, name: str = "hevc") -> None:
    resp = await env.client.put(f"/api/flows/{name}", json={"definition": definition})
    assert resp.status_code == 200, resp.text
    async with env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.flow_id = resp.json()["id"]
        await session.commit()


@pytest.mark.asyncio
async def test_ui_hints_roundtrip(api_env: ApiEnv) -> None:
    body = hevc_flow() | {"_ui": {"nodes": {"cand.0": {"x": 0, "y": 120}}}}
    resp = await api_env.client.put("/api/flows/movies", json={"definition": body})
    assert resp.status_code == 200
    assert FlowDefinition.model_validate(resp.json()["definition"]).ui == body["_ui"]

    resp = await api_env.client.get("/api/flows/movies")
    assert resp.json()["definition"]["_ui"] == body["_ui"]


@pytest.mark.asyncio
async def test_flows_crud_roundtrip(api_env: ApiEnv) -> None:
    resp = await api_env.client.put("/api/flows/movies", json={"definition": hevc_flow()})
    assert resp.status_code == 200
    created = resp.json()
    assert created["name"] == "movies" and created["version"] == 1
    assert FlowDefinition.model_validate(created["definition"]) == FlowDefinition.model_validate(hevc_flow())

    resp = await api_env.client.get("/api/flows")
    assert [flow["name"] for flow in resp.json()] == ["movies"]

    resp = await api_env.client.get("/api/flows/movies")
    assert FlowDefinition.model_validate(resp.json()["definition"]) == FlowDefinition.model_validate(hevc_flow())

    resp = await api_env.client.put(
        "/api/flows/movies", json={"definition": hevc_flow(quality=19), "expected_version": 1}
    )
    assert resp.json()["version"] == 2

    resp = await api_env.client.delete("/api/flows/movies?expected_version=2")
    assert resp.status_code == 204
    resp = await api_env.client.get("/api/flows/movies")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_flows_export_archive(api_env: ApiEnv) -> None:
    await api_env.client.put("/api/flows/movies", json={"definition": hevc_flow()})
    await api_env.client.put("/api/flows/shows", json={"definition": hevc_flow(quality=19)})

    resp = await api_env.client.get("/api/flows/export.zip")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert len(archive.namelist()) == 2
        envelopes = [json.loads(archive.read(name)) for name in archive.namelist()]
    assert {envelope["name"] for envelope in envelopes} == {"movies", "shows"}
    assert all(envelope["_type"] == "flow" for envelope in envelopes)
    assert all(envelope["_version"] == 1 for envelope in envelopes)


@pytest.mark.asyncio
async def test_flow_rename_preserves(api_env: ApiEnv) -> None:
    original = await api_env.client.put("/api/flows/movies", json={"definition": hevc_flow()})
    flow_id = original.json()["id"]
    library = await api_env.client.post("/api/libraries", json={"name": "library", "roots": ["/x"]})
    library = await api_env.client.patch(f"/api/libraries/{library.json()['id']}", json={"flow_id": flow_id})
    changed = hevc_flow(quality=19)
    resp = await api_env.client.patch(
        "/api/flows/movies", json={"name": "films", "definition": changed, "expected_version": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == flow_id and resp.json()["version"] == 2
    assert (await api_env.client.get("/api/flows/movies")).status_code == 404
    assert (await api_env.client.get("/api/flows/films")).json()["id"] == flow_id
    bound = await api_env.client.get(f"/api/libraries/{library.json()['id']}")
    assert bound.json()["flow_id"] == flow_id


@pytest.mark.asyncio
async def test_flow_rename_collision(api_env: ApiEnv) -> None:
    first = await api_env.client.put("/api/flows/first", json={"definition": hevc_flow()})
    second = await api_env.client.put("/api/flows/second", json={"definition": hevc_flow(quality=19)})
    resp = await api_env.client.patch(
        "/api/flows/first", json={"name": "second", "definition": hevc_flow(), "expected_version": 1}
    )
    assert resp.status_code == 409
    assert (await api_env.client.get("/api/flows/first")).json()["id"] == first.json()["id"]
    assert (await api_env.client.get("/api/flows/second")).json()["id"] == second.json()["id"]


@pytest.mark.asyncio
async def test_flow_facts_endpoint(api_env: ApiEnv) -> None:
    resp = await api_env.client.get("/api/flows/facts")
    assert resp.status_code == 200
    facts = resp.json()
    assert facts == sorted(facts, key=lambda row: row["key"])
    types = {row["key"]: row["type"] for row in facts}
    assert types["media.video.pixels"] == "int"
    assert types["media.audio.languages"] == "list[str]"
    resp = await api_env.client.delete("/api/flows/movies")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", [False, True], ids=["save", "rename"])
async def test_flow_unknown(api_env: ApiEnv, tmp_path: Path, rename: bool) -> None:
    body = {"definition": graph_flow("filter.nope")}
    if rename:
        body.update(name="renamed", expected_version=1)
    handler = install_log_file(tmp_path)
    try:
        resp = await api_env.client.request(
            "PATCH" if rename else "PUT", "/api/flows/bad", json=body, headers={"x-request-id": "flow-rejection"}
        )
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert resp.status_code == 422
    entries = [entry for entry in query_logs(tmp_path)["entries"] if entry["logger"] == "server.api.flows"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["level"] == "warning"
    assert entry["fields"]["operation"] == ("flow_rename" if rename else "flow_save")
    assert entry["fields"]["flow_name"] == "bad"
    assert entry["fields"]["request_id"] == "flow-rejection"
    if rename:
        assert entry["fields"]["new_name"] == "renamed"


@pytest.mark.asyncio
async def test_flow_inputs(api_env: ApiEnv) -> None:
    body = graph_flow(("transcode.video", {"quality": "abc"}))
    resp = await api_env.client.put("/api/flows/bad", json={"definition": body})
    assert resp.status_code == 422
    assert (await api_env.client.get("/api/flows/bad")).status_code == 404
    body = graph_flow(("transcode.video", {"encoder": None, "quality": None}))
    response = await api_env.client.put("/api/flows/nullable", json={"definition": body})
    assert response.status_code == 200
    assert response.json()["definition"]["nodes"][0]["inputs"] == {"encoder": None, "quality": None}


@pytest.mark.asyncio
async def test_notify_requires_inputs(api_env: ApiEnv) -> None:
    body = graph_flow("transcode.video", "action.notify")
    response = await api_env.client.put("/api/flows/notify", json={"definition": body})
    assert response.status_code == 422
    assert (await api_env.client.get("/api/flows/notify")).status_code == 404


@pytest.mark.asyncio
async def test_layout_conflict_preserves(api_env: ApiEnv) -> None:
    body = hevc_flow()
    assert (await api_env.client.put("/api/flows/layout", json={"definition": body})).status_code == 200
    edited = hevc_flow(quality=19)
    assert (
        await api_env.client.put("/api/flows/layout", json={"definition": edited, "expected_version": 1})
    ).status_code == 200
    body["_ui"] = {"nodes": {"gate": {"x": 3, "y": 4}}}
    response = await api_env.client.put("/api/flows/layout", json={"definition": body, "expected_version": 1})
    assert response.status_code == 409
    assert FlowDefinition.model_validate(
        (await api_env.client.get("/api/flows/layout")).json()["definition"]
    ) == FlowDefinition.model_validate(edited)


@pytest.mark.asyncio
async def test_flow_facet_save(api_env: ApiEnv) -> None:
    body = hevc_flow()
    body["nodes"][0]["when"] = {"bitrate_estimate.hevc_kbps": {"lt": 2000}}
    body["entry"] = "inspect"
    body["nodes"].insert(0, {"type": "plugin", "id": "inspect", "plugin": "inspector.bitrate_estimate", "inputs": {}})
    body["edges"].extend(
        [
            {"src": "inspect", "port": "match", "dst": "gate"},
            {"src": "inspect", "port": "skip", "dst": "skip"},
        ]
    )
    saved = await api_env.client.put("/api/flows/movies", json={"definition": body})
    assert saved.status_code == 200

    body["entry"] = "gate"
    body["nodes"] = [node for node in body["nodes"] if node["id"] != "inspect"]
    body["edges"] = [edge for edge in body["edges"] if edge["src"] != "inspect"]
    resp = await api_env.client.put("/api/flows/movies", json={"definition": body, "expected_version": 1})

    assert resp.status_code == 422
    assert (await api_env.client.get("/api/flows/movies")).json()["version"] == 1


@pytest.mark.asyncio
async def test_plugins_list_and(api_env: ApiEnv) -> None:
    resp = await api_env.client.get("/api/plugins")
    assert resp.status_code == 200
    plugins = {entry["manifest"]["id"]: entry for entry in resp.json()}
    assert plugins["transcode.video"]["state"] == "ready"
    quality = next(field for field in plugins["transcode.video"]["inputs"] if field["name"] == "quality")
    assert {key: value for key, value in quality.items() if key != "help"} == {
        "required": False,
        "nullable": True,
        "name": "quality",
        "type": "int",
        "default": None,
        "min": 0,
        "max": 63,
        "values": None,
    }

    resp = await api_env.client.get("/api/plugins/inspector.audio_languages")
    assert resp.status_code == 200
    assert resp.json()["manifest"]["kind"] == "inspector"
    expected = resp.json()["inputs"]
    assert [{key: value for key, value in field.items() if key != "help"} for field in expected] == [
        {
            "name": "expected",
            "required": False,
            "nullable": False,
            "type": "strings",
            "default": ["eng", "und"],
            "min": None,
            "max": None,
            "values": None,
        }
    ]

    resp = await api_env.client.get("/api/plugins/filter.nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dry_run_endpoint(api_env: ApiEnv) -> None:
    _, media_id = await seed_probed(api_env)
    resp = await api_env.client.post("/api/flows/dry-run", json={"file_id": media_id, "flow": hevc_flow()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deferred"
    assert [(step.get("node"), step["outcome"]) for step in body["steps"]] == [
        ("gate", "true"),
        ("enc", "plan"),
        ("subs", "deferred"),
    ]
    assert body["actions"] == []
    assert body["post_actions"] == []
    assert body["requires"] == []
    assert body["requires_encoders"] == []
    assert body["plan"]["container"] == "mkv"
    assert body["argv"][0] == "-y"
    assert body["argv"][-1] == str(api_env.lib_dir / "movie.mkv")
    assert [(entry["stream_index"], entry["outcome"]) for entry in body["stream_log"]] == [
        (0, "transcode"),
        (1, "copy"),
    ]

    resp = await api_env.client.post("/api/flows/dry-run", json={"file_id": 9999, "flow": hevc_flow()})
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("indexed", [True, False], ids=["file", "probe"])
@pytest.mark.parametrize("invalid", ["cycle", "unknown", "port"])
async def test_dry_run_validation(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, indexed: bool, invalid: str
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid drafts must not access media or evaluate")

    monkeypatch.setattr("server.api.flows.dry_run", forbidden)
    monkeypatch.setattr("server.api.flows.get_media_file", forbidden)
    monkeypatch.setattr("server.api.flows.probe_for_file", forbidden)
    monkeypatch.setattr(api_env.app.state, "db", forbidden)
    flow = hevc_flow()
    if invalid == "cycle":
        flow["edges"][0]["dst"] = "gate"
    elif invalid == "unknown":
        flow["nodes"][1]["plugin"] = "transcode.unknown"
    else:
        flow["edges"][0]["port"] = "invalid"
    payload = (
        {"flow": flow, "file_id": 1}
        if indexed
        else {
            "flow": flow,
            "probe": probe_from_raw(Path("/local/movie.mkv"), 100, PROBE_RAW).model_dump(mode="json"),
        }
    )
    response = await api_env.client.post("/api/flows/dry-run", json=payload)
    assert response.status_code == 422
    records = [record for record in caplog.records if record.name == "server.api.flows"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.__dict__["operation"] == "flow_dry_run"
    assert record.__dict__["flow_entry"] == "gate"
    if indexed:
        assert record.__dict__["file_id"] == 1
    else:
        assert record.__dict__["path"] == "/local/movie.mkv"


@pytest.mark.asyncio
@pytest.mark.parametrize("indexed", [True, False], ids=["file", "probe"])
async def test_dry_run_titles(api_env: ApiEnv, indexed: bool) -> None:
    from common.media.probe import probe_from_raw

    raw = {
        **PROBE_RAW,
        "streams": [
            {"index": 0, "codec_name": "h264", "codec_type": "video", "tags": {"title": "Main picture"}},
            {"index": 1, "codec_name": "ac3", "codec_type": "audio", "channels": 6},
            {
                "index": 2,
                "codec_name": "subrip",
                "codec_type": "subtitle",
                "tags": {"language": "spa", "title": "Spanish captions [SDH]"},
            },
        ],
    }
    payload: dict[str, object] = {
        "flow": graph_flow(("transcode.remux", {"container": "mp4", "subtitle_keep_languages": ["eng"]}))
    }
    if indexed:
        _, media_id = await seed_probed(api_env)
        async with api_env.factory() as session:
            probe = await session.scalar(select(ProbeRow).where(ProbeRow.media_file_id == media_id))
            assert probe is not None
            probe.raw_json = json.dumps(raw)
            await session.commit()
        payload["file_id"] = media_id
    else:
        source = probe_from_raw(Path("/local/sample.mkv"), 2048, raw)
        payload["probe"] = source.model_dump(mode="json")

    response = await api_env.client.post("/api/flows/dry-run", json=payload)

    assert response.status_code == 200
    assert [(entry["stream_index"], entry["title"], entry["outcome"]) for entry in response.json()["stream_log"]] == [
        (0, "Main picture", "copy"),
        (1, None, "copy"),
        (2, "Spanish captions [SDH]", "drop"),
    ]


@pytest.mark.asyncio
async def test_unindexed_preview(api_env: ApiEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from common.media.probe import probe_from_raw
    from common.media.tools import run_ffprobe

    media = tmp_path / "unindexed.mkv"
    media.write_bytes(b"untouched")
    raw = await run_ffprobe(FAKE_FFPROBE, media)
    probe = probe_from_raw(media, media.stat().st_size, raw)
    flow = {
        "entry": "dates",
        "nodes": [
            {"id": "dates", "type": "plugin", "plugin": "action.keep_dates"},
            {"id": "metadata", "type": "plugin", "plugin": "action.embed_metadata"},
            {"id": "cleanup", "type": "plugin", "plugin": "transcode.remux", "inputs": {"container": "mp4"}},
            {"id": "size", "type": "condition", "when": {"media.file.size_bytes": {"gte": 100}}},
            {"id": "encode", "type": "plugin", "plugin": "transcode.video"},
            {"id": "done", "type": "terminal", "outcome": "done"},
        ],
        "edges": [
            {"src": "dates", "port": "next", "dst": "metadata"},
            {"src": "metadata", "port": "next", "dst": "cleanup"},
            {"src": "cleanup", "port": "plan", "dst": "size"},
            {"src": "cleanup", "port": "skip", "dst": "size"},
            {"src": "size", "port": "true", "dst": "encode"},
            {"src": "size", "port": "false", "dst": "done"},
            {"src": "encode", "port": "plan", "dst": "done"},
        ],
        "actions": [{"kind": "extract_subtitles", "formats": ["srt"], "languages": ["eng"]}],
    }
    saved = await api_env.client.put("/api/flows/preview", json={"definition": flow})
    assert saved.status_code == 200
    definition = (await api_env.client.get("/api/flows/preview")).json()["definition"]

    def no_server_read(*args):
        pytest.fail("local preview must not inspect server paths")

    monkeypatch.setattr("server.flows.dryrun.sidecar_subtitles", no_server_read)
    monkeypatch.setattr("server.flows.dryrun.sidecar_metadata", no_server_read)
    response = await api_env.client.post(
        "/api/flows/dry-run",
        json={
            "probe": probe.model_dump(mode="json"),
            "flow": definition,
            "sidecar_subtitles": [],
            "sidecar_metadata": {"sources": {"nfo": {"title": "Supplied title"}}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == {"kind": "local", "path": str(media)}
    assert body["plan"]["video"] is None and body["continuation"]["node"] == "size"
    assert body["actions"] == [{"kind": "preserve_timestamps", "plugin_id": "action.keep_dates", "config_version": 1}]
    assert body["post_actions"] == [{"kind": "extract_subtitles", "formats": ["srt"], "languages": ["eng"]}]
    assert [(step["node"], step["outcome"]) for step in body["steps"]] == [
        ("dates", "actions"),
        ("metadata", "actions"),
        ("cleanup", "plan"),
        ("size", "deferred"),
        ("encode", "deferred"),
    ]
    assert body["argv"][body["argv"].index("-c") + 1] == "copy"
    assert "title=Supplied title" in body["argv"]
    assert media.read_bytes() == b"untouched"
    async with api_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(MediaFile)) == 0
        assert await session.scalar(select(func.count()).select_from(Job)) == 0
        assert await session.scalar(select(func.count()).select_from(Library)) == 0


@pytest.mark.asyncio
async def test_unresolved_encoder(api_env: ApiEnv) -> None:
    from common.media.probe import probe_from_raw

    flow = hevc_flow()
    flow["nodes"][1]["inputs"] = {"encoder_priority": ["libx265", "hevc_nvenc"]}
    probe = probe_from_raw(Path("/local/sample.mkv"), 100, PROBE_RAW)
    response = await api_env.client.post(
        "/api/flows/dry-run", json={"probe": probe.model_dump(mode="json"), "flow": flow}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["argv"] is None
    assert body["plan"]["video"]["encoder_priority"] == ["libx265", "hevc_nvenc"]
    assert [(step["node"], step["outcome"]) for step in body["steps"]] == [
        ("gate", "true"),
        ("enc", "plan"),
        ("enc", "deferred"),
        ("subs", "deferred"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
@pytest.mark.parametrize("batch_interval", [0, 0.5], ids=["immediate", "coalesced"])
async def test_transcode_enqueue_happy(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, batch_interval: float) -> None:
    # A pending status may be delivered before the queued job's full row.
    monkeypatch.setattr("server.events.EVENT_BATCH_INTERVAL", batch_interval)
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    events = api_env.app.state.events
    sub = events.subscribe()
    try:
        resp = await api_env.client.post(f"/api/files/{media_id}/transcode")
        assert resp.status_code == 202
        outcome = await requeue_outcome(api_env, resp.json()["job_id"], event_type="request_result")
        events.flush_bulk()
        job_id = resp.json()["job_id"]
        payload = None
        async with asyncio.timeout(2):
            while payload is None:
                event = await sub.next()
                assert event is not None
                if event.type == "events.batch":
                    payload = next(
                        (
                            item["payload"]
                            for item in event.payload["events"]
                            if item["type"] == "job.status"
                            and item["payload"]["job_id"] == job_id
                            and item["payload"]["status"] == "staging"
                            and "job" in item["payload"]
                        ),
                        None,
                    )
    finally:
        events.unsubscribe(sub)
    body = resp.json()
    assert body["status"] == "pending" and outcome["decision"]["status"] == "matched"
    listed_job = (await api_env.client.get("/api/jobs")).json()["items"][0]
    assert payload == {
        "job_id": job_id,
        "status": "staging",
        "priority": 0,
        "error": None,
        "warning": None,
        "finished_at": None,
        "job": listed_job,
    }

    async with api_env.factory() as session:
        job = await get_job(session, job_id)
        assert job is not None
        assert job.status == JobStatus.STAGING.value and job.kind == "transcode"
        assert job.plan is not None and job.plan["video"]["encoder"] == "libx265"
        assert job.continuation is not None and job.continuation["node"] == "subs"
        # Seeded builtin facts for the 2048-byte probe over 600 s: the bitrate
        # estimate rounds to 0 kbps and no pixel metadata means no bit_depth.
        assert job.facets == {
            "media.file.container": "avi",
            "media.file.duration_s": 600.0,
            "media.file.size_bytes": 2048,
            "media.file.estimated_bitrate_kbps": 0,
            "media.video.count": 1,
            "media.video.codec": "h264",
            "media.video.codec_family": "avc",
            "media.video.width": 1920,
            "media.video.height": 1080,
            "media.video.pixels": 2073600,
            "media.video.hdr_format": "sdr",
            "media.video.bitrate_kbps": 0,
            "media.video.bitrate_source": "estimated",
            "media.audio.count": 1,
            "media.audio.codecs": ["ac3"],
            "media.audio.languages": [],
            "media.audio.max_channels": 6,
            "media.audio.descriptive_stream_indices": [],
            "media.subtitle.count": 0,
            "media.subtitle.codecs": [],
            "media.subtitle.languages": [],
            "media.subtitle.forced_count": 0,
            "media.subtitle.forced_present": False,
            "media.attachment.count": 0,
            "media.data.count": 0,
        }
        assert job.decision_log is not None
        assert [(step["node"], step.get("step"), step["outcome"]) for step in job.decision_log] == [
            ("gate", None, "true"),
            ("enc", "transcode.video", "plan"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("skipped", [False, True])
@pytest.mark.parametrize("binding", ["default", "other", "none"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_manual_flow_snapshot(api_env: ApiEnv, skipped: bool, binding: str) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    definition = hevc_flow(quality=19)
    definition["nodes"][0]["when"] = {"media.file.size_bytes": {"gte": 2 * 2**30 if skipped else 0}}
    definition["_ui"] = {"nodes": {"gate": {"x": 30, "y": 40}}}
    saved = await api_env.client.patch(
        "/api/flows/hevc", json={"name": "manual", "definition": definition, "expected_version": 1}
    )
    assert saved.status_code == 200
    flow = saved.json()
    assert flow["name"] == "manual" and flow["version"] == 2
    expected = {key: flow[key] for key in ("id", "name", "version", "definition")}
    if binding != "default":
        async with api_env.factory() as session:
            library = await session.get(Library, library_id)
            assert library is not None
            if binding == "other":
                other = Flow(name="other", definition=json.dumps(hevc_flow(quality=30)))
                session.add(other)
                await session.flush()
                library.flow_id = other.id
            else:
                library.flow_id = None
            await session.commit()

    response = await api_env.client.post(
        f"/api/files/{media_id}/transcode",
        json={"flow_id": flow["id"], "expected_version": flow["version"]} if binding != "default" else None,
    )
    assert response.status_code == 202
    outcome = await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result")
    assert outcome["decision"]["status"] == ("skipped" if skipped else "matched")
    job_id = response.json()["job_id"]
    changed = await api_env.client.patch(
        "/api/flows/manual", json={"name": "changed", "definition": hevc_flow(), "expected_version": 2}
    )
    assert changed.status_code == 200

    async with api_env.factory() as session:
        job = await get_job(session, job_id)
        assert job is not None and job.flow_snapshot == expected
        if not skipped:
            assert job.plan is not None and job.plan["video"]["rate"]["quality"] == 19
    for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/export.json"):
        detail = await api_env.client.get(path)
        assert detail.status_code == 200
        assert detail.json()["flow_snapshot"] == expected


@pytest.mark.asyncio
async def test_selected_flow_validation(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    flow = (await api_env.client.get("/api/flows/hevc")).json()
    for body, status_code in (
        ({"flow_id": flow["id"], "expected_version": flow["version"] + 1}, 409),
        ({"flow_id": flow["id"] + 1, "expected_version": 1}, 404),
        ({"flow_id": 0, "expected_version": 1}, 422),
        ({"flow_id": flow["id"], "expected_version": 0}, 422),
        ({"flow_id": flow["id"]}, 422),
    ):
        response = await api_env.client.post(f"/api/files/{media_id}/transcode", json=body)
        assert response.status_code == status_code, response.text
    async with api_env.factory() as session:
        assert await session.scalar(select(func.count(Job.id))) == 0
        media = await session.get(MediaFile, media_id)
        assert media is not None and not media.transcode_decided and media.transcode_status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["skip", "veto", "error"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_selected_flow_isolation(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, decision: Literal["skip", "veto", "error"]
) -> None:
    from server.plugins.host import EvaluatedDecision

    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow(quality=30))
    _, sibling_id = await seed_media(api_env, "sibling.avi")
    other_library, other_id = await seed_media(api_env, "unrelated.avi")
    default = (await api_env.client.get("/api/flows/hevc")).json()
    async with api_env.factory() as session:
        sibling = await session.get(MediaFile, sibling_id)
        library = await session.get(Library, other_library)
        assert sibling is not None and library is not None
        sibling.library_id, library.flow_id = library_id, default["id"]
        for file_id in (media_id, sibling_id, other_id):
            media = await session.get(MediaFile, file_id)
            assert media is not None
            if media.probe_id is None:
                probe = ProbeRow(media_file_id=file_id, raw_json=json.dumps(PROBE_RAW))
                session.add(probe)
                await session.flush()
                media.probe_id = probe.id
            media.health_status = "healthy"
            media.health_source = {"path": media.path, "size": media.size}
        await session.commit()
    definition = hevc_flow(quality=19)
    if decision == "veto":
        definition["nodes"][0]["when"] = {"media.file.size_bytes": {"gte": 2**30}}
        definition["nodes"][3]["outcome"] = "veto"
    saved = await api_env.client.put("/api/flows/selected", json={"definition": definition})
    assert saved.status_code == 200, saved.text
    selected = saved.json()
    original = api_env.app.state.plugin_host.evaluate

    async def evaluate(plugin, ctx, inputs):
        if inputs.get("quality") == 19:
            return EvaluatedDecision(type=decision, error="Selected plugin failed." if decision == "error" else None)
        return await original(plugin, ctx, inputs)

    monkeypatch.setattr(api_env.app.state.plugin_host, "evaluate", evaluate)
    response = await api_env.client.post(
        f"/api/files/{media_id}/transcode", json={"flow_id": selected["id"], "expected_version": selected["version"]}
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    outcome = await requeue_outcome(api_env, job_id, event_type="request_result")
    assert outcome["decision"]["status"] == {"skip": "skipped", "veto": "vetoed", "error": "error"}[decision]
    assert await api_env.app.state.flow_enqueue._pass() == 2
    async with api_env.factory() as session:
        jobs = list(await session.scalars(select(Job).order_by(Job.id)))
        assert {job.media_file_id for job in jobs} == {media_id, sibling_id, other_id}
        assert len(jobs) == 3
        for job in jobs:
            assert job.flow_snapshot is not None
            if job.id == job_id:
                assert job.flow_snapshot["id"] == selected["id"]
                assert job.status == ("failed" if decision == "error" else "skipped")
            else:
                assert job.flow_snapshot["id"] == default["id"]
                assert job.plan is not None and job.plan["video"]["rate"]["quality"] == 30
        library = await session.get(Library, library_id)
        assert library is not None and library.flow_id == default["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "selected_flow"),
    [("unchanged", True), ("stale", True), ("unprepared", True), ("published", True), ("unprepared", False)],
)
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_manual_flow_retries(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, scenario: str, selected_flow: bool
) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow(quality=30))
    saved = (
        await api_env.client.put("/api/flows/selected", json={"definition": hevc_flow(quality=19)})
        if selected_flow
        else await api_env.client.get("/api/flows/hevc")
    )
    assert saved.status_code == 200, saved.text
    flow = saved.json()
    if scenario == "unprepared":
        monkeypatch.setenv("FAKE_FFPROBE_FAIL", "movie.avi")
        (api_env.lib_dir / "movie.avi").write_bytes(b"changed before preparation")
    response = await api_env.client.post(
        f"/api/files/{media_id}/transcode",
        json={"flow_id": flow["id"], "expected_version": flow["version"]} if selected_flow else None,
        params={"refresh": scenario == "unprepared"},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    outcome = await requeue_outcome(api_env, job_id, event_type="request_result")
    assert outcome["outcome"] == ("failed" if scenario == "unprepared" else "queued")
    monkeypatch.delenv("FAKE_FFPROBE_FAIL", raising=False)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        if scenario == "unprepared":
            assert job.plan is None
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.transcode_decided == selected_flow
        else:
            await finish(session, job_id, JobStatus.SUCCEEDED if scenario == "published" else JobStatus.FAILED)
        library = await session.get(Library, library_id)
        assert library is not None
        if selected_flow:
            library.flow_id = None
        await session.commit()
    changed = await api_env.client.put(
        f"/api/flows/{flow['name']}", json={"definition": hevc_flow(quality=35), "expected_version": flow["version"]}
    )
    assert changed.status_code == 200
    if selected_flow:
        deleted = await api_env.client.delete(
            "/api/flows/selected", params={"expected_version": changed.json()["version"]}
        )
        assert deleted.status_code == 204, deleted.text
    if scenario in {"stale", "published"}:
        (api_env.lib_dir / "movie.avi").write_bytes(b"new source for retry")
    before = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202, response.text
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    after = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert after["events"][: len(before["events"])] == before["events"]
    expected = flow if selected_flow else changed.json()
    assert after["flow_snapshot"] == {key: expected[key] for key in ("id", "name", "version", "definition")}
    assert after["plan"]["video"]["rate"]["quality"] == (19 if selected_flow else 35)
    assert after["status"] == "staging"
    captures = [event for event in after["events"] if event["type"] == "enqueued"]
    assert len(captures) == (1 if scenario in {"unchanged", "unprepared"} else 2)
    assert captures[-1]["payload"]["source"]["size"] == (api_env.lib_dir / "movie.avi").stat().st_size


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_bypasses_cooldown(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.cooldown_s = 3600
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.first_seen = utcnow()
        first_seen = media.first_seen
        await session.commit()
    resp = await api_env.client.post(f"/api/files/{media_id}/transcode")
    assert resp.status_code == 202
    assert (await requeue_outcome(api_env, resp.json()["job_id"], event_type="request_result"))["outcome"] == "queued"
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.first_seen is not None
        assert media.first_seen.replace(tzinfo=UTC) == first_seen.replace(tzinfo=UTC)
        job = await session.scalar(select(Job).where(Job.media_file_id == media_id))
        assert job is not None and job.status == JobStatus.STAGING.value and job.hold_until is None


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_veto_no(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(
        api_env,
        library_id,
        {
            "entry": "gate",
            "nodes": [
                {"type": "condition", "id": "gate", "when": {"media.file.size_bytes": {"gte": 2 * 2**30}}},
                {"type": "plugin", "id": "enc", "plugin": "transcode.video", "inputs": {}},
                {"type": "terminal", "id": "veto", "outcome": "veto"},
            ],
            "edges": [
                {"src": "gate", "port": "true", "dst": "enc"},
                {"src": "gate", "port": "false", "dst": "veto"},
            ],
        },
        name="gate",
    )
    resp = await api_env.client.post(f"/api/files/{media_id}/transcode")
    assert resp.status_code == 202
    body = resp.json()
    outcome = await requeue_outcome(api_env, body["job_id"], event_type="request_result")
    assert outcome["outcome"] == "not_queued" and outcome["decision"]["status"] == "vetoed"
    async with api_env.factory() as session:
        count = (await session.execute(select(func.count()).select_from(Job))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("active", ["hold", "staging", "queued", "claimed", "running", "verifying", "finalizing"])
@pytest.mark.parametrize("outcome", [JobStatus.FAILED, JobStatus.CANCELLED])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_file_status_lifecycle(api_env: ApiEnv, active: str, outcome: JobStatus) -> None:
    library_id, media_id = await seed_media(api_env, "work.avi")
    pending_library, pending_id = await seed_media(api_env, "waiting.avi")
    initial = await api_env.client.get(f"/api/files/{media_id}")
    assert initial.json()["transcode_status"] == "pending"
    async with api_env.factory() as session:
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        job.status = active
        await enqueue_healthcheck_job(session, pending_id, pending_library, now=T0)
        await session.commit()
        job_id = job.id

    detail = await api_env.client.get(f"/api/files/{media_id}")
    assert detail.json()["transcode_status"] == active
    assert detail.json()["active_job_status"] == active
    for status, expected in [(active, media_id), ("pending", pending_id)]:
        for query in [None, ".avi"]:
            params = {"transcode_status": status}
            if query:
                params["query"] = query
            files = await api_env.client.get("/api/files", params=params)
            assert [file["id"] for file in files.json()["items"]] == [expected]
            assert files.json()["pagination"]["total"] == 1
    await api_env.app.state.stats.refresh()
    overview = await api_env.client.get("/api/stats/overview")
    assert overview.json()["inventory"]["transcode_status"] == {active: 1, "pending": 1}
    sorted_files = await api_env.client.get("/api/files", params={"sort_by": "transcode_status", "sort_dir": "asc"})
    expected_order = [id for _, id in sorted([(active, media_id), ("pending", pending_id)])]
    assert [file["id"] for file in sorted_files.json()["items"]] == expected_order

    async with api_env.factory() as session:
        assert await finish(session, job_id, outcome, error="did not publish", now=T0)
        await session.commit()
    terminal = await api_env.client.get(f"/api/files/{media_id}")
    assert terminal.json()["transcode_status"] == outcome.value
    assert terminal.json()["active_job_id"] is None
    files = await api_env.client.get("/api/files", params={"transcode_status": outcome.value})
    assert [file["id"] for file in files.json()["items"]] == [media_id]
    deleted, outcomes = await delete_requested(api_env, [job_id], bulk=False)
    assert deleted.status_code == 202 and outcomes[job_id]["outcome"] == "deleted"
    after = await api_env.client.get(f"/api/files/{media_id}")
    assert after.json()["transcode_status"] == outcome.value
    assert after.json()["jobs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_skip_history(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(
        api_env,
        library_id,
        {
            "entry": "gate",
            "nodes": [
                {"type": "condition", "id": "gate", "when": {"media.file.size_bytes": {"gte": 2 * 2**30}}},
                {"type": "terminal", "id": "skip", "outcome": "done"},
                {"type": "plugin", "id": "enc", "plugin": "transcode.video", "inputs": {}},
            ],
            "edges": [
                {"src": "gate", "port": "false", "dst": "skip"},
                {"src": "gate", "port": "true", "dst": "enc"},
            ],
        },
    )
    events = api_env.app.state.events
    sub = events.subscribe()
    try:
        resp = await api_env.client.post(f"/api/files/{media_id}/transcode")
    finally:
        events.unsubscribe(sub)
    body = resp.json()
    assert resp.status_code == 202
    assert body["job_id"] is not None
    outcome = await requeue_outcome(api_env, body["job_id"], event_type="request_result")
    assert outcome["decision"]["status"] == "skipped"
    assert outcome["decision"]["steps"][0]["outcome"] == "false"
    job_id = body["job_id"]
    listed = await api_env.client.get("/api/jobs", params={"status": "skipped"})
    detail = await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})
    assert listed.status_code == detail.status_code == 200
    assert listed.json()["items"][0]["status"] == "skipped"
    assert listed.json()["items"][0]["file"]["id"] == media_id
    assert detail.json()["status"] == "skipped"
    assert detail.json()["finished_at"] is not None
    assert detail.json()["attempts"] == []
    assert detail.json()["decision_log"] == outcome["decision"]["steps"]
    assert [
        (event["type"], event["payload"]["status"]) for event in detail.json()["events"] if event["type"] == "skipped"
    ] == [("skipped", "skipped")]
    assert detail.json()["error"] is None
    retry = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert retry.status_code == 409
    file_detail = await api_env.client.get(f"/api/files/{media_id}")
    assert file_detail.json()["transcode_status"] == "not_needed"
    files = await api_env.client.get("/api/files", params={"transcode_status": "not_needed"})
    assert [file["id"] for file in files.json()["items"]] == [media_id]
    async with api_env.factory() as session:
        job = await get_job(session, job_id)
        assert job is not None and job.plan is None and job.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("decision", ["matched", "skipped", "vetoed", "error"])
@pytest.mark.parametrize("previous", ["not_needed", "transcoded"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_recheck_file_status(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, refresh: bool, decision: str, previous: str
) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.transcode_status = previous
        media.transcode_decided = True
        path = Path(media.path)
        await session.commit()
    if refresh:
        path.write_bytes(b"replacement source")

    async def evaluate(*_args):
        return FlowResult(status=decision, plan=FFmpegPlan() if decision == "matched" else None)

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/files/{media_id}/transcode", params={"refresh": refresh})
    assert response.status_code == 202, response.text
    await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result")
    expected = (
        "staging"
        if decision == "matched"
        else previous
        if previous == "transcoded"
        else {"skipped": "not_needed", "vetoed": "rejected", "error": "failed"}[decision]
    )
    file = await api_env.client.get(f"/api/files/{media_id}")
    assert file.json()["transcode_status"] == expected
    filtered = await api_env.client.get("/api/files", params={"transcode_status": expected})
    assert [file["id"] for file in filtered.json()["items"]] == [media_id]
    if refresh and previous == "not_needed" and decision in {"vetoed", "error"}:
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.transcode_decided


@pytest.mark.asyncio
async def test_transcode_requires_flow(api_env: ApiEnv) -> None:
    _, media_id = await seed_probed(api_env)
    resp = await api_env.client.post(f"/api/files/{media_id}/transcode")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_transcode_active_conflict(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    async with api_env.factory() as session:
        await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()
    resp = await api_env.client.post(f"/api/files/{media_id}/transcode")
    assert resp.status_code == 409
