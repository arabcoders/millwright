from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.fs.identity import source_identity
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.backup import encrypt
from server.core.auth import Principal, hash_token
from server.core.config import ServerConfig
from server.flows.batch import MAX_REASON, BatchDryRuns, BatchError, BatchReport, BatchRow
from server.flows.sidecars import sidecar_subtitles
from server.models import Flow, Job, Library, MediaFile, ProbeRow
from server.plugins.host import EvaluatedDecision, PluginHost
from tests.helpers import database_context, graph_flow

NOW = datetime(2026, 10, 7, tzinfo=UTC)
API_PRINCIPAL = Principal(user_id=None, username="API key")
RAW = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}], "format": {"duration": "10"}}
FLOW = graph_flow("transcode.video", "action.extract_subs")


@dataclass(kw_only=True)
class Env:
    db: async_sessionmaker[AsyncSession]
    app: FastAPI
    client: httpx.AsyncClient
    service: BatchDryRuns
    host: PluginHost
    root: Path
    library_id: int
    flow_id: int

    async def file(self, name: str, *, probe: str | None = json.dumps(RAW), library_id: int | None = None) -> int:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        identity = source_identity(path.stat())
        async with self.db() as db:
            file = MediaFile(
                library_id=library_id or self.library_id,
                path=str(path),
                size=identity["size"],
                mtime_ns=path.stat().st_mtime_ns,
                source_identity=identity,
                container="mkv",
                medium="movie",
                last_scanned_at=NOW,
            )
            db.add(file)
            await db.flush()
            if probe is not None:
                stored = ProbeRow(media_file_id=file.id, raw_json=probe)
                db.add(stored)
                await db.flush()
                file.probe_id = stored.id
            await db.commit()
            return file.id

    async def start(self, *, file_ids: list[int] | None = None) -> BatchReport:
        return await self.service.start(
            self.library_id if file_ids is None else None, self.flow_id, 1, API_PRINCIPAL, file_ids=file_ids
        )

    async def finished(self, report: BatchReport) -> None:
        assert report.task is not None
        await asyncio.wait_for(asyncio.shield(report.task), 5)

    @property
    def url(self) -> str:
        return f"/api/libraries/{self.library_id}/dry-runs"


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with database_context("library-preview") as db:
        root = tmp_path / "media"
        root.mkdir()
        host = PluginHost([Path(__file__).resolve().parents[1] / "plugins"])
        monkeypatch.setattr(host, "validate_inputs", AsyncMock(return_value=[]))

        async def decide(_plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
            return EvaluatedDecision(type="plan", plan={"container": "mkv"}, reason="Remux selected.")

        monkeypatch.setattr(host, "evaluate", decide)
        app = create_app(
            db,
            plugin_host=host,
            config=ServerConfig(workspace_dir=str(tmp_path / "workspace"), trash_dir=str(tmp_path / "trash")),
        )
        service: BatchDryRuns = app.state.dry_runs
        service.clock = lambda: NOW
        async with db() as session:
            library = Library(name="Library", roots=[str(root)], flow_id=None, suspended=True)
            flow = Flow(name="Preview", version=1, definition=json.dumps(FLOW))
            session.add_all([library, flow])
            await session.commit()
            library_id, flow_id = library.id, flow.id
        try:
            async with api_client(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
                yield Env(
                    db=db,
                    app=app,
                    client=client,
                    service=service,
                    host=host,
                    root=root,
                    library_id=library_id,
                    flow_id=flow_id,
                )
        finally:
            await service.aclose()
            await host.aclose()


@pytest.mark.asyncio
async def test_all_indexed_files(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.flows.batch.BATCH_SIZE", 2)
    ids = [await env.file(f"sub/{index}.mkv") for index in range(5)]
    async with env.db() as db:
        other = Library(name="Other", roots=[str(env.root / "other")])
        db.add(other)
        await db.commit()
        other_id = other.id
    await env.file("other/foreign.mkv", library_id=other_id)
    before = {path: path.read_bytes() for path in env.root.rglob("*.mkv")}
    response = await env.client.post(env.url, json={"flow_id": env.flow_id, "expected_version": 1})
    assert response.status_code == 202, response.text
    report = env.service.reports[response.json()["id"]]
    await env.finished(report)
    assert report.processed == len(ids) == len(report.rows)
    assert [row.file_id for row in report.rows] == ids
    assert report.counts == {"deferred": 5}
    assert all(row.deferred_output and not row.deferred_encoder for row in report.rows)
    assert report.rows[0].file == "sub/0.mkv"
    assert report.rows[0].root == str(env.root)
    assert report.inputs == [] and report.definition is None
    assert report.payload()["file_ids"] is None
    async with env.db() as db:
        library = await db.get(Library, env.library_id)
        assert library is not None and library.flow_id is None and library.suspended
        assert await db.scalar(select(func.count()).select_from(Job)) == 0
        files = (await db.scalars(select(MediaFile))).all()
        assert all(file.transcode_status == "pending" and not file.transcode_decided for file in files)
    assert {path: path.read_bytes() for path in before} == before
    page = await env.client.get(f"{env.url}/{report.id}?limit=2&offset=2")
    assert page.json()["pagination"] == {"limit": 2, "offset": 2, "total": 5}
    assert [row["file_id"] for row in page.json()["items"]] == ids[2:4]
    listing = await env.client.get(env.url)
    assert listing.json()[0]["id"] == report.id and listing.json()[0]["items"] == []


@pytest.mark.asyncio
async def test_page_size_limits(env: Env) -> None:
    report = await env.start()
    await env.finished(report)
    report.rows = [BatchRow(file_id=index, root="", file=f"{index}.mkv", status="skipped") for index in range(1100)]
    report.total = report.captured = report.processed = len(report.rows)
    report.counts["skipped"] = len(report.rows)
    for limit in (50, 1000):
        response = await env.client.get(f"{env.url}/{report.id}?limit={limit}&offset=50")
        assert response.status_code == 200
        page = response.json()
        assert page["pagination"] == {"limit": limit, "offset": 50, "total": 1100}
        assert [row["file_id"] for row in page["items"]] == list(range(50, 50 + limit))
    last = await env.client.get(f"{env.url}/{report.id}?limit=1000&offset=1000")
    assert len(last.json()["items"]) == 100
    for limit in (0, 1001):
        assert (await env.client.get(f"{env.url}/{report.id}?limit={limit}")).status_code == 422
    exported = await env.client.get(f"{env.url}/{report.id}/export.csv")
    assert len(list(csv.DictReader(io.StringIO(exported.text)))) == 1100


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_filters_before_paging(env: Env, selected: bool) -> None:
    file_id = await env.file("one.mkv")
    report = await env.start(file_ids=[file_id] if selected else None)
    await env.finished(report)
    report.rows = [
        BatchRow(file_id=index, root="", file=f"{index}.mkv", status=status)
        for index, status in enumerate(["skipped", "deferred", "matched", "error", "matched", "deferred"], start=1)
    ]
    report.total = report.captured = report.processed = len(report.rows)
    report.counts.clear()
    report.counts.update(row.status for row in report.rows)
    url = f"/api/files/dry-runs/{report.id}" if selected else f"{env.url}/{report.id}"
    response = await env.client.get(
        url, params=[("status", "matched"), ("status", "deferred"), ("limit", 2), ("offset", 1)]
    )
    assert response.status_code == 200
    payload = response.json()
    assert [row["file_id"] for row in payload["items"]] == [3, 5]
    assert payload["pagination"] == {"limit": 2, "offset": 1, "total": 4}
    assert payload["total"] == payload["processed"] == 6
    assert payload["counts"] == {"skipped": 1, "deferred": 2, "matched": 2, "error": 1}
    empty = (await env.client.get(url, params={"status": "vetoed"})).json()
    assert empty["items"] == [] and empty["pagination"]["total"] == 0
    unfiltered = (await env.client.get(url, params={"limit": 2, "offset": 1})).json()
    assert [row["file_id"] for row in unfiltered["items"]] == [2, 3]
    assert unfiltered["pagination"]["total"] == 6
    assert (await env.client.get(url, params={"status": "unknown"})).status_code == 422
    exported = await env.client.get(f"{url}/export.csv")
    assert len(list(csv.DictReader(io.StringIO(exported.text)))) == 6


@pytest.mark.asyncio
async def test_selected_mixed_snapshot(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.flows.batch.BATCH_SIZE", 1)
    first = await env.file("one.mkv")
    async with env.db() as db:
        library = await db.get(Library, env.library_id)
        assert library is not None
        library.processing_cutoff = {"at": "2099-01-01T00:00:00Z", "fields": ["created_at"], "match": "all"}
        other = Library(name="Other", roots=[str(env.root / "other")], suspended=True)
        db.add(other)
        await db.commit()
        other_id = other.id
    second = await env.file("other/two.mkv", library_id=other_id)
    outside = await env.file("outside.mkv", library_id=other_id)
    await env.file("unselected.mkv")
    before = {path: path.read_bytes() for path in env.root.rglob("*.mkv")}
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = env.service._snapshot
    contexts: list[tuple[str, str]] = []

    async def capture(report: BatchReport, session: AsyncSession) -> None:
        entered.set()
        await release.wait()
        await snapshot(report, session)

    async def decide(_plugin: str, ctx: dict, _inputs: dict) -> EvaluatedDecision:
        contexts.append((ctx["library"]["name"], ctx["original_filename"]))
        return EvaluatedDecision(type="plan", plan={"container": "mkv"})

    monkeypatch.setattr(env.service, "_snapshot", capture)
    monkeypatch.setattr(env.host, "evaluate", decide)
    try:
        response = await env.client.post(
            "/api/files/dry-runs",
            json={"flow_id": env.flow_id, "expected_version": 1, "file_ids": [second, outside, first, second]},
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["library_id"] is None and payload["library_name"] == ""
        assert payload["file_ids"] == [first, second, outside] and payload["total"] == 3
        report = env.service.reports[payload["id"]]
        await asyncio.wait_for(entered.wait(), 2)
        async with env.db() as db:
            await db.execute(update(Library).values(name=Library.name + " edited", roots=[str(env.root / "changed")]))
            await db.execute(update(Flow).where(Flow.id == env.flow_id).values(version=2, name="Edited"))
            await db.commit()
        await env.file("later.mkv")
        release.set()
        await env.finished(report)
        assert report.state == "completed" and report.snapshot_complete and report.processed == 3
        assert report.flow_name == "Preview" and report.flow_version == 1
        assert [row.file_id for row in report.rows] == [first, second, outside]
        assert contexts == [("Library", "one.mkv"), ("Other", "two.mkv")]
        assert [row.root for row in report.rows] == [str(env.root), str(env.root / "other"), ""]
        assert [row.status for row in report.rows] == ["deferred", "deferred", "unavailable"]
        page = await env.client.get(f"/api/files/dry-runs/{report.id}?limit=1&offset=1")
        assert page.status_code == 200 and page.json()["items"][0]["file_id"] == second
        exported = await env.client.get(f"/api/files/dry-runs/{report.id}/export.csv")
        assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
        assert len(list(csv.DictReader(io.StringIO(exported.text)))) == 3
        async with env.db() as db:
            assert await db.scalar(select(func.count()).select_from(Job)) == 0
            files = (await db.scalars(select(MediaFile))).all()
            assert all(file.transcode_status == "pending" and not file.transcode_decided for file in files)
        assert {path: path.read_bytes() for path in before} == before
    finally:
        release.set()


@pytest.mark.asyncio
async def test_selected_scope_isolation(env: Env) -> None:
    first, second = await env.file("one.mkv"), await env.file("two.mkv")
    library = await env.start()
    await env.finished(library)
    selected = await env.start(file_ids=[second, first, first])
    await env.finished(selected)
    subset = await env.start(file_ids=[first])
    await env.finished(subset)
    listing = await env.client.get(
        "/api/files/dry-runs", params=[("file_ids", second), ("file_ids", first), ("file_ids", first)]
    )
    assert listing.status_code == 200
    assert [report["id"] for report in listing.json()] == [selected.id]
    assert listing.json()[0]["items"] == [] and listing.json()[0]["file_ids"] == [first, second]
    assert [report["id"] for report in (await env.client.get(env.url)).json()] == [library.id]
    assert await env.service.list_reports(None, API_PRINCIPAL, file_ids=[second]) == []
    foreign = Principal(user_id=2, username="API key")
    assert await env.service.list_reports(None, foreign, file_ids=[first, second]) == []
    with pytest.raises(BatchError):
        await env.service.get(None, selected.id, foreign)
    for method, suffix in (("get", ""), ("post", "/cancel"), ("get", "/export.csv")):
        assert (await getattr(env.client, method)(f"/api/files/dry-runs/{library.id}{suffix}")).status_code == 404
        assert (await getattr(env.client, method)(f"{env.url}/{selected.id}{suffix}")).status_code == 404
    env.service.clock = lambda: NOW + timedelta(hours=2)
    assert (await env.client.get(f"/api/files/dry-runs/{selected.id}")).status_code == 404
    assert (await env.client.get("/api/files/dry-runs", params={"file_ids": first})).json() == []


@pytest.mark.asyncio
async def test_selected_invalid_ids(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.flows.batch.BATCH_SIZE", 1)
    file_id = await env.file("one.mkv")
    for ids in ([], [0], [-1], [True], [1.5], [str(file_id)], [None]):
        response = await env.client.post(
            "/api/files/dry-runs", json={"flow_id": env.flow_id, "expected_version": 1, "file_ids": ids}
        )
        assert response.status_code == 422, response.text
    missing = file_id + 100
    response = await env.client.post(
        "/api/files/dry-runs", json={"flow_id": env.flow_id, "expected_version": 1, "file_ids": [file_id, missing]}
    )
    assert response.status_code == 404
    for params in (None, {"file_ids": ""}, {"file_ids": 0}, {"file_ids": -1}, {"file_ids": "abc"}):
        assert (await env.client.get("/api/files/dry-runs", params=params)).status_code == 422
    assert env.service.active is None and env.service.reports == {}


@pytest.mark.asyncio
async def test_selected_version_capacity(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [await env.file("one.mkv"), await env.file("two.mkv")]
    for body, status in (
        ({"flow_id": env.flow_id, "file_ids": ids}, 422),
        ({"flow_id": env.flow_id, "expected_version": 2, "file_ids": ids}, 409),
        ({"flow_id": env.flow_id + 100, "expected_version": 1, "file_ids": ids}, 404),
    ):
        response = await env.client.post("/api/files/dry-runs", json=body)
        assert response.status_code == status, response.text
    monkeypatch.setattr("server.flows.batch.MAX_RETAINED_FILES", 1)
    response = await env.client.post(
        "/api/files/dry-runs", json={"flow_id": env.flow_id, "expected_version": 1, "file_ids": ids}
    )
    assert response.status_code == 413 and env.service.active is None and env.service.reports == {}
    report = await env.start(file_ids=[ids[0]])
    await env.finished(report)
    monkeypatch.setattr("server.flows.batch.MAX_REPORTS", 1)
    response = await env.client.post(
        "/api/files/dry-runs", json={"flow_id": env.flow_id, "expected_version": 1, "file_ids": [ids[0]]}
    )
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_selected_cancel_snapshot(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [await env.file("one.mkv"), await env.file("two.mkv")]
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = env.service._snapshot
    evaluation = AsyncMock()
    monkeypatch.setattr(env.host, "evaluate", evaluation)

    async def capture(report: BatchReport, session: AsyncSession) -> None:
        entered.set()
        await release.wait()
        await snapshot(report, session)

    monkeypatch.setattr(env.service, "_snapshot", capture)
    cancelling = None
    try:
        response = await env.client.post(
            "/api/files/dry-runs", json={"flow_id": env.flow_id, "expected_version": 1, "file_ids": ids}
        )
        assert response.status_code == 202
        report = env.service.reports[response.json()["id"]]
        await asyncio.wait_for(entered.wait(), 2)
        assert (await env.client.get(f"/api/files/dry-runs/{report.id}/export.csv")).status_code == 409
        for url, body in (
            (env.url, {"flow_id": env.flow_id, "expected_version": 1}),
            ("/api/files/dry-runs", {"flow_id": env.flow_id, "expected_version": 1, "file_ids": ids}),
        ):
            assert (await env.client.post(url, json=body)).status_code == 409
        env.service._request_cancel(report)
        cancelling = asyncio.create_task(env.client.post(f"/api/files/dry-runs/{report.id}/cancel"))
        assert env.service.active is report and report.state == "cancelling"
        release.set()
        response = await asyncio.wait_for(cancelling, 2)
        assert response.status_code == 200 and response.json()["state"] == "cancelled"
        assert report.snapshot_complete and report.captured == 2 and report.processed == 0
        assert [row.file_id for row in report.rows] == ids
        assert all(row.status == "not_evaluated" for row in report.rows)
        exported = await env.client.get(f"/api/files/dry-runs/{report.id}/export.csv")
        assert exported.status_code == 200 and len(list(csv.DictReader(io.StringIO(exported.text)))) == 2
        evaluation.assert_not_awaited()
        assert env.service.active is None
    finally:
        release.set()
        if cancelling is not None:
            await asyncio.wait_for(asyncio.gather(cancelling, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_selected_auth_endpoints(env: Env) -> None:
    file_id = await env.file("one.mkv")
    env.app.state.auth.config = env.app.state.auth.config.model_copy(update={"api_key": "test-key"})
    env.client.headers.pop("Authorization")
    body = {"flow_id": env.flow_id, "expected_version": 1, "file_ids": [file_id]}
    assert (await env.client.post("/api/files/dry-runs", json=body)).status_code == 401
    assert (await env.client.get("/api/files/dry-runs", params={"file_ids": file_id})).status_code == 401
    env.client.headers["Authorization"] = "Bearer test-key"
    response = await env.client.post("/api/files/dry-runs", json=body)
    assert response.status_code == 202
    report = env.service.reports[response.json()["id"]]
    await env.finished(report)
    assert report.state == "completed" and report.processed == 1
    env.client.headers.pop("Authorization")
    for method, suffix in (("get", ""), ("post", "/cancel"), ("get", "/export.csv")):
        assert (await getattr(env.client, method)(f"/api/files/dry-runs/{report.id}{suffix}")).status_code == 401
    with pytest.raises(BatchError):
        await env.service.get(None, report.id, Principal(user_id=999, username="other"))


@pytest.mark.asyncio
async def test_matched_explains_plan(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = "Encode video as HEVC."
    await env.file("one.mkv")
    definition = graph_flow("transcode.remux", "transcode.video")
    definition["edges"][0]["port"] = "skip"
    async with env.db() as db:
        await db.execute(update(Flow).where(Flow.id == env.flow_id).values(definition=json.dumps(definition)))
        await db.commit()

    async def decide(plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
        if plugin == "transcode.remux":
            return EvaluatedDecision(type="skip", reason="Container is already suitable.")
        return EvaluatedDecision(
            type="plan",
            plan={"video": {"encoder": "libx265"}},
            reason=reason,
        )

    monkeypatch.setattr(env.host, "evaluate", decide)
    report = await env.start()
    await env.finished(report)
    assert report.state == "completed" and report.processed == 1
    row = report.rows[0]
    assert row.status == "matched"
    assert row.reason == reason
    assert not row.deferred_output and not row.deferred_encoder
    exported = list(csv.DictReader(io.StringIO(env.service.export(report))))
    assert exported[0]["reason"] == row.reason


@pytest.mark.asyncio
async def test_version_and_capacity(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for body in ({"flow_id": env.flow_id}, {"flow_id": env.flow_id, "expected_version": 2}):
        response = await env.client.post(env.url, json=body)
        assert response.status_code == (422 if "expected_version" not in body else 409)
        assert env.service.active is None
    await env.file("one.mkv")
    await env.file("two.mkv")
    monkeypatch.setattr("server.flows.batch.MAX_RETAINED_FILES", 1)
    response = await env.client.post(env.url, json={"flow_id": env.flow_id, "expected_version": 1})
    assert response.status_code == 413
    assert env.service.reports == {} and env.service.active is None


@pytest.mark.asyncio
async def test_preparation_is_visible(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [await env.file(f"{index}.mkv") for index in range(3)]
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = env.service._snapshot

    async def capture(report: BatchReport, session: AsyncSession) -> None:
        entered.set()
        await release.wait()
        await snapshot(report, session)

    monkeypatch.setattr(env.service, "_snapshot", capture)
    validation = AsyncMock(return_value=[])
    monkeypatch.setattr("server.flows.batch.validate_definition", validation)
    cancelling = None
    try:
        response = await asyncio.wait_for(
            env.client.post(env.url, json={"flow_id": env.flow_id, "expected_version": 1}), 2
        )
        assert response.status_code == 202
        initial = response.json()
        assert initial["state"] == "preparing" and initial["total"] == 3
        assert initial["captured"] == 0 and not initial["snapshot_complete"]
        report = env.service.reports[initial["id"]]
        await asyncio.wait_for(entered.wait(), 2)
        assert (await env.client.get(env.url)).json()[0]["id"] == report.id
        reopened = (await env.client.get(f"{env.url}/{report.id}")).json()
        assert reopened["state"] == "preparing" and reopened["pagination"]["total"] == 3
        assert (await env.client.get(f"{env.url}/{report.id}/export.csv")).status_code == 409
        await env.file("later.mkv")
        env.service._request_cancel(report)
        cancelling = asyncio.create_task(env.client.post(f"{env.url}/{report.id}/cancel"))
        assert report.state == "cancelling" and env.service.active is report
        release.set()
        cancelled = await asyncio.wait_for(cancelling, 2)
        assert cancelled.status_code == 200
        assert cancelled.json()["snapshot_complete"] and cancelled.json()["captured"] == 3
        assert report.state == "cancelled" and report.processed == 0
        assert [row.file_id for row in report.rows] == ids
        assert all(row.status == "not_evaluated" for row in report.rows)
        assert len(list(csv.DictReader(io.StringIO(env.service.export(report))))) == 3
        validation.assert_not_awaited()
        assert env.service.active is None
    finally:
        release.set()
        if cancelling is not None:
            await asyncio.wait_for(asyncio.gather(cancelling, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["snapshot", "validation"])
async def test_preparation_failure_retained(env: Env, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    await env.file("one.mkv")
    entered, release = asyncio.Event(), asyncio.Event()

    async def snapshot(_report: BatchReport, _session: AsyncSession) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("Catalog read failed.")

    async def validate(_host, _definition):
        entered.set()
        await release.wait()
        return ["Plugin inputs are invalid."]

    if stage == "snapshot":
        monkeypatch.setattr(env.service, "_snapshot", snapshot)
    else:
        monkeypatch.setattr("server.flows.batch.validate_definition", validate)
    evaluation = AsyncMock()
    monkeypatch.setattr(env.host, "evaluate", evaluation)
    try:
        response = await asyncio.wait_for(
            env.client.post(env.url, json={"flow_id": env.flow_id, "expected_version": 1}), 2
        )
        assert response.status_code == 202
        report = env.service.reports[response.json()["id"]]
        await asyncio.wait_for(entered.wait(), 2)
        preparing = (await env.client.get(f"{env.url}/{report.id}")).json()
        assert preparing["state"] == "preparing" and preparing["total"] == 1
        assert preparing["captured"] == (0 if stage == "snapshot" else 1)
        release.set()
        await env.finished(report)
        retained = (await env.client.get(f"{env.url}/{report.id}")).json()
        assert retained["state"] == "failed" and retained["error"]
        assert retained["total"] == 1 and retained["processed"] == 0
        assert retained["snapshot_complete"] == (stage == "validation")
        assert retained["expires_at"] and report.definition is None and not report.inputs
        export = await env.client.get(f"{env.url}/{report.id}/export.csv")
        assert export.status_code == (200 if stage == "validation" else 409)
        if stage == "validation":
            rows = list(csv.DictReader(io.StringIO(export.text)))
            assert len(rows) == 1 and rows[0]["status"] == "not_evaluated" and rows[0]["complete"] == "False"
        assert (await env.client.get(env.url)).json()[0]["id"] == report.id
        assert env.service.active is None
        evaluation.assert_not_awaited()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_immediate_cancel_captures(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [await env.file(f"{index}.mkv") for index in range(2)]
    evaluation = AsyncMock()
    monkeypatch.setattr(env.host, "evaluate", evaluation)
    report = await env.start()
    assert not report.started
    await env.service.cancel(report)
    assert report.state == "cancelled" and report.snapshot_complete
    assert report.total == report.captured == len(report.rows) == 2
    assert [row.file_id for row in report.rows] == ids and env.service.active is None
    evaluation.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_definition_retained(env: Env) -> None:
    await env.file("one.mkv")
    async with env.db() as db:
        await db.execute(update(Flow).where(Flow.id == env.flow_id).values(definition="{broken"))
        await db.commit()
    response = await env.client.post(env.url, json={"flow_id": env.flow_id, "expected_version": 1})
    assert response.status_code == 202
    report = env.service.reports[response.json()["id"]]
    await env.finished(report)
    retained = (await env.client.get(f"{env.url}/{report.id}")).json()
    assert retained["state"] == "failed" and retained["error"]
    assert retained["snapshot_complete"] and retained["total"] == retained["captured"] == 1
    assert retained["items"][0]["status"] == "not_evaluated" and report.definition is None
    assert env.service.active is None


@pytest.mark.asyncio
async def test_file_outcomes(env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    await env.file("missing.mkv", probe=None)
    await env.file("invalid.mkv", probe="{broken")
    await env.file("error.mkv")
    await env.file("skip.mkv")
    await env.file("veto.mkv")
    await env.file("encoder.mkv")

    async def decide(_plugin: str, ctx: dict, _inputs: dict) -> EvaluatedDecision:
        name = ctx["original_filename"]
        if name == "error.mkv":
            raise RuntimeError("Decision failed.")
        if name == "skip.mkv":
            return EvaluatedDecision(type="skip", reason="Already suitable.")
        if name == "veto.mkv":
            return EvaluatedDecision(type="error", error="Cannot inspect.")
        return EvaluatedDecision(type="plan", plan={"video": {"encoder": "libx265", "encoder_priority": ["libx265"]}})

    monkeypatch.setattr(env.host, "evaluate", decide)
    report = await env.start()
    await env.finished(report)
    assert [row.status for row in report.rows] == [
        "unavailable",
        "unavailable",
        "error",
        "skipped",
        "error",
        "deferred",
    ]
    assert report.rows[3].reason == "Already suitable."
    assert report.rows[4].reason == "Cannot inspect."
    assert report.rows[5].deferred_encoder and report.rows[5].deferred_output
    assert report.state == "completed" and report.processed == 6
    record = next(record for record in caplog.records if record.name == "server.flows.batch")
    assert str(env.root / "invalid.mkv") in record.getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (PermissionError("sidecars denied"), "unavailable"),
        (RuntimeError(), "error"),
        (RuntimeError("x" * (MAX_REASON + 1)), "error"),
    ],
)
async def test_file_failure_details(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    status: str,
) -> None:
    await env.file("failed.mkv")
    await env.file("next.mkv")

    def inspect(path: str):
        if path == str(env.root / "failed.mkv"):
            raise failure
        return sidecar_subtitles(path)

    monkeypatch.setattr("server.flows.batch.sidecar_subtitles", inspect)
    report = await env.start()
    await env.finished(report)
    assert report.state == "completed" and report.processed == 2
    assert report.rows[0].status == status
    assert 0 < len(report.rows[0].reason) <= MAX_REASON
    assert report.rows[1].status == "deferred"
    assert report.counts == {status: 1, "deferred": 1}
    record = next(record for record in caplog.records if record.name == "server.flows.batch")
    assert str(env.root / "failed.mkv") in record.getMessage()
    if str(failure):
        assert str(failure) in getattr(record, "error")


@pytest.mark.asyncio
async def test_preview_uses_catalog(env: Env) -> None:
    id_ = await env.file("invalid.mkv")
    await env.file("valid.mkv")
    async with env.db() as db:
        await db.execute(update(MediaFile).where(MediaFile.id == id_).values(source_identity=None))
        await db.commit()
    report = await env.start()
    await env.finished(report)
    assert report.state == "completed" and report.processed == 2
    assert [row.status for row in report.rows] == ["deferred", "deferred"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "probe", "probe_date", "delete", "library"])
@pytest.mark.parametrize("selected", [False, True])
async def test_changed_input(env: Env, monkeypatch: pytest.MonkeyPatch, change: str, selected: bool) -> None:
    id_ = await env.file("one.mkv")
    entered, release = asyncio.Event(), asyncio.Event()

    async def decide(_plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
        entered.set()
        await release.wait()
        return EvaluatedDecision(type="plan", plan={"container": "mkv"})

    monkeypatch.setattr(env.host, "evaluate", decide)
    report = await env.start(file_ids=[id_] if selected else None)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with env.db() as db:
            file = await db.get(MediaFile, id_)
            assert file is not None
            probe = await db.get(ProbeRow, file.probe_id)
            assert probe is not None
            if change == "identity":
                file.source_identity = {"size": file.size + 1}
            elif change == "probe":
                probe.raw_json = json.dumps({**RAW, "format": {"duration": "20"}})
            elif change == "probe_date":
                probe.updated_at = NOW
            elif change == "delete":
                file.probe_id = None
                await db.flush()
                await db.delete(probe)
                await db.flush()
                await db.delete(file)
            elif change == "library":
                library = Library(name="Reassigned", roots=[str(env.root)])
                db.add(library)
                await db.flush()
                file.library_id = library.id
            await db.commit()
        release.set()
        await env.finished(report)
        assert report.rows[0].status == "unavailable"
        assert not report.rows[0].deferred_output and report.processed == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_snapshot_is_frozen(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.file("one.mkv")
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple[str, str, dict]] = []

    async def decide(_plugin: str, ctx: dict, inputs: dict) -> EvaluatedDecision:
        calls.append((ctx["library"]["name"], ctx["original_filename"], inputs))
        entered.set()
        await release.wait()
        return EvaluatedDecision(type="plan", plan={"container": "mkv"})

    monkeypatch.setattr(env.host, "evaluate", decide)
    report = await env.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await env.file("later.mkv")
        async with env.db() as db:
            library = await db.get(Library, env.library_id)
            flow = await db.get(Flow, env.flow_id)
            assert library is not None and flow is not None
            library.name, library.roots = "Renamed", [str(env.root / "changed")]
            flow.name, flow.version, flow.definition = (
                "Edited",
                2,
                json.dumps(graph_flow(("transcode.video", {"preset": "fast"}))),
            )
            await db.commit()
        release.set()
        await env.finished(report)
        assert report.flow_name == "Preview" and report.flow_version == 1
        assert report.library_name == "Library" and report.roots == [str(env.root)]
        assert len(report.rows) == 1 and calls == [("Library", "one.mkv", {})]
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_cancel_drains_slot(env: Env, monkeypatch: pytest.MonkeyPatch, selected: bool) -> None:
    ids = [await env.file("one.mkv"), await env.file("two.mkv")]
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def decide(_plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
        return EvaluatedDecision(type="skip")

    monkeypatch.setattr(env.host, "evaluate", decide)
    report = await env.start(file_ids=ids if selected else None)
    cancelling = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        cancelling = asyncio.create_task(env.service.cancel(report))
        await asyncio.wait_for(cleaning.wait(), 2)
        assert report.state == "cancelling" and env.service.active is report
        with pytest.raises(BatchError):
            await env.start()
        cancelling.cancel()
        assert report.task is not None and not report.task.done()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(cancelling, return_exceptions=True), 2)
        assert isinstance(results[0], asyncio.CancelledError)
        assert env.service.active is None and report.state == "cancelled"
        assert all(row.status == "not_evaluated" for row in report.rows)
        records = list(csv.DictReader(io.StringIO(env.service.export(report))))
        assert len(records) == 2 and all(row["complete"] == "False" for row in records)
        monkeypatch.setattr(env.host, "evaluate", AsyncMock(return_value=EvaluatedDecision(type="skip")))
        following = await env.start()
        await env.finished(following)
    finally:
        release.set()
        if cancelling is not None:
            await asyncio.gather(cancelling, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_lock_wait_timeout(env: Env, monkeypatch: pytest.MonkeyPatch, selected: bool) -> None:
    file_id = await env.file("one.mkv")
    state = env.host._plugins["transcode.video"]
    timeout = asyncio.Timeout(None)
    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        "server.flows.batch.asyncio.timeout", lambda duration: timeout if duration == 60 else original_timeout(duration)
    )

    async def decide(plugin: str, ctx: dict, inputs: dict) -> EvaluatedDecision:
        timeout.reschedule(asyncio.get_running_loop().time())
        return await PluginHost.evaluate(env.host, plugin, ctx, inputs)

    monkeypatch.setattr(env.host, "evaluate", decide)
    async with state.lock:
        report = await env.start(file_ids=[file_id] if selected else None)
        await env.finished(report)
    assert report.state == "completed" and report.rows[0].status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["revoked", "expired"])
@pytest.mark.parametrize("selected", [False, True])
async def test_session_cancels_work(env: Env, monkeypatch: pytest.MonkeyPatch, mode: str, selected: bool) -> None:
    file_id = await env.file("one.mkv")
    entered = asyncio.Event()

    async def decide(_plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
        entered.set()
        await asyncio.Event().wait()
        return EvaluatedDecision(type="skip")

    monkeypatch.setattr(env.host, "evaluate", decide)
    async with env.db() as db:
        row, token = await create_user_session(db, "operator", 1, now=NOW)
        await db.commit()
    principal = Principal(
        user_id=1,
        username="operator",
        session_hash=hash_token(token),
        session_expires_at=NOW + timedelta(days=1),
    )
    library_id = None if selected else env.library_id
    report = await env.service.start(library_id, env.flow_id, 1, principal, file_ids=[file_id] if selected else None)
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(BatchError):
        await env.service.get(library_id, report.id, API_PRINCIPAL)
    if mode == "revoked":
        env.app.state.auth.ws_tickets.revoke_session(row.token_hash)
    else:
        env.service.clock = lambda: NOW + timedelta(days=2)
        await env.service.purge()
    await env.finished(report)
    assert report.state == "cancelled" and env.service.active is None
    assert env.app.state.auth.ws_tickets._sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "failed"])
async def test_terminal_cancel_finishes(env: Env, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    entered, cleaning, release, requested = (asyncio.Event() for _ in range(4))
    async with env.db() as db:
        _, token = await create_user_session(db, "operator", 1, now=NOW)
        await db.commit()
    principal = Principal(
        user_id=1,
        username="operator",
        session_hash=hash_token(token),
        session_expires_at=NOW + timedelta(days=1),
    )

    async def watcher(_report, _principal, _revoked) -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def evaluate(_report) -> None:
        await entered.wait()
        if state == "failed":
            raise RuntimeError("Preview failed.")

    original_cancel = env.service._request_cancel

    def request_cancel(report: BatchReport) -> None:
        original_cancel(report)
        requested.set()

    monkeypatch.setattr(env.service, "_watch_session", watcher)
    monkeypatch.setattr(env.service, "_evaluate", evaluate)
    monkeypatch.setattr(env.service, "_request_cancel", request_cancel)
    report = await env.service.start(env.library_id, env.flow_id, 1, principal)
    cancelling = None
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert report.state == state and report.task is not None and not report.task.done()
        cancelling = asyncio.create_task(env.service.cancel(report))
        await asyncio.wait_for(requested.wait(), 2)
        assert report.state == state and not report.cancel_requested and report.task.cancelling() == 0
        assert env.service.active is report
        release.set()
        await asyncio.wait_for(cancelling, 2)
        assert report.state == state and report.finished_at and report.expires_at
        assert env.service.active is None and env.app.state.auth.ws_tickets._sessions == {}
    finally:
        release.set()
        if cancelling is not None:
            await asyncio.wait_for(asyncio.gather(cancelling, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_auth_endpoints(env: Env) -> None:
    env.app.state.auth.config = env.app.state.auth.config.model_copy(update={"api_key": "test-key"})
    env.client.headers.pop("Authorization")
    assert (await env.client.get(env.url)).status_code == 401
    env.client.headers["Authorization"] = "Bearer test-key"
    response = await env.client.post(env.url, json={"flow_id": env.flow_id, "expected_version": 1})
    assert response.status_code == 202
    report = env.service.reports[response.json()["id"]]
    await env.finished(report)
    assert report.state == "completed" and report.rows == []
    env.client.headers.pop("Authorization")
    for method, url in (
        ("get", f"{env.url}/{report.id}"),
        ("post", f"{env.url}/{report.id}/cancel"),
        ("get", f"{env.url}/{report.id}/export.csv"),
    ):
        assert (await getattr(env.client, method)(url)).status_code == 401
    with pytest.raises(BatchError):
        await env.service.get(env.library_id, report.id, Principal(user_id=999, username="other"))


@pytest.mark.asyncio
async def test_csv_and_expiry(env: Env) -> None:
    await env.file("=SUM(1,2).mkv")
    await env.file('sub/quoted,"name.mkv')
    report = await env.start()
    await env.finished(report)
    response = await env.client.get(f"{env.url}/{report.id}/export.csv")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["file"] == "'=SUM(1,2).mkv"
    assert rows[1]["file"] == 'sub/quoted,"name.mkv'
    assert rows[0]["deferred_output"] == "True" and rows[0]["flow_version"] == "1"
    for value in [
        "=cmd",
        "+cmd",
        "-cmd",
        "@cmd",
        "＝cmd",
        "＋cmd",
        "－cmd",
        "＠cmd",
        " =cmd",
        " \t＠cmd",
        "\tlabel",
        "\rlabel",
        "\nlabel",
        " label",
        "\u2003label",
        '=SUM("a,b",2)\nnext',
    ]:
        report.rows[0].root = report.rows[0].file = report.rows[0].reason = report.flow_name = report.error = value
        exported = list(csv.DictReader(io.StringIO(env.service.export(report))))
        assert all(exported[0][key] == "'" + value for key in ("root", "file", "reason", "flow", "run_error"))
    env.service.clock = lambda: NOW + timedelta(hours=2)
    assert (await env.client.get(f"{env.url}/{report.id}")).status_code == 404
    assert (await env.client.get(env.url)).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reset", "restore", "close"])
async def test_lifecycle_drains_work(env: Env, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    env.app.state.auth.config = env.app.state.auth.config.model_copy(update={"api_key": "test-key"})
    env.client.headers["Authorization"] = "Bearer test-key"
    await env.file("one.mkv")
    entered, completed = asyncio.Event(), asyncio.Event()

    async def decide(_plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            completed.set()
        return EvaluatedDecision(type="skip")

    monkeypatch.setattr(env.host, "evaluate", decide)
    report = await env.start()
    await asyncio.wait_for(entered.wait(), 2)

    async def reset(_request, _keep):
        assert report.task is not None and completed.is_set() and report.task.done()

    async def restore(_request, _data, _selected, _plugin_data, _outcome):
        assert report.task is not None and completed.is_set() and report.task.done()
        return {"restored": True}

    if operation == "reset":
        monkeypatch.setattr("server.api.backups._reset_locked", reset)
        response = await env.client.post("/api/backups/reset", json={"keep": []})
        assert response.status_code == 200
    elif operation == "restore":
        monkeypatch.setattr("server.api.backups._apply_locked", restore)
        monkeypatch.setattr("server.api.backups._selection", lambda *_: {"config": False, "plugins": []})
        payload = {
            "_type": "millwright-backup",
            "_version": 1,
            "webhooks": [],
            "flows": [],
            "libraries": [],
            "nodes": [],
            "notifications": [],
            "plugin_states": [],
            "uploaded_plugins": {},
            "config": None,
            "settings": {},
        }
        encrypted = encrypt(json.dumps(payload).encode(), "password")
        response = await env.client.post(
            "/api/backups/restore",
            files={"file": ("backup", encrypted)},
            data={"password": "password", "selection": "{}"},
        )
        assert response.status_code == 200, response.text
    else:
        await env.service.aclose()
        with pytest.raises(BatchError):
            await env.start()
    assert completed.is_set() and env.service.active is None
    assert report.state == "cancelled"


@pytest.mark.asyncio
async def test_stop_allows_preview(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.file("one.mkv")
    entered, release = asyncio.Event(), asyncio.Event()
    state = env.host._plugins["action.extract_subs"]
    state.runner = Mock(spec=asyncio.subprocess.Process)

    async def kill(_runner: asyncio.subprocess.Process) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr("server.plugins.host._kill_runner", kill)
    stopping = asyncio.create_task(env.host.stop("action.extract_subs"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert state.retiring and not env.host.unconfirmed_exit
        report = await env.start()
        await env.finished(report)
        assert report.state == "completed" and env.service.active is None
        following = await env.start()
        await env.finished(following)
        assert following.state == "completed"
    finally:
        release.set()
        await asyncio.wait_for(stopping, 2)
    assert state.runner is None and not env.host.unconfirmed_exit


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reset", "restore"])
async def test_unconfirmed_exit_checks(env: Env, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    env.app.state.auth.config = env.app.state.auth.config.model_copy(update={"api_key": "test-key"})
    env.client.headers["Authorization"] = "Bearer test-key"
    await env.file("one.mkv")
    entered = asyncio.Event()
    state = env.host._plugins["transcode.video"]
    state.runner = Mock(spec=asyncio.subprocess.Process)
    kill = AsyncMock(side_effect=RuntimeError("Runner exit is unconfirmed."))
    monkeypatch.setattr("server.plugins.host._kill_runner", kill)

    async def decide(_plugin: str, _ctx: dict, _inputs: dict) -> EvaluatedDecision:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            try:
                await env.host._stop_runner(state)
            except RuntimeError:
                pass
        return EvaluatedDecision(type="skip")

    monkeypatch.setattr(env.host, "evaluate", decide)
    mutation = AsyncMock()
    report = await env.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if operation == "reset":
            monkeypatch.setattr("server.api.backups._reset_locked", mutation)
            response = await env.client.post("/api/backups/reset", json={"keep": []})
        else:
            monkeypatch.setattr("server.api.backups._apply_locked", mutation)
            monkeypatch.setattr("server.api.backups._selection", lambda *_: {"config": False, "plugins": []})
            payload = {
                "_type": "millwright-backup",
                "_version": 1,
                "webhooks": [],
                "flows": [],
                "libraries": [],
                "nodes": [],
                "notifications": [],
                "plugin_states": [],
                "uploaded_plugins": {},
                "config": None,
                "settings": {},
            }
            response = await env.client.post(
                "/api/backups/restore",
                files={"file": ("backup", encrypt(json.dumps(payload).encode(), "password"))},
                data={"password": "password", "selection": "{}"},
            )
        assert response.status_code == 409
        assert report.task is not None and report.task.done()
        assert report.state == "failed" and env.service.active is report and env.host.unconfirmed_exit
        mutation.assert_not_awaited()
        with pytest.raises(BatchError) as conflict:
            await env.start()
        assert conflict.value.status == 409
        async with env.db() as db:
            assert await db.get(Library, env.library_id) is not None
            assert await db.get(Flow, env.flow_id) is not None
        kill.side_effect = None
        await env.host._stop_runner(state)
        assert not env.host.unconfirmed_exit
        async with env.service.pause():
            assert report.task.done() and env.service._paused == 1
        monkeypatch.setattr(env.host, "evaluate", AsyncMock(return_value=EvaluatedDecision(type="skip")))
        following = await env.start()
        await env.finished(following)
        assert following.state == "completed"
    finally:
        kill.side_effect = None
        await env.host._stop_runner(state)
