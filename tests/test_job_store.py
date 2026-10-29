from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.actions import Action, ExtractSubtitles
from common.ctx import FacetValue
from common.plan.models import FFmpegPlan, RateControl, VideoTarget
from common.enums import JobStatus, TranscodeStatus
from pydantic import TypeAdapter
from server.flows.definition import FlowDefinition
from server.flows.engine import FlowContinuation
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, NodeRow
from server.queue.store import (
    ActiveJobError,
    bump_job,
    cancel_pending,
    claim,
    capture_source,
    enqueue_file_job,
    enqueue_healthcheck_job,
    finish,
    get_job,
    list_jobs,
    mark_staged,
    requeue,
    requeue_startup,
    retry_job,
)
from common.fs.identity import source_identity
from tests.helpers import request_retry, database_context, graph_flow
from tests.helpers import make_test_temp_dir

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_ACTIONS = TypeAdapter(list[Action])


def plan() -> FFmpegPlan:
    return FFmpegPlan(video=VideoTarget(encoder="libx264", rate=RateControl(quality=21, quality_option="crf")))


def after(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest_asyncio.fixture(loop_scope="module")
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("jobs") as factory:
        yield factory


async def add_file(session: AsyncSession, name: str) -> tuple[int, int]:
    path = make_test_temp_dir("job-store") / name
    path.write_bytes(b"x" * 1_000_000)
    os.utime(path, ns=(1, 1))
    identity = source_identity(path.stat())
    library = Library(name=name, roots=[str(path.parent)])
    session.add(library)
    await session.flush()
    media = MediaFile(
        library_id=library.id,
        path=str(path),
        size=identity["size"],
        mtime_ns=path.stat().st_mtime_ns,
        source_identity=identity,
        container="mkv",
        medium="movie",
        last_scanned_at=T0,
    )
    session.add(media)
    await session.flush()
    return media.id, library.id


async def add_node(session: AsyncSession, name: str, capabilities: dict | None = None) -> int:
    node = NodeRow(name=name, display_name=name, token_hash="hash", capabilities=capabilities or {})
    session.add(node)
    await session.flush()
    return node.id


async def event_types(session: AsyncSession, job_id: int) -> list[str]:
    stmt = select(JobEvent.type).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
    return list((await session.execute(stmt)).scalars())


@pytest.mark.asyncio(loop_scope="module")
async def test_claim_stale_cancel(db) -> None:
    async with db() as session:
        media, library = await add_file(session, "claim-stale")
        node = await add_node(session, "claim-node")
        job = await enqueue_healthcheck_job(session, media, library)
        await session.commit()
    async with db() as stale:
        cached = await get_job(stale, job.id)
        async with db() as cancelling:
            assert await cancel_pending(cancelling, job.id) is not None
            await cancelling.commit()
        assert cached is not None and cached.status == JobStatus.QUEUED
        assert await claim(stale, job.id, node) is None
        await stale.commit()
    async with db() as session:
        stored = await get_job(session, job.id)
        assert stored is not None and stored.status == JobStatus.CANCELLED
        assert not list((await session.execute(select(JobAttempt))).scalars())


@pytest.mark.asyncio(loop_scope="module")
async def test_claim_once(db) -> None:
    async with db() as session:
        media, library = await add_file(session, "claim-once")
        node = await add_node(session, "claim-node")
        job = await enqueue_healthcheck_job(session, media, library)
        await session.commit()
    async with db() as first, db() as stale:
        cached = await get_job(stale, job.id)
        assert await claim(first, job.id, node) is not None
        await first.commit()
        assert cached is not None and cached.status == JobStatus.QUEUED
        assert await claim(stale, job.id, node) is None
        await stale.commit()
    async with db() as session:
        assert len(list((await session.execute(select(JobAttempt))).scalars())) == 1
        stored = await get_job(session, job.id)
        assert stored is not None and stored.attempts == 1


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("refresh", [False, True])
async def test_staging_rejects_replacement(db, tmp_path: Path, refresh: bool) -> None:
    from server.staging import StagingService

    path = tmp_path / "movie.mkv"
    path.write_bytes(b"source")
    async with db() as session:
        media_id, library_id = await add_file(session, "source-identity")
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.path, media.size, media.mtime_ns = str(path), path.stat().st_size, path.stat().st_mtime_ns
        media.source_identity = source_identity(path.stat())
        library = await session.get(Library, library_id)
        assert library is not None
        library.roots = [str(path.parent)]
        source = await capture_source(media)
        path.write_bytes(b"changed source")
        if refresh:
            media.size, media.mtime_ns = path.stat().st_size, path.stat().st_mtime_ns
        await session.commit()
        job = await enqueue_file_job(session, media_id, library_id, plan(), source=source)
        await session.commit()
    workspace = tmp_path / "workspace"
    assert await StagingService(db, workspace, min_free_gb=1).stage_job(job.id) == 0
    async with db() as session:
        current = await get_job(session, job.id)
        assert current is not None and current.status == JobStatus.FAILED
    assert path.read_bytes() == b"changed source"
    assert not (workspace / "jobs" / str(job.id) / "source.mkv").exists()


@pytest.mark.asyncio(loop_scope="module")
async def test_missing_source_returns(db, tmp_path: Path) -> None:
    from server.staging import StagingService

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    os.utime(source, ns=(1, 1))
    identity = source_identity(source.stat())
    source.unlink()
    async with db() as session:
        media_id, library_id = await add_file(session, "missing-source")
        media = await session.get(MediaFile, media_id)
        media.path, media.size, media.mtime_ns = str(source), 6, 1
        media.source_identity = identity
        library = await session.get(Library, library_id)
        assert library is not None
        library.roots = [str(source.parent)]
        job = await enqueue_file_job(session, media_id, library_id, plan())
        await session.commit()
    source.write_bytes(b"source")
    os.utime(source, ns=(1, 1))
    assert await StagingService(db, tmp_path / "workspace", min_free_gb=1).stage_job(job.id) == 0
    async with db() as session:
        current = await get_job(session, job.id)
        assert current is not None
        assert current.status == JobStatus.FAILED
        assert current.error is not None
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio(loop_scope="module")
async def test_staging_preserves_history(db) -> None:
    async with db() as session:
        media, library = await add_file(session, "history")
        node = await add_node(session, "history-node")
        job = await enqueue_file_job(session, media, library, plan())
        await mark_staged(session, job.id)
        await claim(session, job.id, node, now=T0)
        await finish(session, job.id, JobStatus.FAILED, error="original", source_size=100, output_size=50, now=after(1))
        await session.commit()
        attempt = (await session.execute(select(JobAttempt))).scalar_one()
        before = {column.name: getattr(attempt, column.name) for column in JobAttempt.__table__.columns}
        request_id = await request_retry(session, job.id)
        await retry_job(session, job.id, request_id=request_id)
        await finish(session, job.id, JobStatus.FAILED, error="staging failure", now=after(2))
        await session.commit()
        assert {column.name: getattr(attempt, column.name) for column in JobAttempt.__table__.columns} == before
        event = (
            (await session.execute(select(JobEvent).where(JobEvent.type == "failed").order_by(JobEvent.id.desc())))
            .scalars()
            .first()
        )
        assert event.attempt_id is None


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("miss_precheck", [False, True])
async def test_pending_blocks_queue(db, monkeypatch, miss_precheck: bool) -> None:
    async with db() as session:
        media, library = await add_file(session, "retry-conflict")
        old = await enqueue_file_job(session, media, library, plan())
        await finish(session, old.id, JobStatus.FAILED)
        await session.commit()
        await request_retry(session, old.id)
        if miss_precheck:
            monkeypatch.setattr("server.queue.store.active_job_for_file", AsyncMock(return_value=None))
        with pytest.raises(ActiveJobError):
            await enqueue_healthcheck_job(session, media, library)
        stored = await get_job(session, old.id)
        assert stored is not None and stored.status == JobStatus.PENDING
        await session.commit()


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("kind", ["transcode", "healthcheck"])
@pytest.mark.parametrize("missing", [False, True])
async def test_startup_retains_log(db, kind, missing: bool) -> None:
    from server.healthcheck import HealthcheckService
    from server.healthlogs import create_healthcheck_log, recover_healthcheck_logs
    from server.staging import StagingService
    from server.workspace import snapshot_interrupted_logs

    root = make_test_temp_dir("startup-logs")
    async with db() as session:
        media, library = await add_file(session, "startup-log")
        node = await add_node(session, "log-node")
        job = (
            await enqueue_healthcheck_job(session, media, library)
            if kind == "healthcheck"
            else await enqueue_file_job(session, media, library, plan())
        )
        if kind == "transcode":
            await mark_staged(session, job.id)
            await claim(session, job.id, node, now=T0)
        else:
            await session.commit()
            claimed = await HealthcheckService(db, root, "unused", "unused").claim_next()
            assert claimed is not None and claimed[0] == job.id
        attempt = (await session.execute(select(JobAttempt))).scalar_one()
        attempt.workload = kind
        if kind == "healthcheck":
            log = create_healthcheck_log(root, attempt.id)
            relative, path = log.path.name, log.path
            session.add(
                JobEvent(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    type="healthcheck_log_created",
                    payload={"log_path": relative},
                )
            )
        else:
            relative = f"jobs/{job.id}/attempts/1/{kind}.log"
            path = root / relative
            path.parent.mkdir(parents=True)
        path.write_bytes(b"interrupted\n\xff")
        if missing:
            path.unlink()
        attempt.log_path = relative
        await session.commit()
        if kind == "healthcheck":
            await recover_healthcheck_logs(db, root, startup=True)
        await snapshot_interrupted_logs(session, root)
        recovered = await requeue_startup(session, after(5))
        await session.commit()
    if kind == "healthcheck":
        assert recovered == 1
        await recover_healthcheck_logs(db, root)
        assert await StagingService(db, root, min_free_gb=1).prune_attempt(attempt.id) is False
    else:
        assert recovered == 0
    async with db() as session:
        saved = await session.get(JobAttempt, attempt.id)
        assert saved.log_data == (None if missing else b"interrupted\n\xff")
        assert saved.result == ("interrupted" if kind == "healthcheck" else None)
        stored = await get_job(session, job.id)
        assert stored is not None
        assert stored.status == (JobStatus.QUEUED if kind == "healthcheck" else JobStatus.CLAIMED)
        assert stored.node_id == (None if kind == "healthcheck" else node)
        if kind == "transcode":
            assert stored.lease_expires_at == after(300)
        if missing:
            event = (
                await session.execute(
                    select(JobEvent).where(
                        JobEvent.type == ("healthcheck_log_unavailable" if kind == "healthcheck" else "log_unavailable")
                    )
                )
            ).scalar_one()
            assert event.attempt_id == attempt.id
    assert path.exists() is (kind == "transcode" and not missing)


@pytest.mark.asyncio(loop_scope="module")
async def test_enqueue_claim_finish(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "a.mkv")
        node_id = await add_node(session, "node-a", {"workload": "transcode", "encoders": ["libx264"]})
        log = [{"step": "transcode.video", "outcome": "plan"}]
        flow = FlowDefinition.model_validate(graph_flow("action.extract_subs", "action.notify"))
        continuation = FlowContinuation(flow=flow, node="n0")
        facets: dict[str, FacetValue] = {"min_size.eligible": True, "languages": ["eng", "jpn"]}
        actions: list[Action] = [
            ExtractSubtitles(formats=["srt"], languages=["eng"]),
            ExtractSubtitles(formats=["vtt"], languages=["jpn"]),
        ]
        job = await enqueue_file_job(
            session,
            media_id,
            library_id,
            plan(),
            decision_log=log,
            continuation=continuation,
            facets=facets,
            pre_actions=actions,
            now=T0,
        )
        await session.commit()
        assert job.status == JobStatus.STAGING
        assert job.created_at == T0
        assert FFmpegPlan.model_validate(job.plan) == plan()
        assert job.decision_log == log

        assert await mark_staged(session, job.id, now=after(0.5))
        claimed = await claim(session, job.id, node_id=node_id, lease_ttl_s=100.0, now=after(1))
        assert claimed is not None
        assert claimed.status == JobStatus.CLAIMED
        assert claimed.node_id == node_id
        assert claimed.attempts == 1
        assert claimed.lease_expires_at == after(101)
        assert claimed.started_at == after(1)
        attempt = (await session.execute(select(JobAttempt).where(JobAttempt.job_id == job.id))).scalar_one()
        assert attempt.attempt_number == 1
        assert attempt.node_name == "node-a"
        assert attempt.capabilities == {"workload": "transcode", "encoders": ["libx264"]}
        assert attempt.workload == "transcode"

        assert await finish(session, job.id, JobStatus.SUCCEEDED, now=after(3)) is True
        await session.commit()

    async with db() as session:
        done = await get_job(session, job.id)
        assert done is not None
        assert done.status == JobStatus.SUCCEEDED
        assert done.finished_at == after(3)
        assert done.facets == facets
        assert done.continuation == continuation.model_dump(mode="json")
        assert done.pre_actions is not None
        assert _ACTIONS.validate_python(done.pre_actions) == actions
        assert await event_types(session, job.id) == [
            "enqueued",
            "staged",
            "queued",
            "claimed",
            "succeeded",
        ]
        events = list(
            (await session.execute(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id))).scalars()
        )
        attempt = (await session.execute(select(JobAttempt).where(JobAttempt.job_id == job.id))).scalar_one()
        assert all(event.attempt_id == attempt.id for event in events[3:])
        assert attempt.finished_at == after(3)


@pytest.mark.asyncio(loop_scope="module")
async def test_duplicate_active_raises(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "a.mkv")
        first = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        with pytest.raises(ActiveJobError):
            await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await finish(session, first.id, JobStatus.FAILED, error="boom", now=after(1))
        second = await enqueue_file_job(session, media_id, library_id, plan(), now=after(2))
        assert second.id != first.id
        await session.commit()

    async def miss_active(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.queue.store.active_job_for_file", miss_active)
    async with db() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.watch = True
        with pytest.raises(ActiveJobError):
            await enqueue_file_job(session, media_id, library_id, plan(), now=after(3))
        assert await get_job(session, second.id) is not None
        await session.commit()

    async with db() as session:
        library = await session.get(Library, library_id)
        assert library is not None and library.watch
        jobs = list(
            (await session.execute(select(JobEvent.job_id).where(JobEvent.job_id.in_([first.id, second.id])))).scalars()
        )
        assert set(jobs) == {first.id, second.id}


@pytest.mark.asyncio(loop_scope="module")
async def test_requeue_lifecycle(db: async_sessionmaker[AsyncSession]) -> None:
    ttl = 300.0
    async with db() as session:
        media_id, library_id = await add_file(session, "a.mkv")
        node_a = await add_node(session, "node-a", {})
        node_b = await add_node(session, "node-b", {})
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        assert await mark_staged(session, job.id, now=T0)
        await claim(session, job.id, node_id=node_a, lease_ttl_s=ttl, now=T0)
        await session.commit()

    async with db() as session:
        requeued = await requeue(session, job.id, now=after(ttl + 1))
        assert requeued is not None
        assert requeued.status == JobStatus.STAGING
        assert requeued.attempts == 1
        assert requeued.node_id is None
        assert requeued.lease_expires_at is None

        assert await mark_staged(session, job.id, now=after(ttl + 1.5))
        await claim(session, job.id, node_id=node_b, lease_ttl_s=ttl, now=after(ttl + 2))
        attempts = list(
            (
                await session.execute(
                    select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_number)
                )
            ).scalars()
        )
        assert [(item.attempt_number, item.node_name, item.workload) for item in attempts] == [
            (1, "node-a", "transcode"),
            (2, "node-b", "transcode"),
        ]
        await session.commit()

    async with db() as session:
        requeued = await requeue(session, job.id, now=after(2 * ttl + 3), max_attempts=2)
        assert requeued is not None
        assert requeued.status == JobStatus.FAILED
        assert requeued.error is not None
        assert requeued.finished_at == after(2 * ttl + 3)
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.transcode_status == TranscodeStatus.FAILED.value
        assert await event_types(session, job.id) == [
            "enqueued",
            "staged",
            "queued",
            "claimed",
            "retried",
            "staged",
            "queued",
            "claimed",
            "failed",
        ]
        await session.commit()


@pytest.mark.asyncio(loop_scope="module")
async def test_finish_idempotent(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "a.mkv")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        assert await finish(session, job.id, JobStatus.SUCCEEDED, warning="Cleanup pending.", now=after(1)) is True
        assert await finish(session, job.id, JobStatus.SUCCEEDED, now=after(2)) is False
        assert await finish(session, job.id, JobStatus.CANCELLED, now=after(3)) is False
        await session.commit()

    async with db() as session:
        done = await get_job(session, job.id)
        assert done is not None
        assert done.status == JobStatus.SUCCEEDED
        assert done.warning == "Cleanup pending."
        assert done.finished_at == after(1)
        assert await event_types(session, job.id) == ["enqueued", "succeeded"]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("warning", [None, "Cleanup pending."])
async def test_finish_warning_result(db: async_sessionmaker[AsyncSession], warning: str | None) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "warning.mkv")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        job.warning = "Waiting for capacity."
        assert await finish(session, job.id, JobStatus.SUCCEEDED, warning=warning, now=after(1))
        await session.commit()
    async with db() as session:
        done = await get_job(session, job.id)
        assert done is not None and done.status == JobStatus.SUCCEEDED
        assert done.warning == warning


@pytest.mark.asyncio(loop_scope="module")
async def test_finish_caller_transaction() -> None:
    async with database_context("finish-transaction") as factory:
        async with factory() as session:
            media_id, library_id = await add_file(session, "transaction.mkv")
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
            job.warning = "Waiting for capacity."
            await session.commit()
        async with factory() as session:
            assert await finish(session, job.id, JobStatus.SUCCEEDED, warning="Cleanup pending.", now=after(1))
            async with factory() as reader:
                before = await get_job(reader, job.id)
                assert before is not None and before.status == JobStatus.STAGING
                assert before.warning == "Waiting for capacity."
                assert await event_types(reader, job.id) == ["enqueued"]
            await session.rollback()
        async with factory() as session:
            retained = await get_job(session, job.id)
            assert retained is not None and retained.status == JobStatus.STAGING
            assert retained.warning == "Waiting for capacity."
            assert await event_types(session, job.id) == ["enqueued"]


@pytest.mark.asyncio(loop_scope="module")
async def test_finish_from_queued(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "a.mkv")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        job.warning = "Waiting for capacity."
        assert await finish(session, job.id, JobStatus.CANCELLED, now=after(1)) is True
        done = await get_job(session, job.id)
        assert done is not None
        assert done.status == JobStatus.CANCELLED
        assert done.warning is None
        assert done.finished_at == after(1)
        assert done.started_at is None


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("deleting", [False, True])
async def test_requeue_requires_active(db: async_sessionmaker[AsyncSession], deleting: bool) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "a.mkv")
        node_id = await add_node(session, "node-a")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        assert await requeue(session, job.id, now=after(1)) is None
        assert await mark_staged(session, job.id, now=after(1))
        await claim(session, job.id, node_id=node_id, lease_ttl_s=100.0, now=after(2))
        job.delete_requested = deleting
        requeued = await requeue(session, job.id, now=after(3))
        if deleting:
            assert requeued is None and job.status == JobStatus.CLAIMED
            return
        assert requeued is not None
        assert requeued.status == JobStatus.STAGING
        assert requeued.progress is None
        jobs, total = await list_jobs(session, statuses=["staging"])
        assert len(jobs) == 1
        assert total == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_startup_requeues_batches(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    from server.healthcheck import HealthcheckService
    from server.queue import store

    monkeypatch.setattr(store, "QUERY_BATCH", 2)
    async with db() as session:
        node = await add_node(session, "startup-batches")
        jobs = []
        for index in range(5):
            media, library = await add_file(session, f"startup-health-{index}")
            healthcheck = await enqueue_healthcheck_job(session, media, library)
            jobs.append(healthcheck.id)
        media, library = await add_file(session, "startup-remote-health")
        remote = await enqueue_healthcheck_job(session, media, library)
        await claim(session, remote.id, node, now=T0)
        media, library = await add_file(session, "startup-transcode")
        transcode = await enqueue_file_job(session, media, library, plan())
        await mark_staged(session, transcode.id)
        await claim(session, transcode.id, node, now=T0)
        await session.commit()
    service = HealthcheckService(db, make_test_temp_dir("startup-health-batches"), "unused", "unused")
    for _ in jobs:
        assert await service.claim_next() is not None
    async with db() as session:
        assert await requeue_startup(session, after(5)) == 5
        await session.commit()
    async with db() as session:
        statuses = dict((await session.execute(select(Job.id, Job.status))).all())
        assert all(statuses[job_id] == "queued" for job_id in jobs)
        assert statuses[transcode.id] == "claimed"
        assert statuses[remote.id] == "claimed"
        assert await session.scalar(select(func.count()).select_from(JobEvent).where(JobEvent.type == "retried")) == 5


@pytest.mark.asyncio(loop_scope="module")
async def test_preserves_attempt(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "startup.mkv")
        node_id = await add_node(session, "node-a")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await mark_staged(session, job.id, now=T0)
        await claim(session, job.id, node_id=node_id, now=after(1))
        attempt = (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id))).one()
        attempt.status = JobStatus.FAILED.value
        attempt.result = "interrupted"
        attempt.finished_at = after(2)
        recovered = await requeue_startup(session, after(3))
        await session.commit()

    assert recovered == 0
    async with db() as session:
        recovered_job = await get_job(session, job.id)
        attempts = list((await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id))).all())
    assert recovered_job is not None and recovered_job.status == JobStatus.CLAIMED
    assert recovered_job.node_id == node_id and recovered_job.lease_expires_at == after(301)
    assert len(attempts) == 1
    assert attempts[0].status == JobStatus.FAILED.value
    assert attempts[0].finished_at == after(2)


@pytest.mark.asyncio(loop_scope="module")
async def test_claims_interrupted_healthcheck(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "health-restart.mkv")
        node_id = await add_node(session, "server")
        job = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
        old = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            node_name="server",
            workload="healthcheck",
            status=JobStatus.FAILED.value,
            result="interrupted",
            error="requeued",
            claimed_at=after(1),
            finished_at=after(2),
        )
        job.attempts = 1
        session.add(old)
        await session.flush()
        claimed = await claim(session, job.id, node_id=node_id, now=after(3))
        await session.commit()

    assert claimed is not None and claimed.status == JobStatus.CLAIMED
    async with db() as session:
        attempts = list(
            await session.scalars(
                select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_number)
            )
        )
    assert [(attempt.attempt_number, attempt.status, attempt.result) for attempt in attempts] == [
        (1, JobStatus.FAILED.value, "interrupted"),
        (2, JobStatus.CLAIMED.value, None),
    ]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("kind,previous", [("healthcheck", "pending"), ("transcode", "transcoded")])
@pytest.mark.parametrize("outcome", [JobStatus.FAILED, JobStatus.CANCELLED])
async def test_preserves_file_result(
    db: async_sessionmaker[AsyncSession], kind: str, previous: str, outcome: JobStatus
) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "result.mkv")
        media = await session.get(MediaFile, media_id)
        assert media is not None
        media.transcode_status = previous
        job = (
            await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
            if kind == "healthcheck"
            else await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        )
        assert await finish(session, job.id, outcome, now=after(1))
        await session.commit()
        assert media.transcode_status == previous


@pytest.mark.asyncio(loop_scope="module")
async def test_retry_resets(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "retry-me.mkv")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await finish(session, job.id, JobStatus.FAILED, error="boom", now=after(5))
        media = await session.get(MediaFile, media_id)
        assert media is not None and media.transcode_status == TranscodeStatus.FAILED.value
        await session.commit()

        request_id = await request_retry(session, job.id)
        retried = await retry_job(session, job.id, now=after(6), request_id=request_id)
        await session.commit()

        assert retried is not None
        assert retried.status == JobStatus.STAGING
        assert retried.attempts == 0
        assert retried.error is None
        assert retried.warning is None
        assert retried.finished_at is None
        assert media.transcode_status == TranscodeStatus.PENDING.value
        assert await event_types(session, job.id) == ["enqueued", "failed", "requested", "retried", "requeue"]


@pytest.mark.asyncio(loop_scope="module")
async def test_cancel_pending_states(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "cancel-pending.mkv")
        node_id = await add_node(session, "node-a")
        for status in (JobStatus.QUEUED, JobStatus.STAGING, JobStatus.HOLD):
            job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
            job.status = status.value
            job.warning = "stale"
            assert await cancel_pending(session, job.id, now=after(1)) is not None
            assert job.status == JobStatus.CANCELLED.value and job.warning is None
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.transcode_status == TranscodeStatus.CANCELLED.value
            event = await session.scalar(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id.desc()))
            assert event is not None and event.attempt_id is None
        claimed = await enqueue_file_job(session, media_id, library_id, plan(), now=after(2))
        claimed.status = JobStatus.QUEUED.value
        await claim(session, claimed.id, node_id=node_id, lease_ttl_s=100.0, now=after(3))
        assert await cancel_pending(session, claimed.id, now=after(4)) is None
        await finish(session, claimed.id, JobStatus.SUCCEEDED, now=after(5))
        assert await cancel_pending(session, claimed.id, now=after(6)) is None


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("change", ["superseded", "cancelled", "promoted"])
async def test_retry_checks_owner(db: async_sessionmaker[AsyncSession], change: str) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "active.mkv")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await finish(session, job.id, JobStatus.FAILED)
        await session.commit()
        request_id = await request_retry(session, job.id)
        event = await session.get(JobEvent, request_id)
        assert event is not None
        if change == "superseded":
            session.add(JobEvent(job_id=job.id, type="requested", payload=event.payload))
        elif change == "cancelled":
            event.payload = {**event.payload, "cancelled": True}
        else:
            job.status = JobStatus.STAGING
        await session.commit()
        with pytest.raises(ValueError):
            await retry_job(session, job.id, request_id=request_id)
        assert job.attempts == 0 and job.error is None


@pytest.mark.asyncio(loop_scope="module")
async def test_bump_queued(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        media_id, library_id = await add_file(session, "bump-me.mkv")
        node_id = await add_node(session, "node-a")
        job = await enqueue_file_job(session, media_id, library_id, plan(), now=T0)
        await session.commit()

        bumped = await bump_job(session, job.id, 7)
        await session.commit()
        assert bumped is not None and bumped.priority == 7

        assert await mark_staged(session, job.id, now=after(0.5))
        await claim(session, job.id, node_id=node_id, lease_ttl_s=100.0, now=after(1))
        assert await bump_job(session, job.id, 1) is None
