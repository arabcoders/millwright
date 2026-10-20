from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.protocol import Finished
from common.enums import JobStatus
from server.events import EventBus
from server.finalization import FinalizationError, _authorize_publication
from server.healthcheck import HealthcheckService
from server.library_deletion import LibraryDeletionService
from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    ProbeRow,
    TrashEntry,
    utcnow,
)
from server.nodehub.hub import NodeHub
from server.queue.store import finish
from server.probing import ProbeService
from server.jobs import JobService
from server.plugins.host import PluginHost
from server.scanner.service import ScannerService
from tests.helpers import database_context


@dataclass
class Runtime:
    db: async_sessionmaker[AsyncSession]
    service: LibraryDeletionService
    events: EventBus
    root: Path


@pytest_asyncio.fixture
async def runtime(tmp_path: Path):
    async with database_context("library-deletion") as db:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        trash = tmp_path / "trash"
        trash.mkdir()
        events = EventBus(min_interval_s=0)
        hub = NodeHub(db, workspace_dir=workspace, trash_dir=trash, events=events, min_free_gb=1)
        scanner = ScannerService(db, staging=hub.staging, events=events)
        probes = ProbeService(db, "fake-ffprobe", "fake-mkvpropedit", workspace_dir=workspace)
        healthchecks = HealthcheckService(db, tmp_path / "logs", "fake-ffmpeg", "fake-ffprobe")
        jobs = JobService(
            db,
            staging=hub.staging,
            healthchecks=healthchecks,
            probes=probes,
            hub=hub,
            host=PluginHost([]),
            events=events,
            ffprobe_bin="fake-ffprobe",
            mkvpropedit_bin="fake-mkvpropedit",
            workspace_dir=workspace,
        )
        service = LibraryDeletionService(
            db,
            scanner=scanner,
            probes=probes,
            hub=hub,
            healthchecks=healthchecks,
            staging=hub.staging,
            events=events,
            reset_lock=asyncio.Lock(),
            jobs=jobs,
        )
        await service.start()
        try:
            yield Runtime(db=db, service=service, events=events, root=tmp_path)
        finally:
            await asyncio.wait_for(service.close(), 5)
            await jobs.close()
            await healthchecks.close()
            await events.aclose()


async def indexed(runtime: Runtime, name: str = "target") -> tuple[int, int, Path]:
    root = runtime.root / name
    root.mkdir()
    path = root / "movie.mkv"
    path.write_bytes(b"original media")
    async with runtime.db() as session:
        library = Library(name=name, roots=[str(root)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            container="mkv",
            medium="movie",
            last_scanned_at=utcnow(),
        )
        session.add(media)
        await session.flush()
        probe = ProbeRow(media_file_id=media.id, raw_json="{}")
        session.add(probe)
        await session.flush()
        media.probe_id = probe.id
        await session.commit()
        return library.id, media.id, path


async def job(
    runtime: Runtime,
    library_id: int,
    media_id: int,
    *,
    status: str,
    attempt_status: str | None = None,
    kind: str = "transcode",
) -> tuple[int, int | None]:
    async with runtime.db() as session:
        row = Job(
            library_id=library_id, media_file_id=media_id, status=status, kind=kind, attempts=1 if attempt_status else 0
        )
        session.add(row)
        await session.flush()
        attempt = None
        if attempt_status is not None:
            attempt = JobAttempt(
                job_id=row.id,
                attempt_number=1,
                status=attempt_status,
                result=attempt_status,
                workload=kind,
                finished_at=utcnow() if attempt_status in {"failed", "succeeded", "cancelled"} else None,
            )
            session.add(attempt)
            await session.flush()
        session.add(JobEvent(job_id=row.id, type="enqueued", payload={}))
        await session.commit()
        return row.id, attempt.id if attempt is not None else None


async def completed(runtime: Runtime, library_id: int) -> None:
    task = runtime.service._tasks.get(library_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["hold", "staging", "queued"])
async def test_pending_history(runtime: Runtime, status: str):
    library_id, media_id, source = await indexed(runtime)
    pending, _ = await job(runtime, library_id, media_id, status=status)
    history, attempt_id = await job(runtime, library_id, media_id, status="succeeded", attempt_status="succeeded")
    artifact = runtime.root / "workspace" / "completed.mkv"
    artifact.write_bytes(b"completed artifact")
    trash = runtime.root / "trash" / "retained.mkv"
    trash.write_bytes(b"trashed original")
    async with runtime.db() as session:
        session.add(
            TrashEntry(
                id="0123456789abcdef",
                library_id=library_id,
                original_path=str(source),
                library_root=str(source.parent),
                suffix=".mkv",
                size_bytes=16,
                mtime_ns=1,
                trashed_at=utcnow(),
                purge_at=utcnow(),
            )
        )
        await session.commit()
    await runtime.service.request(library_id)
    await completed(runtime, library_id)
    async with runtime.db() as session:
        assert await session.get(Library, library_id) is None
        assert await session.get(MediaFile, media_id) is None
        assert not list(await session.scalars(select(ProbeRow)))
        assert await session.get(Job, pending) is None
        retained = await session.get(Job, history)
        assert retained is not None and retained.library_id == library_id and retained.media_file_id == media_id
        assert await session.get(JobAttempt, attempt_id) is not None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == "0123456789abcdef")) is not None
    assert source.read_bytes() == b"original media"
    assert artifact.read_bytes() == b"completed artifact"
    assert trash.read_bytes() == b"trashed original"


@pytest.mark.asyncio
async def test_retry_history(runtime: Runtime):
    library_id, media_id, source = await indexed(runtime)
    retried, attempt_id = await job(runtime, library_id, media_id, status="queued", attempt_status="succeeded")
    await runtime.service.request(library_id)
    await completed(runtime, library_id)
    async with runtime.db() as session:
        retained = await session.get(Job, retried)
        assert retained is not None and retained.status == "cancelled"
        attempt = await session.get(JobAttempt, attempt_id)
        assert attempt is not None and attempt.status == "succeeded"
        assert await session.get(Library, library_id) is None
    assert source.read_bytes() == b"original media"


@pytest.mark.asyncio
async def test_ack_deduplicates(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    library_id, media_id, _ = await indexed(runtime)
    pending, _ = await job(runtime, library_id, media_id, status="queued")
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def held(identifier: int):
        calls.append(identifier)
        started.set()
        await release.wait()

    monkeypatch.setattr(runtime.service.scanner, "drain_library", held)
    subscription = runtime.events.subscribe()
    try:
        assert await asyncio.wait_for(runtime.service.request(library_id), 1) == {
            "library_id": library_id,
            "status": "deleting",
        }
        await asyncio.wait_for(started.wait(), 1)
        assert not runtime.service.reset_lock.locked()
        async with runtime.db() as session:
            library = await session.get(Library, library_id)
            assert library is not None and library.deleted and library.suspended
            assert library.delete_error is None
            assert await session.get(MediaFile, media_id) is not None
        event = await asyncio.wait_for(subscription.next(), 1)
        assert event is not None and event.type == "library.deletion"
        assert event.payload == {
            "library_id": library_id,
            "library_name": "target",
            "status": "deleting",
            "error": None,
        }
        worker = runtime.service._tasks[library_id]
        await runtime.service.request(library_id)
        assert runtime.service._tasks[library_id] is worker
        assert calls == [library_id]
        # The second queue event is independent of the eventual completion.
        await asyncio.wait_for(subscription.next(), 1)
        release.set()
        await completed(runtime, library_id)
        async with runtime.db() as session:
            assert await session.get(MediaFile, media_id) is None
        runtime.events.flush_bulk()
        events = []
        while subscription.backlog:
            events.append(await subscription.next())
        assert any(
            event is not None and event.type == "job.deleted" and event.payload == {"job_id": pending}
            for event in events
        )
        assert any(
            event is not None
            and event.type == "events.batch"
            and event.payload["events"]
            == [
                {
                    "type": "file.status",
                    "payload": {"action": "deleted", "media_file_id": media_id, "library_id": library_id},
                }
            ]
            for event in events
        )
        assert any(
            event is not None and event.type == "library.deletion" and event.payload["status"] == "deleted"
            for event in events
        )
    finally:
        release.set()
        runtime.events.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_deletion_commit_failure(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    library_id, media_id, source = await indexed(runtime)
    pending, _ = await job(runtime, library_id, media_id, status="queued")
    session_class = runtime.db.class_
    execute, commit, publish = session_class.execute, session_class.commit, runtime.events.publish
    deleting: set[AsyncSession] = set()
    removals = []

    async def track_delete(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if statement.is_delete and statement.table.name == "media_files":
            deleting.add(session)
        return result

    async def fail_commit(session):
        if session in deleting:
            assert not removals
            raise RuntimeError("Deletion commit failed.")
        await commit(session)

    def capture(type_, payload, key=None, *, bulk=False):
        if type_ == "file.status" and payload["action"] == "deleted":
            removals.append(payload)
        publish(type_, payload, key, bulk=bulk)

    monkeypatch.setattr(session_class, "execute", track_delete)
    monkeypatch.setattr(session_class, "commit", fail_commit)
    monkeypatch.setattr(runtime.events, "publish", capture)
    await runtime.service.request(library_id)
    await completed(runtime, library_id)
    assert removals == []
    async with runtime.db() as session:
        library = await session.get(Library, library_id)
        assert library is not None and library.delete_error == "Deletion commit failed."
        media = await session.get(MediaFile, media_id)
        assert media is not None and await session.get(ProbeRow, media.probe_id) is not None
        assert await session.get(Job, pending) is not None
    assert source.read_bytes() == b"original media"


@pytest.mark.asyncio
async def test_restart_request(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    library_id, _, _ = await indexed(runtime)
    started, release, closing_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scanner_drain = runtime.service.scanner.drain_library
    close = runtime.service._close

    async def held(identifier: int):
        started.set()
        await release.wait()

    async def closing(tasks):
        closing_started.set()
        await close(tasks)

    monkeypatch.setattr(runtime.service.scanner, "drain_library", held)
    monkeypatch.setattr(runtime.service, "_close", closing)
    await runtime.service.request(library_id)
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(runtime.service.close())
    try:
        await asyncio.wait_for(closing_started.wait(), 1)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 5)
        async with runtime.db() as session:
            library = await session.get(Library, library_id)
            assert library is not None and library.deleted and library.delete_error is None
        monkeypatch.setattr(runtime.service.scanner, "drain_library", scanner_drain)
        await runtime.service.start()
        await completed(runtime, library_id)
        async with runtime.db() as session:
            assert await session.get(Library, library_id) is None
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_failure(runtime: Runtime):
    library_id, media_id, source = await indexed(runtime)
    retained, attempt_id = await job(runtime, library_id, media_id, status="failed", attempt_status="failed")
    partial = source.with_suffix(".part")
    partial.write_bytes(b"unfinished artifact")
    async with runtime.db() as session:
        journal = FinalizationJournal(
            attempt_id=attempt_id,
            source_path=str(source),
            destination_path=str(source),
            partial_path=str(partial),
            trash_path=str(runtime.root / "trash" / "claim"),
            state="prepared",
        )
        session.add(journal)
        await session.commit()
        journal_id = journal.id
    await runtime.service.request(library_id)
    await completed(runtime, library_id)
    async with runtime.db() as session:
        library = await session.get(Library, library_id)
        assert library is not None and library.suspended and library.deleted
        assert await session.get(MediaFile, media_id) is not None
        assert await session.get(Job, retained) is not None
        journal = await session.get(FinalizationJournal, journal_id)
        assert journal is not None and journal.state == "prepared"
        journal.state = "rolled_back"
        await session.commit()
    assert partial.read_bytes() == b"unfinished artifact"
    assert source.read_bytes() == b"original media"
    await runtime.service.request(library_id)
    await completed(runtime, library_id)
    async with runtime.db() as session:
        assert await session.get(Library, library_id) is None
        assert await session.get(Job, retained) is not None


@pytest.mark.asyncio
async def test_restart_failed(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    library_id, _, source = await indexed(runtime)
    async with runtime.db() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.deleted = True
        library.suspended = True
        library.delete_error = "Previous cancellation failed."
        await session.commit()
    await runtime.service.close()
    entered, release = asyncio.Event(), asyncio.Event()
    original_drain = runtime.service.scanner.drain_library

    async def held(identifier: int):
        entered.set()
        await release.wait()
        await original_drain(identifier)

    monkeypatch.setattr(runtime.service.scanner, "drain_library", held)
    subscription = runtime.events.subscribe()
    try:
        async with runtime.service.reset_lock:
            await asyncio.wait_for(runtime.service.start(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        async with runtime.db() as session:
            library = await session.get(Library, library_id)
            assert library is not None and library.delete_error is None
        event = await asyncio.wait_for(subscription.next(), 1)
        assert event is not None and event.type == "library.deletion"
        assert event.payload["status"] == "deleting" and event.payload["error"] is None
        release.set()
        await completed(runtime, library_id)
        async with runtime.db() as session:
            assert await session.get(Library, library_id) is None
        assert source.read_bytes() == b"original media"
    finally:
        release.set()
        runtime.events.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_unconfirmed_node(runtime: Runtime):
    library_id, media_id, source = await indexed(runtime)
    retained, _ = await job(runtime, library_id, media_id, status="claimed", attempt_status="claimed")
    await runtime.service.request(library_id)
    await completed(runtime, library_id)
    async with runtime.db() as session:
        library = await session.get(Library, library_id)
        assert library is not None and library.delete_error is not None
        assert await session.get(MediaFile, media_id) is not None
        assert await session.get(Job, retained) is not None
    assert source.read_bytes() == b"original media"


@pytest.mark.asyncio
async def test_completion_barrier(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    library_id, media_id, _ = await indexed(runtime)
    retained, attempt_id = await job(runtime, library_id, media_id, status="cancelled", attempt_status="cancelled")
    entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_drain = runtime.service.hub.drain_library

    async def finishing(node_id: int, message: Finished):
        entered.set()
        await release.wait()

    async def drain(identifier: int):
        draining.set()
        await original_drain(identifier)

    monkeypatch.setattr(runtime.service.hub, "_finish_attempt", finishing)
    monkeypatch.setattr(runtime.service.hub, "drain_library", drain)
    assert attempt_id is not None
    report = Finished(
        job_id=retained, attempt_id=attempt_id, result="cancelled", log_path="jobs/1/attempts/1/ffmpeg.log"
    )
    handler = asyncio.create_task(runtime.service.hub._finish(1, report))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await runtime.service.request(library_id)
        await asyncio.wait_for(draining.wait(), 1)
        async with runtime.db() as session:
            assert await session.get(Library, library_id) is not None
        assert not runtime.service._tasks[library_id].done()
        release.set()
        await asyncio.wait_for(handler, 5)
        await completed(runtime, library_id)
        async with runtime.db() as session:
            assert await session.get(Library, library_id) is None
            assert await session.get(Job, retained) is not None
    finally:
        release.set()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_scoped_healthchecks(runtime: Runtime):
    target, target_file, _ = await indexed(runtime)
    other, other_file, _ = await indexed(runtime, "other")
    target_job, target_attempt = await job(
        runtime, target, target_file, status="running", attempt_status="running", kind="healthcheck"
    )
    other_job, other_attempt = await job(
        runtime, other, other_file, status="running", attempt_status="running", kind="healthcheck"
    )
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def running(job_id: int, ready: asyncio.Event) -> None:
        ready.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            async with runtime.db() as session:
                await finish(session, job_id, JobStatus.CANCELLED)
                await session.commit()
            raise

    healthchecks = runtime.service.healthchecks
    tasks = []
    for (job_id, attempt_id), ready in zip(
        [(target_job, target_attempt), (other_job, other_attempt)], entered, strict=True
    ):
        assert attempt_id is not None
        task = asyncio.create_task(running(job_id, ready))
        healthchecks._running[job_id] = (attempt_id, task)
        healthchecks._started.add(attempt_id)
        healthchecks._tasks.add(task)
        task.add_done_callback(healthchecks._tasks.discard)
        task.add_done_callback(lambda finished, jid=job_id: healthchecks._finished(jid, finished))
        tasks.append(task)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
        await runtime.service.request(target)
        await completed(runtime, target)
        assert tasks[0].cancelled()
        assert not tasks[1].done() and not tasks[1].cancelling()
        async with runtime.db() as session:
            assert await session.get(Library, target) is None
            assert await session.get(Library, other) is not None
            retained = await session.get(Job, target_job)
            assert retained is not None and retained.status == "cancelled"
            unaffected = await session.get(Job, other_job)
            assert unaffected is not None and unaffected.status == "running"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_publication_gate(runtime: Runtime):
    library_id, _, source = await indexed(runtime)
    async with runtime.db() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.deleted = True
        library.suspended = True
        await session.commit()
        with pytest.raises(FinalizationError):
            await _authorize_publication(session, library_id, source, source, [str(source.parent)])
        await session.rollback()
        await _authorize_publication(session, library_id, source, source, [str(source.parent)], recovery=True)
        await session.rollback()
    assert source.read_bytes() == b"original media"


@pytest.mark.asyncio
async def test_claim_handoff(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    library_id, media_id, _ = await indexed(runtime)
    retained, _ = await job(runtime, library_id, media_id, status="queued", kind="healthcheck")
    claimed, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
    healthchecks = runtime.service.healthchecks
    original_claim = healthchecks.claim_next
    original_drain = healthchecks.drain_library

    async def claim():
        result = await original_claim()
        if result is not None:
            claimed.set()
            await release.wait()
        return result

    async def drain(identifier: int):
        draining.set()
        await original_drain(identifier)

    monkeypatch.setattr(healthchecks, "claim_next", claim)
    monkeypatch.setattr(healthchecks, "drain_library", drain)
    healthchecks.start()
    try:
        await asyncio.wait_for(claimed.wait(), 1)
        assert retained not in healthchecks._running
        await runtime.service.request(library_id)
        await asyncio.wait_for(draining.wait(), 1)
        assert not runtime.service._tasks[library_id].done()
        release.set()
        await completed(runtime, library_id)
        async with runtime.db() as session:
            assert await session.get(Library, library_id) is None
            history = await session.get(Job, retained)
            assert history is not None and history.attempts == 1 and history.status == "cancelled"
    finally:
        release.set()
        await healthchecks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Job, MediaFile, JobAttempt])
async def test_cancelled_claim_reads(runtime: Runtime, monkeypatch: pytest.MonkeyPatch, model):
    library_id, media_id, source = await indexed(runtime)
    retained, _ = await job(runtime, library_id, media_id, status="queued", kind="healthcheck")
    entered, release, cancelled = (asyncio.Event() for _ in range(3))
    reading: asyncio.Task | None = None
    healthchecks = runtime.service.healthchecks
    session_class = runtime.db.class_
    original_get = session_class.get
    original_cancel = healthchecks.cancel_job

    async def get(session, entity, identifier, **kwargs):
        nonlocal reading
        lock = kwargs.get("with_for_update")
        locked = lock is True or isinstance(lock, dict) and not lock.get("skip_locked", False)
        if (
            entity is model
            and retained in healthchecks._running
            and (locked or model is MediaFile)
            and not entered.is_set()
        ):
            reading = asyncio.current_task()
            entered.set()
            await release.wait()
        return await original_get(session, entity, identifier, **kwargs)

    def cancel(job_id: int) -> bool:
        result = original_cancel(job_id)
        if job_id == retained:
            cancelled.set()
        return result

    monkeypatch.setattr(session_class, "get", get)
    monkeypatch.setattr(healthchecks, "cancel_job", cancel)
    healthchecks.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Drain the job lookup before the deletion marker takes its resource locks.
        draining = asyncio.create_task(healthchecks.drain_library(library_id))
        try:
            await asyncio.wait_for(cancelled.wait(), 1)
            assert reading is not None and not reading.cancelling() and not reading.done()
            assert not draining.done()
            release.set()
            await asyncio.wait_for(draining, 5)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(draining, return_exceptions=True), 5)
        await runtime.service.request(library_id)
        await completed(runtime, library_id)
        async with runtime.db() as session:
            assert await session.get(Library, library_id) is None
            history = await session.get(Job, retained)
            assert history is not None and history.status == "cancelled"
            attempt = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == retained))
            assert attempt is not None and attempt.status == "cancelled"
        assert source.read_bytes() == b"original media"
    finally:
        release.set()
        await healthchecks.close()


@pytest.mark.asyncio
async def test_scoped_actions(runtime: Runtime):
    target, target_file, _ = await indexed(runtime)
    other, other_file, _ = await indexed(runtime, "other")
    target_job, _ = await job(runtime, target, target_file, status="succeeded", attempt_status="succeeded")
    other_job, _ = await job(runtime, other, other_file, status="succeeded", attempt_status="succeeded")
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    hub = runtime.service.hub

    async def action(ready: asyncio.Event):
        ready.set()
        await release.wait()

    tasks = []
    for job_id, ready in zip([target_job, other_job], entered, strict=True):
        task = asyncio.create_task(action(ready))
        hub._action_jobs[task] = job_id
        task.add_done_callback(lambda finished: hub._action_jobs.pop(finished, None))
        tasks.append(task)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
        await runtime.service.request(target)
        await completed(runtime, target)
        assert tasks[0].cancelled()
        assert not tasks[1].done() and not tasks[1].cancelling()
        async with runtime.db() as session:
            assert await session.get(Library, target) is None
            assert await session.get(Library, other) is not None
            assert await session.get(Job, target_job) is not None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
