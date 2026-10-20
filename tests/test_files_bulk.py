from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from server.flows import evaluation
from server.flows.engine import FlowResult, StepOutcome
from server.models import Job, JobEvent, Library, MediaFile, ProbeRow
from tests.test_api import PROBE_RAW, ApiEnv, api_env, hevc_flow, requeue_outcome, seed_flow_library, seed_media
from tests.test_metadata_jobs import T0


@pytest.mark.asyncio
async def test_bulk_validation(api_env: ApiEnv) -> None:
    for ids in ([], [0], [-1], [1, 0], [True], [1.5], ["1"]):
        response = await api_env.client.post("/api/files/bulk", json={"action": "healthcheck", "file_ids": ids})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_bulk_unlimited(api_env: ApiEnv) -> None:
    ids = list(range(1001, 2004))
    response = await api_env.client.post(
        "/api/files/bulk", json={"action": "transcode", "file_ids": [*ids, ids[0], ids[-1]]}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == []
    assert [item["file_id"] for item in body["failed"]] == ids
    assert all(item["status"] == 404 for item in body["failed"])


@pytest.mark.asyncio
async def test_healthcheck_partial(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _, first = await seed_media(api_env, "first.mkv")
    _, second = await seed_media(api_env, "second.mkv")
    paused_library, paused = await seed_media(api_env, "paused.mkv")
    async with api_env.factory() as session:
        suspended = await session.get(Library, paused_library)
        assert suspended is not None
        suspended.suspended = True
        await session.commit()
    assert (await api_env.client.post(f"/api/files/{first}/healthcheck")).status_code == 202
    wake = Mock()
    monkeypatch.setattr(api_env.app.state.healthchecks, "wake", wake)
    response = await api_env.client.post(
        "/api/files/bulk", json={"action": "healthcheck", "file_ids": [second, first, paused, second, 9999]}
    )
    assert response.status_code == 202
    body = response.json()
    assert [item["file_id"] for item in body["accepted"]] == [second]
    assert [item["file_id"] for item in body["failed"]] == [first, paused, 9999]
    assert [item["status"] for item in body["failed"]] == [409, 409, 404]
    assert wake.call_count == 1
    accepted = body["accepted"][0]
    assert accepted["kind"] == "healthcheck" and accepted["status"] == "queued"
    async with api_env.factory() as session:
        media = await session.get(MediaFile, second)
        assert media is not None and media.health_status == "pending"


@pytest.mark.asyncio
async def test_cooldown_partial(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.api.files.utcnow", lambda: T0)
    held_library, held = await seed_media(api_env, "held.mkv")
    running_library, running = await seed_media(api_env, "running.mkv")
    pending_library, pending = await seed_media(api_env, "pending.mkv")
    async with api_env.factory() as session:
        for library_id, file_id in ((held_library, held), (running_library, running), (pending_library, pending)):
            library = await session.get(Library, library_id)
            media = await session.get(MediaFile, file_id)
            assert library is not None and media is not None
            library.cooldown_s = 300
            media.first_seen = T0
            media.transcode_status = "pending"
        job = Job(
            media_file_id=held,
            library_id=held_library,
            kind="mkv_stats",
            status="hold",
            hold_until=T0 + timedelta(seconds=300),
        )
        session.add(job)
        session.add(Job(media_file_id=running, library_id=running_library, status="running"))
        await session.commit()
        held_job = job.id

    response = await api_env.client.post(
        "/api/files/bulk",
        json={"action": "skip_cooldown", "file_ids": [held, running, 9999, pending, held]},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == [{"file_id": held}, {"file_id": pending}]
    assert [(item["file_id"], item["status"]) for item in body["failed"]] == [(running, 409), (9999, 404)]
    async with api_env.factory() as session:
        for file_id in (held, pending):
            media = await session.get(MediaFile, file_id)
            assert media is not None and media.first_seen == T0 - timedelta(seconds=300)
        active = await session.get(MediaFile, running)
        assert active is not None and active.first_seen == T0
        job = await session.get(Job, held_job)
        assert job is not None and job.hold_until == T0 and job.status == "hold"
        assert await session.scalar(select(Job.id).where(Job.media_file_id == pending)) is None
        events = list(await session.scalars(select(JobEvent).where(JobEvent.job_id == held_job).order_by(JobEvent.id)))
        assert [event.type for event in events] == ["metadata_source", "cooldown_skipped"]


async def probed(env: ApiEnv, name: str, *, codec: str = "h264") -> tuple[int, int]:
    library, file_id = await seed_media(env, name)
    raw = json.loads(json.dumps(PROBE_RAW))
    raw["streams"][0]["codec_name"] = codec
    async with env.factory() as session:
        probe = ProbeRow(media_file_id=file_id, raw_json=json.dumps(raw))
        session.add(probe)
        await session.flush()
        media = await session.get(MediaFile, file_id)
        assert media is not None
        media.probe_id = probe.id
        await session.commit()
    return library, file_id


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_outcomes(api_env: ApiEnv) -> None:
    first_lib, first = await probed(api_env, "first.mkv")
    second_lib, second = await probed(api_env, "second.mkv", codec="mpeg4")
    skip_lib, skip = await probed(api_env, "skip.mkv")
    veto_lib, veto = await probed(api_env, "veto.mkv")
    _, missing = await seed_media(api_env, "missing.mkv")
    await seed_flow_library(api_env, first_lib, hevc_flow(19), "first")
    await seed_flow_library(api_env, second_lib, hevc_flow(25), "second")
    skip_flow = hevc_flow()
    skip_flow["when"] = {"media.file.size_bytes": {"gte": 2**30}}
    await seed_flow_library(api_env, skip_lib, skip_flow, "skip")
    veto_flow = hevc_flow()
    veto_flow["nodes"][0]["when"] = {"media.file.size_bytes": {"gte": 2**30}}
    veto_flow["nodes"][3]["outcome"] = "veto"
    await seed_flow_library(api_env, veto_lib, veto_flow, "veto")
    response = await api_env.client.post(
        "/api/files/bulk", json={"action": "transcode", "file_ids": [first, skip, second, veto, missing]}
    )
    assert response.status_code == 202
    body = response.json()
    assert [item["file_id"] for item in body["accepted"]] == [first, skip, second, veto]
    assert all(item["status"] == "pending" for item in body["accepted"])
    assert [(item["file_id"], item["status"]) for item in body["failed"]] == [(missing, 422)]
    outcomes = [
        await requeue_outcome(api_env, item["job_id"], event_type="request_result") for item in body["accepted"]
    ]
    assert [item["outcome"] for item in outcomes] == ["queued", "not_queued", "queued", "not_queued"]
    assert outcomes[1]["decision"]["status"] == "skipped"
    assert outcomes[3]["decision"]["status"] == "vetoed"
    async with api_env.factory() as session:
        jobs = list(await session.scalars(select(Job).order_by(Job.id)))
        assert [job.id for job in jobs] == [item["job_id"] for item in body["accepted"]]
        assert [job.status for job in jobs] == ["staging", "skipped", "staging", "skipped"]
        assert [job.plan["video"]["rate"]["quality"] for job in jobs if job.plan is not None] == [19, 25]
        assert [job.facets["media.video.codec"] for job in jobs if job.facets is not None] == ["h264", "mpeg4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_source_check(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library, file_id = await probed(api_env, "changed.mkv")
    await seed_flow_library(api_env, library, hevc_flow())
    evaluate = evaluation.evaluate

    async def change_source(*args, **kwargs):
        result = await evaluate(*args, **kwargs)
        async with api_env.factory() as session:
            media = await session.get(MediaFile, file_id)
            assert media is not None
            media.size += 1
            await session.commit()
        return result

    monkeypatch.setattr(evaluation, "evaluate", change_source)
    response = await api_env.client.post("/api/files/bulk", json={"action": "transcode", "file_ids": [file_id]})
    job_id = response.json()["accepted"][0]["job_id"]
    outcome = await requeue_outcome(api_env, job_id, event_type="request_result")
    assert outcome["outcome"] == "failed"
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == "failed"


@pytest.mark.asyncio
async def test_transcode_conflicts(api_env: ApiEnv) -> None:
    first_lib, first = await probed(api_env, "active.mkv")
    second_lib, second = await probed(api_env, "paused.mkv")
    await seed_flow_library(api_env, first_lib, hevc_flow(), "first")
    await seed_flow_library(api_env, second_lib, hevc_flow(), "second")
    assert (await api_env.client.post(f"/api/files/{first}/healthcheck")).status_code == 202
    async with api_env.factory() as session:
        library = await session.get(Library, second_lib)
        assert library is not None
        library.suspended = True
        await session.commit()
    response = await api_env.client.post("/api/files/bulk", json={"action": "transcode", "file_ids": [first, second]})
    assert response.status_code == 202
    assert response.json()["accepted"] == []
    assert all(item["status"] == 409 for item in response.json()["failed"])


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_flow_error_outcome(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    library, file_id = await probed(api_env, "error.mkv")
    await seed_flow_library(api_env, library, hevc_flow())

    async def evaluate(*_args):
        return FlowResult(
            status="error", steps=[StepOutcome(node="enc", decision="error", error="Invalid encoder settings.")]
        )

    monkeypatch.setattr(evaluation, "evaluate", evaluate)
    response = await api_env.client.post("/api/files/bulk", json={"action": "transcode", "file_ids": [file_id]})
    job_id = response.json()["accepted"][0]["job_id"]
    outcome = await requeue_outcome(api_env, job_id, event_type="request_result")
    assert outcome["outcome"] == "failed" and outcome["decision"]["status"] == "error"
    assert outcome["reason"] == "Invalid encoder settings."
    async with api_env.factory() as session:
        media = await session.get(MediaFile, file_id)
        job = await session.get(Job, job_id)
        assert media is not None and media.transcode_status == "failed"
        assert job is not None and job.status == "failed"
