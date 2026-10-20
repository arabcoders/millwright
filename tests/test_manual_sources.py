from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

import server.healthcheck as healthcheck
from common.fs.identity import source_identity
from server.models import Job, JobAttempt, JobEvent, MediaFile, ProbeRow
from tests.test_api import PROBE_RAW, T0, ApiEnv, api_env, hevc_flow, requeue_outcome, seed_flow_library, seed_probed


async def change_source(env: ApiEnv, media_id: int, change: str) -> Path:
    async with env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        path = Path(media.path)
        if change == "identity":
            media.source_identity = None
        elif change == "probe":
            media.probe_id = None
        elif change == "invalid_probe":
            probe = await session.get(ProbeRow, media.probe_id)
            assert probe is not None
            probe.raw_json = "invalid JSON"
        elif change == "changed":
            stamp = path.stat().st_mtime_ns
            replacement = path.with_name("replacement.avi")
            replacement.write_bytes(b"y" * path.stat().st_size)
            os.utime(replacement, ns=(stamp, stamp))
            replacement.replace(path)
        elif change == "missing":
            path.unlink()
        await session.commit()
        return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bulk, change",
    [
        (False, "changed"),
        (False, "identity"),
        (False, "probe"),
        (False, "invalid_probe"),
        (False, "unchanged"),
        (False, "missing"),
        (True, "identity"),
        (True, "missing"),
    ],
)
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_current_source(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, bulk: bool, change: str):
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    path = await change_source(api_env, media_id, change)
    probes: list[Path] = []
    scan = Mock()
    monkeypatch.setattr(api_env.app.state.scanner, "queue_scan", scan)

    async def probe(_binary, source: Path, **_kwargs) -> dict:
        probes.append(source)
        return PROBE_RAW

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    if bulk:
        response = await api_env.client.post("/api/files/bulk", json={"action": "transcode", "file_ids": [media_id]})
        assert response.status_code == 202, response.text
        assert response.json()["failed"] == []
        accepted = response.json()["accepted"][0]
        assert accepted["status"] == "pending"
        job_id = accepted["job_id"]
    else:
        response = await api_env.client.post(f"/api/files/{media_id}/transcode")
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "pending"
        job_id = response.json()["job_id"]
    outcome = await requeue_outcome(api_env, job_id, event_type="request_result")
    assert outcome["outcome"] == ("failed" if change == "missing" else "queued")
    assert probes == ([] if change in {"unchanged", "changed", "missing"} else [path])
    scan.assert_not_called()
    async with api_env.factory() as session:
        jobs = list(await session.scalars(select(Job)))
        assert len(jobs) == 1 and jobs[0].status == ("failed" if change == "missing" else "staging")
        if change == "missing":
            assert jobs[0].error
        else:
            event = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == jobs[0].id, JobEvent.type == "enqueued")
            )
            assert event is not None
            source = event.payload["source"]
            assert {key: source[key] for key in source_identity(path.stat())} == source_identity(path.stat())
            assert event.payload["probe"]["size_bytes"] == path.stat().st_size
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.source_identity == source_identity(path.stat())


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_transcode_forced_probe(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch):
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    calls = []

    async def probe(_binary, source, **_kwargs):
        calls.append(source)
        return PROBE_RAW

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    response = await api_env.client.post(f"/api/files/{media_id}/transcode", params={"refresh": True})
    assert response.status_code == 202, response.text
    outcome = await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result")
    assert outcome["outcome"] == "queued"
    assert calls == [api_env.lib_dir / "movie.avi"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bulk, change, result",
    [
        (False, "changed", "healthy"),
        (False, "identity", "corrupt"),
        (False, "probe", "failed"),
        (True, "identity", "healthy"),
        (True, "probe", "corrupt"),
        (True, "changed", "failed"),
    ],
)
async def test_healthcheck_current_source(
    api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, bulk: bool, change: str, result: str
):
    _, media_id = await seed_probed(api_env)
    path = await change_source(api_env, media_id, change)
    raw = json.loads(json.dumps(PROBE_RAW))
    raw["streams"][0]["codec_name"] = "hevc"
    calls = []
    monkeypatch.setattr("server.fs.library.utcnow", lambda: T0)
    scan = Mock()
    monkeypatch.setattr(api_env.app.state.scanner, "queue_scan", scan)

    async def check(source, _ffmpeg, _ffprobe, log, *, hwaccel, on_progress):
        assert Path(source).samefile(path)
        calls.append(Path(source).read_bytes())
        log.append("healthcheck: completed\n")
        return healthcheck.HealthcheckResult(
            healthy=result == "healthy", failed=result == "failed", probe=raw, samples=[]
        )

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    if bulk:
        response = await api_env.client.post("/api/files/bulk", json={"action": "healthcheck", "file_ids": [media_id]})
        assert response.status_code == 202
        assert response.json()["failed"] == []
        job_id = response.json()["accepted"][0]["job_id"]
    else:
        response = await api_env.client.post(f"/api/files/{media_id}/healthcheck")
        assert response.status_code == 202
        job_id = response.json()["job_id"]
    service = api_env.app.state.healthchecks
    claimed = await service.claim_next()
    assert claimed is not None and claimed[0] == job_id
    await asyncio.wait_for(service.run(*claimed), 3)
    assert calls == [path.read_bytes()]
    scan.assert_not_called()
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        job = await session.get(Job, job_id)
        attempt = await session.get(JobAttempt, claimed[1])
        assert media is not None and media.source_identity == source_identity(path.stat())
        assert media.size == path.stat().st_size and media.mtime_ns == path.stat().st_mtime_ns
        assert media.video_codec == "hevc"
        probe = await session.get(ProbeRow, media.probe_id)
        assert probe is not None and json.loads(probe.raw_json) == raw
        assert media.health_status == ("pending" if result == "failed" else result)
        assert media.health_source == (None if result == "failed" else {"path": str(path), **media.source_identity})
        assert job is not None and job.status == ("failed" if result == "failed" else "succeeded")
        assert attempt is not None and (attempt.health is None) == (result == "failed")
        assert len(list(await session.scalars(select(Job)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["changed", "identity"])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_probe_current_source(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch, change: str):
    _, media_id = await seed_probed(api_env)
    path = await change_source(api_env, media_id, change)
    probes = []
    scan = Mock()
    monkeypatch.setattr(api_env.app.state.scanner, "queue_scan", scan)

    async def probe(_binary, source, **_kwargs):
        probes.append(source)
        return PROBE_RAW

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    response = await api_env.client.post(f"/api/files/{media_id}/probe")
    assert response.status_code == 202
    service = api_env.app.state.probes
    await service._fill()
    assert list(service._tasks) == [media_id]
    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
    assert probes == [path]
    scan.assert_not_called()
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.probe_id is not None
        assert media.source_identity == source_identity(path.stat())
        assert media.health_status == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["probe", "healthcheck", "transcode"])
async def test_manual_active_guard(api_env: ApiEnv, action: str):
    library_id, media_id = await seed_probed(api_env)
    await seed_flow_library(api_env, library_id, hevc_flow())
    await change_source(api_env, media_id, "changed")
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        probe_id, identity = media.probe_id, media.source_identity
        session.add(Job(media_file_id=media_id, library_id=library_id, kind="transcode", status="queued"))
        await session.commit()
    response = await api_env.client.post(f"/api/files/{media_id}/{action}")
    assert response.status_code == 409
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.probe_id == probe_id and media.source_identity == identity
        assert len(list(await session.scalars(select(Job)))) == 1


@pytest.mark.asyncio
async def test_healthcheck_catalog_race(api_env: ApiEnv, monkeypatch: pytest.MonkeyPatch):
    _, media_id = await seed_probed(api_env)
    path = await change_source(api_env, media_id, "changed")
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None
        previous_identity = media.source_identity
        probe = await session.get(ProbeRow, media.probe_id)
        assert probe is not None
        previous_raw = probe.raw_json

    async def check(source, _ffmpeg, _ffprobe, log, *, hwaccel, on_progress):
        assert Path(source).samefile(path)
        log.append("healthcheck: completed\n")
        async with api_env.factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None
            media.updated_at = T0
            await session.commit()
        return healthcheck.HealthcheckResult(healthy=True, probe=PROBE_RAW)

    monkeypatch.setattr(healthcheck, "_decode_healthcheck", check)
    response = await api_env.client.post(f"/api/files/{media_id}/healthcheck")
    assert response.status_code == 202
    service = api_env.app.state.healthchecks
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 3)
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        job = await session.get(Job, claimed[0])
        attempt = await session.get(JobAttempt, claimed[1])
        assert job is not None and job.status == "failed"
        assert attempt is not None and attempt.health is None
        assert media is not None and media.source_identity == previous_identity
        probe = await session.get(ProbeRow, media.probe_id)
        assert probe is not None and probe.raw_json == previous_raw
        assert len(list(await session.scalars(select(Job)))) == 1
