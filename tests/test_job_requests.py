from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.actions import ExtractSubtitles
from common.plan import FFmpegPlan
from common.enums import JobStatus
from server.fs.source import capture_source
from server.models import Job, JobAttempt, JobEvent, Library, NodeRow
from server.queue.store import (
    ActiveJobError,
    claim,
    enqueue_file_job,
    enqueue_healthcheck_job,
    finish,
    mark_staged,
    retry_job,
)
from server.workspace import _stage_source, create_attempt_layout
from tests.helpers import request_retry, database_context
from tests.test_staging import add_job

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(kw_only=True)
class JobRequests:
    factory: async_sessionmaker[AsyncSession]
    job_id: int
    attempt_id: int
    node_id: int
    root: Path


@pytest_asyncio.fixture
async def requests(tmp_path: Path) -> AsyncIterator[JobRequests]:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    async with database_context("action-requests") as factory:
        job_id = await add_job(factory, source, status="failed")
        async with factory() as session:
            node = NodeRow(name="node", display_name="node", token_hash="token")
            session.add(node)
            await session.flush()
            job = await session.get(Job, job_id)
            assert job is not None
            library = await session.get(Library, job.library_id)
            assert library is not None
            library.roots = [str(source.parent)]
            job.attempts = 1
            job.node_id = node.id
            job.error = "original failure"
            job.finished_at = T0
            attempt = JobAttempt(job_id=job_id, attempt_number=1, status="failed", result="failed", error=job.error)
            session.add(attempt)
            await session.flush()
            await session.commit()
        root = tmp_path / "workspace"
        _stage_source(root, job_id, source, expected_source=capture_source(source))
        layout = create_attempt_layout(root, job_id, 1, ".mkv")
        (root / layout["output"]).write_bytes(b"original artifact")
        yield JobRequests(factory=factory, job_id=job_id, attempt_id=attempt.id, node_id=node.id, root=root)


async def protect(requests: JobRequests, action_state: str, *, succeeded: bool = False) -> None:
    async with requests.factory() as session:
        job = await session.get(Job, requests.job_id)
        assert job is not None
        if succeeded:
            job.status = "succeeded"
        if action_state == "declared":
            job.post_actions = [ExtractSubtitles(formats=["srt"], languages=["eng"]).model_dump(mode="json")]
        elif action_state == "deferred":
            session.add(
                JobEvent(job_id=job.id, attempt_id=requests.attempt_id, type="stage", payload={"post_actions": [{}]})
            )
        else:
            session.add(JobEvent(job_id=job.id, attempt_id=requests.attempt_id, type="actions_pending"))
            if action_state == "report":
                session.add(JobEvent(job_id=job.id, attempt_id=requests.attempt_id, type="action_report"))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["pending", "report"])
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_retry_preserves_actions(requests: JobRequests, action_state: str, status: str) -> None:
    async with requests.factory() as session:
        original = await session.get(Job, requests.job_id)
        assert original is not None
        original.status = status
        await session.commit()
    await protect(requests, action_state)
    async with requests.factory() as session:
        original = await session.get(Job, requests.job_id)
        assert original is not None
        request_id = await request_retry(session, original.id)
        await session.refresh(original)
        before = {column.name: getattr(original, column.name) for column in Job.__table__.columns}
        with pytest.raises(ActiveJobError):
            await retry_job(session, original.id, request_id=request_id)
        await session.commit()
    async with requests.factory() as session:
        saved = await session.get(Job, requests.job_id)
        assert saved is not None
        assert {column.name: getattr(saved, column.name) for column in Job.__table__.columns} == before
        assert (await session.scalars(select(JobAttempt))).one().id == requests.attempt_id
        session.add(JobEvent(job_id=saved.id, attempt_id=requests.attempt_id, type="actions_finished"))
        await session.commit()
    async with requests.factory() as session:
        retried = await retry_job(session, requests.job_id, request_id=request_id)
        assert retried is not None and retried.status == JobStatus.STAGING
        assert retried.attempts == 1
        assert await mark_staged(session, retried.id)
        assert await claim(session, retried.id, requests.node_id) is not None
        await session.commit()
        saved = await session.get(Job, retried.id)
        assert saved is not None and saved.attempts == 2
    assert (requests.root / f"jobs/{requests.job_id}/attempts/1/output.mkv").read_bytes() == b"original artifact"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["own", "file", "stage"])
@pytest.mark.parametrize("action_state", ["pending", "report"])
async def test_claim_preserves_actions(requests: JobRequests, target: str, action_state: str) -> None:
    async with requests.factory() as session:
        owner = await session.get(Job, requests.job_id)
        assert owner is not None
        if target == "own":
            owner.status = "queued"
            candidate_id = owner.id
        else:
            owner.status = "succeeded"
            candidate = Job(
                media_file_id=owner.media_file_id,
                library_id=owner.library_id,
                kind="transcode",
                status="running" if target == "stage" else "queued",
                attempts=1 if target == "stage" else 0,
                plan=owner.plan,
            )
            session.add(candidate)
            await session.flush()
            candidate_id = candidate.id
            if target == "stage":
                session.add(JobAttempt(job_id=candidate.id, attempt_number=1, status="running"))
        await session.commit()
    async with requests.factory() as stale:
        cached = await stale.get(Job, candidate_id)
        assert cached is not None
        before = {column.name: getattr(cached, column.name) for column in Job.__table__.columns}
        await protect(requests, action_state)
        assert await claim(stale, candidate_id, requests.node_id, stage=target == "stage") is None
        await stale.commit()
    async with requests.factory() as session:
        saved = await session.get(Job, candidate_id)
        assert saved is not None
        assert {column.name: getattr(saved, column.name) for column in Job.__table__.columns} == before
        session.add(JobEvent(job_id=requests.job_id, attempt_id=requests.attempt_id, type="actions_finished"))
        await session.commit()
    async with requests.factory() as session:
        claimed = await claim(session, candidate_id, requests.node_id, stage=target == "stage")
        assert claimed is not None
        assert claimed.attempts == (2 if target == "own" else 1)
        await session.commit()
    assert (requests.root / f"jobs/{requests.job_id}/attempts/1/output.mkv").read_bytes() == b"original artifact"


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["declared", "deferred", "pending", "report"])
async def test_enqueue_preserves_actions(requests: JobRequests, action_state: str) -> None:
    await protect(requests, action_state, succeeded=True)
    async with requests.factory() as session:
        owner = await session.get(Job, requests.job_id)
        assert owner is not None and owner.media_file_id is not None
        with pytest.raises(ActiveJobError):
            await enqueue_file_job(session, owner.media_file_id, owner.library_id, FFmpegPlan())
        await session.commit()
        assert list((await session.scalars(select(Job.id))).all()) == [owner.id]
        session.add(JobEvent(job_id=owner.id, attempt_id=requests.attempt_id, type="actions_finished"))
        await session.commit()
        queued = await enqueue_file_job(
            session,
            owner.media_file_id,
            owner.library_id,
            FFmpegPlan(),
            post_actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
        )
        assert await mark_staged(session, queued.id)
        assert await claim(session, queued.id, requests.node_id) is not None
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["own", "file"])
@pytest.mark.parametrize("action_state", ["pending", "report"])
async def test_retry_rechecks_actions(
    requests: JobRequests, monkeypatch: pytest.MonkeyPatch, target: str, action_state: str
) -> None:
    scalar = AsyncSession.scalar
    inserted = False
    owner_id, attempt_id = requests.job_id, requests.attempt_id
    if target == "file":
        async with requests.factory() as session:
            candidate = await session.get(Job, requests.job_id)
            assert candidate is not None
            owner = Job(media_file_id=candidate.media_file_id, library_id=candidate.library_id, status="succeeded")
            session.add(owner)
            await session.flush()
            owner_attempt = JobAttempt(job_id=owner.id, attempt_number=1, status="succeeded", result="succeeded")
            session.add(owner_attempt)
            await session.flush()
            owner_id, attempt_id = owner.id, owner_attempt.id
            await session.commit()

    async def protect_before_update(session, statement, *args, **kwargs):
        nonlocal inserted
        if not inserted and statement.is_update and statement.table.name == "jobs":
            inserted = True
            # Inject through requests's transaction to exercise the last-write recovery predicate.
            session.add(JobEvent(job_id=owner_id, attempt_id=attempt_id, type="actions_pending"))
            if action_state == "report":
                session.add(JobEvent(job_id=owner_id, attempt_id=attempt_id, type="action_report"))
            await session.flush()
        return await scalar(session, statement, *args, **kwargs)

    async with requests.factory() as session:
        request_id = await request_retry(session, requests.job_id)
        monkeypatch.setattr(AsyncSession, "scalar", protect_before_update)
        assert await asyncio.wait_for(retry_job(session, requests.job_id, request_id=request_id), 3) is None
        await session.commit()
    assert inserted
    async with requests.factory() as session:
        saved = await session.get(Job, requests.job_id)
        assert saved is not None and saved.status == "pending" and saved.attempts == 1
        requested = await session.get(JobEvent, request_id)
        assert requested is not None and requested.payload["previous"]["error"] == "original failure"
        assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "retried")) is None


@pytest.mark.asyncio
async def test_healthcheck_during_actions(requests: JobRequests) -> None:
    await protect(requests, "pending", succeeded=True)
    async with requests.factory() as session:
        owner = await session.get(Job, requests.job_id)
        assert owner is not None and owner.media_file_id is not None
        healthcheck = await enqueue_healthcheck_job(session, owner.media_file_id, owner.library_id)
        assert await claim(session, healthcheck.id, requests.node_id) is not None
        assert await finish(session, healthcheck.id, JobStatus.SUCCEEDED)
        await session.commit()
        request_id = await request_retry(session, healthcheck.id)
        with pytest.raises(ActiveJobError):
            await retry_job(session, healthcheck.id, request_id=request_id)
        session.add(JobEvent(job_id=owner.id, attempt_id=requests.attempt_id, type="actions_finished"))
        await session.commit()
        retried = await retry_job(session, healthcheck.id, request_id=request_id)
        assert retried is not None and retried.status == JobStatus.QUEUED
        assert await claim(session, healthcheck.id, requests.node_id) is not None
        assert await finish(session, healthcheck.id, JobStatus.SUCCEEDED)
        await session.commit()
        assert await enqueue_file_job(session, owner.media_file_id, owner.library_id, FFmpegPlan()) is not None


@pytest.mark.asyncio
async def test_other_media_unblocked(requests: JobRequests) -> None:
    await protect(requests, "pending", succeeded=True)
    source = requests.root.parent / "other.mkv"
    source.write_bytes(b"other source")
    candidate_id = await add_job(requests.factory, source, status="queued")
    async with requests.factory() as session:
        candidate = await session.get(Job, candidate_id)
        assert candidate is not None
        library = await session.get(Library, candidate.library_id)
        assert library is not None
        library.roots = [str(source.parent)]
        await session.commit()
        assert await claim(session, candidate_id, requests.node_id) is not None
        await session.commit()
