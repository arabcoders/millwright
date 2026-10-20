from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session

from common.protocol import NodeCapabilities
from common.media.probe import ProbeData
from common.enums import JobStatus
from common.workspace import DEFAULT_HARD_SIZE_CEILING
from server.api.jobs import _job_rows
from server.api import jobs as jobs_api
from server.fs.source import MediaSnapshot
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.nodehub.hub import NodeEntry
from server.staging import StagingService
from tests.test_api import ApiEnv, api_env, seed_queue
from tests.test_staging import add_job, db
from tests.test_workspace import stage_source


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transcode", "healthcheck"])
async def test_queue_passes_prefix(api_env: ApiEnv, kind: str, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "_QUEUE_BATCH", 2)
    blocked = [await seed_queue(api_env, f"blocked-{index}", kind=kind, priority=100) for index in range(33)]
    ready = [await seed_queue(api_env, f"ready-{index}", kind=kind) for index in range(2)]
    pending = (
        [await seed_queue(api_env, f"pending-{index}", status="staging", priority=200) for index in range(2)]
        if kind == "transcode"
        else []
    )
    async with api_env.factory() as session:
        for _, library_id in blocked:
            library = await session.get(Library, library_id)
            assert library is not None
            library.suspended = True
        for job_id, _ in pending:
            job = await session.get(Job, job_id)
            assert job is not None
            job.warning = "Capacity is unavailable."
        if kind == "transcode":
            captured = await session.scalar(
                select(JobEvent.payload).where(JobEvent.job_id == ready[0][0], JobEvent.type == "enqueued")
            )
            session.add(JobEvent(job_id=ready[0][0], type="enqueued", payload=captured))
        await session.commit()
    hub = api_env.app.state.hub
    hub.entries["node"] = NodeEntry(
        name="node",
        node_db_id=1,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    limit = 6 if pending else 4
    response = await api_env.client.get(f"/api/jobs/queue?kind={kind}&limit={limit}")
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"] == {"limit": limit, "offset": 0, "total": len(blocked + ready + pending)}
    assert [row["id"] for row in body["items"]] == [identifier for identifier, _ in ready + pending + blocked[:2]]
    assert [row["queue_state"] for row in body["items"]] == ["ready"] * 2 + ["staging"] * len(pending) + ["blocked"] * 2
    assert all(
        row["queue_reason"] == "Capacity is unavailable." for row in body["items"] if row["queue_state"] == "staging"
    )
    assert not hub.entries["node"].in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "a", "b", "ready"])
async def test_blocked_subset_turns(api_env: ApiEnv, monkeypatch, cursor) -> None:
    monkeypatch.setattr(jobs_api, "_QUEUE_BATCH", 1)
    a1, library_a = await seed_queue(api_env, "a1")
    a2, _ = await seed_queue(api_env, "a2", library_id=library_a)
    b1, library_b = await seed_queue(api_env, "b1")
    a3, _ = await seed_queue(api_env, "a3", library_id=library_a)
    b2, _ = await seed_queue(api_env, "b2", library_id=library_b)
    a4, _ = await seed_queue(api_env, "a4", library_id=library_a, status="staging")
    ready, ready_library = await seed_queue(api_env, "ready")
    async with api_env.factory() as session:
        for identifier in (b1, a3, b2, a4):
            captured = await session.scalar(select(JobEvent).where(JobEvent.job_id == identifier))
            assert captured is not None
            captured.payload = {"probe": captured.payload["probe"]}
        await session.commit()
    hub = api_env.app.state.hub
    hub.entries["node"] = NodeEntry(
        name="node",
        node_db_id=1,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    if cursor is not None:
        hub._library_cursors[0] = {"a": library_a, "b": library_b, "ready": ready_library}[cursor]
    expected_ready = {None: [a1, ready, a2], "a": [ready, a1, a2], "b": [ready, a1, a2], "ready": [a1, ready, a2]}[
        cursor
    ]
    expected_blocked = [a3, b1, b2, a4] if cursor == "b" else [b1, a3, b2, a4]
    for limit in (4, 7):
        response = await api_env.client.get(f"/api/jobs/queue?kind=transcode&limit={limit}")
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["items"]] == (expected_ready + expected_blocked)[:limit]
        assert response.json()["pagination"]["total"] == 7


@pytest.mark.asyncio
async def test_preview_payload_cap(api_env: ApiEnv, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "_QUEUE_BATCH", 2)
    decoded = []
    identifiers = [await seed_queue(api_env, f"payload-{index}") for index in range(20)]
    async with api_env.factory() as session:
        for index, (identifier, _) in enumerate(identifiers):
            captured = await session.scalar(select(JobEvent).where(JobEvent.job_id == identifier))
            assert captured is not None
            captured.payload = {**captured.payload, "marker": f"old-{index}"}
            latest = {**captured.payload, "marker": f"latest-{index}"}
            if index == 0:
                latest.pop("source")
            session.add(JobEvent(job_id=identifier, type="enqueued", payload=latest))
        job = await session.get(Job, identifiers[-1][0])
        assert job is not None
        job.delete_requested = True
        await session.commit()
    api_env.app.state.hub.entries["node"] = NodeEntry(
        name="node",
        node_db_id=1,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    loaded = []

    def track(instance, _context):
        loaded.append((type(instance), instance.id))

    def capture(state):
        if state.is_select and "captured" in state.statement.selected_columns:
            result = state.invoke_statement().freeze()
            decoded.extend(row.captured["marker"] for row in result.data)
            return result()
        return None

    event.listen(Session, "do_orm_execute", capture, retval=True)
    for model in (Job, Library, JobEvent):
        event.listen(model, "load", track)
    try:
        response = await api_env.client.get("/api/jobs/queue?kind=transcode&limit=2")
    finally:
        event.remove(Session, "do_orm_execute", capture)
        for model in (Job, Library, JobEvent):
            event.remove(model, "load", track)
    assert response.status_code == 200
    assert response.json()["pagination"] == {"limit": 2, "offset": 0, "total": 19}
    winners = [identifiers[1][0], identifiers[2][0]]
    assert [row["id"] for row in response.json()["items"]] == winners
    # The payload query has no row-order contract. Queue ordering is checked above.
    assert sorted(decoded) == [f"latest-{index}" for index in range(4)]
    assert sorted(loaded, key=lambda entry: entry[1]) == [(Job, identifier) for identifier in winners]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_preview_freezes_inputs(api_env: ApiEnv, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "_QUEUE_BATCH", 1)
    blocked, blocked_library = await seed_queue(api_env, "blocked")
    ready, _ = await seed_queue(api_env, "ready")
    async with api_env.factory() as session:
        captured = await session.scalar(select(JobEvent).where(JobEvent.job_id == blocked))
        assert captured is not None
        captured.payload = {"probe": captured.payload["probe"]}
        await session.commit()
    hub = api_env.app.state.hub
    entry = NodeEntry(
        name="node",
        node_db_id=1,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    hub.entries["node"] = entry
    classify = jobs_api._queue_reasons
    changed = False

    async def mutate(*args):
        nonlocal changed
        async for job, reason in classify(*args):
            if not changed:
                changed = True
                entry.paused = True
                entry.workspace_ready = False
                entry.capabilities.encoders.clear()
                entry.in_flight[ready] = "transcode"
                entry.disconnected.set()
                async with api_env.factory() as session:
                    library = await session.get(Library, blocked_library)
                    assert library is not None
                    library.suspended = True
                    current = await session.get(Job, blocked)
                    assert current is not None
                    current.delete_requested = True
                    session.add(JobEvent(job_id=blocked, type="enqueued", payload={"source": {"size": 2048}}))
                    await session.commit()
            yield job, reason

    monkeypatch.setattr(jobs_api, "_queue_reasons", mutate)
    response = await api_env.client.get("/api/jobs/queue?kind=transcode&limit=2")
    assert response.status_code == 200
    rows = response.json()["items"]
    assert [row["id"] for row in rows] == [ready, blocked]
    assert [row["queue_state"] for row in rows] == ["ready", "blocked"]
    assert rows[1]["queue_reason"]
    assert response.json()["pagination"]["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_preview_completion_load(api_env: ApiEnv, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "_QUEUE_BATCH", 1)
    identifiers = [await seed_queue(api_env, name) for name in ("first", "second")]
    hub = api_env.app.state.hub
    hub.entries["node"] = NodeEntry(
        name="node",
        node_db_id=1,
        capabilities=NodeCapabilities(ffmpeg_version="7", encoders=["libx264"], max_transcodes=1),
        workspace_ready=True,
        conn=AsyncMock(),
    )
    task = asyncio.create_task(asyncio.Event().wait())
    key = (1, 999, 1, "transcode", None)
    hub._completions[key] = task
    classify = jobs_api._queue_reasons

    async def finish_load(*args):
        async for job, reason in classify(*args):
            if not task.done():
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
                hub._completions.pop(key)
            yield job, reason

    monkeypatch.setattr(jobs_api, "_queue_reasons", finish_load)
    try:
        response = await api_env.client.get("/api/jobs/queue?kind=transcode&limit=2")
        assert response.status_code == 200
        rows = response.json()["items"]
        assert [row["id"] for row in rows] == [identifier for identifier, _ in identifiers]
        assert {row["queue_state"] for row in rows} == {"blocked"}
        assert all(row["queue_reason"] for row in rows)
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        hub._completions.pop(key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_env", [True], indirect=True, ids=["background"])
async def test_bulk_metadata_order(api_env: ApiEnv, monkeypatch) -> None:
    await api_env.app.state.jobs.close()
    api_env.app.state.jobs._closed = False
    async with api_env.factory() as session:
        library = Library(name="history", roots=[str(api_env.lib_dir)])
        session.add(library)
        await session.flush()
        media_ids = await session.scalars(
            insert(MediaFile).returning(MediaFile.id),
            [
                {
                    "library_id": library.id,
                    "path": str(api_env.lib_dir / f"history-{index}"),
                    "size": 2048,
                    "mtime_ns": 1,
                    "medium": "movie",
                    "container": "avi",
                    "last_scanned_at": utcnow(),
                }
                for index in range(150)
            ],
        )
        identifiers = list(
            await session.scalars(
                insert(Job).returning(Job.id),
                [
                    {"library_id": library.id, "media_file_id": media_id, "kind": "healthcheck", "status": "failed"}
                    for media_id in media_ids
                ],
            )
        )[::-1]
        for identifier in (identifiers[0], identifiers[-1]):
            job = await session.get(Job, identifier)
            assert job is not None
            job.attempts = 2
            session.add_all(
                JobAttempt(job_id=identifier, attempt_number=number, status="failed", log_data=b"retained log")
                for number in (1, 2)
            )
        await session.commit()
    published = []
    bus = api_env.app.state.events
    publish = bus.publish

    def capture(type_, payload, *args, **kwargs):
        if type_ == "job.status":
            published.append(payload["job_id"])
        publish(type_, payload, *args, **kwargs)

    monkeypatch.setattr(bus, "publish", capture)
    response = await api_env.client.post("/api/jobs/bulk", json={"action": "retry", "job_ids": identifiers})
    assert response.status_code == 202
    assert response.json() == {"accepted": identifiers, "failed": []}
    async with api_env.factory() as session:
        jobs = list(await session.scalars(select(Job).order_by(Job.id.desc())))
        queries = []
        scopes = []

        def track_query(state):
            queries.append(state.statement)
            projection = state.statement.selected_columns
            if projection.contains_column(MediaFile.__table__.c.id):
                result = state.invoke_statement().freeze()
                scopes.append(len(result.data))
                return result()
            return None

        event.listen(session.sync_session, "do_orm_execute", track_query, retval=True)
        try:
            rows = await _job_rows(session, jobs)
        finally:
            event.remove(session.sync_session, "do_orm_execute", track_query)
        assert len(queries) == 4 and scopes == [100, 50]
    assert [row["id"] for row in rows] == identifiers
    assert all(row["status"] == "pending" and row["file"]["id"] == row["media_file_id"] for row in rows)
    assert rows[0]["latest_attempt"]["attempt_number"] == rows[-1]["latest_attempt"]["attempt_number"] == 2
    assert published == identifiers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, source_size, copy_source", [(JobStatus.STAGING, 5, True), (JobStatus.QUEUED, 31, False)]
)
async def test_capacity_preserves_reservations(
    db, tmp_path: Path, monkeypatch, status, source_size, copy_source
) -> None:
    statuses = [JobStatus.QUEUED, JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING]
    sources = [tmp_path / f"{index}.mkv" for index in range(38)]
    for source in sources:
        source.write_bytes(b"x")
    queued = [await add_job(db, sources[index], status=statuses[index % len(statuses)]) for index in range(33)]
    incoming = await add_job(db, sources[33], status=status)
    excluded = [
        await add_job(db, source, status=value)
        for source, value in zip(sources[34:37], (JobStatus.FAILED, JobStatus.HOLD, JobStatus.STAGING), strict=True)
    ]
    healthcheck = await add_job(db, sources[37], status=JobStatus.QUEUED)
    async with db() as session:
        media = await session.get(MediaFile, (await session.get(Job, queued[0])).media_file_id)
        media.size = 17
        session.add_all(
            [
                JobEvent(job_id=queued[1], type="enqueued", payload={"source": {"size": 19}}),
                JobEvent(
                    job_id=queued[2],
                    type="stage",
                    payload={
                        "probe": ProbeData(file_path="input.mkv", duration_s=10, size_bytes=23, streams=[]).model_dump(
                            mode="json"
                        )
                    },
                ),
                JobEvent(job_id=queued[3], type="stage", payload={"output_probe": {"size_bytes": 29}}),
                JobEvent(job_id=queued[3], type="stage", payload={"output_probe": {"size_bytes": 2}}),
                JobEvent(job_id=incoming, type="stage", payload={"output_probe": {"size_bytes": 13}}),
                *[
                    JobEvent(job_id=identifier, type="enqueued", payload={"source": {"size": 1000}})
                    for identifier in excluded + [healthcheck]
                ],
            ]
        )
        (await session.get(Job, healthcheck)).kind = "healthcheck"
        await session.commit()
    sizes = [17, 19, 23, 29] + [1] * 29 + [max(13, source_size)]
    service = StagingService(db, tmp_path / "workspace", min_free_gb=1)
    required = (
        (source_size if copy_source else 0)
        + 3
        + math.ceil(service.min_free_gb * 1e9)
        + sum(math.ceil(size * DEFAULT_HARD_SIZE_CEILING) for size in sizes)
    )
    free = required - 1
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _: SimpleNamespace(free=free))
    async with db() as session:
        assert await service._capacity_warning(session, incoming, source_size, 3, copy_source=copy_source) is not None
        free = required
        assert await service._capacity_warning(session, incoming, source_size, 3, copy_source=copy_source) is None


@pytest.mark.asyncio
async def test_capacity_current_generation(db, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "current.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(db, source)
    async with db() as session:
        session.add_all(
            [
                JobEvent(
                    job_id=job_id,
                    type="stage",
                    payload={
                        "probe": ProbeData(file_path="old.mkv", duration_s=10, size_bytes=1000, streams=[]).model_dump(
                            mode="json"
                        )
                    },
                ),
                JobEvent(job_id=job_id, type="enqueued", payload={"source": {"size": 6}}),
                JobEvent(job_id=job_id, type="stage", payload={"output_probe": {"size_bytes": 9}}),
            ]
        )
        await session.commit()
    service = StagingService(db, tmp_path / "workspace", min_free_gb=1)
    required = 6 + math.ceil(service.min_free_gb * 1e9) + math.ceil(9 * DEFAULT_HARD_SIZE_CEILING)
    free = required - 1
    monkeypatch.setattr("server.staging.shutil.disk_usage", lambda _: SimpleNamespace(free=free))
    async with db() as session:
        assert await service._capacity_warning(session, job_id, 6, 0) is not None
        free = required
        assert await service._capacity_warning(session, job_id, 6, 0) is None


@pytest.mark.asyncio
async def test_catalog_chunks_safely(db, tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    workspace = tmp_path / "workspace"
    async with db() as session:
        library = Library(name="catalog", roots=[str(root)], scan_id=1)
        session.add(library)
        await session.flush()
        media = []
        for index in range(131):
            source = root / f"{index}.mkv"
            source.write_bytes(b"source")
            media.append(
                MediaFile(
                    library_id=library.id,
                    path=str(source),
                    size=6,
                    mtime_ns=1,
                    container="mkv",
                    medium="movie",
                    last_scanned_at=utcnow(),
                )
            )
        session.add_all(media)
        await session.flush()
        probes = [ProbeRow(media_file_id=row.id, raw_json="{}") for row in media]
        session.add_all(probes)
        await session.flush()
        for row, probe in zip(media, probes, strict=True):
            row.probe_id = probe.id
        job = Job(library_id=library.id, media_file_id=media[0].id, status=JobStatus.FAILED)
        protected = Job(library_id=library.id, media_file_id=media[-1].id, status=JobStatus.FAILED)
        session.add_all([job, protected])
        await session.flush()
        session.add_all(
            [JobEvent(job_id=job.id, type="failed"), JobEvent(job_id=protected.id, type="finalization_unprepared")]
        )
        attempt = JobAttempt(job_id=protected.id, attempt_number=1, status="failed")
        session.add(attempt)
        await session.flush()
        session.add(
            FinalizationJournal(
                attempt_id=attempt.id,
                source_path=media[-1].path,
                destination_path=media[-1].path,
                partial_path=str(root / ".1.part"),
                trash_path=str(root / ".1.discard"),
                state="discard_prepared",
            )
        )
        await session.flush()
        await session.commit()
    async with db() as session:
        current = list(await session.scalars(select(MediaFile).order_by(MediaFile.id)))
        snapshots = [MediaSnapshot.from_media(row) for row in current]
        stamp = (await session.get(Library, library.id)).updated_at
        current[-2].size = 7
        await session.commit()
    stage_source(workspace, job.id, Path(media[0].path))
    stage_source(workspace, protected.id, Path(media[-1].path))
    service = StagingService(db, workspace, min_free_gb=1)
    scopes = []

    def scoped_query(_connection, _cursor, statement, parameters, _context, _many):
        if "FOR UPDATE" in statement and " = ANY (" in statement:
            scopes.append(tuple(len(values) for values in parameters.values()))

    engine = db.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", scoped_query)
    try:
        assert (
            await service.prune_catalog(
                snapshots, library_updated_at=stamp, scan_id=1, unavailable=set(), excluded=(root,)
            )
            == 129
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", scoped_query)
        await service.aclose()
    assert scopes == [(131,), (131,)]
    async with db() as session:
        assert list(await session.scalars(select(MediaFile.id).order_by(MediaFile.id))) == [
            row.id for row in media[-2:]
        ]
        assert list(await session.scalars(select(ProbeRow.id).order_by(ProbeRow.id))) == [
            probe.id for probe in probes[-2:]
        ]
        assert await session.get(Job, job.id) is None
        assert await session.get(Job, protected.id) is not None
        assert list(await session.scalars(select(JobEvent.job_id))) == [protected.id]
    assert not (workspace / "jobs" / str(job.id)).exists()
    assert (workspace / "jobs" / str(protected.id) / "source.mkv").read_bytes() == b"source"
    assert all(Path(row.path).read_bytes() == b"source" for row in media)
