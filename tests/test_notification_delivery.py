from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from apprise.plugins.custom_json import NotifyJSON
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.actions import Action, Notify
from common.plan.models import FFmpegPlan, RateControl, VideoTarget
from common.enums import JobStatus
from server.models import Job, JobEvent, Library, MediaFile, NotificationDelivery, NotificationTarget
from server.notifications import NotificationService
from server.queue.store import enqueue_file_job, enqueue_healthcheck_job, finish, retry_job
from common.fs.identity import source_identity
from tests.helpers import request_retry, database_context, make_test_temp_dir

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("notifications") as factory:
        yield factory


async def make_job(session: AsyncSession, *, notify: bool = True) -> int:
    path = make_test_temp_dir("notification-media") / "title.mkv"
    path.write_bytes(b"x" * 100)
    identity = source_identity(path.stat())
    library = Library(name="movies", roots=[str(path.parent)])
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
        last_scanned_at=NOW,
    )
    session.add(media)
    await session.flush()
    actions: list[Action] | None = [Notify(targets=["primary", "primary"], on=["job.failed"])] if notify else None
    plan = FFmpegPlan(video=VideoTarget(encoder="libx264", rate=RateControl(quality=21, quality_option="crf")))
    job = await enqueue_file_job(session, media.id, library.id, plan, pre_actions=actions, now=NOW)
    return job.id


@pytest.mark.asyncio
async def test_terminal_enqueue_is_transactional(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        job_id = await make_job(session)
        await session.flush()
        assert await finish(session, job_id, JobStatus.FAILED, now=NOW)
        deliveries = list((await session.execute(select(NotificationDelivery))).scalars())
        events = list((await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))).scalars())
        assert [(item.target_name, item.job_event_id) for item in deliveries] == [("primary", events[-1].id)]
        await session.rollback()

    async with db() as session:
        assert list((await session.execute(select(NotificationDelivery))).scalars()) == []


@pytest.mark.asyncio
async def test_delivery_success(db: async_sessionmaker[AsyncSession]) -> None:
    calls: list[tuple[str, str, str, float]] = []

    async def send(url: str, body: str, title: str, timeout: float) -> bool:
        calls.append((url, body, title, timeout))
        return True

    async with db() as session:
        job_id = await make_job(session)
        session.add(NotificationTarget(name="primary", url="service://private-token"))
        await finish(session, job_id, JobStatus.FAILED, now=NOW)
        request_id = await request_retry(session, job_id)
        assert await retry_job(session, job_id, now=NOW, request_id=request_id) is not None
        await session.commit()

    service = NotificationService(db, send=send)
    assert await service.deliver_pending() == 1
    async with db() as session:
        delivery = (await session.execute(select(NotificationDelivery))).scalar_one()
        assert (delivery.status, delivery.attempts, delivery.error) == ("sent", 1, None)
    assert calls[0][0] == "service://private-token"
    assert calls[0][3] == 60.0


@pytest.mark.asyncio
async def test_healthcheck_success_suppressed(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        library = Library(name="movies", roots=["/movies"])
        session.add(library)
        await session.flush()
        media = MediaFile(
            path="/movies/title.mkv",
            library_id=library.id,
            size=100,
            mtime_ns=1,
            container="mkv",
            medium="movie",
            last_scanned_at=NOW,
        )
        session.add(media)
        await session.flush()
        job = await enqueue_healthcheck_job(session, media.id, library.id, now=NOW)
        job.pre_actions = [Notify(targets=["primary"], on=["job.succeeded"]).model_dump(mode="json")]
        await finish(session, job.id, JobStatus.SUCCEEDED, now=NOW)
        assert list((await session.execute(select(NotificationDelivery))).scalars()) == []


@pytest.mark.asyncio
async def test_delivery_failure_logs(db: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture) -> None:
    async def send(url: str, body: str, title: str, timeout: float) -> bool:
        raise RuntimeError("connection refused for service://secret-token")

    async with db() as session:
        job_id = await make_job(session)
        session.add(NotificationTarget(name="primary", url="service://secret-token"))
        await finish(session, job_id, JobStatus.FAILED, now=NOW)
        await session.commit()

    service = NotificationService(db, send=send)
    for _ in range(3):
        await service.deliver_pending()
    async with db() as session:
        delivery = (await session.execute(select(NotificationDelivery))).scalar_one()
        assert (delivery.status, delivery.attempts) == ("failed", 3)
        assert delivery.error is not None and "connection refused" in delivery.error
        assert "secret-token" not in (delivery.error or "")
    assert any("connection refused" in getattr(record, "error", "") for record in caplog.records)
    assert all("secret-token" not in str(record.__dict__) for record in caplog.records)


@pytest.mark.asyncio
async def test_missing_target_cause(db: async_sessionmaker[AsyncSession]) -> None:
    async def send(url: str, body: str, title: str, timeout: float) -> bool:
        pytest.fail("missing target must not send")

    async with db() as session:
        job_id = await make_job(session)
        target = NotificationTarget(name="primary", url="service://removed")
        session.add(target)
        await session.flush()
        await finish(session, job_id, JobStatus.FAILED, now=NOW)
        await session.delete(target)
        await session.commit()

    service = NotificationService(db, send=send)
    for _ in range(3):
        await service.deliver_pending()
    async with db() as session:
        delivery = (await session.execute(select(NotificationDelivery))).scalar_one()
        assert (delivery.status, delivery.attempts) == ("failed", 3)


@pytest.mark.asyncio
async def test_provider_failure_saved(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    def send(self: NotifyJSON, body: str, title: str = "", **kwargs: object) -> bool:
        self.logger.warning("HTTP 403 Forbidden at /notify?format=json")
        return False

    monkeypatch.setattr(NotifyJSON, "send", send)
    async with db() as session:
        job_id = await make_job(session)
        session.add(NotificationTarget(name="primary", url="json://example.test/notify?format=json"))
        await finish(session, job_id, JobStatus.FAILED, now=NOW)
        await session.commit()

    await NotificationService(db).deliver_pending()
    async with db() as session:
        delivery = (await session.execute(select(NotificationDelivery))).scalar_one()
        assert delivery.status == "pending"
        assert delivery.error is not None and "HTTP 403 Forbidden at /notify?format=json" in delivery.error
