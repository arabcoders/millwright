from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from common.process import drain
from common.enums import JobStatus
from sqlalchemy import event, select, update
from sqlalchemy.exc import InterfaceError, OperationalError

from server.flows.engine import FlowResult
from server.fs.library import LibraryFS
from server.fs import source as source_fs
from server.models import Job, JobAttempt, JobEvent, Library, NodeRow
from tests.test_api import T0
from server.queue.store import enqueue_file_job, finish, processing_backlog
from tests.test_api import (
    ApiEnv,
    api_env,
    hevc_flow,
    plan,
    requeue_outcome,
    seed_flow_library,
    seed_media,
    seed_probed,
    seed_published,
)
from tests.test_files_bulk import probed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["cleanup", "probe"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_acceptance_during_work(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    job_id, _ = await seed_published(api_env)
    service = api_env.app.state.jobs
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == "cleanup":
        original = service.staging.prune_job

        async def blocked(*args, **kwargs):
            entered.set()
            await drain(asyncio.create_task(release.wait()))
            return await original(*args, **kwargs)

        monkeypatch.setattr(service.staging, "prune_job", blocked)
    else:
        original = LibraryFS.refresh_current

        async def blocked(self, *args, **kwargs):
            entered.set()
            await drain(asyncio.create_task(release.wait()))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(LibraryFS, "refresh_current", blocked)

    async def evaluate(*_args):
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    try:
        response = await asyncio.wait_for(api_env.client.post(f"/api/jobs/{job_id}/retry"), 2)
        assert response.status_code == 202 and response.json() == {"id": job_id, "status": "pending"}
        await asyncio.wait_for(entered.wait(), 2)
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "pending" and job.attempts == 1
            event = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "requested")
            )
            assert event is not None and event.payload["previous"]["status"] == "succeeded"
        assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 409
        release.set()
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_bulk_responds_first(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = []
    for name in ("first.avi", "second.avi"):
        library_id, media_id = await seed_media(api_env, name)
        async with api_env.factory() as session:
            job = await enqueue_file_job(session, media_id, library_id, plan())
            await finish(session, job.id, JobStatus.FAILED)
            await session.commit()
            ids.append(job.id)
    entered, release = asyncio.Event(), asyncio.Event()
    service = api_env.app.state.jobs
    original = service.staging.prune_job

    async def blocked(*args, **kwargs):
        entered.set()
        await drain(asyncio.create_task(release.wait()))
        return await original(*args, **kwargs)

    monkeypatch.setattr(service.staging, "prune_job", blocked)
    try:
        response = await asyncio.wait_for(
            api_env.client.post("/api/jobs/bulk", json={"action": "retry", "job_ids": [*ids, 9999]}), 2
        )
        assert response.status_code == 202
        assert response.json()["accepted"] == ids
        assert [failure["id"] for failure in response.json()["failed"]] == [9999]
        await asyncio.wait_for(entered.wait(), 2)
        async with api_env.factory() as session:
            assert list(await session.scalars(select(Job.status).where(Job.id.in_(ids)))) == ["pending", "pending"]
        release.set()
        for job_id in ids:
            assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["cleanup", "probe", "evaluation"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_cancel_drains_work(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    job_id, media_id = await seed_published(api_env)
    service = api_env.app.state.jobs
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def block():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            draining.set()
            await drain(asyncio.create_task(release.wait()))
            raise

    if phase == "cleanup":
        original = service.staging.prune_job

        async def blocked(*args, **kwargs):
            await block()
            return await original(*args, **kwargs)

        monkeypatch.setattr(service.staging, "prune_job", blocked)
    elif phase == "probe":
        original = LibraryFS.refresh_current

        async def blocked(self, *args, **kwargs):
            await block()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(LibraryFS, "refresh_current", blocked)
    else:

        async def evaluate(*_args):
            await block()
            return FlowResult(status="matched", plan=plan())

        monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    try:
        assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 202
        await asyncio.wait_for(entered.wait(), 2)
        before = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()["attempts"]
        response = await asyncio.wait_for(api_env.client.post(f"/api/jobs/{job_id}/cancel"), 2)
        assert response.status_code == 202 and response.json() == {"id": job_id, "status": "cancelling"}
        await asyncio.wait_for(draining.wait(), 2)
        assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 409
        assert (await api_env.client.post(f"/api/files/{media_id}/healthcheck")).status_code == 409
        release.set()
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "cancelled"
        after = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
        assert after["status"] == "cancelled" and after["attempts"] == before
        assert not service.staging._wake.is_set()
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_close_drains_connection(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    service = api_env.app.state.jobs
    await service.close()
    entered, release, closing_started, acquired = (asyncio.Event() for _ in range(4))
    connect = api_env.factory.class_.connection

    async def connection(session, *args, **kwargs):
        entered.set()
        await release.wait()
        result = await connect(session, *args, **kwargs)
        acquired.set()
        return result

    monkeypatch.setattr(api_env.factory.class_, "connection", connection)

    async def close():
        closing_started.set()
        await service.close()

    closing = None
    try:
        await service.start()
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(close())
        await asyncio.wait_for(closing_started.wait(), 2)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 2)
        assert acquired.is_set()
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_queue_batches_requests(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server import jobs

    service = api_env.app.state.jobs
    await service.close()
    monkeypatch.setattr(jobs, "REQUEST_BATCH", 2)
    async with api_env.factory() as session:
        libraries = [Library(name=name, roots=[str(api_env.lib_dir)]) for name in ("draining", "active")]
        session.add_all(libraries)
        await session.flush()
        rows = [
            Job(
                media_file_id=index + 1,
                library_id=libraries[0 if index == 0 else 1].id,
                status="failed" if index == 2 else "pending",
                delete_requested=index == 2,
            )
            for index in range(6)
        ]
        session.add_all(rows)
        await session.commit()
    visited = []
    complete, release = asyncio.Event(), asyncio.Event()
    queries = []

    def executed(_conn, _cursor, statement, parameters, _context, _many):
        if statement.startswith("SELECT jobs.id, jobs.library_id"):
            assert "LIMIT" in statement and parameters["param_1"] == 2
            queries.append(parameters)

    async def process(job_id):
        assert len(service._tasks) == 1
        visited.append(job_id)
        async with api_env.factory() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(status="queued", delete_requested=False))
            await session.commit()
        if job_id == rows[-1].id:
            complete.set()
            await release.wait()

    monkeypatch.setattr(service, "_process", process)
    engine = api_env.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", executed)
    try:
        await service.start()
        service._draining.add(libraries[0].id)
        await asyncio.wait_for(complete.wait(), 3)
        assert visited == [row.id for row in rows[1:]]
        assert len(queries) == 3
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 3)
        event.remove(engine, "before_cursor_execute", executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_restart_resumes_request(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id, _ = await seed_published(api_env)
    service = api_env.app.state.jobs
    entered = asyncio.Event()
    original = service.staging.prune_job

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.staging, "prune_job", blocked)
    assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 202
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(service.close(), 2)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == "pending"
    monkeypatch.setattr(service.staging, "prune_job", original)

    async def evaluate(*_args):
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    await service.start()
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["shutdown", "reset", "library"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_lifecycle_drains_worker(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    job_id, media_id = await seed_published(api_env)
    service = api_env.app.state.jobs
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await drain(asyncio.create_task(release.wait()))
            raise

    monkeypatch.setattr(service.staging, "prune_job", blocked)
    ending = None
    try:
        assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 202
        await asyncio.wait_for(entered.wait(), 2)
        if operation == "shutdown":
            ending = asyncio.create_task(service.close())
        elif operation == "reset":
            api_env.app.state.services_running = True
            api_env.app.state.auth.config.api_key = "test-job-service"
            api_env.client.headers["Authorization"] = "Bearer test-job-service"
            ending = asyncio.create_task(api_env.client.post("/api/backups/reset", json={"keep": []}))
            # Keep reset's service restarts deterministic and avoid starting unrelated loops.
            for owner, name in (
                (api_env.app.state.scanner, "start"),
                (api_env.app.state.healthchecks, "start"),
                (api_env.app.state.staging, "start"),
                (api_env.app.state.scheduler, "start"),
                (api_env.app.state.notifications, "start"),
            ):
                monkeypatch.setattr(owner, name, lambda: None)

            async def start():
                pass

            monkeypatch.setattr(api_env.app.state.probes, "start", start)
            monkeypatch.setattr(api_env.app.state.flow_enqueue, "start", start)
        else:
            api_env.app.state.services_running = True
            async with api_env.factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                library_id = job.library_id
            await api_env.app.state.library_deletion.start()
            response = await api_env.client.delete(f"/api/libraries/{library_id}")
            assert response.status_code == 202
            ending = api_env.app.state.library_deletion._tasks[library_id]
        await asyncio.wait_for(draining.wait(), 2)
        assert not ending.done()
        release.set()
        result = await asyncio.wait_for(ending, 5)
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            if operation == "shutdown":
                assert job is not None and job.status == "pending"
            elif operation == "reset":
                assert result.status_code == 200, result.text
                assert job is None
            else:
                assert job is not None and job.status == "cancelled"
                assert await session.get(Library, library_id) is None
    finally:
        release.set()
        if ending is not None:
            await asyncio.wait_for(asyncio.gather(ending, return_exceptions=True), 5)
        await asyncio.wait_for(api_env.app.state.library_deletion.close(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_healthcheck_covers_publication(api_env: ApiEnv) -> None:
    job_id, file_id = await seed_published(api_env)
    async with api_env.factory() as session:
        attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
        assert attempt is not None
        marker = JobEvent(job_id=job_id, attempt_id=attempt.id, type="artifact_check_pending")
        session.add(marker)
        await session.commit()
    response = await api_env.client.post(f"/api/files/{file_id}/healthcheck")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    async with api_env.factory() as session:
        completed = await session.get(JobEvent, marker.id)
        assert completed is not None and completed.type == "artifact_check_finished"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["retry", "transcode", "healthcheck"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_minimal_queue_event(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    if operation == "retry":
        job_id, media_id = await seed_published(api_env)
        endpoint = f"/api/jobs/{job_id}/retry"
    else:
        library_id, media_id = await seed_probed(api_env)
        if operation == "transcode":
            await seed_flow_library(api_env, library_id, hevc_flow())
        endpoint = f"/api/files/{media_id}/{operation}"
    await api_env.app.state.jobs.close()
    api_env.app.state.jobs._closed = False
    bus = api_env.app.state.events
    captured = []
    original = bus.publish

    def publish(kind, payload, **kwargs):
        captured.append((kind, payload))
        original(kind, payload, **kwargs)

    monkeypatch.setattr(bus, "publish", publish)
    response = await api_env.client.post(endpoint)
    assert response.status_code == 202
    job_id = response.json()["id"] if operation == "retry" else response.json()["job_id"]
    pending = next(payload for kind, payload in captured if kind == "job.status")
    assert set(pending) == {"job_id", "media_file_id", "kind", "status", "updated_at"}
    assert pending["job_id"] == job_id and pending["media_file_id"] == media_id
    assert pending["kind"] == ("healthcheck" if operation == "healthcheck" else "transcode")
    assert pending["status"] == ("queued" if operation == "healthcheck" else "pending")
    assert isinstance(pending["updated_at"], str)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "error", "skipped", "vetoed"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_pending_clears_result(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    job_id, file_id = await seed_published(api_env)
    service = api_env.app.state.jobs
    await service.close()
    service._closed = False
    async with api_env.factory() as session:
        node = NodeRow(name="previous-node", display_name="Previous node", token_hash="token")
        session.add(node)
        await session.flush()
        job = await session.get(Job, job_id)
        assert job is not None
        job.status = "failed"
        job.progress = {"percent": 100}
        job.error, job.warning, job.stderr_tail = "Original failure.", "Original warning.", "Original stderr."
        job.started_at = job.finished_at = T0
        job.node_id = node.id
        await session.commit()
    previous = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    response = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202
    state = (await api_env.client.get(f"/api/jobs/{job_id}/state")).json()
    assert state["status"] == "pending"
    assert all(state[key] is None for key in ("progress", "error", "warning", "started_at", "finished_at"))
    detail = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    assert detail["node_id"] is None and detail["stderr_tail"] is None
    assert detail["attempts"] == previous["attempts"]
    file = next(row for row in (await api_env.client.get("/api/files")).json()["items"] if row["id"] == file_id)
    assert file["active_job_status"] == "pending" and file["active_job_progress"] is None
    async with api_env.factory() as session:
        backlog = await processing_backlog(session, healthcheck_slots=1)
        assert backlog["remaining_files"] == 1
        assert backlog["remaining_s"] is None and backlog["estimated_finish_at"] is None

    async def evaluate(*_args):
        if outcome == "failed":
            raise ValueError("Preparation failed.")
        return FlowResult(status=outcome, reason="No current plan.")

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    await service.start()
    result = await requeue_outcome(api_env, job_id)
    assert result["outcome"] == ("failed" if outcome in {"failed", "error"} else "not_queued")
    restored = (await api_env.client.get(f"/api/jobs/{job_id}/export.json")).json()
    for key in (
        "status",
        "progress",
        "error",
        "warning",
        "stderr_tail",
        "started_at",
        "finished_at",
        "node_id",
        "attempts",
    ):
        assert restored[key] == previous[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["transcode", "retry"])
@pytest.mark.parametrize("phase", ["root", "capture", "promotion"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_preflight_allows_queue(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, operation: str, phase: str
) -> None:
    if operation == "retry":
        job_id, media_id = await seed_published(api_env)
        endpoint = f"/api/jobs/{job_id}/retry"
    else:
        library_id, media_id = await seed_probed(api_env)
        await seed_flow_library(api_env, library_id, hevc_flow())
        endpoint = f"/api/files/{media_id}/transcode"
    unrelated_library, unrelated_id = await probed(api_env, "unrelated.avi")
    await seed_flow_library(api_env, unrelated_library, hevc_flow(), "unrelated-flow")
    _, health_id = await seed_media(api_env, "health.avi")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    target = api_env.lib_dir / "movie.avi"
    root_check = source_fs.check_source_root
    capture = source_fs.capture_source
    calls = 0

    def hold():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    def check(session, row, library):
        nonlocal calls
        if Path(row.path) == target:
            calls += 1
            if phase == "root" or (phase == "promotion" and calls == (5 if operation == "retry" else 2)):
                hold()
        root_check(session, row, library)

    def snapshot(source):
        if phase == "capture" and source == target:
            hold()
        return capture(source)

    monkeypatch.setattr(source_fs, "check_source_root", check)
    # retry_job imports the same preflight function directly.
    monkeypatch.setattr("server.queue.store.check_source_root", check)
    monkeypatch.setattr("server.fs.library.capture_source", snapshot)
    monkeypatch.setattr("server.queue.store.capture_source_path", snapshot)

    async def evaluate(*_args):
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    try:
        response = await asyncio.wait_for(api_env.client.post(endpoint), 2)
        assert response.status_code == 202
        job_id = response.json().get("id", response.json().get("job_id"))
        await asyncio.wait_for(entered.wait(), 3)
        health = await asyncio.wait_for(api_env.client.post(f"/api/files/{health_id}/healthcheck"), 1)
        transcode = await asyncio.wait_for(api_env.client.post(f"/api/files/{unrelated_id}/transcode"), 1)
        assert health.status_code == transcode.status_code == 202
        assert health.json()["status"] == "queued" and transcode.json()["status"] == "pending"
        release.set()
        assert (
            await requeue_outcome(api_env, job_id, event_type="requeue" if operation == "retry" else "request_result")
        )["outcome"] == "queued"
        assert (await requeue_outcome(api_env, transcode.json()["job_id"], event_type="request_result"))[
            "outcome"
        ] == "queued"
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_cancel_before_start(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id, _ = await seed_published(api_env)
    service = api_env.app.state.jobs
    original = service._schedule
    cancelled = asyncio.Event()
    cancelling = []

    def schedule(identifier):
        task = original(identifier)
        if identifier == job_id and not cancelled.is_set():
            task.cancel()
            cancelled.set()
            cancelling.append(asyncio.create_task(service.cancel(identifier)))
        return task

    monkeypatch.setattr(service, "_schedule", schedule)
    assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 202
    await asyncio.wait_for(cancelled.wait(), 2)
    assert await asyncio.wait_for(cancelling[0], 2)
    assert (await requeue_outcome(api_env, job_id))["outcome"] == "cancelled"
    library_id, file_id = await probed(api_env, "next.avi")
    await seed_flow_library(api_env, library_id, hevc_flow(), "next-flow")
    response = await api_env.client.post(f"/api/files/{file_id}/transcode")
    assert response.status_code == 202
    assert (await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result"))[
        "outcome"
    ] == "queued"
    assert service._loop is not None and not service._loop.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "shutdown", "restart"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_new_request_lifecycle(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    library_id, file_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    service = api_env.app.state.jobs
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(*_args):
        entered.set()
        await release.wait()
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    try:
        response = await api_env.client.post(f"/api/files/{file_id}/transcode")
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        await asyncio.wait_for(entered.wait(), 2)
        if operation == "cancel":
            assert (await api_env.client.post(f"/api/jobs/{job_id}/cancel")).status_code == 202
            assert (await requeue_outcome(api_env, job_id, event_type="request_result"))["outcome"] == "cancelled"
        else:
            await asyncio.wait_for(service.close(), 2)
            async with api_env.factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None and job.status == "pending"
            if operation == "restart":
                release.set()
                await service.start()
                assert (await requeue_outcome(api_env, job_id, event_type="request_result"))["outcome"] == "queued"
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OperationalError, InterfaceError])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_scan_outage_recovers(api_env, monkeypatch, error, shutdown):
    job_id, _ = await seed_published(api_env)
    service = api_env.app.state.jobs
    assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 202
    waiting, release = asyncio.Event(), asyncio.Event()
    scan = service._scan
    failed = False

    async def scan_requests():
        nonlocal failed
        if not failed:
            failed = True
            raise error("scan", {}, OSError("database unavailable"))
        await scan()

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    async def evaluate(*_args):
        return FlowResult(status="matched", plan=plan())

    monkeypatch.setattr(service, "_scan", scan_requests)
    monkeypatch.setattr("server.jobs.asyncio.sleep", timer)
    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    await service.start()
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not service._closed and not service._loop.done()
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job.status == "pending"
        _, file_id = await seed_media(api_env, "next.avi")
        response = await api_env.client.post(f"/api/files/{file_id}/healthcheck")
        assert response.status_code == 202
        if shutdown:
            await asyncio.wait_for(service.close(), 2)
            assert not release.is_set()
            async with api_env.factory() as session:
                job = await session.get(Job, job_id)
                assert job.status == "pending"
            return
        release.set()
        assert (await requeue_outcome(api_env, job_id))["outcome"] == "queued"
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparation", "completion", "publication"])
async def test_worker_outage_finishes(api_env, monkeypatch, phase):
    from server import jobs

    job_id, _ = await seed_published(api_env)
    service = api_env.app.state.jobs
    waiting, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    complete = service._finish_request
    prune = service.staging.prune_job
    retry = jobs.retry_job
    publish = jobs.publish_job_status
    lock = jobs.lock_job_source
    pruned = 0
    failed = False

    async def prune_job(*args, **kwargs):
        nonlocal pruned
        pruned += 1
        return await prune(*args, **kwargs)

    async def retry_job(*args, **kwargs):
        nonlocal failed
        if phase == "preparation" and not failed:
            failed = True
            raise OperationalError("retry", {}, OSError("database unavailable"))
        return await retry(*args, **kwargs)

    async def lock_job(*args, **kwargs):
        nonlocal failed
        if phase == "completion" and not failed:
            failed = True
            raise InterfaceError("complete", {}, OSError("database unavailable"))
        return await lock(*args, **kwargs)

    async def publish_status(*args, **kwargs):
        nonlocal failed
        if phase == "publication" and not failed:
            failed = True
            raise OperationalError("publish", {}, OSError("database unavailable"))
        await publish(*args, **kwargs)

    async def evaluate(*_args):
        return FlowResult(status="matched", plan=plan())

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    async def complete_request(*args, **kwargs):
        await complete(*args, **kwargs)
        if waiting.is_set():
            completed.set()

    monkeypatch.setattr(service.staging, "prune_job", prune_job)
    monkeypatch.setattr(service, "_finish_request", complete_request)
    monkeypatch.setattr("server.jobs.retry_job", retry_job)
    monkeypatch.setattr("server.jobs.lock_job_source", lock_job)
    monkeypatch.setattr("server.jobs.publish_job_status", publish_status)
    monkeypatch.setattr("server.jobs.asyncio.sleep", timer)
    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    assert (await api_env.client.post(f"/api/jobs/{job_id}/retry")).status_code == 202
    await service.start()
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not service._closed and not service._loop.done()
        assert not service._tasks and pruned == 1
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job.status == ("pending" if phase == "preparation" else "staging")
        release.set()
        await asyncio.wait_for(completed.wait(), 2)
        outcome = await requeue_outcome(api_env, job_id)
        assert outcome["outcome"] == ("failed" if phase == "preparation" else "queued")
        if phase == "preparation":
            assert "database outage" in outcome["reason"]
            async with api_env.factory() as session:
                job = await session.get(Job, job_id)
                assert job.status == "succeeded" and job.attempts == 1
        assert pruned == 1
        assert not service._pending_results
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
async def test_delete_outage_recovers(api_env, monkeypatch):
    from server import jobs

    job_id, _ = await seed_published(api_env)
    service = api_env.app.state.jobs
    waiting, release, deleted = (asyncio.Event() for _ in range(3))
    delete = jobs.delete_eligible_job
    publish = service.events.publish
    failed = False

    async def delete_job(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise InterfaceError("delete", {}, OSError("database unavailable"))
        return await delete(*args, **kwargs)

    def publish_event(kind, payload, **kwargs):
        publish(kind, payload, **kwargs)
        if kind == "job.deletion" and payload["job_id"] == job_id:
            assert payload["outcome"] == "deleted"
            deleted.set()

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    monkeypatch.setattr(jobs, "delete_eligible_job", delete_job)
    monkeypatch.setattr(service.events, "publish", publish_event)
    monkeypatch.setattr("server.jobs.asyncio.sleep", timer)
    assert (await service.delete([job_id]))["accepted"] == [job_id]
    await service.start()
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job.delete_requested and job.status == "succeeded"
            assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "deletion")) is None
        release.set()
        await asyncio.wait_for(deleted.wait(), 2)
        async with api_env.factory() as session:
            assert await session.get(Job, job_id) is None
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["probe", "mkv_stats", "healthcheck"])
@pytest.mark.parametrize("committed", [False, True])
async def test_retry_commit_outage(api_env, monkeypatch, kind, committed):
    from server import jobs

    library_id, media_id = await seed_media(api_env)
    async with api_env.factory() as session:
        job = Job(
            library_id=library_id,
            media_file_id=media_id,
            kind=kind,
            status="failed",
            attempts=1,
            error="Previous failure.",
            warning="Previous warning.",
            finished_at=T0,
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    service = api_env.app.state.jobs
    assert (await service.retry([job_id]))["accepted"] == [job_id]
    waiting, release, completed = (asyncio.Event() for _ in range(3))
    commit = api_env.factory.class_.commit
    complete = service._finish_request
    retry = jobs.retry_job
    failed = False
    calls = 0
    wakes = []

    async def commit_retry(session):
        nonlocal failed
        task = asyncio.current_task()
        assert task is not None
        if task.get_name() == f"job-requests:{job_id}" and not failed:
            failed = True
            if committed:
                await commit(session)
            raise OperationalError("commit", {}, OSError("database unavailable"))
        await commit(session)

    async def retry_job(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await retry(*args, **kwargs)

    async def timer(delay):
        assert delay == 1
        waiting.set()
        await release.wait()

    async def complete_request(*args, **kwargs):
        await complete(*args, **kwargs)
        completed.set()

    monkeypatch.setattr(api_env.factory.class_, "commit", commit_retry)
    monkeypatch.setattr(jobs, "retry_job", retry_job)
    monkeypatch.setattr(service, "_finish_request", complete_request)
    monkeypatch.setattr(service.probes, "wake", lambda: wakes.append("probes"))
    monkeypatch.setattr(service.healthchecks, "wake", lambda: wakes.append("healthchecks"))
    monkeypatch.setattr("server.jobs.asyncio.sleep", timer)
    await service.start()
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not service._closed and not service._tasks and calls == 1 and not wakes
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job.status == ("queued" if committed else "pending")
            recorded = await session.scalar(select(JobEvent).where(JobEvent.type == "requeue"))
            original = (recorded.id, recorded.payload) if recorded is not None else None
        release.set()
        await asyncio.wait_for(completed.wait(), 2)
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            outcome = (await session.scalars(select(JobEvent).where(JobEvent.type == "requeue"))).one()
            if committed:
                assert job.status == "queued" and job.error is None
                assert original == (outcome.id, outcome.payload)
                assert outcome.payload["outcome"] == "queued"
                assert wakes == ["probes", *(["healthchecks"] if kind == "healthcheck" else [])]
            else:
                assert job.status == "failed" and job.error == "Previous failure."
                assert job.warning == "Previous warning." and job.finished_at == T0
                assert outcome.payload["outcome"] == "failed"
                assert "database outage" in outcome.payload["reason"]
                assert wakes == ["probes"]
            assert job.attempts == 1
        assert calls == 1 and not service._pending_results
    finally:
        release.set()
        await asyncio.wait_for(service.close(), 2)
