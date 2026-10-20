from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event as sql_event, select

from common.enums import JobStatus
from common.protocol import Heartbeat
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, Library, MediaFile, NotificationDelivery
from server.nodehub.store import get_node, register_node
from server.workspace import cleanup_workspace
from tests.test_api import ApiEnv, api_env, delete_requested, seed_queue
from tests.hub_helpers import FakeConn
from tests.test_hub import answer_challenge, hello, wait_for
from tests.test_staging import add_job
from server.queue.store import claim


@pytest.mark.asyncio
async def test_detail_event_selection(api_env: ApiEnv) -> None:
    job_id, _ = await seed_queue(api_env, "events.mkv")
    async with api_env.factory() as session:
        note = JobEvent(job_id=job_id, type="operator_note", payload={"message": "Retained history."})
        session.add(note)
        await session.flush()
        session.add(
            NotificationDelivery(
                job_id=job_id,
                job_event_id=note.id,
                target_name="external:radarr",
                status="failed",
                attempts=3,
                error="Radarr parser did not identify a managed movie.",
            )
        )
        await session.commit()

    without = await api_env.client.get(f"/api/jobs/{job_id}")
    included = await api_env.client.get(f"/api/jobs/{job_id}", params={"events": 1})
    exported = await api_env.client.get(f"/api/jobs/{job_id}/export.json")
    assert without.status_code == included.status_code == exported.status_code == 200
    assert without.json()["events"] == []
    detail = included.json()
    assert [event["type"] for event in detail["events"]] == ["enqueued", "operator_note"]
    assert detail["events"][-1]["payload"] == {"message": "Retained history."}
    (delivery,) = detail["notification_deliveries"]
    assert (delivery["status"], delivery["target_name"], delivery["attempts"]) == ("failed", "external:radarr", 3)
    assert delivery["error"] == "Radarr parser did not identify a managed movie."
    assert exported.json() == detail
    assert without.json() == {**detail, "events": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_deletion_accepts_first(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    first = await add_job(api_env.factory, None, status=JobStatus.FAILED)
    async with api_env.factory() as session:
        source = await session.get(Job, first)
        assert source is not None
        jobs = [
            Job(media_file_id=source.media_file_id, library_id=source.library_id, status="failed") for _ in range(998)
        ]
        session.add_all(jobs)
        await session.commit()
        ids = [first, *(job.id for job in jobs)]
    entered, release = asyncio.Event(), asyncio.Event()
    service = api_env.app.state.jobs
    original = service._delete

    async def blocked(job_id):
        entered.set()
        await release.wait()
        await original(job_id)

    monkeypatch.setattr(service, "_delete", blocked)
    statements = 0
    engine = api_env.factory.kw["bind"].sync_engine

    def count(*_args):
        nonlocal statements
        statements += 1

    try:
        sql_event.listen(engine, "before_cursor_execute", count)
        try:
            response = await asyncio.wait_for(
                api_env.client.post("/api/jobs/bulk", json={"action": "delete", "job_ids": ids}), 2
            )
        finally:
            sql_event.remove(engine, "before_cursor_execute", count)
        assert response.status_code == 202 and response.json() == {"accepted": ids, "failed": []}
        assert statements <= 10
        await asyncio.wait_for(entered.wait(), 2)
        async with api_env.factory() as session:
            saved = list(await session.scalars(select(Job).where(Job.id.in_(ids))))
            assert len(saved) == 999 and all(job.delete_requested and job.status == "failed" for job in saved)
        duplicate = await api_env.client.delete(f"/api/jobs/{first}")
        assert duplicate.status_code == 409
        assert (await api_env.client.get("/api/jobs?limit=1")).status_code == 200
    finally:
        # Shutdown cancels this pre-transaction wait and leaves all requests persisted.
        await asyncio.wait_for(service.close(), 2)
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.STAGING])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_deletion_blocks_queue(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, status: JobStatus) -> None:
    job_id = await add_job(api_env.factory, None, status=status)
    service = api_env.app.state.jobs
    entered = asyncio.Event()

    async def blocked(job_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_delete", blocked)
    try:
        assert (await api_env.client.delete(f"/api/jobs/{job_id}")).status_code == 202
        await asyncio.wait_for(entered.wait(), 2)
        async with api_env.app.state.hub._dispatch_candidates() as candidates:
            assert [job_id async for job_id in candidates] == []
        async with api_env.factory() as session:
            assert await claim(session, job_id, 1) is None
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.kind = "healthcheck"
            job.status = "queued"
            await session.commit()
        assert await api_env.app.state.healthchecks.claim_next() is None
    finally:
        await asyncio.wait_for(service.close(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_deletion_survives_restart(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = await add_job(api_env.factory, None, status=JobStatus.FAILED)
    service = api_env.app.state.jobs
    entered = asyncio.Event()
    original = service._delete

    async def blocked(job_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_delete", blocked)
    assert (await api_env.client.delete(f"/api/jobs/{job_id}")).status_code == 202
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(service.close(), 2)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.delete_requested
    monkeypatch.setattr(service, "_delete", original)
    sub = api_env.app.state.events.subscribe()
    try:
        await service.start()
        async with asyncio.timeout(3):
            while True:
                event = await sub.next()
                assert event is not None
                if event.type == "job.deletion":
                    assert event.payload["job_id"] == job_id and event.payload["outcome"] == "deleted"
                    assert event.payload["reason"]
                    break
        async with api_env.factory() as session:
            assert await session.get(Job, job_id) is None
    finally:
        api_env.app.state.events.unsubscribe(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_deletion_keeps_nodes(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    first = await add_job(api_env.factory, None, status=JobStatus.FAILED)
    async with api_env.factory() as session:
        source = await session.get(Job, first)
        assert source is not None
        second = Job(media_file_id=source.media_file_id, library_id=source.library_id, status="failed")
        session.add(second)
        await register_node(session, "node-a", "tok")
        await session.commit()
        ids = [first, second.id]
    entered, release, heartbeated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    hub = api_env.app.state.hub
    hub.workspace_dir = api_env.app.state.workspace_dir
    conn = FakeConn([hello()])
    connection = None
    handle_heartbeat = hub._handle_heartbeat

    async def heartbeat_handler(entry, msg):
        await handle_heartbeat(entry, msg)
        heartbeated.set()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr("server.jobs.publish_file_status", blocked)
    monkeypatch.setattr(hub, "_handle_heartbeat", heartbeat_handler)
    try:
        response = await asyncio.wait_for(
            api_env.client.post("/api/jobs/bulk", json={"action": "delete", "job_ids": ids}), 2
        )
        assert response.status_code == 202
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(2), api_env.factory() as session:
            assert await session.get(Job, ids[0]) is None
            waiting = await session.get(Job, ids[1])
            assert waiting is not None and waiting.delete_requested
            library = await session.get(Library, waiting.library_id)
            assert library is not None
            library.name = "Other database work proceeds."
            await session.commit()
        connection = asyncio.create_task(hub.serve_connection(conn))
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        answer_challenge(hub, conn)
        await asyncio.wait_for(conn.ready.wait(), 2)
        assert conn.messages("welcome") and not release.is_set()
        conn.push(Heartbeat(cpu_percent=41).model_dump(mode="json"))
        await asyncio.wait_for(heartbeated.wait(), 2)
        async with api_env.factory() as session:
            node = await get_node(session, "node-a")
            assert node is not None and node.usage is not None and node.usage["cpu_percent"] == 41
    finally:
        release.set()
        conn.disconnect()
        if connection is not None:
            await asyncio.wait_for(connection, 3)
        await asyncio.wait_for(api_env.app.state.jobs.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_deletion_failure_visible(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = await add_job(api_env.factory, None, status=JobStatus.FAILED)

    async def fail(session, job_id):
        raise OSError("History storage unavailable.")

    monkeypatch.setattr("server.jobs.delete_eligible_job", fail)
    response, outcomes = await delete_requested(api_env, [job_id], bulk=False)
    assert response.status_code == 202
    assert outcomes[job_id] == {"job_id": job_id, "outcome": "failed", "reason": "History storage unavailable."}
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and not job.delete_requested and job.status == JobStatus.FAILED
        outcome = await session.scalar(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "deletion"))
        assert outcome is not None and outcome.payload == {
            "outcome": "failed",
            "reason": "History storage unavailable.",
        }
    assert not api_env.app.state.jobs._closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "bulk"])
@pytest.mark.parametrize("state", ["prepared", "original_moved", "discard_prepared"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_delete_preserves_recovery(api_env: ApiEnv, operation: str, state: str) -> None:
    source = api_env.lib_dir / "original.mkv"
    source.write_bytes(b"original")
    trash = api_env.lib_dir.parent / "retained-original.mkv"
    trash.write_bytes(b"retained original")
    job_id = await add_job(api_env.factory, source, status=JobStatus.FAILED)
    completed_source = api_env.lib_dir / "completed.mkv"
    completed_source.write_bytes(b"source")
    completed_id = await add_job(api_env.factory, completed_source, status=JobStatus.SUCCEEDED)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media_id = job.media_file_id
        attempt = JobAttempt(
            job_id=job_id,
            attempt_number=1,
            status="failed",
            result="failed",
        )
        completed_attempt = JobAttempt(job_id=completed_id, attempt_number=1, status="succeeded", result="succeeded")
        session.add_all([attempt, completed_attempt])
        await session.flush()
        journal = FinalizationJournal(
            attempt_id=attempt.id,
            source_path=str(source),
            destination_path=str(source),
            partial_path=str(source) + ".partial",
            trash_path=str(trash),
            state=state,
        )
        completed_journal = FinalizationJournal(
            attempt_id=completed_attempt.id,
            source_path="source",
            destination_path="destination",
            partial_path="partial",
            trash_path="trash",
            state="completed",
        )
        event = JobEvent(
            job_id=job_id,
            attempt_id=attempt.id,
            type="failed",
        )
        session.add_all([journal, completed_journal, event])
        await session.flush()
        delivery = NotificationDelivery(job_id=job_id, job_event_id=event.id, target_name="alerts", status="sent")
        session.add(delivery)
        await session.commit()
        attempt_id, journal_id, event_id, delivery_id = attempt.id, journal.id, event.id, delivery.id
        completed_attempt_id, completed_journal_id = completed_attempt.id, completed_journal.id

    if operation == "single":
        response, outcomes = await delete_requested(api_env, [job_id], bulk=False)
        assert response.status_code == 202 and outcomes[job_id]["outcome"] == "failed"
        _, outcomes = await delete_requested(api_env, [completed_id], bulk=False)
        assert outcomes[completed_id]["outcome"] == "deleted"
        assert (await api_env.client.delete("/api/jobs/9999")).status_code == 404
    else:
        response, outcomes = await delete_requested(api_env, [job_id, completed_id, 9999], bulk=True)
        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] == [job_id, completed_id]
        assert [item["id"] for item in body["failed"]] == [9999]
        assert outcomes[job_id]["outcome"] == "failed" and outcomes[completed_id]["outcome"] == "deleted"
    async with api_env.factory() as session:
        saved = await session.get(Job, job_id)
        assert saved is not None and saved.status == JobStatus.FAILED and not saved.delete_requested
        assert await session.get(MediaFile, media_id) is not None
        assert await session.get(JobAttempt, attempt_id) is not None
        assert await session.get(FinalizationJournal, journal_id) is not None
        assert await session.get(JobEvent, event_id) is not None
        assert await session.get(NotificationDelivery, delivery_id) is not None
        assert await session.get(Job, completed_id) is None
        assert await session.get(JobAttempt, completed_attempt_id) is None
        assert await session.get(FinalizationJournal, completed_journal_id) is None
    assert source.read_bytes() == b"original"
    assert trash.read_bytes() == b"retained original"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "bulk"])
@pytest.mark.parametrize(
    "diagnostic", ["finalization_unprepared", "execution_log_unavailable", "interrupted", "running"]
)
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_delete_terminal_diagnostics(api_env: ApiEnv, operation: str, diagnostic: str) -> None:
    source = api_env.lib_dir / "original.mkv"
    source.write_bytes(b"original")
    job_id = await add_job(api_env.factory, source, status=JobStatus.FAILED)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media_id = job.media_file_id
        attempt = JobAttempt(
            job_id=job_id,
            attempt_number=1,
            status="running" if diagnostic == "running" else "failed",
            result="interrupted" if diagnostic == "interrupted" else "failed",
        )
        session.add(attempt)
        await session.flush()
        event = JobEvent(job_id=job_id, attempt_id=attempt.id, type=diagnostic)
        session.add(event)
        await session.commit()
        attempt_id, event_id = attempt.id, event.id
    workspace = api_env.lib_dir.parent / "diagnostic-workspace"
    artifact = workspace / "jobs" / str(job_id) / "attempts/1/output.mkv"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"workspace output")
    response, outcomes = await delete_requested(api_env, [job_id], bulk=operation == "bulk")
    assert response.status_code == 202 and outcomes[job_id]["outcome"] == "deleted"
    async with api_env.factory() as session:
        assert await session.get(Job, job_id) is None
        assert await session.get(JobAttempt, attempt_id) is None
        assert await session.get(JobEvent, event_id) is None
        assert await session.get(MediaFile, media_id) is not None
    assert artifact.read_bytes() == b"workspace output"
    await cleanup_workspace(api_env.factory, workspace)
    assert not (workspace / "jobs" / str(job_id)).exists()
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "bulk"])
@pytest.mark.parametrize("action_state", ["declared", "deferred", "pending", "report"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_delete_preserves_actions(api_env: ApiEnv, operation: str, action_state: str) -> None:
    job_id = await add_job(api_env.factory, None, status=JobStatus.SUCCEEDED)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.attempts = 1
        attempt = JobAttempt(job_id=job_id, attempt_number=1, status="succeeded", result="succeeded")
        session.add(attempt)
        await session.flush()
        if action_state == "declared":
            job.post_actions = [{"kind": "extract_subtitles", "formats": ["srt"]}]
        elif action_state == "deferred":
            session.add(JobEvent(job_id=job_id, attempt_id=attempt.id, type="stage", payload={"post_actions": [{}]}))
        else:
            session.add(JobEvent(job_id=job_id, attempt_id=attempt.id, type="actions_pending"))
            if action_state == "report":
                session.add(
                    JobEvent(job_id=job_id, attempt_id=attempt.id, type="action_report", payload={"results": []})
                )
        await session.commit()
        attempt_id = attempt.id
    response, outcomes = await delete_requested(api_env, [job_id], bulk=operation == "bulk")
    assert response.status_code == 202 and outcomes[job_id]["outcome"] == "failed"
    async with api_env.factory() as session:
        assert await session.get(Job, job_id) is not None
        session.add(JobEvent(job_id=job_id, attempt_id=attempt_id, type="actions_finished"))
        await session.commit()
    response, outcomes = await delete_requested(api_env, [job_id], bulk=operation == "bulk")
    assert response.status_code == 202 and outcomes[job_id]["outcome"] == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "bulk"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_delete_rechecks_recovery(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    job_id = await add_job(api_env.factory, None, status=JobStatus.FAILED)
    service = api_env.app.state.jobs
    delete = service._delete
    selected = False

    async def protect_selected(job_id):
        nonlocal selected
        if not selected:
            selected = True
            async with api_env.factory() as writer:
                attempt = JobAttempt(job_id=job_id, attempt_number=1, status="failed")
                writer.add(attempt)
                await writer.flush()
                writer.add(
                    FinalizationJournal(
                        attempt_id=attempt.id,
                        source_path="source",
                        destination_path="destination",
                        partial_path="partial",
                        trash_path="trash",
                        state="prepared",
                    )
                )
                await writer.commit()
        await delete(job_id)

    monkeypatch.setattr(service, "_delete", protect_selected)
    response, outcomes = await delete_requested(api_env, [job_id], bulk=operation == "bulk")
    assert response.status_code == 202 and outcomes[job_id]["outcome"] == "failed"
    assert selected
    async with api_env.factory() as session:
        assert await session.get(Job, job_id) is not None
        assert (
            await session.scalar(select(FinalizationJournal.id).where(FinalizationJournal.state == "prepared"))
            is not None
        )
