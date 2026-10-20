from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from server.api.libraries import LibraryBody, LibraryPatch
from server.backup import export_data, restore_data
from server.settings import ScannerConfig
from server.flows.engine import FlowResult
from server.fs.library import LibraryFS
from server.healthcheck import HealthcheckResult, HealthcheckService
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, Library, MediaFile
from server.queue.enqueue import FlowEnqueueService
from server.queue.store import enqueue_healthcheck_job, processing_backlog, retry_job
from server.processing_cutoff import ProcessingCutoff, epoch_ns, processing_allowed, processing_clause
from server.scanner.scan import scan_library
from server.staging import StagingService
from tests.helpers import request_retry
from tests.test_api import api_env, requeue_outcome
from tests.test_flow_enqueue import db, host, remux_definition, seed

T0 = datetime(2026, 1, 1, 12, 0, 0, 999999, tzinfo=UTC)
FIELDS = ("created_at", "first_seen", "mtime")


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", [T0, datetime(1970, 1, 1, tzinfo=UTC)])
async def test_cutoff_combinations(db, cutoff) -> None:
    media_id, library_id = await seed(db)
    async with db() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        for policy in [
            ProcessingCutoff(at=cutoff, fields=["created_at"]),
            ProcessingCutoff(at=cutoff, fields=["first_seen"]),
            ProcessingCutoff(at=cutoff, fields=["mtime"]),
            ProcessingCutoff(at=cutoff, fields=["created_at", "mtime"]),
            ProcessingCutoff(at=cutoff, fields=["created_at", "mtime"], match="any"),
            ProcessingCutoff(at=cutoff, fields=["created_at", "first_seen", "mtime"]),
            ProcessingCutoff(at=cutoff, fields=["created_at", "first_seen", "mtime"], match="any"),
        ]:
            library.processing_cutoff = policy.model_dump()
            for offsets in [
                (-1, -1, -1),
                (0, 0, 0),
                (1, 1, 1),
                (-1, 0, 1),
                (1, -1, -1),
                (-1, 1, -1),
                (1, -1, 1),
                (-1, -1, 1),
                (1, 1, -1),
            ]:
                media.created_at = cutoff + timedelta(microseconds=offsets[0])
                media.first_seen = cutoff + timedelta(microseconds=offsets[1])
                media.mtime_ns = epoch_ns(cutoff) + offsets[2]
                await session.flush()
                matches = [offsets[FIELDS.index(field)] >= 0 for field in policy.fields]
                expected = all(matches) if policy.match == "all" else any(matches)
                assert processing_allowed(library, media) is expected
                selected = await session.scalar(
                    select(MediaFile.id).join(Library).where(MediaFile.id == media_id, processing_clause())
                )
                assert (selected == media_id) is expected
        library.processing_cutoff = {}
        media.created_at = media.first_seen = T0 - timedelta(days=10)
        media.mtime_ns = -1
        await session.flush()
        assert processing_allowed(library, media)
        assert await session.scalar(select(MediaFile.id).join(Library).where(processing_clause())) == media_id


@pytest.mark.asyncio
async def test_default_timestamp_equality(db) -> None:
    media_id, library_id = await seed(db)
    cutoff = T0.replace(microsecond=0)
    async with db() as session:
        await session.execute(
            text("UPDATE media_files SET created_at = :date WHERE id = :id"),
            {"id": media_id, "date": "2026-01-01 12:00:00"},
        )
        library = await session.get(Library, library_id)
        assert library is not None
        library.processing_cutoff = ProcessingCutoff(at=cutoff).model_dump()
        await session.flush()
        assert await session.scalar(select(MediaFile.id).join(Library).where(processing_clause())) == media_id


@pytest.mark.asyncio
async def test_distant_cutoff_equality(db) -> None:
    media_id, library_id = await seed(db)
    cutoff = datetime(9999, 1, 1, 0, 0, 0, 999999, tzinfo=UTC)
    async with db() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        library.processing_cutoff = ProcessingCutoff(at=cutoff).model_dump()
        media.created_at = cutoff
        await session.flush()
        assert processing_allowed(library, media)
        assert await session.scalar(select(MediaFile.id).join(Library).where(processing_clause())) == media_id
        library.processing_cutoff = ProcessingCutoff(at=cutoff + timedelta(microseconds=1)).model_dump()
        await session.flush()
        assert not processing_allowed(library, media)
        assert await session.scalar(select(MediaFile.id).join(Library).where(processing_clause())) is None


@pytest.mark.parametrize("model", [LibraryBody, LibraryPatch])
@pytest.mark.parametrize(
    "cutoff",
    [
        None,
        [],
        {"at": None},
        {"at": "2026-01-01T12:00:00"},
        {"at": "2026-01-01T12:00:00Z", "fields": []},
        {"at": "2026-01-01T12:00:00Z", "fields": ["created_at", "created_at"]},
        {"at": "2026-01-01T12:00:00Z", "fields": ["last_seen"]},
        {"at": "2026-01-01T12:00:00Z", "fields": None},
        {"at": "2026-01-01T12:00:00Z", "match": "first"},
        {"at": "2026-01-01T12:00:00Z", "match": None},
        {"fields": ["created_at"]},
        {"match": "any"},
        {"at": "2026-01-01T12:00:00Z", "other": True},
    ],
)
def test_cutoff_validation(model, cutoff) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"name": "library", "roots": ["/media"], "processing_cutoff": cutoff})


@pytest.mark.asyncio
async def test_cutoff_api_roundtrip(api_env) -> None:
    response = await api_env.client.post("/api/libraries", json={"name": "cutoff", "roots": ["/media"]})
    assert response.status_code == 201
    library = response.json()
    assert library["processing_cutoff"] == {}
    identifier = library["id"]
    settings = {
        "at": "2026-01-01T14:00:00.999999+02:00",
        "fields": ["first_seen", "mtime"],
        "match": "any",
    }
    response = await api_env.client.patch(f"/api/libraries/{identifier}", json={"processing_cutoff": settings})
    assert response.status_code == 200
    settings["at"] = "2026-01-01T12:00:00.999999Z"
    assert response.json()["processing_cutoff"] == settings
    response = await api_env.client.patch(f"/api/libraries/{identifier}", json={"name": "renamed"})
    assert response.status_code == 200
    assert response.json()["processing_cutoff"] == settings
    response = await api_env.client.get("/api/libraries")
    assert response.json()[0]["processing_cutoff"] == settings
    response = await api_env.client.get(f"/api/libraries/{identifier}")
    assert response.json()["processing_cutoff"] == settings
    response = await api_env.client.patch(f"/api/libraries/{identifier}", json={"processing_cutoff": {}})
    assert response.status_code == 200 and response.json()["processing_cutoff"] == {}
    response = await api_env.client.patch(
        f"/api/libraries/{identifier}", json={"processing_cutoff": {"at": "2026-01-01T14:00:00.999999+02:00"}}
    )
    assert response.status_code == 200
    assert response.json()["processing_cutoff"] == {
        "at": "2026-01-01T12:00:00.999999Z",
        "fields": ["created_at"],
        "match": "all",
    }
    async with api_env.factory() as session:
        saved = await session.get(Library, identifier)
        assert saved is not None and saved.processing_cutoff == response.json()["processing_cutoff"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_cutoff_backup_roundtrip(db, enabled) -> None:
    policy = ProcessingCutoff(at=T0, fields=["created_at", "mtime"], match="any").model_dump() if enabled else {}
    async with db() as session:
        session.add(
            Library(
                name="cutoff",
                roots=["/media"],
                processing_cutoff=policy,
            )
        )
        await session.commit()
        data = json.loads(json.dumps(await export_data(session)))
        await session.commit()
        assert data["libraries"][0]["processing_cutoff"] == policy
        if enabled:
            data["libraries"][0]["processing_cutoff"]["at"] = "2026-01-01T14:00:00.999999+02:00"
        await restore_data(session, data, {"libraries": ["cutoff"], "replace": True})
        await session.commit()
    async with db() as session:
        library = (await session.scalars(select(Library))).one()
        assert library.processing_cutoff == policy
        assert library.suspended


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["excluded", "cutoff", "fields", "match"])
async def test_flow_policy_check(db, host, monkeypatch, policy) -> None:
    media_id, library_id = await seed(db, definition=remux_definition())
    async with db() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        media.created_at = T0 - timedelta(days=1)
        media.first_seen = T0
        library.processing_cutoff = (
            {}
            if policy == "cutoff"
            else ProcessingCutoff(at=T0, fields=["created_at"] if policy == "excluded" else ["first_seen"]).model_dump()
        )
        if policy == "match":
            library.processing_cutoff = ProcessingCutoff(
                at=T0, fields=["created_at", "first_seen"], match="any"
            ).model_dump()
        await session.commit()

    async def evaluate(*_args, **_kwargs):
        async with db() as session:
            library = await session.get(Library, library_id)
            assert library is not None
            if policy == "cutoff":
                library.processing_cutoff = ProcessingCutoff(at=T0 + timedelta(days=1)).model_dump()
            elif policy == "fields":
                library.processing_cutoff = {**library.processing_cutoff, "fields": ["created_at"]}
            elif policy == "match":
                library.processing_cutoff = {**library.processing_cutoff, "match": "all"}
            else:
                pytest.fail("excluded files must not be evaluated")
            await session.commit()
        return FlowResult(status="skipped", reason="controlled decision")

    monkeypatch.setattr("server.queue.enqueue.evaluate", evaluate)
    service = FlowEnqueueService(db, host)
    try:
        assert await asyncio.wait_for(service._pass(), 3) == 0
        async with db() as session:
            assert not list(await session.scalars(select(Job)))
            media = await session.get(MediaFile, media_id)
            assert media is not None and not media.transcode_decided
    finally:
        await asyncio.wait_for(service.close(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["healthcheck", "transcode"])
@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_manual_cutoff_bypass(api_env, action, bulk) -> None:
    media_id, library_id = await seed(api_env.factory, definition=remux_definition())
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.processing_cutoff = ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
        await session.commit()
    response = (
        await api_env.client.post("/api/files/bulk", json={"action": action, "file_ids": [media_id]})
        if bulk
        else await api_env.client.post(f"/api/files/{media_id}/{action}")
    )
    assert response.status_code == 202, response.text
    result = response.json()["accepted"][0] if bulk else response.json()
    job_id = result["job_id"]
    assert job_id is not None
    if action == "transcode":
        assert (await requeue_outcome(api_env, job_id, event_type="request_result"))["outcome"] == "queued"
    async with api_env.factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.kind == action
        job.status = "failed"
        await session.commit()
        request_id = await request_retry(session, job_id)
        retried = await retry_job(session, job_id, request_id=request_id)
        assert retried is not None and retried.status in {"queued", "staging"}
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_refreshed_cutoff_check(api_env, monkeypatch) -> None:
    media_id, library_id = await seed(api_env.factory, definition=remux_definition())
    async with api_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.healthcheck_enabled = True
        library.processing_cutoff = ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
        await session.commit()

    async def evaluate(*_args, **_kwargs):
        return FlowResult(status="skipped", reason="manual evaluation")

    monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate)
    response = await api_env.client.post(f"/api/files/{media_id}/transcode", params={"refresh": True})
    assert response.status_code == 202, response.text
    outcome = await requeue_outcome(api_env, response.json()["job_id"], event_type="request_result")
    assert outcome["decision"]["status"] == "skipped"
    async with api_env.factory() as session:
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.probe_id is not None
        assert not list(await session.scalars(select(Job).where(Job.kind == "healthcheck")))


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_policy", [False, True])
async def test_probe_cutoff_check(db, tmp_path, monkeypatch, edit_policy) -> None:
    media_id, library_id = await seed(db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*_args, **_kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), 3)
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    async with db() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.healthcheck_enabled = True
        library.processing_cutoff = (
            {} if edit_policy else ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
        )
        await session.commit()
    from tests.helpers import probe_media

    task = asyncio.create_task(
        probe_media(
            LibraryFS(library_id, sessionmaker=db, ffprobe_bin="fake", mkvpropedit_bin="fake", workspace_dir=tmp_path),
            media_id,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if edit_policy:
            async with db() as session:
                library = await session.get(Library, library_id)
                assert library is not None
                library.processing_cutoff = ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
                await session.commit()
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.healthcheck_id is None and result.media.probe_id is not None
        async with db() as session:
            assert not list(await session.scalars(select(Job).where(Job.kind == "healthcheck")))
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["published", "published_handoff", "probe"])
async def test_artifact_cutoff_finishes(db, tmp_path, monkeypatch, purpose) -> None:
    media_id, library_id = await seed(db)

    async def probe(*_args, **_kwargs):
        return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        assert media is not None and library is not None
        media.transcode_status = "transcoded"
        media.transcode_decided = True
        library.healthcheck_enabled = True
        library.processing_cutoff = (
            {} if purpose == "published_handoff" else ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
        )
        job = Job(media_file_id=media_id, library_id=library_id, status="succeeded", attempts=1)
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="succeeded")
        session.add(attempt)
        await session.flush()
        session.add(
            FinalizationJournal(
                attempt_id=attempt.id,
                source_path=media.path,
                destination_path=media.path,
                partial_path=f"{media.path}.partial",
                trash_path="",
                state="completed",
            )
        )
        session.add_all(
            [
                JobEvent(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    type="publication",
                    payload={"library_id": library_id, "preserve_timestamps": False},
                ),
                JobEvent(job_id=job.id, attempt_id=attempt.id, type="artifact_check_pending", payload={}),
            ]
        )
        await session.commit()
        attempt_id = attempt.id
    if purpose == "published_handoff":
        from server.fs.library import _ProbeOperation

        authorize = _ProbeOperation._authorize

        async def edit_policy(operation):
            if operation.metadata_committed:
                async with db() as session:
                    library = await session.get(Library, library_id)
                    assert library is not None
                    library.processing_cutoff = ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
                    await session.commit()
            await authorize(operation)

        monkeypatch.setattr(_ProbeOperation, "_authorize", edit_policy)
    fs = LibraryFS(library_id, sessionmaker=db, ffprobe_bin="fake", mkvpropedit_bin="fake", workspace_dir=tmp_path)
    from tests.helpers import probe_media

    result = await (probe_media(fs, media_id) if purpose == "probe" else fs.refresh_published(attempt_id))
    assert result.healthcheck_id is None and result.media.probe_id is not None
    async with db() as session:
        assert not list(await session.scalars(select(Job).where(Job.kind == "healthcheck")))
        assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "artifact_check_finished")) is not None
        assert (await processing_backlog(session, healthcheck_slots=4))["remaining_files"] == 0


@pytest.mark.asyncio
async def test_cutoff_backlog(db) -> None:
    media_id, library_id = await seed(db, definition=remux_definition())
    async with db() as session:
        library = await session.get(Library, library_id)
        media = await session.get(MediaFile, media_id)
        assert library is not None and media is not None
        library.processing_cutoff = ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump()
        library.healthcheck_enabled = True
        await session.flush()
        assert (await processing_backlog(session, healthcheck_slots=4))["remaining_files"] == 0
        media.probe_id = None
        media.health_status = "pending"
        await session.flush()
        backlog = await processing_backlog(
            session, healthcheck_slots=0, probe_slots=1, probe_mean_s=2, mkv_stats_slots=1, mkv_stats_mean_s=2
        )
        assert backlog["remaining_files"] == 1 and backlog["remaining_s"] == 2
        media.health_status = "healthy"
        await session.flush()
        job = await enqueue_healthcheck_job(session, media_id, library_id)
        await session.commit()
        backlog = await processing_backlog(session, healthcheck_slots=4)
        assert backlog["remaining_files"] == 1
        service = HealthcheckService(db, Path(media.path).parent / "checks", "fake", "fake")
        claimed = await service.claim_next()
        assert claimed is not None and claimed[0] == job.id


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", [False, True])
async def test_recheck_cutoff_policy(db, tmp_path, monkeypatch, excluded) -> None:
    media_id, library_id = await seed(db)
    async with db() as session:
        job = await enqueue_healthcheck_job(session, media_id, library_id)
        await session.commit()
        job_id = job.id

    async def check(*_args, **kwargs):
        log = _args[3]
        log.path.write_bytes(b"healthcheck: completed old generation\n")
        async with db() as session:
            media = await session.get(MediaFile, media_id)
            library = await session.get(Library, library_id)
            assert media is not None and library is not None
            path = Path(media.path)
            path.write_bytes(b"replacement generation")
            media.size = path.stat().st_size
            media.mtime_ns = path.stat().st_mtime_ns
            media.first_seen = T0
            media.source_identity = {"size": media.size}
            media.health_status = "pending"
            media.health_source = None
            library.processing_cutoff = (
                ProcessingCutoff(at=datetime(9999, 1, 1, tzinfo=UTC)).model_dump() if excluded else {}
            )
            await session.commit()
        return HealthcheckResult(healthy=True)

    monkeypatch.setattr("server.healthcheck._decode_healthcheck", check)
    service = HealthcheckService(db, tmp_path / "checks", "fake", "fake")
    claimed = await service.claim_next()
    assert claimed is not None
    await asyncio.wait_for(service.run(*claimed), 3)
    async with db() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == "failed"
        queued = list(await session.scalars(select(Job).where(Job.status == "queued")))
        assert len(queued) == (0 if excluded else 1)


@pytest.mark.asyncio
async def test_discovery_cutoff_dates(db, tmp_path, monkeypatch) -> None:
    media_id, library_id = await seed(db)
    async with db() as session:
        media = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        assert media is not None and library is not None
        path = Path(media.path)
        stamp = epoch_ns(T0 - timedelta(days=1))
        os.utime(path, ns=(stamp, stamp))
        media.created_at = media.first_seen = T0 - timedelta(days=1)
        media.mtime_ns = stamp
        library.processing_cutoff = ProcessingCutoff(at=T0).model_dump()
        library.healthcheck_enabled = True
        await session.commit()
    monkeypatch.setattr("server.scanner.scan.utcnow", lambda: T0 + timedelta(days=1))
    staging = StagingService(db, tmp_path / "workspace", min_free_gb=1)
    async with db() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        report = await scan_library(session, library, config=ScannerConfig(), staging=staging)
        assert report.unchanged == 1
        media = await session.get(MediaFile, media_id)
        assert media is not None and not processing_allowed(library, media)
        assert epoch_ns(media.first_seen) == stamp
        path.write_bytes(b"changed source")
        fresh = epoch_ns(T0 + timedelta(days=1))
        os.utime(path, ns=(fresh, fresh))
        report = await scan_library(session, library, config=ScannerConfig(), staging=staging)
        assert report.updated == 1
        await session.refresh(media)
        assert epoch_ns(media.created_at) == stamp
        assert epoch_ns(media.first_seen) == fresh and media.mtime_ns == fresh
        assert not processing_allowed(library, media)
        library.processing_cutoff = {**library.processing_cutoff, "fields": ["first_seen", "mtime"]}
        assert processing_allowed(library, media)
