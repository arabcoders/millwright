from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.plan import FFmpegPlan
from common.protocol import NodeCapabilities
from common.enums import JobStatus
from server.flows.engine import FlowResult
from server.models import Flow, Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow
from server.nodehub.hub import NodeEntry
from server.nodehub.store import get_node
from server.queue.enqueue import FlowEnqueueService
from server.queue.store import (
    cancel_pending,
    capture_source,
    enqueue_file_job,
    finish,
    record_flow_decision,
    release_expired_holds,
    requeue,
)
from tests.helpers import database_context, graph_flow
from tests.test_api import PROBE_RAW, ApiEnv, api_env, requeue_outcome, seed_media
from server.serialize import orm_columns
from tests.test_job_requests import T0, JobRequests, requests
from tests.test_flow_enqueue import remux_definition, seed
from tests.hub_helpers import FakeConn
from tests.test_hub import Env, env, file_job, hub
from tests.test_staging import add_job


@pytest.mark.asyncio
async def test_hold_cancel_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    async with database_context("hold-cancellation") as factory:
        job_id = await add_job(factory, source, status="hold", hold_until=T0)
        execute = AsyncSession.execute
        selected = False

        async def cancel_selected(session, statement, *args, **kwargs):
            nonlocal selected
            result = await execute(session, statement, *args, **kwargs)
            if not selected and statement.is_select and statement.column_descriptions[0]["entity"] is Job:
                selected = True
                async with factory() as writer:
                    assert await cancel_pending(writer, job_id, now=T0) is not None
                    await writer.commit()
            return result

        monkeypatch.setattr(AsyncSession, "execute", cancel_selected)
        async with factory() as session:
            released = await asyncio.wait_for(release_expired_holds(session, T0), 3)
            await session.commit()
        assert selected
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.status == JobStatus.CANCELLED
            assert released == []
            assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "released")) is None


@pytest.mark.asyncio
async def test_requeue_cancel_check(requests: JobRequests) -> None:
    async with requests.factory() as session:
        job = await session.get(Job, requests.job_id)
        attempt = await session.get(JobAttempt, requests.attempt_id)
        assert job is not None and attempt is not None
        job.status, job.finished_at = "claimed", None
        attempt.status, attempt.result, attempt.finished_at = "claimed", None, None
        await session.commit()
    async with requests.factory() as stale:
        cached = await stale.get(Job, requests.job_id)
        assert cached is not None and cached.status == JobStatus.CLAIMED
        async with requests.factory() as writer:
            assert await finish(writer, requests.job_id, JobStatus.CANCELLED, error="cancelled", now=T0)
            await writer.commit()
        retried = await requeue(stale, requests.job_id, now=T0)
        await stale.commit()
    async with requests.factory() as session:
        job = await session.get(Job, requests.job_id)
        attempt = await session.get(JobAttempt, requests.attempt_id)
        assert job is not None and job.status == JobStatus.CANCELLED
        assert retried is None
        assert attempt is not None and attempt.status == JobStatus.CANCELLED
        assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "retried")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "bulk"])
@pytest.mark.parametrize("kind", ["transcode", "healthcheck"])
async def test_retry_suspended(api_env: ApiEnv, operation: str, kind: str) -> None:
    source = api_env.lib_dir / "movie.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(api_env.factory, source, status="failed")
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.kind = kind
        library = await session.get(Library, job.library_id)
        assert library is not None
        library.roots, library.suspended = [str(api_env.lib_dir)], True
        await session.commit()
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        before = {column.name: getattr(job, column.name) for column in Job.__table__.columns}
    if operation == "single":
        response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 409
    else:
        response = await api_env.client.post("/api/jobs/bulk", json={"action": "retry", "job_ids": [job_id]})
        assert response.status_code == 202
        assert response.json()["accepted"] == []
        assert [row["id"] for row in response.json()["failed"]] == [job_id]
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert {column.name: getattr(job, column.name) for column in Job.__table__.columns} == before
        assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "retried")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("mutation", ["before", "during"])
@pytest.mark.parametrize("decision", ["matched", "skipped"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_enqueue_root_check(
    api_env: ApiEnv,
    monkeypatch: pytest.MonkeyPatch,
    refresh: bool,
    mutation: str,
    decision: Literal["matched", "skipped"],
) -> None:
    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        flow = Flow(name="request_result", definition=json.dumps(graph_flow("transcode.remux")))
        session.add(flow)
        await session.flush()
        library.flow_id = flow.id
        stored = ProbeRow(media_file_id=media_id, raw_json=json.dumps(PROBE_RAW))
        session.add(stored)
        await session.flush()
        media.probe_id = stored.id
        if mutation == "before":
            library.roots = [str(api_env.lib_dir / "other")]
        await session.commit()

    async def evaluate(*_args):
        if mutation == "during":
            async with api_env.factory() as writer:
                library = await writer.get(Library, library_id)
                assert library is not None
                library.roots = [str(api_env.lib_dir / "other")]
                await writer.commit()
        return FlowResult(status=decision, plan=FFmpegPlan() if decision == "matched" else None)

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    if refresh:
        async with api_env.factory() as session:
            old = Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="succeeded")
            session.add(old)
            await session.commit()
        response = await asyncio.wait_for(api_env.client.post(f"/api/jobs/{old.id}/retry"), 3)
    else:
        response = await asyncio.wait_for(api_env.client.post(f"/api/files/{media_id}/transcode"), 3)
    assert response.status_code == 202
    job_id = response.json()["id"] if refresh else response.json()["job_id"]
    assert (await requeue_outcome(api_env, job_id, event_type="requeue" if refresh else "request_result"))[
        "outcome"
    ] == "failed"
    async with api_env.factory() as session:
        assert not (await session.scalars(select(Job.id).where(Job.status == JobStatus.STAGING))).all()
        media = await session.get(MediaFile, media_id)
        assert media is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "bulk"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_retry_root_check(api_env: ApiEnv, operation: str) -> None:
    source = api_env.lib_dir / "movie.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(api_env.factory, source, status="failed")
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        library = await session.get(Library, job.library_id)
        assert library is not None
        library.roots = [str(api_env.lib_dir / "other")]
        await session.commit()
        before = orm_columns(job)
    if operation == "single":
        response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 202
    else:
        response = await api_env.client.post("/api/jobs/bulk", json={"action": "retry", "job_ids": [job_id]})
        assert response.status_code == 202
        assert response.json() == {"accepted": [job_id], "failed": []}
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "failed"
    async with api_env.factory() as session:
        saved = await session.get(Job, job_id)
        assert saved is not None
        after = orm_columns(saved)
        assert {key: value for key, value in after.items() if key != "updated_at"} == {
            key: value for key, value in before.items() if key != "updated_at"
        }


@pytest.mark.asyncio
async def test_skip_suspended(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id, library_id = await seed(api_env.factory, definition=remux_definition())
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.suspended = True
        await session.commit()

    async def evaluate(*_args):
        return FlowResult(status="skipped", reason="manual skip")

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await asyncio.wait_for(api_env.client.post(f"/api/files/{media_id}/transcode"), 3)
    assert response.status_code == 409
    async with api_env.factory() as session:
        assert not (await session.scalars(select(Job.id))).all()
        media = await session.get(MediaFile, media_id)
        assert media is not None and not media.transcode_decided


@pytest.mark.asyncio
async def test_dispatch_library_fairness(env: Env) -> None:
    first = await file_job(env, "first.avi")
    second = await file_job(env, "second.avi")
    async with env.factory() as session:
        library = (await session.scalars(select(Library))).one()
        library.name = "first-library"
        await session.commit()
    third = await file_job(env, "third.avi")
    fourth = await file_job(env, "fourth.avi")
    urgent = await file_job(env, "urgent.avi", priority=10)
    async with env.factory() as session:
        node = await get_node(session, "node-a")
        assert node is not None
    connection = FakeConn()
    entry = NodeEntry(
        name="node-a",
        node_db_id=node.id,
        capabilities=NodeCapabilities(encoders=["libx264"], max_transcodes=2),
        workspace_ready=True,
        conn=connection,
    )
    service = hub(env)
    service.entries[entry.name] = entry
    assert await asyncio.wait_for(service.dispatch_once(), 3) == 2
    assert [message["job"]["job_id"] for message in connection.messages("job")] == [urgent, first]
    async with env.factory() as session:
        for job_id in (urgent, first):
            assert await finish(session, job_id, JobStatus.CANCELLED)
        await session.commit()
    entry.in_flight.clear()
    assert await asyncio.wait_for(service.dispatch_once(), 3) == 2
    assert [message["job"]["job_id"] for message in connection.messages("job")] == [urgent, first, third, second]
    async with env.factory() as session:
        saved = await session.get(Job, fourth)
        assert saved is not None and saved.status == JobStatus.QUEUED
        attempts = list((await session.scalars(select(JobAttempt))).all())
        assert len(attempts) == 4 and all(attempt.node_id == node.id for attempt in attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [False, True])
async def test_claim_root_check(env: Env, stage: bool) -> None:
    job_id = await file_job(env)
    async with env.factory() as session:
        node = await get_node(session, "node-a")
        assert node is not None
    connection = FakeConn()
    service = hub(env)
    service.entries["node-a"] = NodeEntry(
        name="node-a",
        node_db_id=node.id,
        capabilities=NodeCapabilities(encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=connection,
    )
    if stage:
        assert await asyncio.wait_for(service.dispatch_once(), 3) == 1
        async with env.factory() as session:
            job = await session.get(Job, job_id)
            attempt = (await session.scalars(select(JobAttempt))).one()
            event = (await session.scalars(select(JobEvent).where(JobEvent.type == "stage"))).one()
            assert job is not None and attempt.output_path is not None
            (env.workspace / attempt.output_path).write_bytes(b"stage")
            job.status, job.node_id = JobStatus.RUNNING.value, None
            attempt.status = JobStatus.RUNNING.value
            event.payload = {
                **event.payload,
                "number": 2,
                "probe": {**event.payload["probe"], "file_path": attempt.output_path, "size_bytes": 5},
            }
            await session.commit()
        service.entries["node-a"].in_flight.clear()
        connection.sent.clear()
    async with env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        library = await session.get(Library, job.library_id)
        assert library is not None
        library.roots = [str(env.library / "other")]
        await session.commit()
    async with env.factory() as session:
        before = [
            {column.name: getattr(attempt, column.name) for column in JobAttempt.__table__.columns}
            for attempt in (await session.scalars(select(JobAttempt))).all()
        ]
    offered = await asyncio.wait_for(service.dispatch_once(), 3)
    async with env.factory() as session:
        saved = await session.get(Job, job_id)
        assert saved is not None and saved.status == (JobStatus.RUNNING if stage else JobStatus.QUEUED)
        assert [
            {column.name: getattr(attempt, column.name) for column in JobAttempt.__table__.columns}
            for attempt in (await session.scalars(select(JobAttempt))).all()
        ] == before
        assert offered == 0 and not connection.messages("job")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["clock", "schedule", "suspend", "roots", "media"])
async def test_dispatch_schedule_check(env: Env, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    job_id = await file_job(env)
    async with env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        library = await session.get(Library, job.library_id)
        assert library is not None
        library.schedule = {"type": "grid", "cells": [[hour == 12 for hour in range(24)] for _ in range(7)]}
        node = await get_node(session, "node-a")
        assert node is not None
        await session.commit()
    connection = FakeConn()
    service = hub(env)
    service.entries["node-a"] = NodeEntry(
        name="node-a",
        node_db_id=node.id,
        capabilities=NodeCapabilities(encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=connection,
    )
    scalar = AsyncSession.scalar
    get = AsyncSession.get
    changed = False
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_window(session, statement, *args, **kwargs):
        nonlocal changed
        closing = mutation == "clock" and statement.is_update and statement.table.name == "jobs"
        if not changed and closing:
            changed = True
            entered.set()
            await release.wait()
        return await scalar(session, statement, *args, **kwargs)

    async def lock_policy(session, entity, identifier, **kwargs):
        nonlocal changed
        if mutation != "clock" and not changed and entity is Library and kwargs.get("with_for_update"):
            changed = True
            entered.set()
            await release.wait()
        return await get(session, entity, identifier, **kwargs)

    async def mutate() -> None:
        if mutation == "clock":
            env.clock.advance(3600)
            return
        async with env.factory() as writer:
            current = await writer.get(Library, library.id)
            assert current is not None
            if mutation == "schedule":
                current.schedule = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
            elif mutation == "suspend":
                current.suspended = True
            elif mutation == "roots":
                current.roots = [str(env.library / "other")]
            else:
                media = await writer.get(MediaFile, job.media_file_id)
                assert media is not None
                media.path = str(env.workspace / "outside.avi")
            await writer.commit()

    monkeypatch.setattr(AsyncSession, "scalar", close_window)
    monkeypatch.setattr(AsyncSession, "get", lock_policy)
    dispatching = asyncio.create_task(service.dispatch_once())
    changing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        changing = asyncio.create_task(mutate())
        await asyncio.wait_for(changing, 2)
        release.set()
        offered = await asyncio.wait_for(dispatching, 3)
    finally:
        release.set()
        tasks = [dispatching] + ([changing] if changing is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        await asyncio.wait_for(service.aclose(), 5)
    assert changed
    async with env.factory() as session:
        saved = await session.get(Job, job_id)
        assert saved is not None and saved.status == JobStatus.QUEUED
        assert not (await session.scalars(select(JobAttempt.id))).all()
        assert offered == 0 and not connection.messages("job")


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", ["cancelled", "skipped"])
@pytest.mark.parametrize("automatic", ["matched", "skipped"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_automatic_decision_check(
    api_env: ApiEnv,
    monkeypatch: pytest.MonkeyPatch,
    manual: str,
    automatic: Literal["matched", "skipped"],
) -> None:
    media_id, library_id = await seed(api_env.factory, definition=remux_definition())
    inserted: list[int] = []

    async def evaluate(*_args):
        async with api_env.factory() as writer:
            media = await writer.get(MediaFile, media_id)
            assert media is not None
            source = await capture_source(media)
            if manual == "cancelled":
                job = await enqueue_file_job(writer, media_id, library_id, FFmpegPlan(), source=source)
                assert await cancel_pending(writer, job.id) is not None
            else:
                job = await record_flow_decision(
                    writer,
                    media_id,
                    library_id,
                    [],
                    "manual skip",
                    status="skipped",
                    source=source,
                    first_seen=media.first_seen,
                )
            assert job is not None
            inserted.append(job.id)
            await writer.commit()
        return FlowResult(status=automatic, plan=FFmpegPlan() if automatic == "matched" else None)

    monkeypatch.setattr("server.queue.enqueue.evaluate", evaluate)
    service = FlowEnqueueService(api_env.factory, api_env.app.state.plugin_host)
    processed = await asyncio.wait_for(service._pass(), 3)
    async with api_env.factory() as session:
        assert list((await session.scalars(select(Job.id).order_by(Job.id))).all()) == inserted
        assert processed == 0
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.transcode_decided


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["roots", "suspend"])
@pytest.mark.parametrize("automatic", ["matched", "skipped"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_automatic_library_check(
    api_env: ApiEnv,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    automatic: Literal["matched", "skipped"],
) -> None:
    media_id, library_id = await seed(api_env.factory, definition=remux_definition())

    async def evaluate(*_args):
        async with api_env.factory() as writer:
            library = await writer.get(Library, library_id)
            assert library is not None
            if mutation == "roots":
                library.roots = [str(api_env.lib_dir)]
            else:
                library.suspended = True
            await writer.commit()
        return FlowResult(status=automatic, plan=FFmpegPlan() if automatic == "matched" else None)

    monkeypatch.setattr("server.queue.enqueue.evaluate", evaluate)
    service = FlowEnqueueService(api_env.factory, api_env.app.state.plugin_host)
    assert await asyncio.wait_for(service._pass(), 3) == 0
    async with api_env.factory() as session:
        assert not (await session.scalars(select(Job.id))).all()
        media = await session.get(MediaFile, media_id)
        assert media is not None and not media.transcode_decided
