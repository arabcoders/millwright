from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from server.fs import library as library_fs
from server.models import Job, JobAttempt, Library, MediaFile
from tests.test_api import ApiEnv, api_env, seed_media
from tests.test_flow_enqueue import seed, skip_definition
from tests.test_metadata_jobs import RAW, T0


@pytest.mark.asyncio
async def test_pending_file_cooldown(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.api.files.utcnow", lambda: T0)
    monkeypatch.setattr("server.queue.enqueue.utcnow", lambda: T0)
    media_id, library_id = await seed(api_env.factory, definition=skip_definition(), cooldown_s=300, first_seen=T0)
    service = api_env.app.state.flow_enqueue
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.suspended = True
        await session.commit()
    assert not await service._process(media_id)
    response = await api_env.client.post(f"/api/files/{media_id}/skip-cooldown")
    assert response.status_code == 200 and response.json() == {"id": media_id}
    assert not await service._process(media_id)
    async with api_env.factory() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None and row.transcode_status == "pending"
        assert await session.scalar(select(Job.id).where(Job.media_file_id == media_id)) is None
        library = await session.get(Library, library_id)
        assert library is not None
        library.suspended = False
        await session.commit()
    assert await service._process(media_id)
    async with api_env.factory() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None and row.transcode_status == "not_needed"


@pytest.mark.asyncio
async def test_active_cooldown_rejected(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env, "running.mkv")
    async with api_env.factory() as session:
        row = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        assert row is not None and library is not None
        row.first_seen = T0
        library.cooldown_s = 300
        session.add(Job(media_file_id=media_id, library_id=library_id, status="running"))
        await session.commit()
    response = await api_env.client.post(f"/api/files/{media_id}/skip-cooldown")
    assert response.status_code == 409
    async with api_env.factory() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None and row.first_seen == T0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_statistics_skip_cooldown(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, changed: bool) -> None:
    for module in (
        "server.api.files",
        "server.probing",
        "server.metadata_jobs",
        "server.fs.library",
        "server.queue.store",
    ):
        monkeypatch.setattr(f"{module}.utcnow", lambda: T0)
    library_id, media_id = await seed_media(api_env, "held.mkv")
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.mkv_stats = True
        library.cooldown_s = 300
        await session.commit()
    edits = []

    async def probe(*_args, **_kwargs):
        return RAW

    async def edit(_bin, path, **_kwargs):
        edits.append(path.name)

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    service = api_env.app.state.probes
    assert (await api_env.client.post(f"/api/files/{media_id}/probe")).status_code == 202
    await service._fill()
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
    async with api_env.factory() as session:
        job = await session.scalar(select(Job).where(Job.kind == "mkv_stats"))
        assert job is not None and job.status == "hold" and job.attempts == 0
        job_id = job.id
    api_env.app.state.scanner.set_paused(paused=True)
    response = await api_env.client.post(f"/api/files/{media_id}/skip-cooldown")
    assert response.status_code == 200 and response.json() == {"id": media_id}
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == "hold" and job.attempts == 0
    await service._fill()
    assert not service._tasks and not edits
    if changed:
        (api_env.lib_dir / "held.mkv").write_bytes(b"changed source")
    api_env.app.state.scanner.set_paused(paused=False)
    await service._fill()
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        row = await session.get(MediaFile, media_id)
        assert job is not None and row is not None
        if changed:
            assert not edits and job.status == "hold" and row.probe_id is None
        else:
            assert edits == ["held.mkv"] and job.status == "succeeded" and row.probe_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_probe_preserves_health(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, changed: bool) -> None:
    library_id, media_id = await seed_media(api_env, "movie.mp4")
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        row = await session.get(MediaFile, media_id)
        assert library is not None and row is not None
        library.healthcheck_enabled = True
        row.health_status = "healthy"
        health_source = {"path": row.path, "size": row.size}
        row.health_source = health_source
        await session.commit()
    if changed:
        (api_env.lib_dir / "movie.mp4").write_bytes(b"changed source")

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    assert response.status_code == 202
    service = api_env.app.state.probes
    await service._fill()
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
    async with api_env.factory() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None and row.probe_id is not None
        check = await session.scalar(select(Job).where(Job.media_file_id == media_id, Job.kind == "healthcheck"))
        if changed:
            assert row.health_status == "pending" and row.health_source is None
            assert check is not None and check.status == "queued"
        else:
            assert row.health_status == "healthy" and row.health_source == health_source
            assert check is None


@pytest.mark.asyncio
async def test_metadata_job_controls(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _, media_id = await seed_media(api_env, "movie.mp4")
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    assert response.status_code == 202
    data = response.json()
    job_id = data["job_id"]
    assert data == {"file_id": media_id, "job_id": job_id, "kind": "probe", "status": "queued"}
    preview = await api_env.client.get("/api/jobs/queue?kind=probe")
    assert preview.status_code == 200 and preview.json()["items"][0]["id"] == job_id
    assert (await api_env.client.post(f"/api/jobs/{job_id}/bump", json={"priority": 2})).status_code == 200
    cancelled = await api_env.client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    async with api_env.factory() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None and row.metadata_cancelled
    retried = await api_env.client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 202
    await api_env.app.state.jobs._process(job_id)

    async def probe(*_args, **_kwargs):
        return RAW

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = api_env.app.state.probes
    await service._fill()
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
    exported = await api_env.client.get(f"/api/jobs/{job_id}/export.json")
    assert exported.status_code == 200
    attempt = exported.json()["attempts"][0]
    assert attempt["workload"] == "probe" and attempt["input_probe"] is not None
    logs = await api_env.client.get(f"/api/jobs/{job_id}/attempts/{attempt['id']}/log")
    assert logs.status_code == 200
    deletion = await api_env.client.delete(f"/api/jobs/{job_id}")
    assert deletion.status_code == 202
    await api_env.app.state.jobs._process(job_id)
    assert (await api_env.client.get(f"/api/jobs/{job_id}")).status_code == 404


@pytest.mark.asyncio
async def test_running_bulk_cancel(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _, media_id = await seed_media(api_env, "movie.mp4")
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    job_id = response.json()["job_id"]
    entered = asyncio.Event()

    async def probe(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    service = api_env.app.state.probes
    await service._fill()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        _, queued_media = await seed_media(api_env, "queued.mp4")
        queued = await api_env.client.post(f"/api/files/{queued_media}/probe")
        queued_id = queued.json()["job_id"]
        response = await asyncio.wait_for(
            api_env.client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": [queued_id, job_id]}), 3
        )
        assert response.status_code == 200 and response.json() == {"succeeded": [queued_id, job_id], "failed": []}
        await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        async with api_env.factory() as session:
            job = await session.get(Job, job_id)
            row = await session.get(MediaFile, media_id)
            attempt = await session.scalar(select(JobAttempt))
            assert job is not None and job.status == "cancelled"
            assert row is not None and row.metadata_cancelled
            assert attempt is not None and attempt.result == "cancelled"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_orphan_metadata_cancel(api_env: ApiEnv, bulk: bool) -> None:
    _, media_id = await seed_media(api_env, "orphan.mp4")
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    job_id = response.json()["job_id"]
    service = api_env.app.state.probes
    claimed = await service._claim("probe")
    assert claimed is not None and claimed[0] == job_id and not service._tasks
    if bulk:
        response = await api_env.client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": [job_id]})
        assert response.status_code == 200 and response.json() == {"succeeded": [job_id], "failed": []}
    else:
        response = await api_env.client.post(f"/api/jobs/{job_id}/cancel")
        assert response.status_code == 202
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        row = await session.get(MediaFile, media_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert job is not None and job.status == "cancelled"
        assert row is not None and row.metadata_cancelled
        assert attempt is not None and attempt.status == "cancelled" and attempt.result == "cancelled"


@pytest.mark.asyncio
async def test_metadata_queue_paging(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.api.jobs._QUEUE_BATCH", 2)
    jobs = []
    for index in range(5):
        _, media_id = await seed_media(api_env, f"movie-{index}.mp4")
        response = await api_env.client.post(f"/api/files/{media_id}/probe")
        assert response.status_code == 202
        jobs.append(response.json()["job_id"])
    bumped = await api_env.client.post(f"/api/jobs/{jobs[1]}/bump", json={"priority": 8})
    assert bumped.status_code == 200
    response = await api_env.client.get("/api/jobs/queue?kind=probe")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [jobs[1], *reversed(jobs[2:]), jobs[0]]
