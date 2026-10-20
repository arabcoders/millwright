from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.models import FinalizationJournal, Job, JobAttempt
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_disabled_jobs_preserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, partial = tmp_path / "source.mkv", tmp_path / "output.partial"
    source.write_bytes(b"original")
    partial.write_bytes(b"recovery output")
    async with database_context("disabled-jobs") as factory:
        async with factory() as session:
            transcode = Job(media_file_id=1, library_id=1, status="finalizing", attempts=1)
            healthcheck = Job(media_file_id=2, library_id=1, kind="healthcheck", status="running", attempts=1)
            session.add_all([transcode, healthcheck])
            await session.flush()
            attempt = JobAttempt(job_id=transcode.id, attempt_number=1, status="finalizing")
            session.add(attempt)
            await session.flush()
            journal = FinalizationJournal(
                attempt_id=attempt.id,
                source_path=str(source),
                destination_path=str(tmp_path / "destination.mkv"),
                partial_path=str(partial),
                trash_path=str(tmp_path / "trash.mkv"),
                state="prepared",
            )
            session.add(journal)
            await session.commit()
            job_id, healthcheck_id, attempt_id, journal_id = transcode.id, healthcheck.id, attempt.id, journal.id
        app = create_app(factory, config=ServerConfig(disable_jobs=True))
        starts = []
        for name in ("hub", "scanner", "healthchecks", "staging", "notifications", "scheduler"):
            start = Mock()
            starts.append(start)
            monkeypatch.setattr(getattr(app.state, name), "start", start)
        for name in ("probes", "flow_enqueue", "library_deletion"):
            start = AsyncMock()
            starts.append(start)
            monkeypatch.setattr(getattr(app.state, name), "start", start)
        watching = Mock()
        starts.append(watching)
        monkeypatch.setattr(app.state.plugin_host, "start_watching", watching)
        async with app.router.lifespan_context(app):
            assert not app.state.services_running
            for start in starts:
                start.assert_not_called()
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/api/system")).status_code == 200
                assert (await client.get("/api/logs")).status_code == 200
                jobs = (await client.get("/api/jobs")).json()
                assert {item["status"] for item in jobs["items"]} == {"finalizing", "running"}
                assert (await client.post("/api/auth/ws-ticket")).status_code == 200
            async with factory() as session:
                statuses = {job.id: job.status for job in await session.scalars(select(Job))}
                assert statuses == {job_id: "finalizing", healthcheck_id: "running"}
                assert (
                    await session.execute(select(JobAttempt.status).where(JobAttempt.id == attempt_id))
                ).scalar_one() == "finalizing"
                assert (
                    await session.execute(select(FinalizationJournal.state).where(FinalizationJournal.id == journal_id))
                ).scalar_one() == "prepared"
            assert source.read_bytes() == b"original"
            assert partial.read_bytes() == b"recovery output"
        assert not app.state.services_running


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_disabled_jobs_cancel(bulk: bool) -> None:
    async with database_context("disabled-cancel") as factory:
        async with factory() as session:
            jobs = [
                Job(media_file_id=number, library_id=1, kind=kind, status=status)
                for number, (kind, status) in enumerate(
                    (
                        (kind, status)
                        for kind in ("transcode", "healthcheck")
                        for status in ("hold", "staging", "queued")
                    ),
                    start=1,
                )
            ]
            session.add_all(jobs)
            await session.commit()
            ids = [job.id for job in jobs]
        app = create_app(factory, config=ServerConfig(disable_jobs=True))
        async with app.router.lifespan_context(app):
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                if bulk:
                    response = await client.post("/api/jobs/bulk", json={"action": "cancel", "job_ids": ids})
                    assert response.status_code == 200
                    assert response.json()["succeeded"] == ids
                    assert response.json()["failed"] == []
                else:
                    for job_id in ids:
                        response = await client.post(f"/api/jobs/{job_id}/cancel")
                        assert response.status_code == 200
                        assert response.json() == {"id": job_id, "status": "cancelled"}
            async with factory() as session:
                statuses = list(await session.scalars(select(Job.status)))
                assert statuses == ["cancelled"] * len(ids)
            assert not app.state.services_running


@pytest.mark.asyncio
async def test_disabled_node_rejected() -> None:
    async with database_context("disabled-node") as factory:
        app = create_app(factory, config=ServerConfig(disable_jobs=True))
        receive = AsyncMock(return_value={"type": "websocket.connect"})
        send = AsyncMock()
        await app(
            {
                "type": "websocket",
                "path": "/api/hub",
                "headers": [],
                "query_string": b"",
                "scheme": "ws",
                "extensions": {"websocket.http.response": {}},
            },
            receive,
            send,
        )
        messages = [call.args[0] for call in send.call_args_list]
        assert [message["type"] for message in messages] == [
            "websocket.http.response.start",
            "websocket.http.response.body",
        ]
        assert messages[0]["status"] == 503
        assert not app.state.hub.entries
