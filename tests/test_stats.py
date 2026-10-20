from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.media.probe import ProbeData, ProbeStream
from common.plan.models import AudioTarget, FFmpegPlan, VideoTarget
from common.plan.renderer import render
from common.enums import JobStatus
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.events import EventBus
from server.models import Flow, Job, JobAttempt, JobEvent, Library, MediaFile, NodeRow, ProbeRow
from server.queue.store import (
    codec_distribution,
    enqueue_healthcheck_job,
    jobs_finished_since,
    processing_backlog,
    queue_depth,
)
from server.stats import build_snapshot, overview, savings
from tests.helpers import database_context

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("stats") as factory:
        yield factory


async def add_library(session: AsyncSession, name: str) -> int:
    library = Library(name=name, roots=[f"/srv/{name}"])
    session.add(library)
    await session.flush()
    return library.id


async def add_media(
    session: AsyncSession,
    library_id: int,
    name: str,
    video_codec: str | None,
    *,
    health_status: str = "pending",
    transcode_decided: bool = False,
) -> int:
    media = MediaFile(
        library_id=library_id,
        path=f"/srv/{name}",
        size=1000,
        mtime_ns=1,
        container="mkv",
        video_codec=video_codec,
        medium="movie",
        health_status=health_status,
        transcode_decided=transcode_decided,
        last_scanned_at=T0,
    )
    session.add(media)
    await session.flush()
    return media.id


async def add_job(
    session: AsyncSession,
    library_id: int,
    media_id: int,
    *,
    status: str = "queued",
    kind: str = "transcode",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> int:
    job = Job(
        media_file_id=media_id,
        library_id=library_id,
        kind=kind,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(job)
    await session.flush()
    return job.id


async def add_finished_attempt(
    session: AsyncSession,
    job_id: int,
    *,
    result: str = "succeeded",
    source_size: int | None = None,
    output_size: int | None = None,
    argv: list[str] | None = None,
    attempt_number: int = 1,
) -> JobAttempt:
    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=attempt_number,
        status=result,
        result=result,
        source_size=source_size,
        output_size=output_size,
        argv=argv,
    )
    session.add(attempt)
    return attempt


@pytest_asyncio.fixture
async def estimate_session(db: async_sessionmaker[AsyncSession]) -> AsyncIterator[tuple[AsyncSession, int, int]]:
    async with db() as session:
        library_id = await add_library(session, "estimate")
        await session.execute(update(Library).where(Library.id == library_id).values(healthcheck_enabled=True))
        node = NodeRow(
            name="worker",
            display_name="Worker",
            token_hash="unused",
            state="online",
            workspace_ready=True,
            transcode_limit=3,
        )
        session.add(node)
        for kind, duration in (("transcode", 80), ("transcode", 120), ("healthcheck", 6), ("healthcheck", 14)):
            media_id = await add_media(
                session,
                library_id,
                f"{kind}-history-{duration}.mkv",
                "h264",
                health_status="healthy",
                transcode_decided=True,
            )
            await add_job(
                session,
                library_id,
                media_id,
                kind=kind,
                status="succeeded",
                started_at=T0 - timedelta(seconds=duration),
                finished_at=T0,
            )
        await session.flush()
        yield session, library_id, node.id


async def add_work(
    session: AsyncSession,
    library_id: int,
    *,
    kind: str = "transcode",
    status: str = "queued",
    count: int = 1,
    node_id: int | None = None,
) -> list[Job]:
    jobs = []
    for index in range(count):
        media_id = await add_media(
            session,
            library_id,
            f"{kind}-{status}-{index}.mkv",
            "h264",
            health_status="healthy",
            transcode_decided=True,
        )
        job = Job(media_file_id=media_id, library_id=library_id, kind=kind, status=status, node_id=node_id)
        session.add(job)
        jobs.append(job)
    await session.flush()
    return jobs


@pytest.mark.asyncio
async def test_estimate_whole_queue(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, node_id = estimate_session
    await add_work(session, library_id, count=13)
    for status in ("staging", "claimed", "running", "verifying", "finalizing"):
        await add_work(session, library_id, status=status, node_id=node_id)
    await add_work(session, library_id, kind="healthcheck", count=9)
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 27
    assert estimate["remaining_s"] == 690
    assert datetime.fromisoformat(estimate["estimated_finish_at"]) == T0 + timedelta(seconds=690)


@pytest.mark.asyncio
async def test_estimate_check_capacity(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    await add_work(session, library_id, count=1)
    await add_work(session, library_id, kind="healthcheck", count=40)
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_s"] == 210


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["healthcheck", "mkv_stats"])
async def test_estimate_pooled_capacity(estimate_session: tuple[AsyncSession, int, int], kind: str) -> None:
    session, library_id, node_id = estimate_session
    node = await session.get(NodeRow, node_id)
    assert node is not None
    node.healthcheck_limit = node.mkv_stats_limit = 3
    node.capabilities = {"mkvpropedit": True}
    if kind == "mkv_stats":
        media = await add_media(
            session, library_id, "stats-history.mkv", "h264", health_status="healthy", transcode_decided=True
        )
        await add_job(
            session,
            library_id,
            media,
            kind=kind,
            status="succeeded",
            started_at=T0 - timedelta(seconds=10),
            finished_at=T0,
        )
    await add_work(session, library_id, kind=kind, count=5)
    events = EventBus()
    events.healthcheck_slots = events.mkv_stats_slots = 2
    events.healthchecks_on_nodes = events.mkv_stats_on_nodes = True
    assert (await build_snapshot(session, events, T0))["summary"]["backlog"]["remaining_s"] == 10

    events.healthchecks_on_nodes = events.mkv_stats_on_nodes = False
    assert (await build_snapshot(session, events, T0))["summary"]["backlog"]["remaining_s"] == 30
    events.healthcheck_slots = events.mkv_stats_slots = 0
    unavailable = (await build_snapshot(session, events, T0))["summary"]["backlog"]
    assert unavailable["remaining_s"] is None
    assert unavailable["reason"] == f"No available {kind} capacity."

    events.healthchecks_on_nodes = events.mkv_stats_on_nodes = True
    assert (await build_snapshot(session, events, T0))["summary"]["backlog"]["remaining_s"] == 20
    node.healthcheck_limit = node.mkv_stats_limit = 0
    await session.flush()
    assert (await build_snapshot(session, events, T0))["summary"]["backlog"]["remaining_s"] is None
    if kind == "mkv_stats":
        node.mkv_stats_limit = 3
        node.capabilities = {"mkvpropedit": False}
        await session.flush()
        assert (await build_snapshot(session, events, T0))["summary"]["backlog"]["remaining_s"] is None


@pytest.mark.asyncio
async def test_estimate_partial_batch(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    await add_work(session, library_id, count=10)
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_s"] == 450
    assert datetime.fromisoformat(estimate["estimated_finish_at"]) == T0 + timedelta(seconds=450)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transcode", "healthcheck"])
async def test_estimate_missing_history(estimate_session: tuple[AsyncSession, int, int], kind: str) -> None:
    session, library_id, _ = estimate_session
    media_id = await add_media(
        session, library_id, "invalid-history.mkv", "h264", health_status="healthy", transcode_decided=True
    )
    await session.execute(update(Job).where(Job.kind == kind).values(finished_at=T0 - timedelta(hours=25)))
    for duration in (0, -10):
        await add_job(
            session,
            library_id,
            media_id,
            kind=kind,
            status="succeeded",
            started_at=T0 - timedelta(seconds=duration),
            finished_at=T0,
        )
    for work_kind in ("transcode", "healthcheck"):
        await add_work(session, library_id, kind=work_kind)
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 2
    assert estimate["remaining_s"] is None
    assert estimate["estimated_finish_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,paused,ready", [("offline", False, True), ("online", True, True), ("online", False, False)]
)
@pytest.mark.parametrize("kind", ["transcode", "healthcheck", "mkv_stats"])
async def test_estimate_unavailable_nodes(
    estimate_session: tuple[AsyncSession, int, int], state: str, paused: bool, ready: bool, kind: str
) -> None:
    session, library_id, node_id = estimate_session
    node = await session.get(NodeRow, node_id)
    assert node is not None
    node.state, node.paused, node.workspace_ready = state, paused, ready
    node.capabilities = {"mkvpropedit": True}
    await add_work(session, library_id, kind=kind)
    estimate = await processing_backlog(
        session,
        healthcheck_slots=2 if kind == "transcode" else 0,
        healthchecks_on_nodes=True,
        mkv_stats_on_nodes=True,
        now=T0,
    )
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["history", "capacity"])
async def test_estimate_artifact_checks(estimate_session: tuple[AsyncSession, int, int], blocker: str) -> None:
    session, library_id, _ = estimate_session
    await add_work(session, library_id)
    if blocker == "history":
        await session.execute(update(Job).where(Job.kind == "healthcheck").values(finished_at=T0 - timedelta(hours=25)))
    estimate = await processing_backlog(session, healthcheck_slots=0 if blocker == "capacity" else 2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
async def test_estimate_suspended_artifacts(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, node_id = estimate_session
    await add_work(session, library_id, status="running", node_id=node_id)
    library = await session.get(Library, library_id)
    assert library is not None
    library.suspended = True
    await session.flush()
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("kind", ["transcode", "healthcheck", "mkv_stats"])
async def test_estimate_active_nodes(estimate_session: tuple[AsyncSession, int, int], paused: bool, kind: str) -> None:
    session, library_id, node_id = estimate_session
    node = await session.get(NodeRow, node_id)
    assert node is not None
    node.paused = paused
    jobs = await add_work(session, library_id, kind=kind, status="running", node_id=node_id)
    jobs[0].started_at = T0 - timedelta(seconds=90)
    estimate = await processing_backlog(session, healthcheck_slots=2, mkv_stats_slots=2, mkv_stats_mean_s=10, now=T0)
    assert estimate["remaining_s"] == (110 if kind == "transcode" else 10)
    node.state = "offline"
    await session.flush()
    estimate = await processing_backlog(session, healthcheck_slots=2, mkv_stats_slots=2, mkv_stats_mean_s=10, now=T0)
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["suspended", "schedule", "warning"])
async def test_estimate_blocked_work(estimate_session: tuple[AsyncSession, int, int], blocker: str) -> None:
    session, library_id, _ = estimate_session
    library = await session.get(Library, library_id)
    assert library is not None
    jobs = await add_work(session, library_id, status="staging")
    await add_work(session, library_id, kind="healthcheck")
    if blocker == "suspended":
        library.suspended = True
    elif blocker == "schedule":
        library.schedule = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
    else:
        jobs[0].warning = "Insufficient workspace space."
    await session.flush()
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
async def test_estimate_schedule_timezone(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    library = await session.get(Library, library_id)
    assert library is not None
    library.schedule = {"type": "grid", "cells": [[hour == 2 for hour in range(24)] for _ in range(7)]}
    jobs = await add_work(session, library_id)
    jobs[0].warning = "no node can claim this job: all nodes are at max transcodes"
    await session.flush()
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_s"] is None
    estimate = await processing_backlog(session, healthcheck_slots=2, timezone=ZoneInfo("Pacific/Honolulu"), now=T0)
    assert estimate["remaining_s"] == 110
    jobs[0].status = "succeeded"
    await add_work(session, library_id, kind="healthcheck")
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_s"] == 10


@pytest.mark.asyncio
async def test_estimate_idle_queue(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, _, _ = estimate_session
    assert await processing_backlog(session, healthcheck_slots=0, now=T0) == {
        "remaining_files": 0,
        "remaining_s": 0,
        "estimated_finish_at": None,
        "reason": None,
    }


@pytest.mark.asyncio
async def test_backlog_held_files(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    held = (await add_work(session, library_id, status="hold"))[0]
    # A file can match discovery and an active job. It still counts only once.
    await session.execute(update(MediaFile).where(MediaFile.id == held.media_file_id).values(health_status="pending"))
    await add_work(session, library_id)
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 2
    assert estimate["remaining_s"] is None
    assert estimate["estimated_finish_at"] is None


@pytest.mark.asyncio
async def test_estimate_pending_probes(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    pending = [await add_media(session, library_id, f"probe-{index}.mkv", "h264") for index in range(5)]
    await add_work(session, library_id, kind="healthcheck", count=8)
    estimate = await processing_backlog(session, healthcheck_slots=2, probe_slots=2, probe_mean_s=4, now=T0)
    assert estimate["remaining_files"] == 13
    assert estimate["remaining_s"] == 82
    assert estimate["reason"] is None
    assert datetime.fromisoformat(estimate["estimated_finish_at"]) == T0 + timedelta(seconds=82)

    probe = ProbeRow(media_file_id=pending[0], raw_json="{}")
    session.add(probe)
    await session.flush()
    await session.execute(update(MediaFile).where(MediaFile.id == pending[0]).values(probe_id=probe.id))
    await add_job(session, library_id, pending[0], kind="healthcheck")
    estimate = await processing_backlog(session, healthcheck_slots=2, probe_slots=2, probe_mean_s=4, now=T0)
    assert estimate["remaining_files"] == 13
    assert estimate["remaining_s"] == 78


@pytest.mark.asyncio
@pytest.mark.parametrize("scanner_paused", [False, True])
async def test_estimate_probe_overlap(estimate_session: tuple[AsyncSession, int, int], scanner_paused: bool) -> None:
    session, library_id, _ = estimate_session
    media_id = await add_media(session, library_id, "checking.mkv", "h264")
    await add_job(session, library_id, media_id, kind="healthcheck")
    estimate = await processing_backlog(
        session, healthcheck_slots=2, probe_slots=2, probe_mean_s=4, scanner_paused=scanner_paused, now=T0
    )
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] == (None if scanner_paused else 14)
    if scanner_paused:
        assert estimate["reason"]


@pytest.mark.asyncio
async def test_estimate_probe_only(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    await session.execute(update(Library).where(Library.id == library_id).values(healthcheck_enabled=False))
    await add_media(session, library_id, "fresh.mkv", "h264")
    await session.execute(update(Job).where(Job.kind == "healthcheck").values(finished_at=T0 - timedelta(hours=25)))
    estimate = await processing_backlog(session, healthcheck_slots=0, probe_slots=2, probe_mean_s=4, now=T0)
    assert estimate["remaining_files"] == 1 and estimate["remaining_s"] == 4
    assert estimate["reason"] is None


@pytest.mark.asyncio
async def test_estimate_no_checks(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    await session.execute(update(Library).where(Library.id == library_id).values(healthcheck_enabled=False))
    await add_work(session, library_id, count=3)
    await session.execute(update(Job).where(Job.kind == "healthcheck").values(finished_at=T0 - timedelta(hours=25)))
    estimate = await processing_backlog(session, healthcheck_slots=0, now=T0)
    assert estimate["remaining_files"] == 3 and estimate["remaining_s"] == 100
    assert estimate["reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["probe_capacity", "probe_history", "check_capacity", "check_history"])
async def test_estimate_probe_blockers(estimate_session: tuple[AsyncSession, int, int], blocker: str) -> None:
    session, library_id, _ = estimate_session
    await add_media(session, library_id, "new.mkv", "h264")
    if blocker == "check_history":
        await session.execute(update(Job).where(Job.kind == "healthcheck").values(finished_at=T0 - timedelta(hours=25)))
    estimate = await processing_backlog(
        session,
        healthcheck_slots=0 if blocker == "check_capacity" else 2,
        probe_slots=0 if blocker == "probe_capacity" else 2,
        probe_mean_s=None if blocker == "probe_history" else 4,
        now=T0,
    )
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None
    assert estimate["estimated_finish_at"] is None


@pytest.mark.asyncio
async def test_check_retires_refresh(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    publication = (await add_work(session, library_id))[0]
    publication.status = "succeeded"
    publication.attempts = 1
    attempt = JobAttempt(job_id=publication.id, attempt_number=1, status="succeeded", result="succeeded")
    session.add(attempt)
    await session.flush()
    marker = JobEvent(job_id=publication.id, attempt_id=attempt.id, type="artifact_check_pending")
    session.add(marker)
    await session.commit()
    media_id, marker_id = publication.media_file_id, marker.id

    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 1
    await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
    await session.refresh(marker)
    assert marker.type == "artifact_check_finished"
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 1
    await session.rollback()
    marker = await session.get(JobEvent, marker_id)
    assert marker is not None and marker.type == "artifact_check_pending"

    check = await enqueue_healthcheck_job(session, media_id, library_id, now=T0)
    await session.commit()
    await session.refresh(marker)
    assert marker.type == "artifact_check_finished"
    check.status = "succeeded"
    await session.flush()
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 0


@pytest.mark.asyncio
async def test_backlog_stage_transitions(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, node_id = estimate_session
    library = await session.get(Library, library_id)
    assert library is not None
    flow = Flow(name="automatic", definition="{}")
    session.add(flow)
    await session.flush()
    library.flow_id = flow.id
    media_id = await add_media(session, library_id, "new.mkv", "h264")
    media = await session.get(MediaFile, media_id)
    assert media is not None
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None

    probe = ProbeRow(media_file_id=media_id, raw_json="{}")
    session.add(probe)
    await session.flush()
    media.probe_id = probe.id
    check_id = await add_job(session, library_id, media_id, kind="healthcheck")
    check = await session.get(Job, check_id)
    assert check is not None
    for status in ("queued", "claimed", "running"):
        check.status = status
        await session.flush()
        estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
        assert estimate["remaining_files"] == 1
        assert estimate["remaining_s"] is None

    check.status = "succeeded"
    media.health_status = "healthy"
    media.health_source = {"path": media.path}
    await session.flush()
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None

    media.transcode_decided = True
    transcode_id = await add_job(session, library_id, media_id)
    transcode = await session.get(Job, transcode_id)
    assert transcode is not None
    for status in ("staging", "queued", "claimed", "running", "verifying", "finalizing"):
        transcode.status = status
        transcode.node_id = node_id if status not in {"staging", "queued"} else None
        await session.flush()
        estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
        assert estimate["remaining_files"] == 1
        assert estimate["remaining_s"] == 110

    transcode.status = "succeeded"
    media.health_status = "pending"
    await session.flush()
    artifact_id = await add_job(session, library_id, media_id, kind="healthcheck")
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] == 10
    artifact = await session.get(Job, artifact_id)
    assert artifact is not None
    artifact.status = "succeeded"
    media.health_status = "healthy"
    await session.flush()
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 0
    assert estimate["remaining_s"] == 0

    await add_media(session, library_id, "another.mkv", "h264")
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
async def test_backlog_terminal_outcomes(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    for status in ("cancelled", "failed"):
        media_id = await add_media(session, library_id, f"{status}.mkv", "h264")
        probe = ProbeRow(media_file_id=media_id, raw_json="{}")
        session.add(probe)
        await session.flush()
        await session.execute(
            update(MediaFile).where(MediaFile.id == media_id).values(probe_id=probe.id, transcode_decided=True)
        )
        await add_job(session, library_id, media_id, kind="healthcheck", status=status)
    for health in ("probe_failed", "corrupt", "healthy"):
        await add_media(session, library_id, f"{health}.mkv", "h264", health_status=health)
    no_flow_id = await add_library(session, "no-flow")
    healthy_id = await add_media(session, no_flow_id, "no-flow.mkv", "h264", health_status="healthy")
    await session.execute(update(MediaFile).where(MediaFile.id == healthy_id).values(health_source={"path": "no-flow"}))
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 0

    library = await session.get(Library, library_id)
    assert library is not None
    flow = Flow(name="automatic", definition="{}")
    session.add(flow)
    await session.flush()
    library.flow_id = flow.id
    # A healthy file without source details is not a flow candidate.
    await session.flush()
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 0


@pytest.mark.asyncio
async def test_backlog_suspended_candidates(estimate_session: tuple[AsyncSession, int, int]) -> None:
    session, library_id, _ = estimate_session
    library = await session.get(Library, library_id)
    assert library is not None
    flow = Flow(name="automatic", definition="{}")
    session.add(flow)
    await session.flush()
    library.flow_id = flow.id
    await add_media(session, library_id, "pending.mkv", "h264")
    healthy_id = await add_media(session, library_id, "healthy.mkv", "h264", health_status="healthy")
    probe = ProbeRow(media_file_id=healthy_id, raw_json="{}")
    session.add(probe)
    await session.flush()
    healthy = await session.get(MediaFile, healthy_id)
    assert healthy is not None
    healthy.probe_id = probe.id
    healthy.health_source = {"path": healthy.path, "size": healthy.size}
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 2
    library.suspended = True
    await session.flush()
    assert (await processing_backlog(session, healthcheck_slots=2, now=T0))["remaining_files"] == 0
    await add_work(session, library_id)
    estimate = await processing_backlog(session, healthcheck_slots=2, now=T0)
    assert estimate["remaining_files"] == 1
    assert estimate["remaining_s"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scanner_paused, pending_probe", [(False, False), (False, True), (True, True)])
async def test_stats_api_event(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scanner_paused: bool,
    pending_probe: bool,
) -> None:
    monkeypatch.setattr("server.queue.store.utcnow", lambda: T0)
    async with db() as session:
        library_id = await add_library(session, "estimate")
        library = await session.get(Library, library_id)
        assert library is not None
        library.schedule = {"type": "grid", "cells": [[hour == 2 for hour in range(24)] for _ in range(7)]}
        session.add(
            NodeRow(name="worker", display_name="Worker", token_hash="unused", state="online", workspace_ready=True)
        )
        for kind in ("transcode", "healthcheck"):
            media_id = await add_media(
                session, library_id, f"{kind}-history.mkv", "h264", health_status="healthy", transcode_decided=True
            )
            await add_job(
                session,
                library_id,
                media_id,
                kind=kind,
                status="succeeded",
                started_at=T0 - timedelta(seconds=10),
                finished_at=T0,
            )
        await add_work(session, library_id)
        await add_work(session, library_id, kind="healthcheck", count=8)
        if pending_probe:
            await add_media(session, library_id, "discovered.mkv", "h264")
        await session.commit()
    app = create_app(db, config=ServerConfig(tz="Pacific/Honolulu"), cache_root=tmp_path)
    await app.state.settings.update({"max_healthchecks": 2})
    app.state.stats.clock = lambda: T0
    app.state.cache.clock = lambda: T0
    bus = app.state.events
    bus.probe_samples.append((bus.clock(), 4))
    app.state.scanner.set_paused(paused=scanner_paused)
    sub = bus.subscribe()
    try:
        await app.state.stats.refresh()
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/api/stats")
        assert response.status_code == 200
        event = await asyncio.wait_for(sub.next(), timeout=1)
        assert event is not None and event.type == "stats"
        assert event.payload == response.json()
        blocked = scanner_paused
        remaining_s = 44 if pending_probe else 40
        backlog = event.payload["backlog"]
        assert backlog["remaining_files"] == (10 if pending_probe else 9)
        assert backlog["remaining_s"] == (None if blocked else remaining_s)
        assert backlog["estimated_finish_at"] == (
            None if blocked else (T0 + timedelta(seconds=remaining_s)).isoformat().replace("+00:00", "Z")
        )
    finally:
        bus.unsubscribe(sub)
        await bus.aclose()


@pytest.mark.asyncio
async def test_scanner_control_stats(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("server.queue.store.utcnow", lambda: T0)
    async with db() as session:
        library_id = await add_library(session, "scanner")
        await add_media(session, library_id, "waiting.mkv", "h264")
        await session.commit()
    app = create_app(db, cache_root=tmp_path)
    app.state.stats.clock = lambda: T0
    app.state.cache.clock = lambda: T0
    bus = app.state.events
    bus.min_interval_s = 0
    bus.probe_samples.append((bus.clock(), 4))
    sub = bus.subscribe()
    try:
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await app.state.stats.refresh()
            await asyncio.wait_for(sub.next(), 1)
            baseline = (await client.get("/api/stats")).json()["backlog"]
            assert baseline["remaining_s"] == 4
            cached = baseline
            for action in ("pause", "resume"):
                response = await client.post(f"/api/libraries/scanner/{action}")
                assert response.status_code == 200
                assert (await client.get("/api/stats")).json()["backlog"] == cached
                await app.state.stats.refresh()
                async with asyncio.timeout(2):
                    while (event := await sub.next()) is not None and event.type != "stats":
                        pass
                assert event is not None and event.type == "stats"
                current = (await client.get("/api/stats")).json()
                assert event.payload == current
                backlog = event.payload["backlog"]
                assert backlog["remaining_files"] == 1
                if action == "pause":
                    assert backlog["remaining_s"] is None and backlog["estimated_finish_at"] is None
                else:
                    assert backlog == baseline
                cached = backlog
    finally:
        bus.unsubscribe(sub)
        await bus.aclose()


@pytest.mark.asyncio
async def test_queue_depth_by(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        lib1 = await add_library(session, "one")
        lib2 = await add_library(session, "two")
        m1 = await add_media(session, lib1, "a.mkv", "h264")
        m2 = await add_media(session, lib2, "b.mkv", "h264")
        m3 = await add_media(session, lib1, "c.mkv", "h264")
        await add_job(session, lib1, m1, status="queued")
        await add_job(session, lib1, m3, status="queued")
        await add_job(session, lib2, m2, status="claimed")
        await add_job(session, lib2, m2, status="succeeded")
        await session.commit()

        depth = await queue_depth(session)

    assert depth["by_status"]["claimed"] == 1
    assert depth["by_status"]["queued"] == 2
    assert depth["by_library"][str(lib1)] == 2
    assert depth["by_library"][str(lib2)] == 1


@pytest.mark.asyncio
async def test_finished_since_counts(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        lib = await add_library(session, "one")
        m1 = await add_media(session, lib, "a.mkv", "h264")
        m2 = await add_media(session, lib, "b.mkv", "h264")
        m3 = await add_media(session, lib, "c.mkv", "h264")
        await add_job(session, lib, m1, status="succeeded", started_at=T0, finished_at=T0 + timedelta(seconds=10))
        await add_job(session, lib, m2, status="succeeded", started_at=T0, finished_at=T0 + timedelta(seconds=30))
        await add_job(
            session,
            lib,
            m3,
            status="succeeded",
            started_at=T0 - timedelta(hours=48),
            finished_at=T0 - timedelta(hours=48) + timedelta(seconds=5),
        )
        await add_job(session, lib, m3, status="succeeded", finished_at=T0 + timedelta(seconds=60))
        await add_job(session, lib, m3, status="failed", started_at=T0, finished_at=T0 + timedelta(seconds=99))
        await session.commit()

        summary = await jobs_finished_since(session, T0 - timedelta(hours=24))

    assert summary["count"] == 2
    assert summary["avg_duration_s"] == 20.0


@pytest.mark.asyncio
async def test_space_saved_per(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        lib1 = await add_library(session, "one")
        lib2 = await add_library(session, "two")
        m1 = await add_media(session, lib1, "a.mkv", "h264")
        m2 = await add_media(session, lib2, "b.mkv", "h264")
        m3 = await add_media(session, lib1, "c.mkv", "h264")
        j1 = await add_job(session, lib1, m1, status="succeeded")
        j2 = await add_job(session, lib2, m2, status="succeeded")
        j3 = await add_job(session, lib1, m3, status="failed")
        j4 = await add_job(session, lib1, m3, status="succeeded")
        await add_finished_attempt(session, j1, source_size=1000, output_size=400)
        await add_finished_attempt(session, j2, source_size=2000, output_size=1700)
        await add_finished_attempt(session, j3, result="failed", source_size=5000, output_size=100)
        await add_finished_attempt(session, j4, source_size=999)
        await session.commit()

        saved = (await build_snapshot(session, EventBus(), T0))["summary"]["space_saved_by_library"]

    assert saved == {str(lib1): 600, str(lib2): 300}


@pytest.mark.asyncio
async def test_space_saved_attempts(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        lib = await add_library(session, "one")
        media = await add_media(session, lib, "a.mkv", "h264")
        job_id = await add_job(session, lib, media, status="succeeded")
        await add_finished_attempt(session, job_id, source_size=1000, output_size=700)
        session.add(
            JobAttempt(
                job_id=job_id,
                attempt_number=2,
                status="succeeded",
                result="succeeded",
                source_size=1000,
                output_size=600,
            )
        )
        await session.commit()

        saved = (await build_snapshot(session, EventBus(), T0))["summary"]["space_saved_by_library"]

    assert saved == {str(lib): 400}


@pytest.mark.asyncio
async def test_processing_codec_records(db: async_sessionmaker[AsyncSession]) -> None:
    probe = ProbeData(
        file_path="source.mkv",
        container="mkv",
        duration_s=60,
        size_bytes=1000,
        streams=[
            ProbeStream(index=0, codec_type="video", codec="h264"),
            ProbeStream(index=1, codec_type="audio", codec="aac"),
            ProbeStream(index=2, codec_type="subtitle", codec="subrip"),
        ],
    )
    plans = [
        FFmpegPlan(video=VideoTarget(encoder="libx265")),
        FFmpegPlan(audio_encode=[AudioTarget(codec="ac3", bitrate="192k")]),
        FFmpegPlan(container="mp4"),
        FFmpegPlan(audio_encode=[AudioTarget(codec="ac3", bitrate="192k", when_codec_in=["dts"])]),
    ]
    async with db() as session:
        library = await add_library(session, "codecs")
        for index, plan in enumerate(plans):
            media = await add_media(session, library, f"codec-{index}.mkv", "h264")
            job = await add_job(session, library, media, status="succeeded", finished_at=T0)
            await session.execute(update(Job).where(Job.id == job).values(plan=plan.model_dump(mode="json")))
            await add_finished_attempt(
                session,
                job,
                source_size=1000,
                output_size=900,
                argv=["ffmpeg", *render(plan, probe, "source.mkv", "output.mkv").argv],
            )
        await session.commit()
        result = overview(await build_snapshot(session, EventBus(), T0), 1)

    assert result["jobs"]["by_kind"]["transcode"]["succeeded"] == 3
    assert result["jobs"]["by_kind"]["remux"]["succeeded"] == 1
    assert result["savings"]["by_kind"]["transcode"]["totals"]["saved_bytes"] == 300
    assert result["savings"]["by_kind"]["remux"]["totals"]["saved_bytes"] == 100


@pytest.mark.asyncio
async def test_processing_stage_attempts(db: async_sessionmaker[AsyncSession]) -> None:
    encode = ["-i", "source.mkv", "-map", "0", "-c", "copy", "-c:v:0", "libx265", "output.mkv"]
    remux = ["-i", "source.mkv", "-map", "0", "-c", "copy", "output.mkv"]
    async with db() as session:
        library = await add_library(session, "stages")
        media = await add_media(session, library, "stages.mkv", "h264")
        staged = await add_job(session, library, media, status="succeeded", finished_at=T0)
        retry = await add_job(session, library, media, status="succeeded", finished_at=T0)
        failed = await add_job(session, library, media, status="failed", finished_at=T0)
        incomplete = await add_job(session, library, media, status="succeeded", finished_at=T0)
        await add_job(session, library, media, status="skipped", finished_at=T0)
        attempt = await add_finished_attempt(session, staged, source_size=1000, output_size=600, argv=remux)
        prior = await add_finished_attempt(session, retry, result="failed", argv=encode)
        current = await add_finished_attempt(
            session, retry, source_size=1000, output_size=1100, argv=remux, attempt_number=2
        )
        await add_finished_attempt(session, failed, source_size=1000, output_size=200, argv=encode)
        await add_finished_attempt(session, failed, result="failed", argv=remux, attempt_number=2)
        partial = await add_finished_attempt(session, incomplete, source_size=1000, output_size=950, argv=remux)
        await session.flush()
        session.add_all(
            [
                JobEvent(
                    job_id=staged, attempt_id=attempt.id, type="stage", payload={"argv": argv, "result": "succeeded"}
                )
                for argv in (encode, remux)
            ]
            + [
                JobEvent(job_id=retry, attempt_id=prior.id, type="stage", payload={"argv": encode, "result": "failed"}),
                # An offered but unexecuted encode is not execution log.
                JobEvent(job_id=retry, attempt_id=current.id, type="stage", payload={"argv": encode}),
                # Missing records for an executed stage cannot establish a pure remux.
                JobEvent(job_id=incomplete, attempt_id=partial.id, type="stage", payload={"result": "succeeded"}),
                JobEvent(
                    job_id=incomplete,
                    attempt_id=partial.id,
                    type="stage",
                    payload={"argv": remux, "result": "succeeded"},
                ),
            ]
        )
        await session.execute(update(Job).where(Job.id == staged).values(plan={"container": "mkv"}))
        await session.execute(update(Job).where(Job.id == retry).values(plan={"video": {"encoder": "libx265"}}))
        await session.commit()
        result = overview(await build_snapshot(session, EventBus(), T0), 1)

    assert result["jobs"]["by_kind"]["transcode"]["succeeded"] == 1
    assert result["jobs"]["by_kind"]["remux"]["succeeded"] == 1
    assert result["jobs"]["by_kind"]["remux"]["failed"] == 1
    assert result["jobs"]["by_kind"]["unclassified"]["succeeded"] == 1
    assert result["jobs"]["by_kind"]["not_run"]["skipped"] == 1
    assert result["jobs"]["skipped"] == 1 and result["jobs"]["completed"] == 5
    assert result["savings"]["totals"]["saved_bytes"] == 350
    assert result["savings"]["by_kind"]["transcode"]["totals"]["saved_bytes"] == 400
    assert result["savings"]["by_kind"]["remux"]["totals"]["saved_bytes"] == -100
    assert result["savings"]["by_kind"]["unclassified"]["totals"]["saved_bytes"] == 50
    assert result["libraries"][0]["successful_transcodes"] == 1
    assert result["libraries"][0]["successful_remuxes"] == 1


@pytest.mark.asyncio
async def test_savings_uses_utc(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        lib = await add_library(session, "one")
        media = await add_media(session, lib, "a.mkv", "h264")
        for finished_at in (
            datetime(2026, 1, 1, 23, 59, tzinfo=UTC),
            datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
        ):
            job_id = await add_job(session, lib, media, status="succeeded", finished_at=finished_at)
            await add_finished_attempt(session, job_id, source_size=1000, output_size=900)
        await session.commit()

        history = savings(await build_snapshot(session, EventBus(), datetime(2026, 1, 2, tzinfo=UTC)), 2)

    assert history["series"] == [
        {"date": "2026-01-01", "files": 1, "saved_bytes": 100},
        {"date": "2026-01-02", "files": 1, "saved_bytes": 100},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timezone, now, midnight, today",
    [
        ("Asia/Kuwait", "2026-01-01T20:30:00Z", "2025-12-31T21:00:00Z", "2026-01-01"),
        ("Asia/Kuwait", "2026-01-01T23:30:00Z", "2026-01-01T21:00:00Z", "2026-01-02"),
    ],
)
async def test_calendar_local_midnight(db, timezone, now, midnight, today):
    events = EventBus()
    events.timezone = ZoneInfo(timezone)
    boundary = datetime.fromisoformat(midnight)
    async with db() as session:
        lib = await add_library(session, "one")
        media = await add_media(session, lib, "a.mkv", "h264")
        for finished in (boundary - timedelta(microseconds=1), boundary):
            job = await add_job(session, lib, media, status="succeeded", finished_at=finished)
            await add_finished_attempt(session, job, source_size=1000, output_size=900)
        await session.commit()
        snapshot = await build_snapshot(session, events, datetime.fromisoformat(now))

    assert snapshot["date"] == today
    assert savings(snapshot, 1)["series"] == [{"date": today, "files": 1, "saved_bytes": 100}]
    assert overview(snapshot, 1)["jobs"]["succeeded"] == 1
    assert overview(snapshot, 2)["jobs"]["succeeded"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first, now, finished",
    [
        (
            "2026-03-28",
            "2026-03-30T12:00:00Z",
            [
                "2026-03-28T22:59:59Z",
                "2026-03-28T23:00:00Z",
                "2026-03-29T00:59:59Z",
                "2026-03-29T01:00:00Z",
                "2026-03-29T21:59:59Z",
                "2026-03-29T22:00:00Z",
            ],
        ),
        (
            "2026-10-24",
            "2026-10-26T12:00:00Z",
            [
                "2026-10-24T21:59:59Z",
                "2026-10-24T22:00:00Z",
                "2026-10-25T00:30:00Z",
                "2026-10-25T01:30:00Z",
                "2026-10-25T22:59:59Z",
                "2026-10-25T23:00:00Z",
            ],
        ),
    ],
)
async def test_calendar_dst_buckets(db, first, now, finished):
    events = EventBus()
    events.timezone = ZoneInfo("Europe/Berlin")
    async with db() as session:
        lib = await add_library(session, "one")
        media = await add_media(session, lib, "a.mkv", "h264")
        for timestamp in finished:
            job = await add_job(session, lib, media, status="succeeded", finished_at=datetime.fromisoformat(timestamp))
            await add_finished_attempt(session, job, source_size=1000, output_size=900)
        await session.commit()
        snapshot = await build_snapshot(session, events, datetime.fromisoformat(now))

    history = savings(snapshot, 3)
    assert history["series"][0] == {"date": first, "files": 1, "saved_bytes": 100}
    assert [row["files"] for row in history["series"]] == [1, 4, 1]
    assert [row["saved_bytes"] for row in history["series"]] == [100, 400, 100]
    assert overview(snapshot, 1)["jobs"]["succeeded"] == 1
    assert overview(snapshot, 2)["jobs"]["succeeded"] == 5
    assert overview(snapshot, 3)["jobs"]["succeeded"] == 6


@pytest.mark.asyncio
async def test_acquisition_calendar_scope(db: async_sessionmaker[AsyncSession]) -> None:
    events = EventBus()
    events.timezone = ZoneInfo("Asia/Kuwait")
    now = datetime(2026, 1, 2, 1, tzinfo=UTC)
    async with db() as session:
        one = await add_library(session, "one")
        two = await add_library(session, "two")
        empty = await add_library(session, "empty")
        media_ids = {}
        for name, library, created, size in (
            ("previous", one, "2026-01-01T20:59:59.999999Z", 100),
            ("midnight", one, "2026-01-01T21:00:00Z", 200),
            ("today", one, "2026-01-02T00:00:00Z", 300),
            ("other", two, "2026-01-01T22:00:00Z", 400),
            ("expired", one, "2025-01-02T20:59:59.999999Z", 500),
            ("oldest", one, "2025-01-02T21:00:00Z", 600),
            ("future", one, "2026-01-02T21:00:00Z", 700),
        ):
            media = await add_media(session, library, f"{name}.mkv", "h264")
            media_ids[name] = media
            await session.execute(
                update(MediaFile)
                .where(MediaFile.id == media)
                .values(created_at=datetime.fromisoformat(created), first_seen=now, size=size)
            )
        later = await add_job(session, one, media_ids["midnight"], status="succeeded", finished_at=now)
        later_attempt = await add_finished_attempt(session, later, source_size=1200, output_size=200)
        later_attempt.input_probe = {"size_bytes": 1200}
        later_attempt.created_at = now - timedelta(hours=1)
        earliest = await add_job(session, one, media_ids["midnight"], status="succeeded", finished_at=now)
        retry = await add_finished_attempt(session, earliest, source_size=1500, output_size=200, attempt_number=3)
        retry.input_probe = {"size_bytes": 1500}
        retry.created_at = now - timedelta(hours=2)
        original = await add_finished_attempt(
            session, earliest, result="failed", source_size=1800, output_size=1500, attempt_number=2
        )
        original.input_probe = {"size_bytes": 2000}
        original.created_at = retry.created_at
        unrecorded = await add_finished_attempt(session, earliest, result="failed")
        unrecorded.created_at = now - timedelta(hours=3)
        source_only = await add_job(session, one, media_ids["today"], status="succeeded", finished_at=now)
        await add_finished_attempt(session, source_only, source_size=3000, output_size=300)
        running = await add_job(session, one, media_ids["previous"], status="running")
        session.add(JobAttempt(job_id=running, attempt_number=1, status="running", input_probe={"size_bytes": 800}))
        metadata = await add_job(session, two, media_ids["other"], kind="probe", status="succeeded", finished_at=now)
        metadata_attempt = await add_finished_attempt(session, metadata, source_size=9000)
        metadata_attempt.input_probe = {"size_bytes": 9000}
        no_snapshot = await add_job(session, two, media_ids["other"], status="failed", finished_at=now)
        await add_finished_attempt(session, no_snapshot, result="failed")
        await session.commit()
        snapshot = await build_snapshot(session, events, now)

        assert overview(snapshot, 3)["acquisition"] == {
            "series": [
                {"date": "2025-12-31", "files": 0, "bytes": 0, "current_size_files": 0},
                {"date": "2026-01-01", "files": 1, "bytes": 800, "current_size_files": 0},
                {"date": "2026-01-02", "files": 3, "bytes": 5400, "current_size_files": 1},
            ]
        }
        assert overview(snapshot, 1, one)["acquisition"]["series"] == [
            {"date": "2026-01-02", "files": 2, "bytes": 5000, "current_size_files": 0}
        ]
        assert overview(snapshot, 1, two)["acquisition"]["series"] == [
            {"date": "2026-01-02", "files": 1, "bytes": 400, "current_size_files": 1}
        ]
        assert overview(snapshot, 1, empty)["acquisition"]["series"] == [
            {"date": "2026-01-02", "files": 0, "bytes": 0, "current_size_files": 0}
        ]
        year = overview(snapshot, 365)["acquisition"]["series"]
        assert year[0] == {"date": "2025-01-03", "files": 1, "bytes": 600, "current_size_files": 1}
        assert sum(row["files"] for row in year) == 5
        assert sum(row["bytes"] for row in year) == 6800
        assert sum(row["current_size_files"] for row in year) == 2
        assert overview(snapshot, 1)["inventory"]["bytes"] == 2800

        await session.execute(delete(JobAttempt).where(JobAttempt.job_id.in_((earliest, later))))
        await session.execute(delete(Job).where(Job.id.in_((earliest, later))))
        await session.commit()
        after_purge = await build_snapshot(session, events, now)
        assert overview(after_purge, 1, one)["acquisition"]["series"] == [
            {"date": "2026-01-02", "files": 2, "bytes": 3200, "current_size_files": 1}
        ]

        await session.execute(delete(MediaFile).where(MediaFile.id == media_ids["today"]))
        await session.commit()
        after_removal = await build_snapshot(session, events, now)
        assert overview(after_removal, 1, one)["acquisition"]["series"] == [
            {"date": "2026-01-02", "files": 1, "bytes": 200, "current_size_files": 1}
        ]


@pytest.mark.asyncio
async def test_calendar_retention_boundary(db):
    events = EventBus()
    events.timezone = ZoneInfo("Asia/Kuwait")
    async with db() as session:
        lib = await add_library(session, "one")
        media = await add_media(session, lib, "a.mkv", "h264")
        for finished in (datetime(2025, 1, 2, 20, 59, tzinfo=UTC), datetime(2025, 1, 2, 21, tzinfo=UTC)):
            job = await add_job(session, lib, media, status="succeeded", finished_at=finished)
            await add_finished_attempt(session, job, source_size=1000, output_size=900)
        await session.commit()
        snapshot = await build_snapshot(session, events, datetime(2026, 1, 2, 1, tzinfo=UTC))

    assert savings(snapshot, 365)["series"][0] == {"date": "2025-01-03", "files": 1, "saved_bytes": 100}
    assert overview(snapshot, 365)["jobs"]["succeeded"] == 1


@pytest.mark.asyncio
async def test_codec_distribution_counts(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        lib = await add_library(session, "one")
        await add_media(session, lib, "a.mkv", "h264")
        await add_media(session, lib, "b.mkv", "h264")
        await add_media(session, lib, "c.mkv", "hevc")
        await add_media(session, lib, "d.mkv", None)
        await session.commit()

        distribution = await codec_distribution(session)

    assert distribution == {"h264": 2, "hevc": 1, "unknown": 1}
