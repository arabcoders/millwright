from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from common.fs.identity import source_identity
from server.settings import ScannerConfig
from server.flows.definition import FlowDefinition, TerminalNode
from server.fs import library as library_fs
from server.metadata_jobs import enqueue_metadata_job, metadata_source
from server.models import FinalizationJournal, Flow, Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow
from server.queue.enqueue import _candidate_statement
from server.queue.store import capture_source, processing_backlog
from server.probing import ProbeService
from server.scanner.scan import scan_library
from server.staging import StagingService
from tests.helpers import database_context


@pytest_asyncio.fixture
async def cached(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    path = root / "movie.mkv"
    path.write_bytes(b"original")
    async with database_context("probe-cache") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(root)], cooldown_s=300)
            session.add(library)
            await session.flush()
            row = MediaFile(
                library_id=library.id,
                path=str(path),
                size=path.stat().st_size,
                mtime_ns=path.stat().st_mtime_ns,
                source_identity=source_identity(path.stat()),
                container="mkv",
                video_codec="h264",
                medium="movie",
                health_status="healthy",
                health_source={"path": str(path), **source_identity(path.stat())},
                transcode_status="not_needed",
                transcode_decided=True,
                first_seen=datetime(2026, 1, 1, tzinfo=UTC),
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            session.add(row)
            await session.flush()
            probe = ProbeRow(
                media_file_id=row.id,
                raw_json=json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264", "bit_rate": "500"}]}),
            )
            session.add(probe)
            await session.flush()
            row.probe_id = probe.id
            await session.commit()
        yield (
            factory,
            library.id,
            row.id,
            probe.id,
            path,
            StagingService(factory, tmp_path / "workspace", min_free_gb=1),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["missing", "bytes", "mtime"])
async def test_stat_cache_reuse(cached, field: str) -> None:
    factory, library_id, media_id, probe_id, path, staging = cached
    async with factory() as session:
        row = await session.get(MediaFile, media_id)
        probe = await session.get(ProbeRow, probe_id)
        assert row is not None and probe is not None
        raw = probe.raw_json
        if field == "missing":
            row.source_identity = None
        elif field == "bytes":
            dates = path.stat().st_atime_ns, path.stat().st_mtime_ns
            path.write_bytes(b"changed!")
            os.utime(path, ns=dates)
        elif field == "mtime":
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
        await session.commit()
        library = await session.get(Library, library_id)
        assert library is not None
        wakes = []

        async def wake(_ids: list[int]) -> None:
            wakes.append(True)

        for _ in range(2):
            report = await scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=staging,
                on_files_pending=wake,
            )
            assert report.unchanged == 1 and report.updated == report.pruned == 0 and not report.errors
            await session.refresh(row)
            assert row.probe_id == probe_id and row.source_identity == source_identity(path.stat())
            assert row.first_seen == datetime(2026, 1, 1, tzinfo=UTC)
            assert row.transcode_status == "not_needed" and row.transcode_decided
            assert row.health_status == "healthy" and row.health_source == {"path": str(path), **row.source_identity}
            await session.refresh(probe)
            assert probe.raw_json == raw
        assert not wakes
        assert (await capture_source(row))["size"] == path.stat().st_size


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health,checks",
    [
        ("stale", False),
        ("current", False),
        ("missing", False),
        ("foreign", False),
        ("current", True),
    ],
)
async def test_cache_keeps_eligibility(cached, health: str, checks: bool) -> None:
    factory, library_id, media_id, probe_id, path, staging = cached
    async with factory() as session:
        row = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        probe = await session.get(ProbeRow, probe_id)
        assert row is not None and library is not None and probe is not None
        raw = probe.raw_json
        definition = FlowDefinition(entry="done", nodes=[TerminalNode(id="done", outcome="done")], edges=[])
        flow = Flow(name="flow", definition=definition.model_dump_json())
        session.add(flow)
        await session.flush()
        library.flow_id = flow.id
        library.healthcheck_enabled = checks
        row.transcode_decided = False
        row.transcode_status = "pending"
        previous = source_identity(path.stat())
        row.source_identity = None
        if health == "stale":
            row.health_source = {"path": str(path), "size": previous["size"] + 1}
        elif health == "current":
            row.health_source = {"path": str(path), **source_identity(path.stat())}
        elif health == "missing":
            row.health_source = None
        else:
            row.health_source = {"path": str(path.with_name("other.mkv")), **previous}
        await session.commit()
        eligible = health == "current"
        for _ in range(2):
            report = await scan_library(session, library, config=ScannerConfig(), staging=staging)
            assert report.unchanged == 1 and report.updated == 0 and not report.errors
            await session.refresh(row)
            assert row.probe_id == probe_id and row.source_identity == source_identity(path.stat())
            assert row.first_seen == datetime(2026, 1, 1, tzinfo=UTC)
            assert not row.transcode_decided and row.transcode_status == "pending"
            assert row.health_status == ("healthy" if eligible else "pending")
            candidates = list(await session.scalars(_candidate_statement(10)))
            assert [candidate.id for candidate in candidates] == ([media_id] if eligible else [])
            if eligible:
                assert row.health_source == {"path": str(path), **row.source_identity}
            else:
                assert row.health_source is None
            backlog = await processing_backlog(session, healthcheck_slots=0)
            assert backlog["remaining_files"] == int(eligible)
            await session.refresh(probe)
            assert probe.raw_json == raw
        assert await session.scalar(select(Job.id).where(Job.media_file_id == media_id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["size", "failed", "stats", "missing_probe"])
async def test_cache_updates(cached, reason: str) -> None:
    factory, library_id, media_id, probe_id, path, staging = cached
    async with factory() as session:
        row = await session.get(MediaFile, media_id)
        library = await session.get(Library, library_id)
        probe = await session.get(ProbeRow, probe_id)
        assert row is not None and library is not None and probe is not None
        if reason == "size":
            path.write_bytes(b"larger replacement")
        elif reason == "failed":
            row.health_status = "probe_failed"
        elif reason == "stats":
            library.mkv_stats = True
            probe.raw_json = json.dumps({"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]})
        else:
            row.probe_id = None
            await session.flush()
            await session.delete(probe)
        await session.commit()
        wakes = []

        async def wake(_ids: list[int]) -> None:
            wakes.append(True)

        report = await scan_library(session, library, config=ScannerConfig(), staging=staging, on_files_pending=wake)
        assert (report.updated, report.unchanged) == ((0, 1) if reason == "failed" else (1, 0))
        assert not report.errors
        assert wakes == ([] if reason == "failed" else [True])
        await session.refresh(row)
        assert row.probe_id == (probe_id if reason == "failed" else None)
        assert row.health_status == ("probe_failed" if reason == "failed" else "pending")
        assert (row.transcode_status, row.transcode_decided) == (
            ("pending", False) if reason == "size" else ("not_needed", True)
        )


@pytest.mark.asyncio
async def test_unknown_identity_rejected(cached) -> None:
    factory, _library_id, media_id, _probe_id, _path, _staging = cached
    async with factory() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None
        row.source_identity = None
        await session.commit()
        with pytest.raises(ValueError):
            await capture_source(row)


@pytest.mark.asyncio
async def test_rebind_requests_healthcheck(cached) -> None:
    factory, library_id, media_id, probe_id, path, staging = cached
    async with factory() as session:
        library = await session.get(Library, library_id)
        row = await session.get(MediaFile, media_id)
        assert library is not None and row is not None
        library.healthcheck_enabled = True
        previous = source_identity(path.stat())
        row.source_identity = None
        row.health_source = {"path": str(path), "size": previous["size"] + 1}
        await session.commit()
        report = await scan_library(session, library, config=ScannerConfig(), staging=staging)
        assert report.unchanged == 1 and report.updated == 0
        await session.refresh(row)
        assert row.probe_id == probe_id and row.health_status == "pending" and row.health_source is None
        job = await session.scalar(select(Job).where(Job.media_file_id == media_id))
        assert job is not None and job.kind == "healthcheck" and job.status == "queued"


@pytest.mark.asyncio
async def test_corrupt_cache_retained(cached) -> None:
    factory, library_id, media_id, probe_id, path, staging = cached
    async with factory() as session:
        library = await session.get(Library, library_id)
        row = await session.get(MediaFile, media_id)
        assert library is not None and row is not None
        row.source_identity = None
        row.health_status = "corrupt"
        await session.commit()
        report = await scan_library(session, library, config=ScannerConfig(), staging=staging)
        assert report.unchanged == 1
        await session.refresh(row)
        assert row.probe_id == probe_id and row.health_status == "corrupt"
        assert row.health_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["healthy", "corrupt"])
async def test_probe_retains_health(cached, monkeypatch: pytest.MonkeyPatch, health: str) -> None:
    factory, library_id, media_id, _probe_id, path, staging = cached

    async def probe(*_args, **_kwargs) -> dict:
        return {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "bit_rate": "500"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "bit_rate": "100"},
            ],
            "format": {"duration": "1"},
        }

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    async with factory() as session:
        library = await session.get(Library, library_id)
        row = await session.get(MediaFile, media_id)
        assert library is not None and row is not None
        row.probe_id = None
        row.health_status = health
        health_source = {"path": str(path), **source_identity(path.stat())}
        row.health_source = health_source
        library.healthcheck_enabled = True
        library.mkv_stats = True
        await session.flush()
        await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id == media_id))
        job = await enqueue_metadata_job(session, row, library, kind="probe", priority=1)
        await session.commit()
        source = await session.scalar(
            select(JobEvent.payload).where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
        )
        stamp = row.updated_at

        report = await scan_library(session, library, config=ScannerConfig(), staging=staging)

        assert report.unchanged == 1 and report.updated == 0
        await session.refresh(row)
        assert row.probe_id is None and row.health_status == health
        assert row.health_source == health_source
        assert row.updated_at == stamp
        assert await session.scalar(select(Job.status).where(Job.id == job.id)) == "queued"
        assert (
            await session.scalar(
                select(JobEvent.payload).where(JobEvent.job_id == job.id, JobEvent.type == "metadata_source")
            )
            == source
            == metadata_source(row)
        )

    service = ProbeService(factory, "fake-probe", "fake-editor", workspace_dir=staging.workspace_dir)
    try:
        await asyncio.wait_for(service._fill(), 3)
        await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        async with factory() as session:
            row = await session.get(MediaFile, media_id)
            assert row is not None and row.probe_id is not None
            assert row.health_status == health and row.health_source == health_source
            jobs = (
                await session.execute(select(Job.id, Job.kind, Job.status).where(Job.media_file_id == media_id))
            ).all()
            assert jobs == [(job.id, "probe", "succeeded")]
    finally:
        await asyncio.wait_for(service.close(), 3)
