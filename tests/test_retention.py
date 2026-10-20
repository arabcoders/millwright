from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums import JobStatus
from common.fs.identity import source_identity
from common.plan import FFmpegPlan
from server.events import EventBus
from tests.helpers import create_test_app as create_app
from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NodeRow,
    NotificationDelivery,
)
from server.queue.store import capture_source, claim, mark_staged, retry_job
from server.retention import prune_jobs
from server.workspace import cleanup_workspace
from tests.test_staging import add_job
from tests.helpers import request_retry, database_context


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.SUCCEEDED, JobStatus.SKIPPED])
async def test_prunes_graph(tmp_path, status: JobStatus, database_factory, monkeypatch: pytest.MonkeyPatch):
    factory = database_factory
    root = tmp_path
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    async with factory() as session:
        library = Library(name="library", roots=[str(tmp_path)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(source),
            size=1,
            mtime_ns=1,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(
            media_file_id=media.id,
            library_id=library.id,
            status=status,
            finished_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="succeeded")
        session.add(attempt)
        await session.flush()
        event = JobEvent(job_id=job.id, type="done")
        session.add(event)
        await session.flush()
        session.add_all(
            [
                NotificationDelivery(job_id=job.id, job_event_id=event.id, target_name="alerts", status="sent"),
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path="a",
                    destination_path="d",
                    partial_path="b",
                    trash_path="c",
                    state="completed",
                ),
            ]
        )
        await session.commit()
        job_id = job.id
    (root / "jobs" / str(job_id)).mkdir(parents=True)
    marker = root / "jobs" / str(job_id)
    marker.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.retention.datetime", SimpleNamespace(now=lambda _tz: T0))
    app = create_app(factory, cache_root=tmp_path / "cache")
    history = next(entry.run for entry in app.state.scheduler._callbacks if entry.name == "history")
    await app.state.settings.update({"job_retention_days": 0})
    await history()
    async with factory() as session:
        assert await session.get(Job, job_id) is not None
    events = app.state.events
    sub = events.subscribe()
    await app.state.settings.update({"job_retention_days": 1})
    await history()
    deleted = await asyncio.wait_for(sub.next(), 1)
    assert deleted is not None and deleted.type == "job.deleted" and deleted.payload == {"job_id": job_id}
    events.unsubscribe(sub)
    await events.aclose()
    assert marker.exists()
    async with factory() as session:
        assert (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none() is None
        assert not (await session.execute(select(JobAttempt).where(JobAttempt.job_id == job_id))).scalars().all()
        assert not (await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))).scalars().all()
        assert (
            not (await session.execute(select(NotificationDelivery).where(NotificationDelivery.job_id == job_id)))
            .scalars()
            .all()
        )
        assert not (await session.execute(select(FinalizationJournal))).scalars().all()


@pytest.mark.asyncio
async def test_preserves_active(tmp_path, database_factory):
    factory = database_factory
    async with factory() as session:
        library = Library(name="library", roots=[str(tmp_path)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path="missing",
            size=1,
            mtime_ns=1,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        session.add(
            Job(
                media_file_id=media.id,
                library_id=library.id,
                status=JobStatus.RUNNING,
                finished_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    assert await prune_jobs(factory, 1, now=datetime(2026, 1, 1, tzinfo=UTC)) == 0


@pytest.mark.asyncio
async def test_zero_disables(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    async with database_context("zero-retention") as factory:
        job_id = await add_job(factory, source, status="failed")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
            await session.commit()
        assert await prune_jobs(factory, 0, now=T0) == 0
        async with factory() as session:
            assert await session.get(Job, job_id) is not None
        assert await prune_jobs(factory, 1, now=T0) == 1


@pytest.mark.asyncio
async def test_retention_source_locks(tmp_path, database_factory):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(database_factory, source, status="failed")
    async with database_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
        await session.commit()
        library_id, media_id = job.library_id, job.media_file_id

    async with database_factory() as writer:
        await writer.get(Library, library_id, with_for_update=True)
        await writer.get(MediaFile, media_id, with_for_update=True)
        assert await asyncio.wait_for(prune_jobs(database_factory, 1, now=T0), 5) == 1

    async with database_factory() as reader:
        assert await reader.get(Job, job_id) is None
        assert await reader.get(Library, library_id) is not None
        assert await reader.get(MediaFile, media_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_type",
    ["unprepared", "unprepared_missing_attempt", "interrupted", "journal", "discard_journal", "unfinished"],
)
async def test_retention_after_restart(tmp_path, record_type: str) -> None:
    async with database_context("retention-recovery") as factory:
        source = tmp_path / "source.mkv"
        source.write_bytes(b"source")
        partial = tmp_path / "source.mkv.partial"
        trash = tmp_path / "trash" / "original.mkv"
        partial.write_bytes(b"verified output")
        trash.parent.mkdir()
        trash.write_bytes(b"retained original")
        job_id = await add_job(factory, source, status="failed")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
            attempt = JobAttempt(
                job_id=job_id,
                attempt_number=1,
                status="running" if record_type == "unfinished" else "failed",
                result="interrupted" if record_type == "interrupted" else "failed",
            )
            session.add(attempt)
            await session.flush()
            event = JobEvent(job_id=job_id, attempt_id=attempt.id, type="failed")
            session.add(event)
            if record_type.startswith("unprepared"):
                session.add(
                    JobEvent(
                        job_id=job_id,
                        attempt_id=None if record_type == "unprepared_missing_attempt" else attempt.id,
                        type="finalization_unprepared",
                    )
                )
            elif record_type in {"journal", "discard_journal"}:
                session.add(
                    FinalizationJournal(
                        attempt_id=attempt.id,
                        source_path=str(source),
                        destination_path=str(source),
                        partial_path=str(partial),
                        trash_path=str(trash),
                        state="discard_prepared" if record_type == "discard_journal" else "prepared",
                    )
                )
            await session.commit()
            attempt_id, event_id = attempt.id, event.id
        root = tmp_path / "workspace"
        artifact = root / "jobs" / str(job_id) / "attempts" / "1" / "output.mkv"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"artifact")
        recovering = record_type in {"journal", "discard_journal"}
        assert await prune_jobs(factory, 1, now=T0) == (0 if recovering else 1)
        await cleanup_workspace(factory, root)
        assert not (root / "jobs" / str(job_id)).exists()
        assert source.read_bytes() == b"source"
        assert partial.read_bytes() == b"verified output"
        assert trash.read_bytes() == b"retained original"
        async with factory() as session:
            assert (await session.get(Job, job_id) is not None) == recovering
            assert (await session.get(JobAttempt, attempt_id) is not None) == recovering
            assert (await session.get(JobEvent, event_id) is not None) == recovering
            if recovering:
                journal = await session.scalar(
                    select(FinalizationJournal).where(FinalizationJournal.attempt_id == attempt_id)
                )
                assert journal is not None
                journal.state = "discard_rolled_back" if record_type == "discard_journal" else "rolled_back"
                await session.commit()
                assert await prune_jobs(factory, 1, now=T0) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["declared", "deferred", "pending", "report"])
async def test_retention_preserves_actions(tmp_path, action_state):
    async with database_context("retention-actions") as factory:
        source = tmp_path / "source.mkv"
        source.write_bytes(b"source")
        job_id = await add_job(factory, source, status="succeeded")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.attempts = 1
            job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
            attempt = JobAttempt(job_id=job_id, attempt_number=1, status="succeeded", result="succeeded")
            session.add(attempt)
            await session.flush()
            if action_state == "declared":
                job.post_actions = [{"kind": "extract_subtitles", "formats": ["srt"]}]
            elif action_state == "deferred":
                session.add(
                    JobEvent(job_id=job_id, attempt_id=attempt.id, type="stage", payload={"post_actions": [{}]})
                )
            else:
                session.add(JobEvent(job_id=job_id, attempt_id=attempt.id, type="actions_pending"))
                if action_state == "report":
                    session.add(JobEvent(job_id=job_id, attempt_id=attempt.id, type="action_report"))
            await session.commit()
            attempt_id = attempt.id
        root = tmp_path / "workspace"
        artifact = root / "jobs" / str(job_id) / "attempts/1/output.eng.srt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"subtitle")
        assert await prune_jobs(factory, 1, now=T0) == 0
        await cleanup_workspace(factory, root)
        assert not (root / "jobs" / str(job_id)).exists()
        async with factory() as session:
            assert await session.get(Job, job_id) is not None
            completed = await session.scalar(
                select(JobEvent).where(
                    JobEvent.job_id == job_id, JobEvent.attempt_id == attempt_id, JobEvent.type == "actions_finished"
                )
            )
            assert completed is not None and completed.payload["abandoned"]
        assert await prune_jobs(factory, 1, now=T0) == 1
        assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_rechecks_recovery_retention(tmp_path, monkeypatch) -> None:
    async with database_context("retention-record-race") as factory:
        source = tmp_path / "source.mkv"
        source.write_bytes(b"source")
        job_id = await add_job(factory, source, status="failed")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
            await session.commit()
        execute = AsyncSession.execute
        selected = False

        async def select_then_protect(session, statement, *args, **kwargs):
            nonlocal selected
            result = await execute(session, statement, *args, **kwargs)
            if not selected and statement.is_select and statement.column_descriptions[0]["entity"] is Job:
                selected = True
                async with factory() as writer:
                    attempt = JobAttempt(job_id=job_id, attempt_number=1, status="failed")
                    writer.add(attempt)
                    await writer.flush()
                    writer.add(
                        FinalizationJournal(
                            attempt_id=attempt.id,
                            source_path=str(source),
                            destination_path=str(source),
                            partial_path=str(source) + ".partial",
                            trash_path=str(source) + ".trash",
                            state="prepared",
                        )
                    )
                    await writer.commit()
            return result

        monkeypatch.setattr(AsyncSession, "execute", select_then_protect)
        assert await asyncio.wait_for(prune_jobs(factory, 1, now=T0), 3) == 0
        assert selected
        async with factory() as session:
            assert await session.get(Job, job_id) is not None
            assert (
                await session.scalar(select(FinalizationJournal.id).where(FinalizationJournal.state == "prepared"))
                is not None
            )


@pytest.mark.asyncio
async def test_retry_during_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    async with database_context("retention-claim") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(source.parent)])
            node = NodeRow(name="node", display_name="node", token_hash="token")
            session.add_all([library, node])
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(source),
                size=1,
                mtime_ns=source.stat().st_mtime_ns,
                source_identity=source_identity(source.stat()),
                container="mkv",
                medium="video",
                transcode_decided=True,
                last_scanned_at=T0,
            )
            session.add(media)
            await session.flush()
            job = Job(
                media_file_id=media.id,
                library_id=library.id,
                kind="transcode",
                status=JobStatus.FAILED.value,
                plan=FFmpegPlan().model_dump(mode="json"),
                finished_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
            session.add(job)
            await session.flush()
            session.add(JobEvent(job_id=job.id, type="enqueued", payload={"source": await capture_source(media)}))
            await session.commit()
        execute = AsyncSession.execute
        selected = False

        async def claim_selected() -> None:
            async with factory() as session:
                request_id = await request_retry(session, job.id)
                assert await retry_job(session, job.id, now=T0, request_id=request_id) is not None
                assert await mark_staged(session, job.id, now=T0)
                assert await claim(session, job.id, node.id, now=T0) is not None
                await session.commit()

        async def select_then_claim(session, statement, *args, **kwargs):
            nonlocal selected
            result = await execute(session, statement, *args, **kwargs)
            if not selected and statement.is_select and statement.column_descriptions[0]["entity"] is Job:
                selected = True
                await asyncio.wait_for(claim_selected(), 3)
            return result

        monkeypatch.setattr(AsyncSession, "execute", select_then_claim)
        assert await asyncio.wait_for(prune_jobs(factory, 30, now=T0), 3) == 0
        assert selected
        async with factory() as session:
            saved = await session.get(Job, job.id)
            assert saved is not None and saved.status == JobStatus.CLAIMED.value
            assert (await session.scalars(select(JobAttempt))).one().job_id == job.id
            assert (await session.scalars(select(JobEvent))).all()


@pytest.mark.asyncio
async def test_retention_filesystem_untouched(tmp_path, database_factory):
    factory = database_factory
    async with factory() as session:
        library = Library(name="library", roots=[str(tmp_path)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path="missing",
            size=1,
            mtime_ns=1,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(
            media_file_id=media.id,
            library_id=library.id,
            status=JobStatus.FAILED,
            finished_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
        session.add(attempt)
        await session.flush()
        session.add_all(
            [
                JobEvent(job_id=job.id, type="failed"),
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path="a",
                    destination_path="d",
                    partial_path="b",
                    trash_path="c",
                    state="rolled_back",
                ),
            ]
        )
        await session.commit()
        job_id = job.id
    marker = tmp_path / "jobs" / str(job_id)
    marker.mkdir(parents=True)
    assert await prune_jobs(factory, 1, now=datetime(2026, 1, 1, tzinfo=UTC)) == 1
    assert marker.exists()
    async with factory() as session:
        assert (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none() is None
        assert (
            await session.execute(select(JobAttempt).where(JobAttempt.job_id == job_id))
        ).scalar_one_or_none() is None
        assert (await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))).scalar_one_or_none() is None
        assert (await session.execute(select(FinalizationJournal))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_prune_commit_failure(monkeypatch, tmp_path):
    async with database_context("retention-commit") as factory:
        source = tmp_path / "source.mkv"
        source.write_bytes(b"source")
        job_id = await add_job(factory, source, status="failed")
        async with factory() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.finished_at = datetime(2020, 1, 1, tzinfo=UTC)
            await session.commit()
        events = EventBus()
        sub = events.subscribe()
        calls = 0

        async def fail_commit(session):
            nonlocal calls
            calls += 1
            raise RuntimeError("commit failed")

        try:
            with monkeypatch.context() as patch:
                patch.setattr(AsyncSession, "commit", fail_commit)
                with pytest.raises(RuntimeError):
                    await prune_jobs(factory, 1, now=T0, events=events)
            assert calls == 1
            assert sub.backlog == 0
            async with factory() as session:
                assert await session.get(Job, job_id) is not None
            assert await prune_jobs(factory, 1, now=T0, events=events) == 1
            deleted = await asyncio.wait_for(sub.next(), 3)
            assert deleted is not None and deleted.type == "job.deleted" and deleted.payload == {"job_id": job_id}
        finally:
            events.unsubscribe(sub)
            await events.aclose()


@pytest.mark.asyncio
async def test_prune_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.retention.QUERY_BATCH", 2)
    async with database_context("retention-batches") as factory:
        job_ids = []
        for index in range(5):
            path = tmp_path / f"source-{index}.mkv"
            path.write_bytes(b"source")
            job_ids.append(await add_job(factory, path, status="failed"))
        async with factory() as session:
            await session.execute(update(Job).values(finished_at=datetime(2020, 1, 1, tzinfo=UTC)))
            event_id = await session.scalar(select(JobEvent.id).where(JobEvent.job_id == job_ids[0]))
            assert event_id is not None
            await session.commit()
        execute = AsyncSession.execute
        selections = 0
        events = EventBus()
        sub = events.subscribe()

        async def check_batch(session, statement, *args, **kwargs):
            nonlocal selections
            result = await execute(session, statement, *args, **kwargs)
            if (
                statement.is_select
                and statement.column_descriptions[0]["entity"] is Job
                and statement._limit_clause is not None
            ):
                selections += 1
                async with factory() as writer:
                    connection = await writer.connection()
                    await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                    if selections == 1:
                        writer.add(
                            NotificationDelivery(
                                job_id=job_ids[0], job_event_id=event_id, target_name="alerts", status="pending"
                            )
                        )
                        await writer.commit()
                    else:
                        await execute(writer, update(Library).values(updated_at=Library.updated_at))
                        await writer.commit()
                        assert sub.backlog == min(2 * selections - 3, 4)
            return result

        monkeypatch.setattr(AsyncSession, "execute", check_batch)
        try:
            assert await asyncio.wait_for(prune_jobs(factory, 1, now=T0, events=events), 3) == 4
            assert selections == 4
            async with factory() as session:
                assert list((await session.scalars(select(Job.id))).all()) == [job_ids[0]]
            deleted = [await asyncio.wait_for(sub.next(), 1) for _ in range(4)]
            assert [event.payload["job_id"] for event in deleted if event is not None] == job_ids[1:]
        finally:
            events.unsubscribe(sub)
            await events.aclose()


@pytest.mark.asyncio
async def test_prune_batch_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.retention import _delete_job_graphs

    monkeypatch.setattr("server.retention.QUERY_BATCH", 2)
    async with database_context("retention-cancel") as factory:
        job_ids = []
        for index in range(4):
            path = tmp_path / f"source-{index}.mkv"
            path.write_bytes(b"source")
            job_ids.append(await add_job(factory, path, status="failed"))
        async with factory() as session:
            await session.execute(update(Job).values(finished_at=datetime(2020, 1, 1, tzinfo=UTC)))
            await session.commit()

        async def cancel_delete(session, ids):
            await _delete_job_graphs(session, ids)
            if job_ids[2] in ids:
                raise asyncio.CancelledError

        events = EventBus()
        sub = events.subscribe()
        try:
            with monkeypatch.context() as patch:
                patch.setattr("server.retention._delete_job_graphs", cancel_delete)
                with pytest.raises(asyncio.CancelledError):
                    await prune_jobs(factory, 1, now=T0, events=events)
            assert sub.backlog == 2
            async with factory() as session:
                assert list((await session.scalars(select(Job.id).order_by(Job.id))).all()) == job_ids[2:]
                assert list((await session.scalars(select(JobEvent.job_id).order_by(JobEvent.id))).all()) == job_ids[2:]
            assert await prune_jobs(factory, 1, now=T0, events=events) == 2
        finally:
            events.unsubscribe(sub)
            await events.aclose()
