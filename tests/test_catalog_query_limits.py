from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Request
from sqlalchemy import event, select

from common.fs.identity import source_identity
from server import backup, workspaces
from server.api import libraries as libraries_api
from server.settings import ScannerConfig
from server.flows.definition import FlowDefinition
from server.flows.store import save_flow
from server.fs.source import lock_job_source
from server.models import FinalizationJournal, Flow, Job, JobAttempt, JobEvent, Library, MediaFile
from server.queue import store
from server.scanner import scan
from server.staging import StagingService
from tests.helpers import database_context, graph_flow


def media_row(library: Library, path: Path) -> MediaFile:
    return MediaFile(
        library_id=library.id,
        path=str(path),
        size=1,
        mtime_ns=1,
        container="mkv",
        medium="other",
        last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_job_source_refresh(tmp_path: Path) -> None:
    async with database_context("job-source-refresh") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            media = media_row(library, tmp_path / "movie.mkv")
            session.add(media)
            await session.flush()
            job = Job(library_id=library.id, media_file_id=media.id, status="failed")
            session.add(job)
            await session.commit()
        async with factory() as session:
            stale_library = await session.get(Library, library.id)
            stale_media = await session.get(MediaFile, media.id)
            stale_job = await session.get(Job, job.id)
            assert stale_library is not None and stale_media is not None and stale_job is not None
            async with factory() as writer:
                current_library = await writer.get(Library, library.id)
                current_media = await writer.get(MediaFile, media.id)
                current_job = await writer.get(Job, job.id)
                assert current_library is not None and current_media is not None and current_job is not None
                current_library.suspended = True
                current_media.size = 2
                current_job.warning = "Changed."
                await writer.commit()
            locked = await lock_job_source(session, job.id)
            assert locked is not None and locked is stale_job
            assert stale_library.suspended and stale_media.size == 2 and locked.warning == "Changed."
        async with factory() as session:
            locked = await lock_job_source(session, job.id)
            assert locked is not None
            queries = []

            def queried(_connection, _cursor, statement, _parameters, _context, _many):
                queries.append(statement)

            connection = await session.connection()
            event.listen(connection.sync_connection, "before_cursor_execute", queried)
            refreshed_library = await session.get(Library, locked.library_id)
            refreshed_media = await session.get(MediaFile, locked.media_file_id)
            assert refreshed_library is not None and refreshed_media is not None
            assert refreshed_library.suspended and refreshed_media.size == 2
            assert not queries


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", [Library, MediaFile, Job, JobAttempt])
async def test_job_source_skips(tmp_path: Path, resource) -> None:
    async with database_context("job-source-skip") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            media = media_row(library, tmp_path / "movie.mkv")
            session.add(media)
            await session.flush()
            job = Job(library_id=library.id, media_file_id=media.id, status="failed", attempts=1)
            session.add(job)
            await session.flush()
            attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
            session.add(attempt)
            await session.commit()
        identifiers = {Library: library.id, MediaFile: media.id, Job: job.id, JobAttempt: attempt.id}
        async with factory() as holder, factory() as contender:
            await holder.get(resource, identifiers[resource], with_for_update=True)
            assert (
                await asyncio.wait_for(lock_job_source(contender, job.id, attempt_id=attempt.id, skip_locked=True), 2)
                is None
            )
            await holder.rollback()
            locked = await asyncio.wait_for(lock_job_source(contender, job.id, attempt_id=attempt.id), 2)
            assert locked is not None and locked.id == job.id


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_job_source_history(present) -> None:
    async with database_context("job-source-history") as factory, factory() as session:
        library = Library(name="library", roots=["/media"])
        if present:
            session.add(library)
            await session.flush()
        job = Job(library_id=library.id if present else 999, media_file_id=1000, status="failed")
        session.add(job)
        await session.commit()
        assert await lock_job_source(session, job.id) is job
        assert await lock_job_source(session, job.id, skip_locked=True) is None


@pytest.mark.asyncio
async def test_scan_request_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(libraries_api, "QUERY_BATCH", 2)
    async with database_context("scan-request-limit") as factory:
        async with factory() as session:
            first = Library(name="first", roots=[str(tmp_path)])
            suspended = Library(name="suspended", roots=[str(tmp_path)], suspended=True)
            last = Library(name="last", roots=[str(tmp_path)])
            session.add_all([first, suspended, last])
            await session.commit()
        queue = Mock(side_effect=["already_running", "queued"])
        request = Request(
            {
                "type": "http",
                "app": SimpleNamespace(
                    state=SimpleNamespace(db=factory, scanner=SimpleNamespace(paused=False, queue_scan=queue))
                ),
            }
        )
        ids = [last.id, *range(1000, 1065), suspended.id, first.id, last.id]
        body = libraries_api.BatchScanBody(library_ids=ids)
        result = await libraries_api.scan_libraries(body, request)
        assert [item["library_id"] for item in result["outcomes"]] == ids[:-1]
        assert result["outcomes"][0]["status"] == "already_running"
        assert all(item["status"] == "missing" for item in result["outcomes"][1:-2])
        assert [item["status"] for item in result["outcomes"][-2:]] == ["suspended", "queued"]
        assert [call.args for call in queue.call_args_list] == [(last.id, "manual"), (first.id, "manual")]


@pytest.mark.asyncio
async def test_bump_global_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "QUERY_BATCH", 2)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    async with database_context("bump-request-limit") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            jobs = []
            for index, (priority, age, status) in enumerate(
                [
                    (3, 2, "queued"),
                    (8, 3, "staging"),
                    (3, 1, "queued"),
                    (3, 1, "queued"),
                    (99, 0, "failed"),
                    (20, 0, "queued"),
                ]
            ):
                media = media_row(library, tmp_path / f"{index}.mkv")
                session.add(media)
                await session.flush()
                job = Job(
                    library_id=library.id,
                    media_file_id=media.id,
                    status=status,
                    priority=priority,
                    created_at=now + timedelta(seconds=age),
                )
                session.add(job)
                jobs.append(job)
            await session.commit()
        ids = [jobs[0].id, jobs[3].id, *range(1000, 1065), jobs[1].id, jobs[2].id, jobs[4].id, jobs[0].id]
        async with factory() as session:
            session.add(jobs[0])
            bumped = await store.bump_jobs(session, ids)
            assert [job.id for job in bumped] == [jobs[1].id, jobs[2].id, jobs[3].id, jobs[0].id]
            assert [job.priority for job in bumped] == [24, 23, 22, 21]
            await session.commit()
            failed = await session.get(Job, jobs[4].id)
            unselected = await session.get(Job, jobs[5].id)
            assert failed is not None and failed.priority == 99
            assert unselected is not None and unselected.priority == 20
            filtered, total = await store.list_jobs(session, statuses=["queued"] * 65 + ["unknown"] * 65)
            assert total == 4 and {job.status for job in filtered} == {"queued"}
            assert await store.list_jobs(session, statuses=["unknown"] * 65) == ([], 0)


@pytest.mark.asyncio
async def test_workspace_protection_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspaces, "QUERY_BATCH", 2)
    async with database_context("workspace-owner-limit") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(tmp_path)])
            session.add(library)
            await session.flush()
            jobs = []
            for index in range(6):
                media = media_row(library, tmp_path / f"{index}.mkv")
                session.add(media)
                await session.flush()
                job = Job(library_id=library.id, media_file_id=media.id, status="failed", attempts=2)
                session.add(job)
                await session.flush()
                session.add_all(
                    [
                        JobAttempt(
                            job_id=job.id, attempt_number=2, status="failed", result="failed", node_name="latest"
                        ),
                        JobAttempt(
                            job_id=job.id,
                            attempt_number=1,
                            status="failed",
                            result="interrupted" if index == 3 else "failed",
                            node_name="earlier",
                        ),
                    ]
                )
                if index == 1:
                    job.status = "running"
                elif index == 2:
                    session.add(JobEvent(job_id=job.id, type="staging_unresolved"))
                elif index == 4:
                    session.add(JobEvent(job_id=job.id, type="finalization_unprepared"))
                elif index == 5:
                    session.add(JobEvent(job_id=job.id, type="actions_pending"))
                jobs.append(job)
            await session.commit()
        ids = [job.id for job in jobs] + list(range(1000, 1065))
        async with factory() as session:
            owners = await workspaces.job_details(session, ids)
        assert set(owners) == {job.id for job in jobs}
        assert owners[jobs[0].id]["eligible"]
        assert owners[jobs[2].id]["eligible"]
        assert not owners[jobs[1].id]["eligible"]
        assert all(owners[jobs[index].id]["eligible"] for index in (3, 4, 5))
        assert not owners[jobs[3].id]["empty_only"]
        assert not owners[jobs[4].id]["empty_only"]
        assert [attempt["attempt_number"] for attempt in owners[jobs[3].id]["attempts"]] == [1, 2]
        assert owners[jobs[3].id]["node_name"] == "latest"
        disk = {"workspace_id": jobs[3].id, "file_count": 0, "error": None}
        assert workspaces.workspace_row(disk, owners)["eligible"]
        assert workspaces.workspace_row(disk | {"file_count": 1}, owners)["eligible"]
        orphan = workspaces.workspace_row(disk | {"workspace_id": 1000}, owners)
        assert orphan["eligible"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["libraries", "flows"])
@pytest.mark.parametrize("protection", ["active", "recovery", "idle"])
async def test_restore_scope_queries(tmp_path: Path, selected: str, protection: str) -> None:
    definition = FlowDefinition.model_validate(graph_flow("transcode.video"))
    async with database_context("restore-scope-limit") as factory:
        async with factory() as session:
            flow = await save_flow(session, "last", definition)
            libraries = [
                Library(name=f"library-{index}", roots=[str(tmp_path)], flow_id=flow.id) for index in range(65)
            ]
            session.add_all(libraries)
            await session.flush()
            unrelated = Library(name="unrelated", roots=[str(tmp_path)])
            session.add(unrelated)
            await session.flush()
            unrelated_media = media_row(unrelated, tmp_path / "unrelated.mkv")
            session.add(unrelated_media)
            await session.flush()
            session.add(Job(library_id=unrelated.id, media_file_id=unrelated_media.id, status="running"))
            media = media_row(libraries[-1], tmp_path / "movie.mkv")
            session.add(media)
            await session.flush()
            job = Job(
                library_id=libraries[-1].id,
                media_file_id=media.id,
                status="running" if protection == "active" else "failed",
            )
            session.add(job)
            await session.flush()
            if protection == "recovery":
                attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
                session.add(attempt)
                await session.flush()
                session.add(
                    FinalizationJournal(
                        attempt_id=attempt.id,
                        source_path=media.path,
                        destination_path=media.path,
                        partial_path=str(tmp_path / ".1.part"),
                        trash_path=str(tmp_path / ".1.discard"),
                        state="discard_prepared",
                    )
                )
            await session.commit()
        names = [f"new-{index}" for index in range(65)]
        if selected == "libraries":
            names.append(libraries[-1].name)
            data = {"libraries": [{"name": name, "roots": [str(tmp_path / "changed")]} for name in names]}
        else:
            names.append("last")
            data = {"flows": [{"name": name, "definition": graph_flow("transcode.video")} for name in names]}
        async with factory() as session:
            ended = []
            scopes = []

            def scoped_query(_connection, _cursor, statement, parameters, _context, _many):
                if "FOR UPDATE" in statement and " = ANY (" in statement:
                    table = _context.compiled.statement.get_final_froms()[0].name
                    scopes.append((table, tuple(len(values) for values in parameters.values())))

            def transaction_ended(_session, transaction):
                if transaction.parent is None:
                    ended.append(transaction)

            event.listen(session.sync_session, "after_transaction_end", transaction_ended)
            connection = await session.connection()
            event.listen(connection.sync_connection, "before_cursor_execute", scoped_query)
            if protection == "idle":
                await backup.restore_data(session, data, {selected: names, "replace": True})
                assert not ended and session.in_transaction()
                await session.commit()
            else:
                with pytest.raises(ValueError):
                    await backup.restore_data(session, data, {selected: names, "replace": True})
                assert not ended and session.in_transaction()
                await session.rollback()
            assert len(ended) == 1
            assert scopes == [
                ("libraries", (66, 0) if selected == "libraries" else (0, 66)),
                ("flows", (0,) if selected == "libraries" else (66,)),
                ("notification_targets", (0,)),
                ("plugin_states", (0,)),
                ("nodes", (0,)),
                ("webhook_registrations", (0,)),
            ]
        async with factory() as session:
            if selected == "libraries":
                current = await session.get(Library, libraries[-1].id)
                assert current is not None
                assert current.roots == [str(tmp_path / "changed" if protection == "idle" else tmp_path)]
                count = len(list(await session.scalars(select(Library))))
                assert count == (131 if protection == "idle" else 66)
            else:
                current = await session.get(Flow, flow.id)
                assert current is not None
                assert current.version == (2 if protection == "idle" else 1)
                assert len(list(await session.scalars(select(Flow)))) == (66 if protection == "idle" else 1)


@pytest.mark.asyncio
async def test_prune_excluded_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "COMMIT_BATCH", 2)
    root = tmp_path / "library"
    excluded = root / "workspace"
    unavailable = root / "unavailable"
    root.mkdir()
    async with database_context("scanner-prune-limit") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)], scan_id=1)
            other = Library(name="other", roots=[str(tmp_path / "other")])
            session.add_all([library, other])
            await session.flush()
            rows = [media_row(library, excluded / f"{index}.mkv") for index in range(65)]
            unavailable_row = media_row(library, unavailable / "missing.mkv")
            other_row = media_row(other, excluded / "other.mkv")
            session.add_all([*rows, unavailable_row, other_row])
            await session.flush()
            jobs = [Job(library_id=library.id, media_file_id=row.id, status="queued") for row in rows]
            session.add_all(jobs)
            await session.flush()
            attempts = [JobAttempt(job_id=job.id, attempt_number=1, status="failed") for job in jobs[2:]]
            session.add_all(attempts)
            await session.flush()
            session.add_all(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path=row.path,
                    destination_path=row.path,
                    partial_path=str(excluded / f".{attempt.id}.part"),
                    trash_path=str(excluded / f".{attempt.id}.discard"),
                    state="discard_prepared",
                )
                for attempt, row in zip(attempts, rows[2:], strict=True)
            )
            await session.commit()
        try:
            async with factory() as session:
                assert (
                    await scan._prune_missing(
                        session, library, 1, staging=staging, unavailable={unavailable}, excluded=(excluded,)
                    )
                    == 2
                )
            async with factory() as session:
                remaining = (await session.scalars(select(MediaFile).order_by(MediaFile.id))).all()
                assert [row.id for row in remaining] == [
                    *[row.id for row in rows[2:]],
                    unavailable_row.id,
                    other_row.id,
                ]
                assert all(row.transcode_decided for row in remaining[:-2])
                assert not remaining[-1].transcode_decided
                retained = await session.get(Job, jobs[-1].id)
                assert retained is not None
                assert retained.status == "cancelled"
                assert (await session.scalars(select(JobEvent).where(JobEvent.job_id == retained.id))).all()
        finally:
            await staging.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 9])
async def test_scan_staging_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    monkeypatch.setattr(scan, "COMMIT_BATCH", 16)
    root = tmp_path / "library"
    root.mkdir()
    sources = [root / f"movie-{index}.mkv" for index in range(count)]
    for source in sources:
        source.write_bytes(b"media")
    async with database_context("scanner-staging-id-limit") as factory:
        staging = StagingService(factory, tmp_path / "workspace", min_free_gb=1)
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.flush()
            media = [media_row(library, source) for source in sources]
            for row, source in zip(media, sources, strict=True):
                row.size = source_identity(source.stat())["size"]
                row.mtime_ns = source.stat().st_mtime_ns
                row.source_identity = {"size": 0}
            session.add_all(media)
            await session.flush()
            jobs = [Job(library_id=library.id, media_file_id=row.id, status="failed") for row in media]
            session.add_all(jobs)
            await session.commit()
        staging.active_jobs.update(job.id for job in jobs)
        staging.active_jobs.update(range(1000, 1065))
        try:
            async with factory() as session:
                library = await session.get(Library, library.id)
                assert library is not None
                report = await scan.scan_library(session, library, config=ScannerConfig(), staging=staging)
                assert report.updated == report.pruned == 0
                current = list(await session.scalars(select(MediaFile).where(MediaFile.library_id == library.id)))
                assert len(current) == count and all(row.source_identity == {"size": 0} for row in current)
        finally:
            staging.active_jobs.clear()
            await staging.aclose()
