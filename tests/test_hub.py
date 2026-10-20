from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.actions import ActionOutcome, ExtractSubtitles, Notify, NotifyExternal, PreserveTimestamps
from common.journal import append_message, read_messages
from common.media.probe import ProbeData, probe_from_raw
from common.plan import FFmpegPlan
from common.plan.models import StreamPolicy, StreamSelection, VideoTarget
from common.protocol import (
    ActionReport,
    AttemptMissing,
    Cancel,
    Finished,
    HealthResult,
    Heartbeat,
    NodeCapabilities,
    NodeLog,
    NodeLogs,
    Progress,
    WorkspaceResponse,
    parse_message,
)
from common.enums import ExecutionLocation, HealthStatus, JobStatus
from common.workspace import DEFAULT_HARD_SIZE_CEILING, workspace_lock
from server.events import EventBus
from server.healthcheck import HealthcheckService
from server.metadata_jobs import enqueue_metadata_job
from server.probing import ProbeService
from server.stats import build_snapshot, savings
from server.finalization import FinalizationError, publish_artifacts, recover_incomplete
from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NodeRow,
    ProbeRow,
    NotificationDelivery,
)
from server.nodehub.hub import NodeEntry, NodeHub
import server.nodehub.hub as hub_module
from server.nodehub.store import get_node, hash_token, register_node
from server.fs.library import LibraryFS
from server.queue.store import (
    requeue,
    StaleJobError,
    _insert_job,
    cancel_pending,
    claim,
    enqueue_file_job,
    enqueue_healthcheck_job,
    get_job,
    mark_staged,
    processing_backlog,
    retry_job,
)

pytestmark = pytest.mark.usefixtures("mock_ffprobe", "mock_plugin_runners", "local_storage")


from server.fs.source import capture_source
from server.workspace import _stage_source, _stage_subtitle, recovery_jobs, source_identity
from server.retention import protected_history_jobs
from server.healthlogs import create_healthcheck_log
from tests.helpers import request_retry, database_context, fd_path, make_test_temp_dir
from tests.hub_helpers import Clock, FakeConn

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
RAW: dict = {
    "streams": [
        {"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1920, "height": 1080},
        {"index": 1, "codec_name": "ac3", "codec_type": "audio", "channels": 6, "tags": {"language": "eng"}},
    ],
    "format": {"duration": "60", "size": "2048"},
}
FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"


class FailingConn(FakeConn):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def send_json(self, data):
        raise self.error


@dataclass
class Env:
    factory: async_sessionmaker[AsyncSession]
    library: Path
    workspace: Path
    trash: Path
    clock: Clock


@dataclass
class PostActionCase:
    jid: int
    hub: NodeHub
    conn: FakeConn
    task: asyncio.Task
    offer: dict
    action: ExtractSubtitles
    probe: ProbeData


@asynccontextmanager
async def post_action_case(env, *, hub_kwargs=None, conn=None, succeed=True, job_kwargs=None):
    jid = await file_job(env, **(job_kwargs or {}))
    h = hub(env, **(hub_kwargs or {}))
    c = conn or FakeConn([hello()])
    task = await serve(h, c)
    try:
        await h.dispatch_once()
        offer = c.messages("job")[0]["job"]
        output = h.workspace_dir / offer["output_path"]
        output.write_bytes(b"output")
        action = ExtractSubtitles(formats=["srt"], languages=["eng"])
        probe = ProbeData.model_validate(
            {"file_path": str(env.library / "movie.mp4"), "duration_s": 60, "size_bytes": 6, "streams": []}
        )
        if succeed:
            published = Path(probe.file_path)
            published.write_bytes(b"output")
            async with env.factory() as session:
                current = await session.get(Job, jid)
                current.status = JobStatus.SUCCEEDED
                stored_attempt = await session.get(JobAttempt, offer["attempt_id"])
                stored_attempt.status = JobStatus.SUCCEEDED
                stored_attempt.output_probe = probe.model_dump(mode="json")
                stored_attempt.source_size = 6
                stored_attempt.output_size = 6
                library = await session.get(Library, current.library_id)
                media = await session.get(MediaFile, current.media_file_id)
                session.add(
                    FinalizationJournal(
                        attempt_id=offer["attempt_id"],
                        source_path=media.path,
                        destination_path=str(published),
                        partial_path=str(published.with_name(f".{offer['attempt_id']}.part")),
                        trash_path=str(published.with_name(f".{offer['attempt_id']}.discard")),
                        state="discard_completed",
                    )
                )
                session.add(
                    JobEvent(
                        job_id=jid,
                        attempt_id=offer["attempt_id"],
                        type="publication",
                        payload={
                            "library_id": library.id,
                            "roots": library.roots,
                            "preserve_timestamps": False,
                        },
                    )
                )
                await session.commit()
        yield PostActionCase(jid, h, c, task, offer, action, probe)
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 2)


def materialize_offer(hub, offer):
    output = hub.workspace_dir / offer["output_path"]
    output.write_bytes(b"output")
    (hub.workspace_dir / offer["log_path"]).write_bytes(b"log")
    return output


def stage_fixture(workspace, job_id, source):
    captured = capture_source(source)
    return _stage_source(workspace, job_id, source, expected_source=captured)


async def resolve_action(case, *, artifacts=(), ok=True, error=None):
    dispatch = asyncio.create_task(
        case.hub._dispatch_post_actions(
            case.hub.entries["node-a"],
            case.jid,
            case.offer["attempt_id"],
            case.offer["output_path"],
            case.probe,
            [case.action],
            [],
            [],
        )
    )
    await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
    await case.hub._handle_report(
        case.hub.entries["node-a"].node_db_id,
        ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[ActionOutcome(kind=case.action.kind, ok=ok, artifacts=list(artifacts), error=error)],
        ),
    )
    await asyncio.wait_for(dispatch, 2)


@pytest.mark.asyncio
async def test_foreign_action_report(env):
    async with env.factory() as session:
        await register_node(session, "node-b", "other-token")
        await session.commit()
    async with post_action_case(env) as case:
        output = materialize_offer(case.hub, case.offer)
        foreign = FakeConn([hello("node-b", "other-token")])
        connection = await serve(case.hub, foreign)
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[ActionOutcome(kind=case.action.kind, ok=True)],
        ).model_dump(mode="json")
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            assert not foreign.messages("action_task")
            pending = case.hub._action_reports[(case.jid, case.offer["attempt_id"])]
            foreign.receiving.clear()
            foreign.push(report)
            await asyncio.wait_for(foreign.receiving.wait(), 2)
            accepted = pending.done()
            if accepted:
                await asyncio.wait_for(dispatch, 2)
            retained = output.exists()
            case.conn.push(report)
            await asyncio.wait_for(dispatch, 2)
            assert not accepted and retained, f"foreign report accepted={accepted}; workspace retained={retained}"
        finally:
            case.conn.push(report)
            foreign.disconnect()
            await asyncio.wait_for(asyncio.gather(dispatch, connection, return_exceptions=True), 2)


@pytest_asyncio.fixture
async def env():
    async with database_context("hub") as factory:
        async with factory() as session:
            await register_node(session, "node-a", "tok")
            await session.commit()
        yield Env(
            factory,
            make_test_temp_dir("library"),
            make_test_temp_dir("workspace"),
            make_test_temp_dir("trash"),
            Clock(),
        )


@pytest_asyncio.fixture
async def writer_env(tmp_path: Path):
    library = tmp_path / "library"
    library.mkdir()
    async with database_context("hub-writer") as factory:
        async with factory() as session:
            await register_node(session, "node-a", "tok")
            await session.commit()
        yield Env(factory, library, tmp_path / "workspace", tmp_path / "trash", Clock())


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stat", "probe", "post"])
@pytest.mark.parametrize("supersede", [False, True])
async def test_finish_allows_writes(writer_env, monkeypatch, phase, supersede):
    from server.flows.engine import FlowResult

    env = writer_env
    jid = await file_job(env, plan=FFmpegPlan())
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    entered, release = asyncio.Event(), asyncio.Event()
    release_stat = threading.Event()
    heartbeated = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_probe = hub_module.run_ffprobe
    handle_heartbeat = h._handle_heartbeat

    async def heartbeat_handler(entry, msg):
        await handle_heartbeat(entry, msg)
        heartbeated.set()

    async def probe(*args):
        if phase == "probe":
            entered.set()
            await release.wait()
        return await original_probe(*args)

    async def post(*args):
        if phase == "post":
            entered.set()
            await release.wait()
        return FlowResult(status="vetoed", reason="Rejected test output."), [], None, [], []

    monkeypatch.setattr("server.nodehub.hub.run_ffprobe", probe)
    monkeypatch.setattr(h, "_post_gate", post)
    monkeypatch.setattr(h, "_handle_heartbeat", heartbeat_handler)
    finishing = None
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        output = materialize_offer(h, offer)
        stat = Path.stat

        def blocked_stat(path, *args, **kwargs):
            if phase == "stat" and path == output:
                loop.call_soon_threadsafe(entered.set)
                assert release_stat.wait(5)
            return stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", blocked_stat)
        finishing = asyncio.create_task(
            h._handle_finished(
                h.entries["node-a"],
                finished(
                    jid,
                    offer["attempt_id"],
                    offer["log_path"],
                    "succeeded",
                    output_path=offer["output_path"],
                    output_probe=ProbeData.model_validate(offer["probe"]).model_copy(
                        update={"file_path": offer["output_path"]}
                    ),
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        conn.push(Heartbeat(cpu_percent=41).model_dump(mode="json"))
        await asyncio.wait_for(heartbeated.wait(), 2)
        heartbeat_node = await node(env)
        assert heartbeat_node.usage is not None and heartbeat_node.usage["cpu_percent"] == 41
        async with env.factory() as writer:
            await asyncio.wait_for(writer.execute(update(Library).values(name="renamed")), 2)
            if supersede:
                await writer.execute(update(Job).where(Job.id == jid).values(status=JobStatus.CANCELLED.value))
                await writer.execute(
                    update(JobAttempt)
                    .where(JobAttempt.id == offer["attempt_id"])
                    .values(status=JobStatus.CANCELLED.value)
                )
            await writer.commit()
        release.set()
        release_stat.set()
        await asyncio.wait_for(finishing, 3)
        current = await job(env, jid)
        assert current.status == (JobStatus.CANCELLED if supersede else JobStatus.FAILED)
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not output.exists()
        if supersede:
            assert (await attempt(env, jid))[0].log_data == b"log"
    finally:
        release.set()
        release_stat.set()
        conn.disconnect()
        tasks = [connection, *([finishing] if finishing is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_dispatch_releases_writer(writer_env, monkeypatch):
    env = writer_env
    first = await file_job(env, name="first.avi")
    second = await file_job(env, name="second.avi")
    second_file = (await job(env, second)).media_file_id
    h, conn = hub(env), FakeConn([hello(encoders=("hevc_nvenc",))])
    connection = await serve(h, conn)
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncSession.get
    blocked = False

    async def get(session, entity, identifier, *args, **kwargs):
        nonlocal blocked
        if entity is MediaFile and identifier == second_file and not blocked:
            blocked = True
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        return await original(session, entity, identifier, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", get)
    dispatching = asyncio.create_task(h.dispatch_once())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with env.factory() as writer:
            await asyncio.wait_for(writer.execute(update(Library).values(name="renamed")), 2)
            await writer.commit()
        release.set()
        assert await asyncio.wait_for(dispatching, 3) == 0
        assert (await job(env, first)).warning is not None
        assert (await job(env, second)).warning is not None
    finally:
        release.set()
        conn.disconnect()
        dispatching.cancel()
        await asyncio.wait_for(asyncio.gather(dispatching, connection, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_heartbeat_batches_leases(writer_env):
    env = writer_env
    ids = [await file_job(env, name=f"movie-{index}.avi") for index in range(3)]
    async with env.factory() as session:
        current = await get_node(session, "node-a")
        assert current is not None
        current.transcode_limit = 3
        await session.commit()
    h, conn = hub(env), FakeConn([hello(transcodes=3)])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 3
        entry = h.entries["node-a"]
        async with env.factory() as session:
            await session.execute(update(Job).where(Job.id == ids[1]).values(status=JobStatus.CANCELLED.value))
            await session.commit()
        h._stall_cancelled.add(ids[2])
        env.clock.advance(10)
        updates = []

        def capture(_conn, _cursor, statement, _params, context, _many):
            if context.isupdate:
                updates.append(statement)

        engine = env.factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            await h._handle_heartbeat(entry, Heartbeat(cpu_percent=25))
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(updates) == 2
        assert set(entry.in_flight) == {ids[0], ids[2]}
        active = await job(env, ids[0])
        cancelled = await job(env, ids[1])
        stalled = await job(env, ids[2])
        assert active.lease_expires_at == env.clock() + timedelta(seconds=h.lease_ttl_s)
        assert cancelled.status == JobStatus.CANCELLED
        assert cancelled.lease_expires_at == stalled.lease_expires_at
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.VERIFYING, JobStatus.CANCELLED])
async def test_progress_checks_completion(writer_env, status):
    env = writer_env
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    progress_task = None
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        entry = h.entries["node-a"]
        entered = asyncio.Event()

        async def progress():
            entered.set()
            await h._handle_progress(entry, Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=50))

        async with env.factory() as owner:
            await owner.execute(update(Job).where(Job.id == jid).values(status=status.value))
            progress_task = asyncio.create_task(progress())
            await asyncio.wait_for(entered.wait(), 2)
            assert not progress_task.done()
            await owner.commit()
        await asyncio.wait_for(progress_task, 2)
        current = await job(env, jid)
        assert current.status == status and current.progress is None
        assert jid not in entry.last_progress_at
        assert "started" not in await events(env, jid)
    finally:
        conn.disconnect()
        tasks = [connection, *([progress_task] if progress_task is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("supersede", ["cancel", "attempt"])
async def test_dispatch_failure_checked(writer_env, monkeypatch, supersede):
    env = writer_env
    jid = await file_job(env)
    async with env.factory() as session:
        enqueued = await session.scalar(select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "enqueued"))
        enqueued.payload = {}
        await session.commit()
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h._fail_inspection

    async def fail_inspection(session, current, error, now):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(session, current, error, now)

    monkeypatch.setattr(h, "_fail_inspection", fail_inspection)
    dispatching = asyncio.create_task(h.dispatch_once())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with env.factory() as writer:
            values = {"status": JobStatus.CANCELLED.value} if supersede == "cancel" else {"attempts": 1}
            await writer.execute(update(Job).where(Job.id == jid).values(**values))
            if supersede == "attempt":
                writer.add(
                    JobAttempt(job_id=jid, attempt_number=1, status="failed", result="interrupted", error="requeued")
                )
            await writer.commit()
        release.set()
        assert await asyncio.wait_for(dispatching, 3) == 0
        current = await job(env, jid)
        assert current.status == (JobStatus.CANCELLED if supersede == "cancel" else JobStatus.QUEUED)
        assert current.error is None
        assert "failed" not in await events(env, jid)
    finally:
        release.set()
        conn.disconnect()
        dispatching.cancel()
        await asyncio.wait_for(asyncio.gather(dispatching, connection, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_report_shape_checked(env):
    async with post_action_case(env) as case:
        owner = case.hub.entries["node-a"]
        output = materialize_offer(case.hub, case.offer)
        replacement = FakeConn([hello()])
        replacement_task = None
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                owner,
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[ActionOutcome(kind="preserve_timestamps", ok=True)],
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            pending = case.hub._action_reports[(case.jid, case.offer["attempt_id"])]
            replacement_task = await serve(case.hub, replacement)
            await case.hub._handle_report(owner.node_db_id, report)
            assert not pending.done()
            replacement.receiving.clear()
            replacement.push(report.model_dump(mode="json"))
            await asyncio.wait_for(replacement.receiving.wait(), 2)
            assert not pending.done() and not dispatch.done()
            assert output.read_bytes() == b"output"
            replacement.push(
                ActionReport(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                ).model_dump(mode="json")
            )
            await asyncio.wait_for(dispatch, 2)
            assert not output.exists()
            assert not case.hub._action_reports and not case.hub._action_nodes
            assert "actions_finished" in await events(env, case.jid)
        finally:
            case.conn.disconnect()
            replacement.disconnect()
            if not dispatch.done():
                dispatch.cancel()
            tasks = [dispatch, case.task] + ([replacement_task] if replacement_task is not None else [])
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_report_stale_denied(env):
    async with post_action_case(env) as case:
        replacement = FakeConn([hello()])
        replacement_task = await serve(case.hub, replacement)
        owner = case.hub.entries["node-a"]
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))])
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                owner,
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: replacement.messages("action_task"), replacement.changed)
            pending = case.hub._action_reports[(case.jid, case.offer["attempt_id"])]
            case.conn.push(report.model_dump(mode="json"))
            await asyncio.wait_for(case.task, 2)
            assert not pending.done()
            replacement.push(report.model_dump(mode="json"))
            await asyncio.wait_for(dispatch, 2)
            assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
            assert not (env.workspace / f"jobs/{case.jid}").exists()
            assert not case.hub._action_reports and not case.hub._action_nodes
        finally:
            case.conn.disconnect()
            replacement.disconnect()
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, case.task, replacement_task, return_exceptions=True), 2)


def plan():
    return FFmpegPlan(video=VideoTarget(encoder="libx264"))


def hub(env, **kw):
    kw.setdefault("max_attempts", 1)
    kw.setdefault("min_free_gb", 1)
    return NodeHub(
        env.factory,
        workspace_dir=env.workspace,
        trash_dir=env.trash,
        ffprobe_bin=str(FAKE_FFPROBE),
        clock=env.clock,
        **kw,
    )


def hello(
    name="node-a", token="tok", encoders=("libx264",), tags=(), transcodes=1, active_attempts=(), mkvpropedit=False
):
    return {
        "v": 1,
        "type": "hello",
        "node_id": name,
        "token": token,
        "active_attempts": list(active_attempts),
        "capabilities": {
            "ffmpeg_version": "7",
            "encoders": list(encoders),
            "hwaccels": [],
            "worker_tags": list(tags),
            "max_transcodes": transcodes,
            "mkvpropedit": mkvpropedit,
        },
    }


async def wait_for(pred, event, timeout=3):
    async with asyncio.timeout(timeout):
        while not pred():
            event.clear()
            await event.wait()


async def serve(h, conn, *, enroll=False):
    task = asyncio.create_task(h.serve_connection(conn))
    await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
    answer_challenge(h, conn)
    if enroll:
        await wait_for(lambda: conn.messages("enrolled"), conn.changed)
        conn.push({"v": 1, "type": "enrollment_saved", "node_id": conn.messages("enrolled")[0]["node_id"]})
    await wait_for(lambda: conn.messages("welcome"), conn.changed)
    await asyncio.wait_for(conn.ready.wait(), 3)
    return task


def answer_challenge(h, conn):
    challenge = conn.messages("workspace_challenge")[0]
    response = h.workspace_dir / challenge["response_path"]
    response.parent.mkdir(parents=True, exist_ok=True)
    response.write_text(hashlib.sha256((h.workspace_dir / challenge["challenge_path"]).read_bytes()).hexdigest() + "\n")
    conn.push(
        WorkspaceResponse(
            challenge_id=challenge["challenge_id"],
            response_path=challenge["response_path"],
            workspace_ready=True,
            workspace_free_bytes=123456,
        ).model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_welcome_timezone(env) -> None:
    from zoneinfo import ZoneInfo

    timezone = "Asia/Kuwait"
    h = hub(env, timezone=ZoneInfo(timezone))
    conn = FakeConn([hello()])
    task = await serve(h, conn)
    try:
        assert conn.messages("welcome")[0]["timezone"] == timezone
        assert conn.messages("welcome")[0]["v"] == 1
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_finish_during_delivery(env, monkeypatch) -> None:
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    task = await serve(h, conn)
    original_send = conn.send_json

    async def send(data):
        await original_send(data)
        if data["type"] == "job":
            offer = data["job"]
            (env.workspace / offer["log_path"]).write_bytes(b"fast failure")
            await h._handle_finished(
                h.entries["node-a"],
                Finished(job_id=jid, attempt_id=offer["attempt_id"], result="failed", log_path=offer["log_path"]),
            )

    monkeypatch.setattr(conn, "send_json", send)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        async with env.factory() as session:
            job = await get_job(session, jid)
            assert job is not None and job.status == JobStatus.FAILED
            assert not list(
                (await session.execute(select(JobEvent).where(JobEvent.type == "foreign_finished"))).scalars()
            )
        assert jid not in h.entries["node-a"].in_flight
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
@pytest.mark.parametrize("result", ["failed", "cancelled", "stalled"])
async def test_delivery_report_finishes(env, monkeypatch, failure, result):
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    send = conn.send_json

    async def delivered(data):
        await send(data)
        if data["type"] == "job":
            offer = data["job"]
            materialize_offer(h, offer)
            await h._handle_finished(h.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"], result))
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("send failed after report completion")

    monkeypatch.setattr(conn, "send_json", delivered)
    try:
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(h.dispatch_once(), 3)
        else:
            assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        current = await job(env, jid)
        expected = JobStatus.FAILED if result == "stalled" else result
        assert current.status == expected and current.attempts == 1
        original = (await attempt(env, jid))[0]
        assert original.result == result and original.log_data == b"log"
        assert jid not in h.entries["node-a"].in_flight
        assert not (env.workspace / original.output_path).exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert "foreign_finished" not in await events(env, jid)
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_delivery_cancellation(env, monkeypatch) -> None:
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(_data):
        offer = _data["job"]
        materialize_offer(h, offer)
        started.set()
        await asyncio.wait_for(release.wait(), 3)

    monkeypatch.setattr(conn, "send_json", blocked)
    dispatch = asyncio.create_task(h.dispatch_once())
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with env.factory() as session:
            job = await get_job(session, jid)
            assert job is not None and job.status == JobStatus.CLAIMED
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(dispatch, 3)
        assert jid in h.entries["node-a"].in_flight
        async with env.factory() as session:
            job = await get_job(session, jid)
            assert job is not None and job.status == JobStatus.CLAIMED
        assert (env.workspace / f"jobs/{jid}/attempts/1/output.mkv").read_bytes() == b"output"
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        assert jid in h.entries["node-a"].in_flight
        async with env.factory() as session:
            retained = await get_job(session, jid)
            assert retained is not None and retained.status == JobStatus.CLAIMED
        env.clock.advance(h.lease_ttl_s + 1)
        await h.sweep_once()
        async with env.factory() as session:
            job = await get_job(session, jid)
            current = await session.scalar(select(JobAttempt).where(JobAttempt.job_id == jid))
            assert job is not None and job.status == JobStatus.FAILED
            assert current is not None and current.result == "failed" and current.log_data == b"log"
            assert current.error is not None
            assert not (env.workspace / f"jobs/{jid}").exists()
    finally:
        release.set()
        if not dispatch.done():
            dispatch.cancel()
        await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_ambiguous_delivery_checked(env, monkeypatch, failure):
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    original_send = conn.send_json

    async def delivered(data):
        await original_send(data)
        if data["type"] == "job":
            materialize_offer(h, data["job"])
        if data["type"] == "job" and len(conn.messages("job")) == 1:
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("delivery acknowledgement lost")

    monkeypatch.setattr(conn, "send_json", delivered)
    try:
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(h.dispatch_once(), 3)
        else:
            await asyncio.wait_for(h.dispatch_once(), 3)
        original = conn.messages("job")[0]["job"]
        async with env.factory() as session:
            await mark_staged(session, jid, now=env.clock())
            await session.commit()
        await asyncio.wait_for(h.dispatch_once(), 3)
        assert (env.workspace / original["output_path"]).read_bytes() == b"output"
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert len(conn.messages("job")) == 1, "a live worker received a retry before its earlier task finished"
        assert jid in h.entries["node-a"].in_flight, "ambiguous delivery released live execution assignment"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["finish", "disconnect", "reconnect"])
async def test_ambiguous_attempt_finishes(env, monkeypatch, completion):
    jid = await file_job(env)
    h, conn = hub(env, lease_ttl_s=30, stall_timeout_s=1), FakeConn([hello()])
    connection = await serve(h, conn)
    send = conn.send_json
    replacement = None
    replacement_connection = None

    async def delivered(data):
        await send(data)
        if data["type"] == "job":
            materialize_offer(h, data["job"])
            raise OSError("send failed after delivery")

    monkeypatch.setattr(conn, "send_json", delivered)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        offer = conn.messages("job")[0]["job"]
        owner = h.entries["node-a"]
        async with env.factory() as session:
            failure = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "offer_failed")
            )
            assert failure.attempt_id == offer["attempt_id"]
            assert failure.payload["error"]
            assert failure.payload["node"] == owner.name and failure.payload["node_id"] == owner.node_db_id
            assert failure.payload["media_path"] == str(env.library / "movie.avi")
            assert failure.payload["workspace"] == str(env.workspace)
            assert failure.payload["output_path"] == offer["output_path"]
        before = await job(env, jid)
        env.clock.advance(0.5)
        await h._handle_heartbeat(owner, Heartbeat())
        await h._handle_progress(owner, Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=10))
        running = await job(env, jid)
        assert running.status == JobStatus.RUNNING and running.lease_expires_at > before.lease_expires_at
        env.clock.advance(2)
        await h.sweep_once()
        assert (await job(env, jid)).status == JobStatus.RUNNING
        assert conn.messages("cancel")[0]["attempt_id"] == offer["attempt_id"]
        assert jid in owner.in_flight and (env.workspace / offer["output_path"]).exists()
        if completion == "finish":
            await h._handle_finished(owner, finished(jid, offer["attempt_id"], offer["log_path"], "cancelled"))
            assert (await job(env, jid)).status == JobStatus.CANCELLED and jid not in owner.in_flight
            assert not (env.workspace / offer["output_path"]).exists()
        else:
            if completion == "reconnect":
                replacement = FakeConn([hello(active_attempts=[{"job_id": jid, "attempt_id": offer["attempt_id"]}])])
                replacement_connection = asyncio.create_task(h.serve_connection(replacement))
                await wait_for(lambda: replacement.messages("workspace_challenge"), replacement.changed)
                answer_challenge(h, replacement)
                await wait_for(lambda: replacement.messages("set_limits"), replacement.changed)
                current = h.entries["node-a"]
                assert jid in current.in_flight
                await h._handle_progress(current, Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=90))
                assert (await job(env, jid)).status == JobStatus.RUNNING
            conn.disconnect()
            await asyncio.wait_for(connection, 3)
            if replacement is not None:
                replacement.disconnect()
                assert replacement_connection is not None
                await asyncio.wait_for(replacement_connection, 3)
            assert (await job(env, jid)).status == JobStatus.RUNNING
            assert (env.workspace / offer["output_path"]).read_bytes() == b"output"
            env.clock.advance(h.lease_ttl_s + 1)
            await h.sweep_once()
            assert (await job(env, jid)).status == JobStatus.FAILED
            original = (await attempt(env, jid))[0]
            assert original.id == offer["attempt_id"] and original.result == "failed"
            assert original.log_data == b"log"
            assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        if replacement is not None:
            replacement.disconnect()
        if replacement_connection is not None:
            await asyncio.wait_for(replacement_connection, 3)


@pytest.mark.asyncio
async def test_challenge_revocation_checked(env):
    from server.backup import restore_data

    h, conn = hub(env), FakeConn([hello()])
    connection = asyncio.create_task(h.serve_connection(conn))
    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        async with env.factory() as session:
            await restore_data(
                session,
                {
                    "nodes": [
                        {
                            "name": "node-a",
                            "display_name": "node-a",
                            "token_hash": hash_token("replacement-token"),
                            "paused": False,
                            "transcode_limit": 1,
                            "healthcheck_limit": 1,
                            "mkv_stats_limit": 1,
                        }
                    ]
                },
                {"nodes": ["node-a"], "replace": True},
            )
            await session.commit()
        answer_challenge(h, conn)
        await wait_for(lambda: conn.messages("welcome") or conn.messages("error") or connection.done(), conn.changed)
        assert not conn.messages("welcome"), "revoked credentials queued a connection after workspace verification"
        assert "node-a" not in h.entries
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_removed_challenge_refused(env, replace):
    from server.queue.store import remove_node

    h, conn = hub(env), FakeConn([hello()])
    connection = asyncio.create_task(h.serve_connection(conn))
    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        async with env.factory() as session:
            original = await get_node(session, "node-a")
            assert original is not None
            assert await remove_node(session, original.id)
            await session.commit()
            if replace:
                await register_node(session, "node-a", "replacement-token")
                await session.commit()
        answer_challenge(h, conn)
        await asyncio.wait_for(connection, 3)
        assert not conn.messages("welcome") and not h.entries
        assert conn.messages("error")[0]["code"] == "auth" and conn.closed == 4401
        async with env.factory() as session:
            stored = await get_node(session, "node-a")
            if replace:
                assert stored is not None and stored.token_hash == hash_token("replacement-token")
                assert stored.state == "offline" and not stored.workspace_ready
            else:
                assert stored is None
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "capacity"])
async def test_response_failure_cleans(env, monkeypatch, failure):
    from common.protocol import WorkspaceChallenge
    from node import agent as agent_module
    from node.agent import Agent, AgentConfig

    h, conn = hub(env), FakeConn([hello()])
    connection = asyncio.create_task(h.serve_connection(conn))
    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        challenge = WorkspaceChallenge.model_validate(conn.messages("workspace_challenge")[0])
        directory = (env.workspace / challenge.challenge_path).parent
        neighbor = env.workspace / "unrelated.log"
        neighbor.write_bytes(b"retained event")
        agent = Agent(
            AgentConfig(
                node_id="node-a", token="tok", server_url="ws://hub", workspace_dir=str(env.workspace), min_free_gb=1
            )
        )

        def failed(*_args, **_kwargs):
            raise OSError("workspace response failed")

        with monkeypatch.context() as patch:
            if failure == "response":
                patch.setattr(Path, "write_text", failed)
            else:
                patch.setattr(agent_module.shutil, "disk_usage", failed)
            response = agent._workspace_response(challenge)
        assert not response["workspace_ready"]
        conn.push(response)
        await asyncio.wait_for(connection, 3)
        assert not h.entries and not conn.messages("welcome")
        assert conn.messages("error")[0]["code"] == "workspace"
        assert not directory.exists() and neighbor.read_bytes() == b"retained event"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connection", "identity"])
async def test_restore_checks_connection(env, monkeypatch, change):
    from server import backup
    from server.queue.store import remove_node

    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    cached, release = asyncio.Event(), asyncio.Event()
    connection = asyncio.create_task(h.serve_connection(conn))
    restoring = None
    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        async with env.factory() as session:
            execute = AsyncSession.execute

            async def blocked_lock(current: AsyncSession, statement, *args, **kwargs):
                if (
                    current is session
                    and statement.is_select
                    and statement.column_descriptions[0].get("entity") is NodeRow
                    and statement._for_update_arg is not None
                ):
                    cached.set()
                    await asyncio.wait_for(release.wait(), 5)
                return await execute(current, statement, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "execute", blocked_lock)

            async def restore():
                try:
                    await backup.restore_data(
                        session,
                        {
                            "nodes": [
                                {
                                    "name": "node-a",
                                    "display_name": "node-a",
                                    "token_hash": hash_token("replacement-token"),
                                    "paused": False,
                                    "transcode_limit": 1,
                                    "healthcheck_limit": 1,
                                    "mkv_stats_limit": 1,
                                }
                            ]
                        },
                        {"nodes": ["node-a"], "replace": True},
                    )
                    await session.commit()
                    return True
                except ValueError:
                    await session.rollback()
                    return False

            restoring = asyncio.create_task(restore())
            try:
                await asyncio.wait_for(cached.wait(), 2)
                if change == "connection":
                    answer_challenge(h, conn)
                    await asyncio.wait_for(conn.ready.wait(), 2)
                    assert await asyncio.wait_for(h.dispatch_once(), 2) == 1
                    offer = conn.messages("job")[0]["job"]
                    output = materialize_offer(h, offer)
                else:
                    async with env.factory() as other:
                        original = await get_node(other, "node-a")
                        assert original is not None
                        assert await remove_node(other, original.id)
                        await other.commit()
                        replacement = await register_node(other, "node-new", "new-token")
                        replacement_id = replacement.id
                        await other.commit()
                release.set()
                restored = await asyncio.wait_for(restoring, 2)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(restoring, return_exceptions=True), 3)
        async with env.factory() as session:
            if change == "identity":
                remaining = await get_node(session, "node-new")
                assert remaining is not None and remaining.id == replacement_id
                assert remaining.token_hash == hash_token("new-token"), (
                    "restoring node-a credentials overwrote the credentials of unrelated node-new"
                )
                return
            stored = await get_node(session, "node-a")
            assert stored is not None
            if not restored:
                assert stored.token_hash == hash_token("tok")
                assert stored.state == "online" and output.exists()
                return
            assert stored.token_hash == hash_token("replacement-token")
        await h._handle_finished(h.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"], "failed"))
        current = await job(env, jid)
        assert current.status == JobStatus.CLAIMED and output.exists(), (
            f"revoked connection finished job={current.status}; live worker output retained={output.exists()}"
        )
    finally:
        release.set()
        if restoring is not None:
            await asyncio.wait_for(asyncio.gather(restoring, return_exceptions=True), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_callers", [False, True])
async def test_close_drains_completion(env, monkeypatch, cancel_callers):
    from server.workspace import post_action_jobs

    started, release = asyncio.Event(), asyncio.Event()
    async with post_action_case(env) as case:
        (env.workspace / case.offer["log_path"]).write_bytes(b"action log")

        async def blocked(*args, **kwargs):
            started.set()
            await release.wait()
            return await publish_artifacts(*args, **kwargs)

        monkeypatch.setattr("server.nodehub.hub.publish_artifacts", blocked)
        case.hub._spawn_action_dispatch(
            case.hub.entries["node-a"],
            case.jid,
            case.offer["output_path"],
            case.probe,
            [case.action],
            case.offer["attempt_id"],
            [],
            [],
        )
        dispatch = next(iter(case.hub._action_waits))
        closers = []
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            await case.hub._handle_report(
                case.hub.entries["node-a"].node_db_id,
                ActionReport(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    results=[ActionOutcome(kind=case.action.kind, ok=True)],
                ),
            )
            await asyncio.wait_for(started.wait(), 2)
            for _ in range(2):
                closers.append(asyncio.create_task(case.hub.aclose()))
                checkpoint = asyncio.Event()
                asyncio.get_running_loop().call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 2)
            async with env.factory() as session:
                assert await session.scalar(post_action_jobs().where(Job.id == case.jid)) == case.jid
            assert not dispatch.done() and (env.workspace / case.offer["output_path"]).exists()
            assert not any(task.done() for task in closers), "hub shutdown returned before accepted completion drained"
            assert dispatch in case.hub._action_waits
            case.hub.start()
            assert not case.hub._tasks
            if cancel_callers:
                for caller in (closers[0], closers[0], closers[1]):
                    caller.cancel()
                    checkpoint = asyncio.Event()
                    asyncio.get_running_loop().call_soon(checkpoint.set)
                    await asyncio.wait_for(checkpoint.wait(), 2)
                    assert not any(task.done() for task in closers)
                    assert dispatch in case.hub._action_waits and not dispatch.done()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*closers, dispatch, return_exceptions=True), 3)
            if cancel_callers:
                assert all(isinstance(result, asyncio.CancelledError) for result in results[:2])
            else:
                assert results[:2] == [None, None]
            assert "actions_finished" in await events(env, case.jid)
            assert not (env.workspace / case.offer["output_path"]).exists()
            await asyncio.wait_for(case.hub.aclose(), 2)
            case.hub.start()
            assert not case.hub._tasks and not case.hub._action_waits
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*closers, dispatch, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", ["draining", "drained"])
async def test_close_checks_completions(env, monkeypatch, workers):
    from server.workspace import post_action_jobs, recovery_jobs

    jid = await file_job(env, post_actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])])
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    finishing, release_finish, draining, release_loop, heartbeated = (asyncio.Event() for _ in range(5))
    close = None
    publish = h._publish_status
    handle_heartbeat = h._handle_heartbeat

    async def heartbeat_handler(entry, msg):
        await handle_heartbeat(entry, msg)
        heartbeated.set()

    async def blocked_publish(job):
        finishing.set()
        await release_finish.wait()
        await publish(job)

    async def loop():
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release_loop.wait()

    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
        monkeypatch.setattr(h, "_publish_status", blocked_publish)
        monkeypatch.setattr(h, "_handle_heartbeat", heartbeat_handler)
        conn.push(
            finished(
                jid,
                offer["attempt_id"],
                offer["log_path"],
                "succeeded",
                output_path=offer["output_path"],
                output_probe=ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[]),
            ).model_dump(mode="json")
        )
        await asyncio.wait_for(finishing.wait(), 3)
        completion = next(iter(h._completions.values()))
        conn.push(Heartbeat(cpu_percent=41).model_dump(mode="json"))
        await asyncio.wait_for(heartbeated.wait(), 2)
        assert not completion.done()
        async with env.factory() as session:
            node = await get_node(session, "node-a")
            assert node is not None and node.usage is not None
            assert node.usage["cpu_percent"] == 41
        monkeypatch.setattr(h, "_dispatch_loop", loop)
        monkeypatch.setattr(h, "_sweep_loop", loop)
        h.start()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        captured = list(h._tasks)
        close = asyncio.create_task(h.aclose())
        await asyncio.wait_for(draining.wait(), 2)
        h.start()
        assert h._tasks == captured
        if workers == "drained":
            release_loop.set()
            await asyncio.wait_for(asyncio.gather(*captured, return_exceptions=True), 2)
            h.start()
            assert h._tasks == captured
        assert not close.done() and not completion.done(), "hub shutdown returned before accepted completion drained"
        assert jid in h._finishing and jid in h.staging.busy_jobs
        assert (env.workspace / offer["output_path"]).exists()
        release_finish.set()
        await asyncio.wait_for(asyncio.shield(completion), 3)
        release_loop.set()
        await asyncio.wait_for(close, 3)
        h.start()
        assert not h._tasks and not h._completions and not h._finishing
        assert jid not in h.staging.busy_jobs and not conn.messages("action_task")
        assert not h._action_waits and not h._action_reports and not h._action_nodes
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        async with env.factory() as session:
            pending = await session.scalar(
                select(JobEvent).where(
                    JobEvent.job_id == jid,
                    JobEvent.attempt_id == offer["attempt_id"],
                    JobEvent.type == "actions_pending",
                )
            )
            assert pending is not None
            assert pending.payload["actions"] == [
                ExtractSubtitles(formats=["srt"], languages=["eng"]).model_dump(mode="json")
            ]
            assert await session.scalar(post_action_jobs().where(Job.id == jid)) is None
            assert await session.scalar(recovery_jobs().where(Job.id == jid)) is None
            stored = await session.get(JobAttempt, offer["attempt_id"])
            assert stored is not None and stored.status == JobStatus.SUCCEEDED and stored.log_data == b"log"
        assert "actions_finished" in await events(env, jid)
        assert "action_report" not in await events(env, jid)
        assert not (env.workspace / f"jobs/{jid}").exists()
    finally:
        release_finish.set()
        release_loop.set()
        h.begin_shutdown()
        conn.disconnect()
        await asyncio.wait_for(asyncio.gather(connection, h.aclose(), *([close] if close else [])), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["online", "active"])
async def test_restore_refreshes_owner(env, owner):
    from server.backup import restore_data

    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = None
    try:
        async with env.factory() as session:
            cached = await get_node(session, "node-a")
            assert cached is not None and cached.state == "offline"
            if owner == "online":
                connection = await serve(h, conn)
            else:
                async with env.factory() as other:
                    current = await get_job(other, jid)
                    assert current is not None
                    current.status = JobStatus.QUEUED
                    await other.flush()
                    assert await claim(other, jid, cached.id, now=env.clock()) is not None
                    await other.commit()
            with pytest.raises(
                ValueError,
                match="only offline nodes may be replaced"
                if owner == "online"
                else "cannot replace credentials while node work is active",
            ):
                await restore_data(
                    session,
                    {
                        "nodes": [
                            {
                                "name": "node-a",
                                "display_name": "replacement",
                                "token_hash": hash_token("replacement-token"),
                                "paused": True,
                                "transcode_limit": 2,
                                "healthcheck_limit": 1,
                                "mkv_stats_limit": 1,
                            }
                        ]
                    },
                    {"nodes": ["node-a"], "replace": True},
                )
            assert cached.token_hash == hash_token("tok")
            assert cached.state == ("online" if owner == "online" else "offline")
            await session.rollback()
        async with env.factory() as session:
            stored = await get_node(session, "node-a")
            assert stored is not None and stored.token_hash == hash_token("tok") and not stored.paused
            if owner == "active":
                current = await get_job(session, jid)
                assert current is not None and current.status == JobStatus.CLAIMED and current.node_id == stored.id
    finally:
        conn.disconnect()
        if connection is not None:
            await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["claim", "report"])
async def test_removal_preserves_owner(env, monkeypatch, check):
    from server.queue.store import NodeRemovalError, remove_node

    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    checked, release = asyncio.Event(), asyncio.Event()
    connection = asyncio.create_task(h.serve_connection(conn))
    removal = None
    replacement_conn = None
    replacement_connection = None
    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        async with env.factory() as session:
            stored_node = await get_node(session, "node-a")
            assert stored_node is not None
            node_id = stored_node.id
            get = session.get

            async def checked_get(entity, identifier, **kwargs):
                if entity is NodeRow and identifier == node_id and kwargs.get("with_for_update"):
                    if check == "claim":
                        checked.set()
                        await release.wait()
                    result = await get(entity, identifier, **kwargs)
                    if check == "report":
                        checked.set()
                        await release.wait()
                    return result
                return await get(entity, identifier, **kwargs)

            monkeypatch.setattr(session, "get", checked_get)

            async def remove():
                try:
                    removed = await remove_node(session, node_id)
                    await session.commit()
                    return removed
                except NodeRemovalError:
                    await session.rollback()
                    return False

            removal = asyncio.create_task(remove())
            try:
                await asyncio.wait_for(checked.wait(), 2)
                if check == "claim":
                    answer_challenge(h, conn)
                    await asyncio.wait_for(conn.ready.wait(), 2)
                    assert await asyncio.wait_for(h.dispatch_once(), 2) == 1
                    offer = conn.messages("job")[0]["job"]
                    materialize_offer(h, offer)
                else:
                    async with env.factory() as competing:
                        with pytest.raises(OperationalError) as raised:
                            await competing.get(NodeRow, node_id, with_for_update={"nowait": True})
                        assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
                    async with env.factory() as independent:
                        replacement_node = await register_node(independent, "node-b", "replacement-token")
                        replacement_id = replacement_node.id
                        await independent.commit()
                    answer_challenge(h, conn)
                release.set()
                removed = await asyncio.wait_for(removal, 2)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(removal, return_exceptions=True), 3)
        if check == "claim":
            async with env.factory() as session:
                current = await session.get(Job, jid)
                owner = await session.get(NodeRow, node_id)
                current_attempt = await session.get(JobAttempt, offer["attempt_id"])
                assert current is not None and current_attempt is not None
                assert not removed and owner is not None, (
                    f"removed={removed}; live job owner={current.node_id}; attempt owner={current_attempt.node_id}"
                )
                assert current.node_id == node_id and current_attempt.node_id == node_id
            assert (env.workspace / offer["output_path"]).read_bytes() == b"output"
        else:
            assert removed and replacement_id != node_id
            await asyncio.wait_for(connection, 3)
            assert conn.messages("error")[-1]["code"] == "auth"
            replacement_conn = FakeConn([hello("node-b", "replacement-token")])
            replacement_connection = await serve(h, replacement_conn)
            assert await asyncio.wait_for(h.dispatch_once(), 2) == 1
            other_offer = replacement_conn.messages("job")[0]["job"]
            other_output = materialize_offer(h, other_offer)
            await h._finish(
                node_id,
                finished(jid, other_offer["attempt_id"], other_offer["log_path"], "failed"),
            )
            other_job = await job(env, jid)
            assert other_job.status == JobStatus.CLAIMED and other_output.exists(), (
                f"removed node id={node_id}; replacement id={replacement_id}; "
                f"foreign report changed status to {other_job.status}; output retained={other_output.exists()}"
            )
    finally:
        release.set()
        if removal is not None:
            await asyncio.wait_for(asyncio.gather(removal, return_exceptions=True), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        if replacement_conn is not None:
            replacement_conn.disconnect()
        if replacement_connection is not None:
            await asyncio.wait_for(replacement_connection, 3)


@pytest.mark.asyncio
async def test_removal_identity_checked(env, monkeypatch):
    from server.queue.store import NodeRemovalError, remove_node

    checked, release = asyncio.Event(), asyncio.Event()
    async with env.factory() as session:
        original = await get_node(session, "node-a")
        assert original is not None
        node_id = original.id
        get = session.get

        async def checked_get(entity, identifier, **kwargs):
            if entity is NodeRow and identifier == node_id and kwargs.get("with_for_update"):
                checked.set()
                await release.wait()
            return await get(entity, identifier, **kwargs)

        monkeypatch.setattr(session, "get", checked_get)

        async def remove():
            try:
                removed = await remove_node(session, node_id)
                await session.commit()
                return removed
            except NodeRemovalError:
                await session.rollback()
                return False

        removal = asyncio.create_task(remove())
        try:
            await asyncio.wait_for(checked.wait(), 2)
            async with env.factory() as other:
                assert await remove_node(other, node_id)
                await other.commit()
                replacement = await register_node(other, "node-new", "new-token")
                replacement_id = replacement.id
                await other.commit()
            release.set()
            assert not await asyncio.wait_for(removal, 2)
            async with env.factory() as other:
                remaining = await get_node(other, "node-new")
                assert remaining is not None and remaining.id == replacement_id, (
                    f"removal of node-a id={node_id} deleted node-new id={replacement_id}"
                )
                assert remaining.token_hash == hash_token("new-token")
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(removal, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["slot", "probe", "actions", "ffmpeg"])
@pytest.mark.parametrize("reason", ["cancelled", "stalled"])
async def test_node_cancel_boundary(env, monkeypatch, phase, reason) -> None:
    from node.agent import Agent, AgentConfig
    from node.executor import _Run

    jid = await file_job(env)
    h, conn = hub(env, max_attempts=3), FakeConn([hello()])
    server_task = await serve(h, conn)
    started, finished = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    class BoundaryAgent(Agent):
        @asynccontextmanager
        async def _transcode_slot(self):
            if phase == "slot":
                started.set()
            async with super()._transcode_slot():
                yield

    agent = BoundaryAgent(
        AgentConfig(token="tok", server_url="ws://test", workspace_dir=str(env.workspace), min_free_gb=1)
    )
    if phase == "slot":
        agent._transcode_limit = 0
    node_conn = FakeConn()
    delivered = []

    async def from_node(data):
        if data["type"] == "finished":
            delivered.append(data)
            await h._handle_finished(h.entries["node-a"], Finished.model_validate(data))
            finished.set()

    monkeypatch.setattr(node_conn, "send_json", from_node)

    async def ffmpeg(_spec, argv, *_args):
        if phase == "ffmpeg":
            started.set()
            try:
                await asyncio.wait_for(release.wait(), 3)
            except asyncio.CancelledError:
                return _Run(returncode=0, stalled=False, cancelled=True, progress=None)
        Path(argv[-1]).write_bytes(b"out")
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    async def probe(*_args):
        if phase == "probe":
            started.set()
            await asyncio.wait_for(release.wait(), 3)
        return RAW

    async def actions(*_args):
        if phase == "actions":
            started.set()
            await asyncio.wait_for(release.wait(), 3)
        return []

    monkeypatch.setattr("node.executor._run_ffmpeg", ffmpeg)
    monkeypatch.setattr("node.executor._probe_json", probe)
    monkeypatch.setattr(agent, "_run_pre_actions", actions)
    receiver = asyncio.create_task(agent._receive_loop(node_conn))
    writer = asyncio.create_task(agent._writer(node_conn))
    try:
        await h.dispatch_once()
        offered = conn.messages("job")[0]
        offered["job"]["min_free_gb"] = 0
        node_conn.push(offered)
        await asyncio.wait_for(started.wait(), 3)
        offer = offered["job"]
        node_conn.push(
            Cancel(job_id=jid, attempt_id=offer["attempt_id"], reason=reason, log_path=offer["log_path"]).model_dump(
                mode="json"
            )
        )
        await asyncio.wait_for(finished.wait(), 3)
        assert len(delivered) == 1 and delivered[0]["result"] == ("stalled" if reason == "stalled" else "cancelled")
        assert agent._active_transcodes == 0 and not agent._jobs
        assert jid not in h.entries["node-a"].in_flight
        async with env.factory() as session:
            job = await get_job(session, jid)
            assert job is not None
        assert job.status == (JobStatus.STAGING if reason == "stalled" else JobStatus.CANCELLED)
        if reason == "stalled":
            assert await h.dispatch_once() == 0
            await asyncio.wait_for(h.staging.wait_idle(), 3)
            assert await h.dispatch_once() == 1
            assert conn.messages("job")[-1]["job"]["attempt_id"] != offer["attempt_id"]
    finally:
        release.set()
        receiver.cancel()
        writer.cancel()
        for running in list(agent._jobs.values()):
            running.cancel()
        await asyncio.wait_for(asyncio.gather(receiver, writer, *list(agent._jobs.values()), return_exceptions=True), 3)
        conn.disconnect()
        await asyncio.wait_for(server_task, 3)


@pytest.mark.asyncio
async def test_post_cleanup_retry(env, monkeypatch) -> None:
    from server.queue.store import ActiveJobError

    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    async with post_action_case(env) as case:
        cleanup = case.hub._cleanup_action_workspace

        async def delayed_cleanup(*args):
            cleanup_started.set()
            await release_cleanup.wait()
            await cleanup(*args)

        monkeypatch.setattr(case.hub, "_cleanup_action_workspace", delayed_cleanup)
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            async with env.factory() as session:
                from server.queue.store import restore_retry_result

                request_id = await request_retry(session, case.jid)
                with pytest.raises(ActiveJobError):
                    await retry_job(session, case.jid, request_id=request_id)
                original = await get_job(session, case.jid)
                assert original is not None
                requested = await session.get(JobEvent, request_id)
                assert requested is not None
                restore_retry_result(original, requested)
                await session.commit()
                with pytest.raises(ActiveJobError):
                    await enqueue_file_job(session, original.media_file_id, original.library_id, plan())
            await case.hub._handle_report(
                case.hub.entries["node-a"].node_db_id,
                ActionReport(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                ),
            )
            await asyncio.wait_for(cleanup_started.wait(), 3)
            async with env.factory() as session:
                request_id = await request_retry(session, case.jid)
                with pytest.raises(StaleJobError):
                    await retry_job(session, case.jid, request_id=request_id)
                original = await get_job(session, case.jid)
                assert original is not None
                requested = await session.get(JobEvent, request_id)
                assert requested is not None
                restore_retry_result(original, requested)
                await session.commit()
                requeued = await enqueue_file_job(session, original.media_file_id, original.library_id, plan())
                await session.commit()
            relative = stage_fixture(env.workspace, requeued.id, env.library / "movie.avi")
            sentinel = env.workspace / "jobs" / str(requeued.id) / "attempts/1/output.mp4"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_bytes(b"new attempt")
            release_cleanup.set()
            await asyncio.wait_for(dispatch, 3)
            assert sentinel.read_bytes() == b"new attempt" and (env.workspace / relative).is_file()
            assert (env.workspace / "jobs" / str(requeued.id) / "manifest.json").is_file()
        finally:
            release_cleanup.set()
            if not dispatch.done():
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                    ),
                )
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_disconnect_retains_log(env) -> None:
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    task = await serve(h, conn)
    try:
        await h.dispatch_once()
        offer = conn.messages("job")[0]["job"]
        log = env.workspace / offer["log_path"]
        log.write_bytes(b"disconnect log\n")
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, 3)
    retained = await job(env, jid)
    assert retained.status == JobStatus.CLAIMED and retained.node_id is not None
    assert (await attempt(env, jid))[0].result is None
    env.clock.advance(h.lease_ttl_s + 1)
    await h.sweep_once()
    async with env.factory() as session:
        saved = await session.get(JobAttempt, offer["attempt_id"])
        assert saved.log_data == b"disconnect log\n" and saved.result == "failed"
    assert not log.exists()
    assert (await job(env, jid)).status == JobStatus.FAILED
    assert saved.error is not None
    finished = log.parent.parent / "2"
    finished.mkdir(parents=True)
    (finished / "output.mkv").write_bytes(b"completed")
    async with env.factory() as session:
        row = await get_job(session, jid)
        assert row is not None
        row.attempts = 2
        row.status = JobStatus.SUCCEEDED
        session.add(JobAttempt(job_id=jid, attempt_number=2, status=JobStatus.SUCCEEDED.value, result="succeeded"))
        await session.commit()
    await h._cleanup_workspace(jid, [(jid, 2, True)])
    assert not finished.exists()
    assert (await attempt(env, jid))[0].log_data == b"disconnect log\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.CLAIMED, JobStatus.RUNNING])
async def test_sweep_preserves_healthchecks(env: Env, status: JobStatus) -> None:
    transcode_id = await file_job(env)
    healthcheck_id = await file_job(env, name="health.avi", health=True)
    diagnostic = create_healthcheck_log(make_test_temp_dir("healthcheck-diagnostics"), 3)
    log_path, healthcheck_log = diagnostic.path.name, diagnostic.path
    healthcheck_log.write_bytes(b"healthcheck in progress\n")
    async with env.factory() as session:
        healthcheck = await session.get(Job, healthcheck_id)
        assert healthcheck is not None
        healthcheck.status = status
        healthcheck.attempts = 3
        healthcheck.progress = {"stage": "probe"}
        session.add(
            JobAttempt(
                job_id=healthcheck_id,
                attempt_number=3,
                status=status,
                workload="healthcheck",
                node_name="server",
                log_path=log_path,
                claimed_at=env.clock(),
            )
        )
        await session.commit()
    healthcheck_events = await events(env, healthcheck_id)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        offer = conn.messages("job")[0]["job"]
        assert offer["job_id"] == transcode_id
        output = materialize_offer(h, offer)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        env.clock.advance(h.lease_ttl_s + 1)
        for _ in range(2):
            await asyncio.wait_for(h.sweep_once(), 3)
            retained = await job(env, healthcheck_id)
            assert retained.status == status and retained.attempts == 3
            assert retained.error is None and retained.progress == {"stage": "probe"}
            assert retained.node_id is None and retained.lease_expires_at is None
            current = (await attempt(env, healthcheck_id))[0]
            assert current.status == status and current.result is None and current.error is None
            assert current.finished_at is None and current.log_data is None
            assert healthcheck_log.read_bytes() == b"healthcheck in progress\n"
            assert await events(env, healthcheck_id) == healthcheck_events
        expired = await job(env, transcode_id)
        assert expired.status == JobStatus.FAILED
        assert expired.error is not None
        interrupted = (await attempt(env, transcode_id))[0]
        assert interrupted.result == "failed" and interrupted.log_data == b"log"
        assert not output.exists()
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_node_limit_reconnects(env) -> None:
    async with env.factory() as session:
        node = await register_node(session, "node-a", "tok")
        node.transcode_limit = 3
        await session.commit()
        node_id = node.id

    h = hub(env)
    conn = FakeConn([hello(transcodes=1)])
    task = await serve(h, conn)
    try:
        assert conn.messages("set_limits")[0]["max_transcodes"] == 3
        assert h.entries["node-a"].capabilities.max_transcodes == 3
        await h.set_limits(node_id, max_transcodes=2, max_healthchecks=1, max_mkv_stats=1)
        assert conn.messages("set_limits")[-1]["max_transcodes"] == 2
        assert h.entries["node-a"].capabilities.max_transcodes == 2

        await h._handle_heartbeat(
            h.entries["node-a"],
            Heartbeat(
                cpu_percent=41,
                memory_percent=62,
                gpus=[{"name": "Intel GPU", "percent": 25}, {"name": "NVIDIA RTX", "percent": None}],
            ),
        )
        async with env.factory() as session:
            node = await session.get(NodeRow, node_id)
            assert node is not None
            assert node.usage == {
                "cpu_percent": 41,
                "memory_percent": 62,
                "gpus": [{"name": "Intel GPU", "percent": 25.0}, {"name": "NVIDIA RTX", "percent": None}],
            }
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, timeout=3)

    async with env.factory() as session:
        node = await session.get(NodeRow, node_id)
        assert node is not None
        assert node.usage["cpu_percent"] == 41


async def file_job(env, name="movie.avi", with_probe=True, health=False, staged=True, **kwargs):
    path = env.library / name
    path.write_bytes(b"source")
    identity = source_identity(path)
    async with env.factory() as s:
        lib = (await s.execute(select(Library).where(Library.name == "library"))).scalar_one_or_none()
        if lib is None:
            lib = Library(name="library", roots=[str(env.library)], healthcheck_enabled=True)
            s.add(lib)
            await s.flush()
        media = MediaFile(
            library_id=lib.id,
            path=str(path),
            size=identity["size"],
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=identity,
            container="avi",
            medium="movie",
            first_seen=env.clock(),
            last_scanned_at=env.clock(),
        )
        s.add(media)
        await s.flush()
        if with_probe:
            probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps(RAW))
            s.add(probe)
            await s.flush()
            media.probe_id = probe.id
        captured = capture_source(path)
        # Create the rows directly without repeating the queue tests in every hub scenario.
        kind = kwargs.pop("kind", "healthcheck" if health else "transcode")
        if kind == "mkv_stats":
            job = await enqueue_metadata_job(s, media, lib, kind="mkv_stats", priority=kwargs.pop("priority", 0))
        else:
            job = await _insert_job(
                s,
                media.id,
                lib.id,
                kind=kind,
                plan=None if health else kwargs.pop("plan", plan()),
                priority=kwargs.pop("priority", 0),
                now=kwargs.pop("now", env.clock()),
                source=None if health else captured,
                probe=probe_from_raw(path, media.size, RAW) if with_probe and not health else None,
                **kwargs,
            )
        if staged:
            stage_fixture(env.workspace, job.id, path)
            await mark_staged(s, job.id, now=env.clock())
        await s.commit()
    return job.id


@pytest_asyncio.fixture
async def remote_hub(env):
    bus = EventBus()
    h = hub(env, events=bus)
    h.healthcheck_service = HealthcheckService(
        env.factory,
        env.workspace / "healthchecks",
        str(FAKE_FFPROBE.with_name("ffmpeg.py")),
        str(FAKE_FFPROBE),
        events=bus,
    )
    h.healthcheck_service.configure(max_concurrency=1, hwaccel=False, location=ExecutionLocation.NODE)
    h.probe_service = ProbeService(
        env.factory,
        str(FAKE_FFPROBE),
        str(FAKE_FFPROBE.with_name("mkvpropedit.py")),
        events=bus,
        workspace_dir=env.workspace,
    )
    h.probe_service.configure(max_concurrency=4, max_statistics=1, statistics_location=ExecutionLocation.NODE)
    try:
        yield h
    finally:
        await asyncio.wait_for(h.aclose(), 5)
        await asyncio.wait_for(asyncio.gather(h.healthcheck_service.close(), h.probe_service.close(), bus.aclose()), 5)


@pytest.mark.asyncio
async def test_inspection_stall_queue(env, remote_hub):
    h = remote_hub
    h.stall_timeout_s = 1
    job_id = await file_job(env, health=True, staged=False)
    conn = FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        async with h.staging._stage_lock:
            assert await h.dispatch_once() == 1
            env.clock.advance(2)
            await h.sweep_once()
            assert not conn.messages("cancel")
            assert job_id in h.entries["node-a"].in_flight
            assert (await job(env, job_id)).status == JobStatus.CLAIMED
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        offer = conn.messages("job")[0]["job"]
        assert offer["job_id"] == job_id
        await h.sweep_once()
        assert not conn.messages("cancel")
        env.clock.advance(2)
        await h.sweep_once()
        cancellation = conn.messages("cancel")
        assert len(cancellation) == 1
        assert cancellation[0]["job_id"] == job_id
        assert cancellation[0]["attempt_id"] == offer["attempt_id"]
        assert cancellation[0]["reason"] == "stalled"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("mkvpropedit", [False, True])
async def test_remote_lane_capacity(env, remote_hub, mkvpropedit):
    h = remote_hub
    ids = {
        "transcode": [await file_job(env, name=f"transcode-{number}.avi") for number in range(2)],
        "healthcheck": [
            await file_job(env, name=f"health-{number}.avi", health=True, staged=False) for number in range(2)
        ],
        "mkv_stats": [
            await file_job(env, name=f"stats-{number}.mkv", kind="mkv_stats", staged=False) for number in range(2)
        ],
    }
    assert await h.healthcheck_service.claim_next() is None
    assert await h.probe_service._claim("mkv_stats") is None
    conn = FakeConn([hello(mkvpropedit=mkvpropedit)])
    connection = await serve(h, conn)
    try:
        entry = h.entries["node-a"]
        async with env.factory() as session:
            node = await session.get(NodeRow, entry.node_db_id)
            node.display_name = "Custom worker"
            await session.commit()
        h.set_paused(entry.node_db_id, True)
        assert await h.dispatch_once() == 0
        assert not conn.messages("job")
        h.set_paused(entry.node_db_id, False)
        expected = {kind: int(kind != "mkv_stats" or mkvpropedit) for kind in ids}
        assert await h.dispatch_once() == sum(expected.values())
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        offered = [item["job"] for item in conn.messages("job")]
        assert {item["kind"] for item in offered} == {kind for kind, count in expected.items() if count}
        assert len(offered) == sum(expected.values())
        assert await h.dispatch_once() == 0
        for item in offered:
            assert (await attempt(env, item["job_id"]))[0].node_name == "Custom worker"
        for kind, jobs in ids.items():
            assert sum(value == kind for value in entry.in_flight.values()) == expected[kind]
            statuses = {(await job(env, jid)).status for jid in jobs}
            if expected[kind]:
                assert statuses == {JobStatus.CLAIMED, JobStatus.QUEUED}
            else:
                assert statuses == {JobStatus.QUEUED}
        health = next(item for item in offered if item["kind"] == "healthcheck")
        for item in offered:
            if item["kind"] == "transcode":
                continue
            phase = "decode" if item["kind"] == "healthcheck" else "mkvpropedit"
            await h._handle_progress(
                entry, Progress(job_id=item["job_id"], attempt_id=item["attempt_id"], phase=phase, percent=35)
            )
            running = await job(env, item["job_id"])
            assert running.status == JobStatus.RUNNING and running.started_at is not None
            assert running.progress["percent"] == 35
            if item["kind"] == "healthcheck":
                assert running.progress["stage"] == "decode"
            else:
                assert running.progress["tool"] == "mkvpropedit"
        await h._handle_finished(
            entry,
            finished(
                health["job_id"],
                health["attempt_id"],
                health["log_path"],
                kind="healthcheck",
                error="worker launch failed",
            ),
        )
        assert await h.dispatch_once() == 1
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        next_offer = conn.messages("job")[-1]["job"]
        assert next_offer["kind"] == "healthcheck" and next_offer["job_id"] != health["job_id"]
        assert {kind: sum(value == kind for value in entry.in_flight.values()) for kind in ids} == expected
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "end"),
    [("healthcheck", "ready"), ("mkv_stats", "ready"), ("healthcheck", "expired"), ("mkv_stats", "cancelled")],
)
async def test_inspection_reconnect_copy(env, remote_hub, monkeypatch, kind, end):
    h = remote_hub
    h.max_attempts = 3
    job_id = await file_job(env, name="reconnect.mkv", health=kind == "healthcheck", kind=kind, staged=False)
    old = FakeConn([hello(mkvpropedit=True)])
    first = await serve(h, old)
    replacement = FakeConn([hello(mkvpropedit=True)])
    second = None
    inventory, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    missing, signal = h._finish_missing, h._await_signal

    async def finish_missing(entry, message):
        inventory.set()
        await release.wait()
        await missing(entry, message)

    async def observe(key, check, timeout_s):
        if key == "node:node-a:ready":
            waiting.set()
        return await signal(key, check, timeout_s)

    monkeypatch.setattr(h, "_finish_missing", finish_missing)
    monkeypatch.setattr(h, "_await_signal", observe)
    try:
        async with h.staging._stage_lock:
            assert await h.dispatch_once() == 1
            claimed = (await attempt(env, job_id))[0]
            second = asyncio.create_task(h.serve_connection(replacement))
            await wait_for(lambda: replacement.messages("workspace_challenge"), replacement.changed)
            answer_challenge(h, replacement)
            await asyncio.wait_for(inventory.wait(), 3)
            assert h.entries["node-a"].conn is replacement
            assert not h.entries["node-a"].workspace_ready
        await asyncio.wait_for(waiting.wait(), 3)
        assert not replacement.messages("job") and not old.messages("job")
        assert (await job(env, job_id)).status == JobStatus.CLAIMED
        other = await file_job(env, name="other.avi", staged=False)
        assert await asyncio.wait_for(h.staging.stage_job(other), 3) == 1
        if end != "ready":
            tasks = list(h._inspection_tasks.values())
            if end == "expired":
                env.clock.advance(h.lease_ttl_s + 1)
                await h.sweep_once()
            else:
                assert await h.cancel_job(job_id)
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
            release.set()
            await asyncio.wait_for(replacement.ready.wait(), 3)
            assert not replacement.messages("job") and not old.messages("job")
            retained = await job(env, job_id)
            assert retained.status == (JobStatus.QUEUED if end == "expired" else JobStatus.CANCELLED)
            assert retained.attempts == 1
            if end == "cancelled":
                async with env.factory() as session:
                    media = await session.get(MediaFile, retained.media_file_id)
                    assert media.metadata_cancelled
            return
        release.set()
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        offered = replacement.messages("job")[0]["job"]
        assert offered["job_id"] == job_id and offered["attempt_id"] == claimed.id
        assert len(await attempt(env, job_id)) == 1
        assert (await job(env, job_id)).attempts == 1
        assert not old.messages("job") and not old.messages("cancel")
        assert h.entries["node-a"].in_flight == {job_id: kind}
    finally:
        release.set()
        old.disconnect()
        replacement.disconnect()
        await asyncio.wait_for(asyncio.gather(first, *([second] if second is not None else [])), 3)


@pytest.mark.asyncio
async def test_staging_requeue_kept(env, remote_hub, monkeypatch):
    h = remote_hub
    job_id = await file_job(env, name="retry.mkv", kind="mkv_stats", staged=False)
    connection = FakeConn([hello(mkvpropedit=True)])
    serving = await serve(h, connection)
    copying, release = asyncio.Event(), asyncio.Event()
    storage = hub_module.storage_io

    async def fail_copy(operation, *args, **kwargs):
        if operation is _stage_source:
            copying.set()
            await release.wait()
            raise OSError("Copy failed after retry.")
        return await storage(operation, *args, **kwargs)

    monkeypatch.setattr(hub_module, "storage_io", fail_copy)
    try:
        assert await h.dispatch_once() == 1
        await asyncio.wait_for(copying.wait(), 3)
        async with env.factory() as session:
            await requeue(session, job_id, now=env.clock())
            await session.commit()
        await h.sweep_once()
        assert await h.dispatch_once() == 0
        release.set()
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        retained = await job(env, job_id)
        assert retained.status == JobStatus.QUEUED and retained.error is None
        assert retained.attempts == 1
        assert not connection.messages("job")
        async with env.factory() as session:
            media = await session.get(MediaFile, retained.media_file_id)
            assert media.health_status == "pending"
    finally:
        release.set()
        connection.disconnect()
        await asyncio.wait_for(serving, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "local_editor"), [("healthcheck", True), ("mkv_stats", True), ("mkv_stats", False)])
async def test_remote_pool_claims(env, remote_hub, kind, local_editor):
    h = remote_hub
    ids = [
        await file_job(env, name=f"pooled-{number}.mkv", health=kind == "healthcheck", kind=kind, staged=False)
        for number in range(2)
    ]
    h.healthcheck_service.configure(max_concurrency=1, hwaccel=False, location=ExecutionLocation.NODE_LOCAL)
    if not local_editor:
        h.probe_service = ProbeService(
            env.factory,
            str(FAKE_FFPROBE),
            str(env.workspace / "missing-editor"),
            workspace_dir=env.workspace,
            events=h.events,
        )
    h.probe_service.configure(max_concurrency=4, max_statistics=1, statistics_location=ExecutionLocation.NODE_LOCAL)
    conn = FakeConn([hello(mkvpropedit=True)])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        local = (
            await h.healthcheck_service.claim_next() if kind == "healthcheck" else await h.probe_service._claim(kind)
        )
        offered = conn.messages("job")[0]["job"]
        if not local_editor:
            assert local is None
            assert h.events.mkv_stats_slots == 0
            assert await h.dispatch_once() == 0
            await h._handle_finished(
                h.entries["node-a"],
                finished(
                    offered["job_id"], offered["attempt_id"], offered["log_path"], kind=kind, error="editor exited"
                ),
            )
            assert await h.dispatch_once() == 1
            await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
            assert {item["job"]["job_id"] for item in conn.messages("job")} == set(ids)
            for jid in ids:
                assert len(await attempt(env, jid)) == 1
            return
        assert local is not None
        assert {local[0], offered["job_id"]} == set(ids)
        assert await h.dispatch_once() == 0
        remaining = (
            await h.healthcheck_service.claim_next() if kind == "healthcheck" else await h.probe_service._claim(kind)
        )
        assert remaining is None
        for jid in ids:
            stored = await attempt(env, jid)
            assert len(stored) == 1 and stored[0].workload == kind
        assert (await job(env, local[0])).node_id is None
        assert (await job(env, offered["job_id"])).node_id == h.entries["node-a"].node_db_id
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["healthcheck", "mkv_stats"])
async def test_remote_storage_waits(env, remote_hub, monkeypatch, kind):
    h = remote_hub
    ids = [
        await file_job(env, name=f"space-{number}.mkv", health=kind == "healthcheck", kind=kind, staged=False)
        for number in range(2)
    ]
    h.staging.min_free_gb = 0
    budget = 0

    def free(_path):
        return budget - sum(path.stat().st_size for path in env.workspace.glob("jobs/*/source.mkv"))

    monkeypatch.setattr("server.staging._workspace_free", free)
    conn = FakeConn([hello(mkvpropedit=True)])
    connection = await serve(h, conn)
    try:
        entry = h.entries["node-a"]
        await h.set_limits(entry.node_db_id, max_transcodes=1, max_healthchecks=2, max_mkv_stats=2)
        for _ in range(h.max_attempts + 2):
            assert await h.dispatch_once() == 2
            await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
            assert not conn.messages("job")
            assert not entry.in_flight
            for jid in ids:
                current = await job(env, jid)
                assert current.status == JobStatus.QUEUED and current.attempts == 0
                assert "Staging waiting" in current.warning
            assert await h.dispatch_once() == 0
            env.clock.advance(h.dispatch_interval_s)

        budget = 6 + (math.ceil(6 * DEFAULT_HARD_SIZE_CEILING) if kind == "mkv_stats" else 0)
        assert await h.dispatch_once() == 2
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        assert len(conn.messages("job")) == 1
        offered = conn.messages("job")[0]["job"]
        assert (await job(env, offered["job_id"])).attempts == 1
        assert (await attempt(env, offered["job_id"]))[0].attempt_number == 1
        queued = next(jid for jid in ids if jid != offered["job_id"])
        waiting = await job(env, queued)
        assert waiting.status == JobStatus.QUEUED and waiting.attempts == 0
        assert sum(1 for _ in env.workspace.glob("jobs/*/source.mkv")) == 1
        await h._handle_finished(
            entry,
            finished(
                offered["job_id"], offered["attempt_id"], offered["log_path"], kind=kind, error="worker launch failed"
            ),
        )
        env.clock.advance(h.dispatch_interval_s)
        assert await h.dispatch_once() == 1
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        assert conn.messages("job")[-1]["job"]["job_id"] == queued
        assert all(path.read_bytes() == b"source" for path in env.library.iterdir())
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "prior_health", "prior_transcode", "expected_transcode"),
    [
        ("healthy", "pending", "skipped", "skipped"),
        ("corrupt", "pending", "skipped", "skipped"),
        ("failed", "pending", "skipped", "skipped"),
        ("stale", "pending", "skipped", "skipped"),
        ("healthy", "healthy", "cancelled", "cancelled"),
        ("healthy", "pending", "cancelled", "pending"),
    ],
)
async def test_remote_health_result(env, remote_hub, outcome, prior_health, prior_transcode, expected_transcode):
    h = remote_hub
    jid = await file_job(env, health=True, staged=False)
    async with env.factory() as session:
        current = await session.get(Job, jid)
        media = await session.get(MediaFile, current.media_file_id)
        media.transcode_status, media.transcode_decided = prior_transcode, True
        media.health_status = prior_health
        media.health_source = {"path": media.path, "size": media.size} if prior_health == "healthy" else None
        original_probe = media.probe_id
        session.add(JobEvent(job_id=jid, type="requested", payload={"operation": "healthcheck", "refresh": False}))
        await session.commit()
    conn = FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        offer = conn.messages("job")[0]["job"]
        assert offer["kind"] == "healthcheck" and offer["healthcheck_hwaccel"] is False
        (env.workspace / offer["log_path"]).write_bytes(b"healthcheck diagnostic\n")
        if outcome == "stale":
            (env.library / "movie.avi").write_bytes(b"changed source")
        report = finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            "failed" if outcome == "failed" else "succeeded",
            kind="healthcheck",
            health=None
            if outcome == "failed"
            else HealthResult(healthy=outcome != "corrupt", error_count=int(outcome == "corrupt")),
            raw_probe=None if outcome == "failed" else RAW,
            error="worker launch failed" if outcome == "failed" else None,
        )
        await h._handle_finished(h.entries["node-a"], report)
        async with env.factory() as session:
            current = await session.get(Job, jid)
            media = await session.get(MediaFile, current.media_file_id)
            accepted = outcome in {"healthy", "corrupt"}
            assert current.status == (JobStatus.SUCCEEDED if accepted else JobStatus.FAILED)
            assert media.health_status == (outcome if accepted else HealthStatus.PENDING)
            assert media.health_source == ({"path": str(env.library / "movie.avi"), "size": 6} if accepted else None)
            assert (media.transcode_status, media.transcode_decided) == (
                expected_transcode,
                expected_transcode != "pending",
            )
            if not accepted:
                assert media.probe_id == original_probe
                assert (
                    "Source changed" in current.error if outcome == "stale" else current.error == "worker launch failed"
                )
        stored = (await attempt(env, jid))[0]
        assert stored.log_data == b"healthcheck diagnostic\n"
        assert stored.health == (
            report.health.model_dump(mode="json") if report.health is not None and accepted else None
        )
        assert (env.library / "movie.avi").read_bytes() == (b"changed source" if outcome == "stale" else b"source")
        assert not conn.messages("action_task")
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "invalid"])
async def test_remote_stats_publication(env, remote_hub, outcome):
    from tests.fakes.ffprobe import DEFAULT_PROBE

    h = remote_hub
    jid = await file_job(env, name="movie.mkv", kind="mkv_stats", staged=False)
    async with env.factory() as session:
        current = await session.get(Job, jid)
        media = await session.get(MediaFile, current.media_file_id)
        library = await session.get(Library, current.library_id)
        library.mkv_stats, library.healthcheck_enabled = True, False
        library.cooldown_s = 0
        media.container = "mkv"
        media.transcode_status, media.transcode_decided = "skipped", True
        original_probe = media.probe_id
        await session.commit()
    conn = FakeConn([hello(mkvpropedit=True)])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        await asyncio.wait_for(asyncio.gather(*h._inspection_tasks.values()), 3)
        offer = conn.messages("job")[0]["job"]
        assert offer["kind"] == "mkv_stats" and offer["statistics_edit"] is True
        output = materialize_offer(h, offer)
        output.write_bytes(b"stats!")
        raw = json.loads(json.dumps(DEFAULT_PROBE))
        raw["streams"][0]["bit_rate"] = "4000000"
        raw["format"]["duration"] = "1" if outcome == "invalid" else "600"
        Path(f"{output}.probe.json").write_text(json.dumps(raw))
        reported = ProbeData.model_validate(offer["probe"]).model_copy(
            update={"file_path": offer["output_path"], "size_bytes": output.stat().st_size}
        )
        report = finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            "failed" if outcome == "failed" else "succeeded",
            kind="mkv_stats",
            raw_probe=None if outcome == "failed" else raw,
            error="statistics edit failed" if outcome == "failed" else None,
            **({} if outcome == "failed" else {"output_path": offer["output_path"], "output_probe": reported}),
        )
        await h._handle_finished(h.entries["node-a"], report)
        async with env.factory() as session:
            current = await session.get(Job, jid)
            media = await session.get(MediaFile, current.media_file_id)
            assert current.status == (JobStatus.SUCCEEDED if outcome == "succeeded" else JobStatus.FAILED), (
                current.error
            )
            assert (media.transcode_status, media.transcode_decided, media.first_seen) == ("skipped", True, T0)
            if outcome == "succeeded":
                assert media.size == len(b"stats!")
                probe = await session.get(ProbeRow, media.probe_id)
                assert json.loads(probe.raw_json)["streams"][0]["bit_rate"] == "4000000"
            else:
                assert media.probe_id == original_probe and media.size == 6
            snapshot = await build_snapshot(session, h.events, env.clock())
            assert savings(snapshot, 1)["totals"]["files"] == 0
            assert savings(snapshot, 1)["totals"]["saved_bytes"] == 0
        assert (env.library / "movie.mkv").read_bytes() == (b"stats!" if outcome == "succeeded" else b"source")
        assert not conn.messages("action_task")
        assert not set(await events(env, jid)) & {"actions_pending", "actions_finished"}
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("slots", [1, 2])
async def test_dispatch_stages_slots(writer_env, slots):
    env = writer_env
    ids = [await file_job(env, name=f"movie-{number}.avi", staged=False) for number in range(8)]
    async with env.factory() as session:
        current = await get_node(session, "node-a")
        assert current is not None
        current.transcode_limit = slots
        await session.commit()
    h, conn = hub(env), FakeConn([hello(transcodes=slots)])
    connection = await serve(h, conn)
    try:
        assert not (env.workspace / "jobs").exists()
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await asyncio.wait_for(h.dispatch_once(), 3) == slots
        assert {int(path.name) for path in (env.workspace / "jobs").iterdir()} == set(ids[:slots])
        assert [item["job"]["job_id"] for item in conn.messages("job")] == ids[:slots]
        assert await h.dispatch_once() == 0
        for jid in ids[slots:]:
            assert (await job(env, jid)).status == JobStatus.STAGING
            assert not (env.workspace / "jobs" / str(jid)).exists()
        offer = conn.messages("job")[0]["job"]
        assert (env.workspace / offer["source_path"]).read_bytes() == b"source"
        await h._handle_finished(
            h.entries["node-a"], finished(ids[0], offer["attempt_id"], offer["log_path"], "cancelled")
        )
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        assert conn.messages("job")[-1]["job"]["job_id"] == ids[slots]
        assert {int(path.name) for path in (env.workspace / "jobs").iterdir()} == set(ids[1 : slots + 1])
        assert all(path.read_bytes() == b"source" for path in env.library.iterdir())
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_activity_blocks_dispatch(env, staged):
    jid = await file_job(env, staged=staged)
    paused = True
    h, conn = hub(env, activity_paused=lambda: paused), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 0
        assert not conn.messages("job")
        assert not h.staging.tasks
        assert (await job(env, jid)).status == (JobStatus.QUEUED if staged else JobStatus.STAGING)
        paused = False
        if not staged:
            assert await h.dispatch_once() == 0
            paused = True
            await asyncio.wait_for(h.staging.wait_idle(), 3)
            assert await h.dispatch_once() == 0
            assert (await job(env, jid)).status == JobStatus.QUEUED
            assert (env.workspace / "jobs" / str(jid) / "source.avi").read_bytes() == b"source"
            paused = False
        assert await h.dispatch_once() == 1
        assert conn.messages("job")[0]["job"]["job_id"] == jid
        assert (await job(env, jid)).status == JobStatus.CLAIMED
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        await h.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["offline", "paused", "workspace", "encoder", "suspended", "schedule"])
async def test_dispatch_withholds_staging(env, blocked):
    jid = await file_job(env, staged=False)
    h = hub(env)
    conn = FakeConn([hello(encoders=() if blocked == "encoder" else ("libx264",))])
    connection = None
    try:
        if blocked != "offline":
            connection = await serve(h, conn)
            entry = h.entries["node-a"]
            if blocked == "paused":
                h.set_paused(entry.node_db_id, True)
            if blocked == "workspace":
                entry.workspace_ready = False
        if blocked in {"suspended", "schedule"}:
            async with env.factory() as session:
                current = await get_job(session, jid)
                assert current is not None
                library = await session.get(Library, current.library_id)
                assert library is not None
                if blocked == "suspended":
                    library.suspended = True
                else:
                    library.schedule = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
                await session.commit()
        assert await h.dispatch_once() == 0
        assert (await job(env, jid)).status == JobStatus.STAGING
        assert not (env.workspace / "jobs").exists()
    finally:
        conn.disconnect()
        if connection is not None:
            await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_copy_allows_dispatch(writer_env, monkeypatch):
    env = writer_env
    ids = [await file_job(env, name=f"pending-{number}.avi", staged=False) for number in range(3)]
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    copy = _stage_source
    calls = []

    def blocked(*args, **kwargs):
        calls.append(args[1])
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return copy(*args, **kwargs)

    monkeypatch.setattr("server.staging._stage_source", blocked)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        await asyncio.wait_for(started.wait(), 3)
        assert set(h.staging.tasks) == {ids[0]}
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        assert set(h.staging.tasks) == {ids[0]}
        ready = await file_job(env, name="ready.avi")
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        assert conn.messages("job")[0]["job"]["job_id"] == ready
        assert calls == [ids[0]] and not release.is_set()
        assert await h.dispatch_once() == 0
        release.set()
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert h._wake.is_set()
        assert not h.staging.tasks and not h._staging_slots
        assert (await job(env, ids[0])).status == JobStatus.QUEUED
        assert all(not (env.workspace / "jobs" / str(identifier)).exists() for identifier in ids[1:])
    finally:
        release.set()
        await asyncio.wait_for(h.aclose(), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reset", "close", "library"])
async def test_staging_lifecycle_drains(writer_env, monkeypatch, operation):
    env = writer_env
    jid = await file_job(env, staged=False)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    started, draining = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    copy, wait_idle = _stage_source, h.staging.wait_idle

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return copy(*args, **kwargs)

    async def idle():
        draining.set()
        await wait_idle()

    async def reset():
        async with h.pause_for_reset():
            assert not h.staging.tasks

    monkeypatch.setattr("server.staging._stage_source", blocked)
    monkeypatch.setattr(h.staging, "wait_idle", idle)
    lifecycle = None
    try:
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(started.wait(), 3)
        if operation == "library":
            async with env.factory() as session:
                current = await session.get(Job, jid)
                library = await session.get(Library, current.library_id)
                library.suspended = True
                await session.commit()
                library_id = library.id
            cancel = h.staging.tasks[jid].cancel

            def cancelling(*args, **kwargs):
                draining.set()
                return cancel(*args, **kwargs)

            monkeypatch.setattr(h.staging.tasks[jid], "cancel", cancelling)
            lifecycle = asyncio.create_task(h.drain_library(library_id))
        else:
            lifecycle = asyncio.create_task(reset() if operation == "reset" else h.aclose())
        await asyncio.wait_for(draining.wait(), 3)
        assert not lifecycle.done()
        assert jid in h.staging.tasks and jid in h.staging.active_jobs
        assert await h.dispatch_once() == 0
        release.set()
        await asyncio.wait_for(lifecycle, 3)
        assert not h.staging.tasks and not h.staging.active_jobs
        assert (env.workspace / "jobs" / str(jid) / "source.avi").read_bytes() == b"source"
    finally:
        release.set()
        if lifecycle is not None:
            await asyncio.wait_for(asyncio.gather(lifecycle, return_exceptions=True), 3)
        await asyncio.wait_for(h.aclose(), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_staging_reserves_compatible(writer_env, monkeypatch):
    env = writer_env
    ids = [await file_job(env, name=f"movie-{number}.avi", staged=False) for number in range(3)]
    async with env.factory() as session:
        await register_node(session, "node-b", "other-token")
        await session.commit()
    h = hub(env)
    connections = [FakeConn([hello()]), FakeConn([hello("node-b", "other-token", encoders=("libx265",))])]
    serving = [await serve(h, conn) for conn in connections]
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    copy = _stage_source

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return copy(*args, **kwargs)

    monkeypatch.setattr("server.staging._stage_source", blocked)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 0
        await asyncio.wait_for(started.wait(), 3)
        assert set(h.staging.tasks) == {ids[0]}
        assert await h.dispatch_once() == 0
        assert set(h.staging.tasks) == {ids[0]}
        # A replacement connection can move the compatible slot to another node.
        for name, encoders in (("node-a", ["libx265"]), ("node-b", ["libx264"])):
            entry = h.entries[name]
            entry.capabilities = entry.capabilities.model_copy(update={"encoders": encoders})
        assert await h.dispatch_once() == 0
        assert set(h.staging.tasks) == {ids[0]}
        release.set()
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        assert not connections[0].messages("job")
        assert connections[1].messages("job")[0]["job"]["job_id"] == ids[0]
    finally:
        release.set()
        await asyncio.wait_for(h.aclose(), 3)
        for conn in connections:
            conn.disconnect()
        await asyncio.wait_for(asyncio.gather(*serving), 3)


@pytest.mark.asyncio
async def test_capacity_allows_next(writer_env, monkeypatch):
    env = writer_env
    blocked = await file_job(env, name="large.avi", staged=False, priority=100)
    ready = await file_job(env, name="small.avi", staged=False)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    capacity = h.staging._capacity_warning
    stage_job = h.staging.stage_job
    completed, release = asyncio.Event(), asyncio.Event()
    checks = []

    async def limited(session, job_id, *args, **kwargs):
        checks.append(job_id)
        if job_id == blocked:
            return "Workspace capacity is unavailable."
        return await capacity(session, job_id, *args, **kwargs)

    async def stage(job_id):
        result = await stage_job(job_id)
        if job_id == blocked:
            await release.wait()
            completed.set()
        return result

    monkeypatch.setattr(h.staging, "_capacity_warning", limited)
    monkeypatch.setattr(h.staging, "stage_job", stage)
    try:
        assert await h.dispatch_once() == 0
        release.set()
        async with asyncio.timeout(3):
            await completed.wait()
            # Resume before the completed task's removal callback runs.
            assert h.staging.tasks[blocked].done()
            await h.staging.wait_idle()
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        assert checks == [blocked, ready]
        assert conn.messages("job")[0]["job"]["job_id"] == ready
        assert (await job(env, blocked)).status == JobStatus.STAGING
        assert not (env.workspace / "jobs" / str(blocked)).exists()
        offer = conn.messages("job")[0]["job"]
        await h._handle_finished(
            h.entries["node-a"], finished(ready, offer["attempt_id"], offer["log_path"], "cancelled")
        )
        assert await h.dispatch_once() == 0 and checks == [blocked, ready]
        env.clock.advance(h.dispatch_interval_s)
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert checks == [blocked, ready, blocked]
    finally:
        release.set()
        await asyncio.wait_for(h.aclose(), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_staging_skips_incompatible(env):
    blocked = await file_job(
        env, name="blocked.avi", staged=False, plan=FFmpegPlan(video=VideoTarget(encoder="libx265"))
    )
    ready = await file_job(env, name="ready.avi", staged=False)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        assert conn.messages("job")[0]["job"]["job_id"] == ready
        assert (await job(env, blocked)).status == JobStatus.STAGING
        assert not (env.workspace / "jobs" / str(blocked)).exists()
        assert (env.workspace / "jobs" / str(ready) / "source.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["node", "schedule", "suspended"])
async def test_staging_rechecks_queue(env, monkeypatch, changed):
    ids = [await file_job(env, name=f"movie-{number}.avi", staged=False) for number in range(2)]
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    copied, release = asyncio.Event(), asyncio.Event()
    stage_job = h.staging.stage_job

    async def blocked_stage(job_id):
        count = await stage_job(job_id)
        copied.set()
        await release.wait()
        return count

    monkeypatch.setattr(h.staging, "stage_job", blocked_stage)
    dispatch = asyncio.create_task(h.dispatch_once())
    try:
        await asyncio.wait_for(copied.wait(), 3)
        if changed == "node":
            h.set_paused(h.entries["node-a"].node_db_id, True)
        else:
            async with env.factory() as session:
                library = await session.scalar(select(Library).where(Library.name == "library"))
                assert library is not None
                if changed == "schedule":
                    library.schedule = {"type": "grid", "cells": [[False] * 24 for _ in range(7)]}
                else:
                    library.suspended = True
                await session.commit()
        release.set()
        assert await asyncio.wait_for(dispatch, 3) == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 0
        assert (await job(env, ids[0])).status == JobStatus.QUEUED
        assert (await job(env, ids[1])).status == JobStatus.STAGING
        assert not conn.messages("job")
        assert not (env.workspace / "jobs" / str(ids[1])).exists()
        if changed == "node":
            h.set_paused(h.entries["node-a"].node_db_id, False)
        else:
            async with env.factory() as session:
                library = await session.scalar(select(Library).where(Library.name == "library"))
                assert library is not None
                library.schedule = None
                library.suspended = False
                await session.commit()
        assert await h.dispatch_once() == 1
        assert conn.messages("job")[0]["job"]["job_id"] == ids[0]
        assert not (env.workspace / "jobs" / str(ids[1])).exists()
    finally:
        release.set()
        conn.disconnect()
        await asyncio.wait_for(asyncio.gather(dispatch, connection, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_dispatch_preserves_probe(env) -> None:
    jid = await file_job(env)
    async with env.factory() as session:
        current = await session.get(Job, jid)
        library = await session.get(Library, current.library_id)
        media = await session.scalar(select(MediaFile).where(MediaFile.id == current.media_file_id))
        assert media is not None
        from server.fs.library import apply_probe_metadata
        from tests.fakes.ffprobe import probe_output

        path = Path(media.path)
        _, refreshed = await apply_probe_metadata(
            session,
            path=path,
            stat=path.stat(),
            raw=json.loads(probe_output(path)),
            row=media,
            library=library,
            purpose="current",
        )
        await session.commit()
        assert refreshed.duration_s != 60
    h, conn = hub(env), FakeConn([hello()])
    task = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        probe = conn.messages("job")[0]["job"]["probe"]
        assert probe["duration_s"] == 60 and probe["size_bytes"] == 6
        assert probe["raw"] == RAW
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("node socket closed")])
async def test_post_action_errors(env, error, caplog):
    async with post_action_case(env) as case:
        entry = case.hub.entries["node-a"]
        entry.conn = FailingConn(error)
        await asyncio.wait_for(
            case.hub._dispatch_post_actions(
                entry, case.jid, case.offer["attempt_id"], case.offer["output_path"], case.probe, [case.action], [], []
            ),
            2,
        )
        results = (await attempt(env, case.jid))[0].action_results
        assert results and not results[0]["ok"]
        assert "actions_pending" in await events(env, case.jid)
        assert "actions_finished" in await events(env, case.jid)
        assert not (env.workspace / f"jobs/{case.jid}").exists()
        assert any(
            record.levelno == logging.ERROR and record.exc_info is not None and record.exc_info[1] is error
            for record in caplog.records
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["dispatch", "journal"])
async def test_completion_failure_diagnostics(env, monkeypatch, caplog, delivery):
    async with post_action_case(env) as case:
        cause = OSError("sidecar destination is read-only")
        failure = RuntimeError("artifact publication stopped")

        async def fail_publication(*_args, **_kwargs):
            raise failure from cause

        monkeypatch.setattr("server.nodehub.hub.publish_artifacts", fail_publication)
        if delivery == "dispatch":
            dispatch = asyncio.create_task(
                case.hub._dispatch_post_actions(
                    case.hub.entries["node-a"],
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["output_path"],
                    case.probe,
                    [case.action],
                    [],
                    [],
                )
            )
            try:
                await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=True)],
                    ),
                )
                with pytest.raises(RuntimeError) as raised:
                    await asyncio.wait_for(dispatch, 2)
            finally:
                if not dispatch.done():
                    dispatch.cancel()
                await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
        else:
            await case.hub._prepare_post_actions(case.jid, case.offer["attempt_id"], [case.action])
            with pytest.raises(RuntimeError) as raised:
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=True)],
                    ),
                )
        assert raised.value is failure and raised.value.__cause__ is cause
        assert "actions_finished" in await events(env, case.jid)
        assert "action_report" in await events(env, case.jid)
        assert not (env.workspace / f"jobs/{case.jid}").exists()
        assert any(
            record.levelno == logging.ERROR and record.exc_info is not None and record.exc_info[1] is failure
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_post_failure_diagnostics(env):
    async with post_action_case(env) as case:
        await resolve_action(case, ok=False, error="mkvpropedit could not read the subtitle track")
        assert "actions_finished" in await events(env, case.jid)
        assert (await job(env, case.jid)).warning == "mkvpropedit could not read the subtitle track"


async def job(env, jid):
    async with env.factory() as s:
        return await get_job(s, jid)


async def node(env, name="node-a"):
    async with env.factory() as s:
        return await get_node(s, name)


async def events(env, jid):
    async with env.factory() as s:
        return list(
            (await s.execute(select(JobEvent.type).where(JobEvent.job_id == jid).order_by(JobEvent.id))).scalars()
        )


async def attempt(env, jid):
    async with env.factory() as s:
        return (
            (await s.execute(select(JobAttempt).where(JobAttempt.job_id == jid).order_by(JobAttempt.id)))
            .scalars()
            .all()
        )


def finished(jid, aid, path, result="failed", **kw):
    return Finished(job_id=jid, attempt_id=aid, result=result, log_path=path, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_journal_finish_validates(env, monkeypatch, valid):
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 2)
    assert (await job(env, jid)).status == JobStatus.CLAIMED
    report = finished(
        jid,
        offer["attempt_id"],
        offer["log_path"],
        "succeeded",
        output_path=offer["output_path"],
        output_probe=ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[]),
    )
    append_message(env.workspace, offer["log_path"], report.model_dump(mode="json"))
    if not valid:

        async def invalid_probe(*_args):
            raise ValueError("invalid journal artifact")

        monkeypatch.setattr("server.nodehub.hub.run_ffprobe", invalid_probe)
    recovered = hub(env)
    env.clock.advance(1)
    await asyncio.wait_for(recovered.recover_startup(), 3)
    await asyncio.wait_for(recovered.sweep_once(), 3)
    assert (await job(env, jid)).status == (JobStatus.SUCCEEDED if valid else JobStatus.FAILED)
    assert (await attempt(env, jid))[0].log_data == b"log"
    assert (env.library / "movie.mkv").exists() is valid
    if valid:
        assert (env.library / "movie.mkv").read_bytes() == b"output"
        assert not (env.library / "movie.avi").exists()
        assert (await events(env, jid)).count("succeeded") == 1
    else:
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert (await job(env, jid)).error is not None


@pytest.mark.asyncio
async def test_journal_batches_recover(writer_env, monkeypatch):
    env = writer_env
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    ids = [await file_job(env, name=f"movie-{index}.avi") for index in range(3)]
    h, conn = hub(env), FakeConn([hello(transcodes=3)])
    connection = await serve(h, conn)
    try:
        await h.set_limits(h.entries["node-a"].node_db_id, max_transcodes=3, max_healthchecks=1, max_mkv_stats=1)
        assert await h.dispatch_once() == 3
        offers = [message["job"] for message in conn.messages("job")]
        for offer in offers:
            materialize_offer(h, offer)
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
    for offer in offers:
        append_message(
            env.workspace,
            offer["log_path"],
            finished(offer["job_id"], offer["attempt_id"], offer["log_path"]).model_dump(mode="json"),
        )
    recovered = hub(env)
    await asyncio.wait_for(recovered.recover_startup(), 3)
    for jid in ids:
        assert (await job(env, jid)).status == JobStatus.FAILED
        assert (await attempt(env, jid))[0].log_data == b"log"
        assert not (env.workspace / f"jobs/{jid}").exists()
    assert all((env.library / f"movie-{index}.avi").read_bytes() == b"source" for index in range(3))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_reconnect_batches_restore(writer_env, monkeypatch, missing):
    env = writer_env
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    ids = [await file_job(env, name=f"movie-{index}.avi") for index in range(3)]
    h, conn = hub(env), FakeConn([hello(transcodes=3)])
    connection = await serve(h, conn)
    replacement = None
    reconnecting = None
    try:
        await h.set_limits(h.entries["node-a"].node_db_id, max_transcodes=3, max_healthchecks=1, max_mkv_stats=1)
        assert await h.dispatch_once() == 3
        offers = [message["job"] for message in conn.messages("job")]
        for offer in offers:
            materialize_offer(h, offer)
        active = [
            {"job_id": offer["job_id"], "attempt_id": offer["attempt_id"]}
            for offer in offers
            if not missing or offer["job_id"] != ids[1]
        ]
        invalid = {"job_id": ids[1], "attempt_id": offers[1]["attempt_id"] + 1000}
        replacement = FakeConn([hello(transcodes=3, active_attempts=[*active, invalid])])
        reconnecting = await serve(h, replacement)
        assert replacement.messages("welcome")[0]["active_attempts"] == active
        assert set(h.entries["node-a"].in_flight) == {item["job_id"] for item in active}
        for offer in offers:
            absent = missing and offer["job_id"] == ids[1]
            assert (await job(env, offer["job_id"])).status == (JobStatus.FAILED if absent else JobStatus.CLAIMED)
            assert (env.workspace / offer["output_path"]).exists() is not absent
        assert all((env.library / f"movie-{index}.avi").read_bytes() == b"source" for index in range(3))
    finally:
        conn.disconnect()
        if replacement is not None:
            replacement.disconnect()
        tasks = [connection, *([reconnecting] if reconnecting is not None else [])]
        await asyncio.wait_for(asyncio.gather(*tasks), 3)


@pytest.mark.asyncio
async def test_sweep_batch_cancellation(writer_env, monkeypatch):
    env = writer_env
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    ids = [await file_job(env, name=f"movie-{index}.avi") for index in range(3)]
    h, conn = hub(env), FakeConn([hello(transcodes=3)])
    connection = await serve(h, conn)
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = hub_module.snapshot_attempt_log
    sweeping = None

    async def blocked(session, workspace, current):
        if current.job_id == ids[1]:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        await snapshot(session, workspace, current)

    try:
        await h.set_limits(h.entries["node-a"].node_db_id, max_transcodes=3, max_healthchecks=1, max_mkv_stats=1)
        assert await h.dispatch_once() == 3
        offers = [message["job"] for message in conn.messages("job")]
        for offer in offers:
            materialize_offer(h, offer)
        env.clock.advance(h.lease_ttl_s + 1)
        monkeypatch.setattr(hub_module, "snapshot_attempt_log", blocked)
        sweeping = asyncio.create_task(h.sweep_once())
        await asyncio.wait_for(entered.wait(), 3)
        sweeping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(sweeping, 3)
        assert (await job(env, ids[0])).status == JobStatus.FAILED
        assert ids[0] not in h.entries["node-a"].in_flight
        assert not (env.workspace / offers[0]["output_path"]).exists()
        for jid, offer in zip(ids[1:], offers[1:], strict=True):
            assert (await job(env, jid)).status == JobStatus.CLAIMED
            assert jid in h.entries["node-a"].in_flight
            assert (env.workspace / offer["output_path"]).read_bytes() == b"output"
        release.set()
        await asyncio.wait_for(h.sweep_once(), 3)
        assert not h.entries["node-a"].in_flight
        for jid in ids:
            assert (await job(env, jid)).status == JobStatus.FAILED
            assert (await attempt(env, jid))[0].log_data == b"log"
            assert not (env.workspace / f"jobs/{jid}").exists()
        assert all((env.library / f"movie-{index}.avi").read_bytes() == b"source" for index in range(3))
    finally:
        release.set()
        if sweeping is not None:
            sweeping.cancel()
            await asyncio.wait_for(asyncio.gather(sweeping, return_exceptions=True), 3)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_pending_actions_batches(env, monkeypatch):
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    ids = [await file_job(env, name=f"movie-{index}.avi") for index in range(3)]
    async with env.factory() as session:
        await session.execute(update(Job).where(Job.id.in_(ids)).values(status=JobStatus.FAILED.value))
        session.add_all(JobEvent(job_id=jid, type="actions_pending", payload={}) for jid in ids)
        await session.commit()
    h = hub(env)
    pruned = []
    prune = h.staging.prune_job

    async def record(jid):
        pruned.append(jid)
        await prune(jid)

    monkeypatch.setattr(h.staging, "prune_job", record)
    await asyncio.wait_for(h.sweep_once(), 3)
    assert pruned == ids
    assert all((env.library / f"movie-{index}.avi").read_bytes() == b"source" for index in range(3))


@pytest.mark.asyncio
async def test_journal_stage_checked(env, monkeypatch):
    from server.flows.engine import FlowResult

    jid = await file_job(env, plan=FFmpegPlan())
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)

    async def gate(_session, _job, _output, _source, stage):
        return FlowResult(status="matched", plan=plan()) if stage["number"] == 1 else None, [], None, [], []

    monkeypatch.setattr(h, "_post_gate", gate)
    try:
        assert await h.dispatch_once() == 1
        first = conn.messages("job")[0]["job"]
        first_output = materialize_offer(h, first)
        report = finished(
            jid,
            first["attempt_id"],
            first["log_path"],
            "succeeded",
            output_path=first["output_path"],
            output_probe=ProbeData(file_path=first["output_path"], duration_s=60, size_bytes=6, streams=[]),
        )
        append_message(env.workspace, first["log_path"], report.model_dump(mode="json"))
        await h.read_journals()
        assert (await job(env, jid)).status == JobStatus.RUNNING
        assert await h.dispatch_once() == 1
        second = conn.messages("job")[1]["job"]
        assert second["attempt_id"] == first["attempt_id"] and second["log_path"] != first["log_path"]
        second_output = materialize_offer(h, second)
        append_message(env.workspace, second["log_path"], report.model_dump(mode="json"))
        foreign = finished(jid, second["attempt_id"] + 1, second["log_path"], "cancelled")
        append_message(env.workspace, second["log_path"], foreign.model_dump(mode="json"))
        await h.read_journals()
        current = await job(env, jid)
        assert current.status == JobStatus.CLAIMED and current.node_id is not None
        assert first_output.read_bytes() == second_output.read_bytes() == b"output"
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
        failed = finished(jid, second["attempt_id"], second["log_path"], "failed")
        append_message(env.workspace, second["log_path"], failed.model_dump(mode="json"))
        await h.read_journals()
        await h.read_journals()
        assert (await job(env, jid)).status == JobStatus.FAILED
        assert (await events(env, jid)).count("failed") == 1
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 2)


@pytest.mark.asyncio
async def test_journal_expiry_checked(env):
    jid = await file_job(env)
    h, conn = hub(env, lease_ttl_s=1), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        output = materialize_offer(h, offer)
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 2)
    with workspace_lock(output.parent):
        env.clock.advance(2)
        await h.sweep_once()
        assert (await job(env, jid)).status == JobStatus.FAILED
        late = finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            "succeeded",
            output_path=offer["output_path"],
            output_probe=ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[]),
        )
        append_message(env.workspace, offer["log_path"], late.model_dump(mode="json"))
        await h.read_journals()
        await h.sweep_once()
        assert (await job(env, jid)).status == JobStatus.FAILED
        assert (await attempt(env, jid))[0].result == "failed"
        assert output.read_bytes() == b"output"
        assert (env.workspace / offer["log_path"]).read_bytes() == b"log"
    assert (env.library / "movie.avi").read_bytes() == b"source"
    assert not (env.library / "movie.mkv").exists()


@pytest.mark.asyncio
async def test_journal_actions_finish(env):
    async with post_action_case(env) as case:
        output = materialize_offer(case.hub, case.offer)
        sidecar = output.with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        assert await case.hub._prepare_post_actions(case.jid, case.offer["attempt_id"], [case.action])
        case.conn.disconnect()
        await asyncio.wait_for(case.task, 2)
        assert "actions_finished" not in await events(env, case.jid)
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))])
            ],
        )
        append_message(env.workspace, case.offer["log_path"], report.model_dump(mode="json"))
        append_message(env.workspace, case.offer["log_path"], report.model_dump(mode="json"))
        recovered = hub(env)
        await asyncio.wait_for(recovered.recover_startup(), 3)
        await asyncio.wait_for(recovered.sweep_once(), 3)
        assert not case.conn.messages("action_task")
        assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
        assert not output.exists()
        assert (await events(env, case.jid)).count("action_report") == 1
        assert (await events(env, case.jid)).count("action_results") == 1
        assert (await events(env, case.jid)).count("actions_finished") == 1
        assert (await attempt(env, case.jid))[0].action_results == [
            report.results[0]
            .model_copy(update={"artifacts": [str(env.library / "movie.eng.srt")]})
            .model_dump(mode="json")
        ]


@pytest.mark.asyncio
async def test_offline_actions_finished(env):
    from server.workspace import post_action_jobs, cleanup_workspace

    async with post_action_case(env) as case:
        owner = case.hub.entries["node-a"]
        materialize_offer(case.hub, case.offer)
        assert await case.hub._prepare_post_actions(case.jid, case.offer["attempt_id"], [case.action])
        case.conn.disconnect()
        await asyncio.wait_for(case.task, 2)
        await asyncio.wait_for(
            case.hub._dispatch_post_actions(
                owner, case.jid, case.offer["attempt_id"], case.offer["output_path"], case.probe, [case.action], [], []
            ),
            2,
        )
        await cleanup_workspace(env.factory, env.workspace)
        async with env.factory() as session:
            assert await session.scalar(post_action_jobs().where(Job.id == case.jid)) is None
        stored = (await attempt(env, case.jid))[0]
        assert stored.action_results and not stored.action_results[0]["ok"]
        assert stored.log_data == b"log"
        assert not case.conn.messages("action_task")
        assert "actions_finished" in await events(env, case.jid)
        assert not (env.workspace / f"jobs/{case.jid}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", [None, -1, 1000])
async def test_startup_normalizes_lease(env, lease):
    jid = await file_job(env)
    original, conn = hub(env), FakeConn([hello()])
    connection = await serve(original, conn)
    try:
        assert await original.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(original, offer)
        await original._handle_progress(
            original.entries["node-a"], Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=10)
        )
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 2)
    async with env.factory() as session:
        current = await session.get(Job, jid)
        current.lease_expires_at = None if lease is None else T0 + timedelta(seconds=lease)
        stored_node = await session.get(NodeRow, current.node_id)
        stored_node.state = "online"
        stored_node.workspace_ready = True
        await session.commit()
    env.clock.advance(1)
    recovered = hub(env)
    await recovered.recover_startup()
    current = await job(env, jid)
    assert current.status == JobStatus.RUNNING and current.node_id is not None
    assert current.lease_expires_at == env.clock() + timedelta(seconds=recovered.lease_ttl_s)
    assert current.progress["percent"] == 10
    stored_node = await node(env)
    assert stored_node.state == "offline" and not stored_node.workspace_ready
    assert (await attempt(env, jid))[0].id == offer["attempt_id"]
    assert (await attempt(env, jid))[0].result is None
    replacement = FakeConn([hello(active_attempts=[{"job_id": jid, "attempt_id": offer["attempt_id"]}])])
    replacement_connection = await serve(recovered, replacement)
    try:
        assert jid in recovered.entries["node-a"].in_flight
        assert await recovered.dispatch_once() == 0
        assert not replacement.messages("job")
        assert (env.workspace / offer["output_path"]).read_bytes() == b"output"
        assert (env.library / "movie.avi").read_bytes() == b"source"
        await recovered._handle_finished(
            recovered.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"], "failed")
        )
        assert (await job(env, jid)).status == JobStatus.FAILED
        assert len(await attempt(env, jid)) == 1
    finally:
        replacement.disconnect()
        await asyncio.wait_for(replacement_connection, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "missing", "staging_error", "dispatch_error"])
async def test_continuation_burn(env, monkeypatch, outcome: str) -> None:
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from tests.helpers import plugin_host

    flow = FlowDefinition.model_validate(
        {
            "entry": "burn",
            "nodes": [
                {"id": "burn", "type": "plugin", "plugin": "transcode.burn_subtitles"},
                {"id": "dates", "type": "plugin", "plugin": "action.keep_dates"},
                {"id": "done", "type": "terminal", "outcome": "done"},
            ],
            "edges": [
                {"src": "burn", "port": "plan", "dst": "dates"},
                {"src": "burn", "port": "skip", "dst": "done"},
                {"src": "dates", "port": "next", "dst": "done"},
            ],
        }
    )
    jid = await file_job(env, plan=FFmpegPlan(), continuation=FlowContinuation(flow=flow, node="burn"))
    stamp = (env.library / "movie.avi").stat().st_mtime_ns
    if outcome != "missing":
        (env.library / "movie.eng.srt").write_bytes(b"captions")
    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        h, conn = hub(env, plugin_host=host), FakeConn([hello()])
        task = await serve(h, conn)
        previous_decisions = (await job(env, jid)).decision_log

        async def fail_staging(attempt_id, filename):
            assert filename == "movie.eng.srt"
            async with env.factory() as session:
                current = await session.get(Job, jid)
                assert current is not None and current.status == JobStatus.VERIFYING
                assert current.decision_log == previous_decisions
                stage = await h._stage_event(session, (await attempt(env, jid))[0])
                assert stage is not None and stage.payload["result"] == "succeeded"
                assert not list(
                    await session.scalars(
                        select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "post_actions")
                    )
                )
            raise OSError("subtitle staging unavailable")

        if outcome == "staging_error":
            monkeypatch.setattr(h.staging, "stage_continuation_subtitle", fail_staging)
        try:
            assert await h.dispatch_once() == 1
            first = conn.messages("job")[0]["job"]
            (env.workspace / first["output_path"]).write_bytes(b"four")
            (env.workspace / first["log_path"]).write_bytes(b"cleanup")
            await h._handle_finished(
                h.entries["node-a"],
                finished(
                    jid,
                    first["attempt_id"],
                    first["log_path"],
                    "succeeded",
                    output_path=first["output_path"],
                    output_probe=ProbeData.model_validate(first["probe"]).model_copy(
                        update={"file_path": first["output_path"], "size_bytes": 4}
                    ),
                ),
            )
            if outcome == "staging_error":
                current = await job(env, jid)
                assert current.status == JobStatus.FAILED
                assert current.error
                assert current.decision_log == previous_decisions
                async with env.factory() as session:
                    post = (
                        await session.scalars(
                            select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "post_actions")
                        )
                    ).one()
                    assert post.payload == {"error": current.error}
                    assert post.attempt_id == first["attempt_id"]
                assert await h.dispatch_once() == 0
                assert (env.library / "movie.avi").read_bytes() == b"source"
                assert not (env.library / "movie.mkv").exists()
            elif outcome == "missing":
                assert (await job(env, jid)).status == JobStatus.SUCCEEDED
                assert await h.dispatch_once() == 0
                assert (env.library / "movie.mkv").read_bytes() == b"four"
                async with env.factory() as session:
                    rows = (
                        await session.scalars(
                            select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "post_actions")
                        )
                    ).all()
                    assert all(row.attempt_id == first["attempt_id"] for row in rows)
                    assert any(step.get("reason") is not None for row in rows for step in row.payload.get("steps", []))
            elif outcome == "dispatch_error":

                def fail_subtitle(*_args):
                    raise PermissionError("subtitle access denied")

                monkeypatch.setattr("server.nodehub.hub.staged_subtitle", fail_subtitle)
                assert await h.dispatch_once() == 0
                current = (await attempt(env, jid))[0]
                assert current.result == "interrupted"
                assert current.log_data == b"cleanup"
                async with env.factory() as session:
                    assert jid not in await session.scalars(recovery_jobs())
                assert not (env.workspace / first["output_path"]).exists()
                assert (env.library / "movie.avi").read_bytes() == b"source"
                assert not (env.library / "movie.mkv").exists()
            else:
                assert (env.library / "movie.avi").read_bytes() == b"source"
                assert await h.dispatch_once() == 1
                second = conn.messages("job")[1]["job"]
                assert second["source_path"] == first["output_path"]
                assert second["subtitle_path"] == f"jobs/{jid}/subtitle.srt"
                assert (env.workspace / second["subtitle_path"]).read_bytes() == b"captions"
                assert second["plan"]["burn_subtitle"]["filename"] == "movie.eng.srt"
                async with env.factory() as session:
                    stage = await h._stage_event(session, (await attempt(env, jid))[0])
                    assert stage is not None
                    assert "subtitles=filename=" in " ".join(stage.payload["argv"])
                (env.workspace / second["output_path"]).write_bytes(b"xx")
                (env.workspace / second["log_path"]).write_bytes(b"burn")
                await h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        second["attempt_id"],
                        second["log_path"],
                        outcome,
                        output_path=second["output_path"] if outcome == "succeeded" else None,
                        output_probe=ProbeData.model_validate(second["probe"]).model_copy(
                            update={"file_path": second["output_path"], "size_bytes": 2}
                        )
                        if outcome == "succeeded"
                        else None,
                    ),
                )
                if outcome == "succeeded":
                    assert (await job(env, jid)).status == JobStatus.SUCCEEDED
                    assert (env.library / "movie.mkv").read_bytes() == b"xx"
                    assert (env.library / "movie.mkv").stat().st_mtime_ns == stamp
                else:
                    assert (await job(env, jid)).status == JobStatus.FAILED
                    assert (env.library / "movie.avi").read_bytes() == b"source"
                    assert not (env.library / "movie.mkv").exists()
            assert not (env.workspace / "jobs" / str(jid)).exists()
            assert not conn.messages("action_task")
        finally:
            conn.disconnect()
            await asyncio.wait_for(task, 2)
            await h.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["succeeded", "ceiling", "failed", "cancelled", "waiting", "restart"])
@pytest.mark.parametrize("paused", [False, True])
async def test_staged_output(env, result, paused, monkeypatch):
    import httpx

    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from server.notifications import NotificationService
    from tests.helpers import plugin_host

    notify = NotifyExternal(application="radarr", url="https://radarr.test", api_key="private-api-key")
    second_notify = NotifyExternal(application="radarr", url="https://second.test", api_key="private-api-key")
    flow = FlowDefinition.model_validate(
        {
            "entry": "subs",
            "nodes": [
                {
                    "id": "subs",
                    "type": "plugin",
                    "plugin": "action.extract_subs",
                    "inputs": {"formats": ["srt"], "languages": ["eng"]},
                },
                {"id": "dates", "type": "plugin", "plugin": "action.keep_dates"},
                {
                    "id": "notify",
                    "type": "plugin",
                    "plugin": "action.notify_external",
                    "inputs": notify.model_dump(mode="json", exclude={"kind"}),
                },
                {
                    "id": "second_notify",
                    "type": "plugin",
                    "plugin": "action.notify_external",
                    "inputs": second_notify.model_dump(mode="json", exclude={"kind"}),
                },
                {
                    "id": "encode",
                    "type": "plugin",
                    "plugin": "transcode.video",
                    "inputs": {
                        "encoder_priority": ["hevc_nvenc"] if result in {"waiting", "restart"} else ["libx264"],
                        "quality": 23,
                        "ratio": 0.5,
                        "target": "avc",
                        "size_ceiling_ratio": 1.1,
                    },
                },
                {"id": "ratio", "type": "plugin", "plugin": "inspector.output_ratio", "inputs": {"min_ratio": 0.4}},
                {"id": "done", "type": "terminal", "outcome": "done"},
            ],
            "edges": [
                {"src": "subs", "port": "next", "dst": "dates"},
                {"src": "dates", "port": "next", "dst": "notify"},
                {"src": "notify", "port": "next", "dst": "second_notify"},
                {"src": "second_notify", "port": "next", "dst": "encode"},
                {"src": "encode", "port": "plan", "dst": "ratio"},
                {"src": "ratio", "port": "match", "dst": "done"},
            ],
        }
    )
    jid = await file_job(
        env, plan=FFmpegPlan(), continuation=FlowContinuation(flow=flow, node="subs"), pre_actions=[notify]
    )
    activity_paused = False
    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        h, c = hub(env, plugin_host=host, activity_paused=lambda: activity_paused), FakeConn([hello()])
        task = await serve(h, c)
        try:
            assert await h.dispatch_once() == 1
            activity_paused = paused
            first = c.messages("job")[0]["job"]
            output = env.workspace / first["output_path"]
            output.write_bytes(b"four")
            (env.workspace / first["log_path"]).write_bytes(b"cleanup log")
            probe = ProbeData.model_validate(
                {"file_path": first["output_path"], "duration_s": 600, "size_bytes": 4, "streams": []}
            )
            await h._handle_finished(
                h.entries["node-a"],
                finished(
                    jid,
                    first["attempt_id"],
                    first["log_path"],
                    "succeeded",
                    output_path=first["output_path"],
                    output_probe=probe,
                ),
            )
            assert (env.library / "movie.avi").read_bytes() == b"source"
            assert not (env.library / "movie.mkv").exists()
            async with env.factory() as session:
                assert not (await session.scalars(select(NotificationDelivery))).all()
            assert "private-api-key" not in json.dumps(c.messages("job"))
            rows = await attempt(env, jid)
            assert len(rows) == 1 and rows[0].result is None
            offered = await h.dispatch_once()
            if result in {"waiting", "restart"}:
                assert offered == 0
                assert (await job(env, jid)).warning is not None
                if result == "waiting":
                    assert await h.cancel_job(jid)
                    assert (await job(env, jid)).status == JobStatus.CANCELLED
                else:
                    from server.queue.store import requeue_startup

                    async with env.factory() as session:
                        assert await requeue_startup(session, env.clock()) == 0
                        await session.commit()
                    await h.recover_startup()
                    current = await job(env, jid)
                    assert current.status == JobStatus.RUNNING and current.node_id is None
                    assert await h.dispatch_once() == 0
                    h.entries["node-a"].capabilities.encoders.append("hevc_nvenc")
                    assert await h.dispatch_once() == 1
                    retry = c.messages("job")[-1]["job"]
                    assert retry["attempt_id"] == first["attempt_id"]
                    assert retry["source_path"] == first["output_path"]
                    assert retry["probe"]["size_bytes"] == 4
                    assert retry["plan"]["video"]["encoder"] == "hevc_nvenc"
            else:
                assert offered == 1
                second = c.messages("job")[1]["job"]
                assert second["attempt_id"] == first["attempt_id"]
                assert second["source_path"] == first["output_path"]
                assert second["probe"]["size_bytes"] == 4
                assert second["actions"] == [
                    ExtractSubtitles(
                        formats=["srt"], languages=["eng"], plugin_id="action.extract_subs", config_version=1
                    ).model_dump(mode="json")
                ]
                async with env.factory() as session:
                    stage = await h._stage_event(session, rows[0])
                    assert stage is not None and stage.payload["post_actions"] == [
                        PreserveTimestamps(plugin_id="action.keep_dates", config_version=1).model_dump(mode="json"),
                        notify.model_copy(
                            update={"plugin_id": "action.notify_external", "config_version": 2}
                        ).model_dump(mode="json"),
                        second_notify.model_copy(
                            update={"plugin_id": "action.notify_external", "config_version": 2}
                        ).model_dump(mode="json"),
                    ]
                (env.workspace / second["log_path"]).write_bytes(b"encode log")
                size = 5 if result == "ceiling" else 2
                (env.workspace / second["output_path"]).write_bytes(b"x" * size)
                probe = probe.model_copy(update={"file_path": second["output_path"], "size_bytes": size})
                succeeded = result in {"succeeded", "ceiling"}
                sidecar = Path(second["output_path"]).with_suffix(".eng.srt")
                (env.workspace / sidecar).write_bytes(b"subtitles")
                assert not (env.library / "movie.eng.srt").exists()
                report = finished(
                    jid,
                    second["attempt_id"],
                    second["log_path"],
                    "succeeded" if succeeded else result,
                    output_path=second["output_path"] if succeeded else None,
                    output_probe=probe if succeeded else None,
                    action_results=[ActionOutcome(kind="extract_subtitles", ok=True, artifacts=[str(sidecar)])],
                )
                await h._handle_finished(h.entries["node-a"], report)
                if result == "succeeded":
                    await h._handle_finished(h.entries["node-a"], report)
                row = await job(env, jid)
                async with env.factory() as session:
                    deliveries = (await session.scalars(select(NotificationDelivery))).all()
                    assert len(deliveries) == (2 if result == "succeeded" else 0)
                    if deliveries:
                        destinations = set()
                        for delivery in deliveries:
                            event = await session.get(JobEvent, delivery.job_event_id)
                            assert event is not None
                            assert event.payload["path"] == str(env.library / "movie.mkv")
                            assert event.payload["original_filename"] == "movie.avi"
                            destinations.add(event.payload["action"]["url"])
                        assert destinations == {"https://radarr.test/", "https://second.test/"}
                assert "private-api-key" not in json.dumps(c.messages("job"))
                assert row.status == (
                    JobStatus.SUCCEEDED
                    if result == "succeeded"
                    else JobStatus.CANCELLED
                    if result == "cancelled"
                    else JobStatus.FAILED
                )
                if result == "succeeded":
                    assert (env.library / "movie.mkv").read_bytes() == b"xx"
                    assert (env.library / "movie.eng.srt").read_bytes() == b"subtitles"
                    assert not c.messages("action_task")
                    assert not (env.library / "movie.avi").exists()
                    requests = []

                    def respond(request):
                        requests.append((request.url.host, request.method))
                        if request.url.host == "radarr.test":
                            return httpx.Response(503)
                        if request.method == "GET":
                            return httpx.Response(200, json={"movie": {"id": 7}})
                        return httpx.Response(201, json={"id": 23, "status": "completed"})

                    client = httpx.AsyncClient
                    monkeypatch.setattr(
                        "server.external_notifications.arr.httpx.AsyncClient",
                        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
                    )
                    for _ in range(3):
                        await NotificationService(env.factory).deliver_pending()
                    async with env.factory() as session:
                        deliveries = (
                            await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
                        ).all()
                        assert [(item.status, item.attempts) for item in deliveries] == [("failed", 3), ("sent", 1)]
                        current = await session.get(Job, jid)
                        assert current is not None and current.status == JobStatus.SUCCEEDED
                    assert requests.count(("second.test", "POST")) == 1
                else:
                    assert (env.library / "movie.avi").read_bytes() == b"source"
                    assert not (env.library / "movie.eng.srt").exists()
                    assert not (env.library / "movie.mkv").exists()
                rows = await attempt(env, jid)
                assert len(rows) == 1 and rows[0].source_size == 6
                assert rows[0].log_data == b"cleanup log\nencode log"
                async with env.factory() as session:
                    stages = list(
                        (
                            await session.scalars(
                                select(JobEvent)
                                .where(JobEvent.job_id == jid, JobEvent.type == "stage")
                                .order_by(JobEvent.id)
                            )
                        ).all()
                    )
                assert len(stages) == 2 and stages[0].payload["output_probe"]["size_bytes"] == 4
                assert stages[1].payload["probe"]["size_bytes"] == 4
                if result == "ceiling":
                    inspected = stages[1].payload["output_probe"]
                    assert inspected["file_path"] == second["output_path"] and inspected["size_bytes"] == 5
                    assert inspected["streams"] and not probe.streams
        finally:
            c.disconnect()
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_cancel_stage_handoff(env, monkeypatch):
    from server.flows.engine import FlowResult

    jid = await file_job(env, plan=FFmpegPlan())
    h, c = hub(env), FakeConn([hello()])
    task = await serve(h, c)
    try:
        await h.dispatch_once()
        offer = c.messages("job")[0]["job"]
        materialize_offer(h, offer)

        async def cancel_gate(*args):
            assert await h.cancel_job(jid)
            return FlowResult(status="matched", plan=plan()), [], None, [], []

        monkeypatch.setattr(h, "_post_gate", cancel_gate)
        probe = ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[])
        await asyncio.wait_for(
            h._handle_finished(
                h.entries["node-a"],
                finished(
                    jid,
                    offer["attempt_id"],
                    offer["log_path"],
                    "succeeded",
                    output_path=offer["output_path"],
                    output_probe=probe,
                ),
            ),
            3,
        )
        assert (await job(env, jid)).status == JobStatus.CANCELLED
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
        assert await h.dispatch_once() == 0
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("invalid", ["render", "plan", "probe", "actions", "offer"])
async def test_invalid_continuation_dispatch(env, protected: bool, invalid: str) -> None:
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from tests.helpers import plugin_host

    flow = FlowDefinition.model_validate(
        {
            "entry": "encode",
            "nodes": [
                {
                    "id": "encode",
                    "type": "plugin",
                    "plugin": "transcode.video",
                    "inputs": {"encoder": "libx264", "extras": ["-c:v", "libx265"]},
                },
                {"id": "done", "type": "terminal", "outcome": "done"},
            ],
            "edges": [{"src": "encode", "port": "plan", "dst": "done"}],
        }
    )
    identifier = await file_job(
        env, plan=FFmpegPlan(), priority=10, continuation=FlowContinuation(flow=flow, node="encode")
    )
    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        h, c = hub(env, plugin_host=host), FakeConn([hello()])
        task = await serve(h, c)
        try:
            assert await h.dispatch_once() == 1
            first = c.messages("job")[0]["job"]
            output = env.workspace / first["output_path"]
            output.write_bytes(b"four")
            (env.workspace / first["log_path"]).write_bytes(b"cleanup log")
            await h._handle_finished(
                h.entries["node-a"],
                finished(
                    identifier,
                    first["attempt_id"],
                    first["log_path"],
                    "succeeded",
                    output_path=first["output_path"],
                    output_probe=ProbeData(file_path=first["output_path"], duration_s=600, size_bytes=4, streams=[]),
                ),
            )
            assert (await job(env, identifier)).status == JobStatus.RUNNING
            assert (await job(env, identifier)).node_id is None
            async with env.factory() as session:
                current = await session.get(JobAttempt, first["attempt_id"])
                stage = await h._stage_event(session, current)
                assert stage is not None and stage.payload["plan"]["extras"] == ["-c:v", "libx265"]
                if invalid != "render":
                    data = {**stage.payload, "plan": {**stage.payload["plan"], "extras": []}}
                    if invalid == "plan":
                        data["plan"]["container"] = "invalid"
                    elif invalid == "probe":
                        data["probe"] = {**data["probe"], "duration_s": "invalid"}
                    elif invalid == "actions":
                        data["actions"] = [{"kind": "invalid"}]
                    else:
                        data["probe"] = {**data["probe"], "file_path": "/outside/workspace.mkv"}
                    stage.payload = data
                if protected:
                    session.add(JobEvent(job_id=identifier, type="finalization_unprepared", payload={}))
                await session.commit()
            healthy = await file_job(env, name="healthy.avi")
            assert await h.dispatch_once() == 1
            assert c.messages("job")[-1]["job"]["job_id"] == healthy
            failed_job = await job(env, identifier)
            assert failed_job.status == JobStatus.FAILED and failed_job.attempts == 1
            assert failed_job.error is not None
            assert failed_job.node_id is None and failed_job.lease_expires_at is None
            rows = await attempt(env, identifier)
            assert len(rows) == 1 and rows[0].status == JobStatus.FAILED and rows[0].result == "failed"
            assert rows[0].source_size == 6 and rows[0].log_data == b"cleanup log"
            async with env.factory() as session:
                event = await session.scalar(
                    select(JobEvent).where(JobEvent.job_id == identifier, JobEvent.type == "failed")
                )
                assert event is not None and event.attempt_id == first["attempt_id"]
                assert not (await session.scalars(select(FinalizationJournal))).all()
            assert (env.library / "movie.avi").read_bytes() == b"source"
            assert not (env.library / "movie.mkv").exists()
            assert not (env.workspace / "jobs" / str(identifier)).exists()
            assert await h.dispatch_once() == 0
            assert identifier not in h.entries["node-a"].in_flight
        finally:
            c.disconnect()
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_cleanup_finishes(env):
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from tests.helpers import plugin_host

    flow = FlowDefinition.model_validate(
        {
            "entry": "size",
            "nodes": [
                {"id": "size", "type": "condition", "when": {"media.file.size_bytes": {"gte": 5}}},
                {"id": "encode", "type": "plugin", "plugin": "transcode.video", "inputs": {"encoder": "libx264"}},
                {"id": "done", "type": "terminal", "outcome": "done"},
            ],
            "edges": [
                {"src": "size", "port": "true", "dst": "encode"},
                {"src": "size", "port": "false", "dst": "done"},
            ],
        }
    )
    jid = await file_job(
        env, plan=FFmpegPlan(container="original"), continuation=FlowContinuation(flow=flow, node="size")
    )
    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        h, c = hub(env, plugin_host=host), FakeConn([hello()])
        task = await serve(h, c)
        try:
            await h.dispatch_once()
            offer = c.messages("job")[0]["job"]
            (env.workspace / offer["output_path"]).write_bytes(b"four")
            (env.workspace / offer["log_path"]).write_bytes(b"cleanup")
            probe = ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=4, streams=[])
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        offer["attempt_id"],
                        offer["log_path"],
                        "succeeded",
                        output_path=offer["output_path"],
                        output_probe=probe,
                    ),
                ),
                3,
            )
            assert (await job(env, jid)).status == JobStatus.SUCCEEDED
            assert (env.library / "movie.avi").read_bytes() == b"four"
            assert len(c.messages("job")) == 1
            assert (await attempt(env, jid))[0].source_size == 6
        finally:
            c.disconnect()
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_staged_retention(env, monkeypatch):
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from tests.helpers import plugin_host

    flow = FlowDefinition.model_validate(
        {
            "entry": "cleanup",
            "nodes": [
                {
                    "id": "cleanup",
                    "type": "plugin",
                    "plugin": "transcode.remux",
                    "inputs": {
                        "container": "original",
                        "audio_keep_languages": ["eng"],
                    },
                },
                {"id": "done", "type": "terminal", "outcome": "done"},
            ],
            "edges": [{"src": "cleanup", "port": "plan", "dst": "done"}],
        }
    )
    raw = {
        "format": {"duration": "60"},
        "streams": [
            {"index": 0, "codec_name": "h264", "codec_type": "video"},
            {"index": 1, "codec_name": "ac3", "codec_type": "audio", "tags": {"language": "eng"}},
            {"index": 2, "codec_name": "ac3", "codec_type": "audio", "tags": {"language": "jpn"}},
        ],
    }
    monkeypatch.setitem(RAW, "streams", raw["streams"])
    jid = await file_job(env, continuation=FlowContinuation(flow=flow, node="cleanup"))
    async with env.factory() as session:
        library = await session.scalar(select(Library))
        assert library is not None
        library.trash_mode = "transcode"
        await session.commit()
    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        h, c = hub(env, plugin_host=host), FakeConn([hello()])
        task = await serve(h, c)
        try:
            await h.dispatch_once()
            first = c.messages("job")[0]["job"]
            output = env.workspace / first["output_path"]
            output.write_bytes(b"four")
            (env.workspace / first["log_path"]).write_bytes(b"encode")
            Path(f"{output}.probe.json").write_text(json.dumps(raw))
            probe = ProbeData(file_path=first["output_path"], duration_s=60, size_bytes=4, streams=[])
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        first["attempt_id"],
                        first["log_path"],
                        "succeeded",
                        output_path=first["output_path"],
                        output_probe=probe,
                    ),
                ),
                3,
            )
            await h.dispatch_once()
            second = c.messages("job")[1]["job"]
            assert second["plan"]["video"] is None and second["output_path"].endswith(".mkv")
            (env.workspace / second["output_path"]).write_bytes(b"new")
            (env.workspace / second["log_path"]).write_bytes(b"cleanup")
            probe = probe.model_copy(update={"file_path": second["output_path"], "size_bytes": 3})
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        second["attempt_id"],
                        second["log_path"],
                        "succeeded",
                        output_path=second["output_path"],
                        output_probe=probe,
                    ),
                ),
                3,
            )
            assert (await job(env, jid)).status == JobStatus.SUCCEEDED
            assert (env.library / "movie.mkv").read_bytes() == b"new"
            assert next(env.trash.rglob("file.avi")).read_bytes() == b"source"
        finally:
            c.disconnect()
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_idle_hello_availability(env):
    jid = await file_job(env)
    async with env.factory() as session:
        await register_node(session, "node-b", "tok-b")
        await session.commit()
    h = hub(env)
    active = FakeConn([hello()])
    active_task = await serve(h, active)
    idle = FakeConn()
    other = FakeConn([hello(name="node-b", token="tok-b")])
    tasks = [active_task, asyncio.create_task(h.serve_connection(idle))]
    try:
        await asyncio.wait_for(idle.receiving.wait(), 3)
        async with asyncio.timeout(3), h.pause_for_reset():
            assert h.entries["node-a"].paused
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        assert active.messages("job")[0]["job"]["job_id"] == jid
        tasks.append(asyncio.create_task(h.serve_connection(other)))
        await wait_for(lambda: other.messages("workspace_challenge"), other.changed)
        answer_challenge(h, other)
        await wait_for(lambda: other.messages("set_limits"), other.changed)
        assert h.entries["node-b"].conn is other
        assert not idle.sent and not tasks[1].done()
    finally:
        for conn in (active, idle, other):
            conn.disconnect()
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_hello_rejects_unlocked(env):
    h = hub(env)
    conn = FakeConn([hello(token="wrong")])
    async with h.dispatch_lock:
        await asyncio.wait_for(h.serve_connection(conn), 3)
    assert conn.messages("error") and conn.closed is not None
    assert not conn.messages("workspace_challenge") and not h.entries


@pytest.mark.asyncio
async def test_reset_waits_queue(env):
    h = hub(env)
    conn = FakeConn([hello()])
    connection = asyncio.create_task(h.serve_connection(conn))
    resetting = asyncio.Event()
    reset = None

    async def reset_hub():
        resetting.set()
        async with h.pause_for_reset():
            assert h.entries["node-a"].conn is conn
            assert h.entries["node-a"].paused

    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        reset = asyncio.create_task(reset_hub())
        await asyncio.wait_for(resetting.wait(), 3)
        assert not h._reset_active and not reset.done()
        answer_challenge(h, conn)
        await asyncio.wait_for(reset, 3)
        assert not h.entries["node-a"].paused
    finally:
        conn.disconnect()
        tasks = [connection, *([reset] if reset is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_reset_defers_queue(env, monkeypatch, revoke):
    h = hub(env)
    conn = FakeConn([hello()])
    waiting = asyncio.Event()
    original_wait = h._reset_done.wait

    async def wait_reset():
        waiting.set()
        await original_wait()

    monkeypatch.setattr(h._reset_done, "wait", wait_reset)
    task = None
    try:
        async with h.pause_for_reset():
            task = asyncio.create_task(h.serve_connection(conn))
            await asyncio.wait_for(waiting.wait(), 3)
            assert not conn.sent and not h.entries
            if revoke:
                async with env.factory() as session:
                    current = await get_node(session, "node-a")
                    assert current is not None
                    current.token_hash = hash_token("replacement")
                    await session.commit()
        if revoke:
            await asyncio.wait_for(task, 3)
            assert conn.messages("error")[0]["code"] == "auth" and conn.closed == 4401
            assert not conn.messages("workspace_challenge") and not h.entries
        else:
            await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
            answer_challenge(h, conn)
            await wait_for(lambda: conn.messages("set_limits"), conn.changed)
            assert h.entries["node-a"].conn is conn
    finally:
        conn.disconnect()
        if task is not None:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_handshake_ready(env):
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    n = await node(env)
    assert n.state == "online" and n.workspace_ready and n.workspace_free_bytes == 123456
    c.disconnect()
    await asyncio.wait_for(task, 2)
    n = await node(env)
    assert n.state == "offline" and not n.workspace_ready and n.workspace_free_bytes is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "check"])
async def test_handshake_io_drains(env, monkeypatch, phase):
    h, existing = hub(env), FakeConn([hello()])
    async with env.factory() as session:
        await register_node(session, "node-b", "tok")
        await session.commit()
    entered, heartbeated = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    helper_name = "_prepare_challenge" if phase == "prepare" else "_check_challenge"
    helper = getattr(hub_module, helper_name)
    directories = []
    handle_heartbeat = h._handle_heartbeat

    def blocked(root, relative, payload):
        result = helper(root, relative, payload)
        directories.append(root / relative if phase == "prepare" else (root / relative).parent)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return result

    async def heartbeat_handler(entry, msg):
        await handle_heartbeat(entry, msg)
        heartbeated.set()

    new_connection = FakeConn([hello(name="node-b")])
    handshake = None
    connection = await serve(h, existing)
    try:
        monkeypatch.setattr(hub_module, helper_name, blocked)
        monkeypatch.setattr(h, "_handle_heartbeat", heartbeat_handler)
        handshake = asyncio.create_task(h.serve_connection(new_connection))
        if phase == "check":
            await wait_for(lambda: new_connection.messages("workspace_challenge"), new_connection.changed)
            answer_challenge(h, new_connection)
        await asyncio.wait_for(entered.wait(), 3)
        existing.push(Heartbeat(cpu_percent=43).model_dump(mode="json"))
        await asyncio.wait_for(heartbeated.wait(), 2)
        current = await node(env)
        assert current.usage is not None and current.usage["cpu_percent"] == 43
        for _ in range(2):
            handshake.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not handshake.done() and directories[0].is_dir()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handshake, 3)
        assert not directories[0].exists() and not new_connection.messages("welcome")
    finally:
        release.set()
        new_connection.disconnect()
        existing.disconnect()
        if handshake is not None and not handshake.done():
            handshake.cancel()
        try:
            tasks = [connection, *([handshake] if handshake is not None else [])]
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        finally:
            await asyncio.wait_for(h.aclose(), 3)


@pytest.mark.asyncio
async def test_handshake_rejects(env):
    async with env.factory() as s:
        s.add(await register_node(s, "node-a", "right"))
        await s.commit()
    c = FakeConn([hello(token="wrong")])
    await hub(env).serve_connection(c)
    assert c.messages("error")[0]["code"] == "auth" and c.closed == 4401


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["node", "missing", "digest", "permission", "verification", "closure"])
async def test_workspace_failure_diagnostics(env, monkeypatch, caplog, failure):
    h = hub(env, enrollment_token="fleet-secret")
    conn = FakeConn([hello()])
    task = asyncio.create_task(h.serve_connection(conn))
    try:
        await wait_for(lambda: conn.messages("workspace_challenge"), conn.changed)
        challenge = conn.messages("workspace_challenge")[0]
        response_path = h.workspace_dir / challenge["response_path"]
        if failure in {"digest", "permission", "verification"}:
            answer_challenge(h, conn)
            if failure == "digest":
                response_path.write_bytes(b"0" * 64 + b"\n")
            elif failure == "permission":

                def denied(_path):
                    raise PermissionError("Response denied for tok fleet-secret")

                monkeypatch.setattr(Path, "read_bytes", denied)
            else:

                def invalid(*_args):
                    raise ValueError("Unsafe shared mount for tok fleet-secret")

                monkeypatch.setattr("server.nodehub.hub._check_challenge", invalid)
        elif failure == "closure":
            conn.disconnect()
        else:
            conn.push(
                WorkspaceResponse(
                    challenge_id=challenge["challenge_id"],
                    response_path=challenge["response_path"],
                    workspace_ready=failure != "node",
                    workspace_free_bytes=0,
                    error="Shared mount missing for tok fleet-secret" if failure == "node" else None,
                ).model_dump(mode="json")
            )
        await asyncio.wait_for(task, 2)
        assert conn.messages("error")[0]["code"] == "workspace"
        assert not h.entries and not conn.messages("welcome")
        records = [record for record in caplog.records if record.name == "server.nodehub.hub"]
        assert records and all(record.levelno == logging.WARNING for record in records)
        diagnostics = "\n".join(getattr(record, "error", "") for record in records)
        assert diagnostics
        if failure in {"missing", "digest"}:
            assert str(response_path) in diagnostics
        assert "tok" not in caplog.text + diagnostics and "fleet-secret" not in caplog.text + diagnostics
    finally:
        conn.disconnect()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_workspace_creation_diagnostics(env, monkeypatch, caplog):
    h = hub(env, enrollment_token="fleet-secret")

    def denied(*_args):
        raise PermissionError("Challenge directory denied for tok fleet-secret")

    monkeypatch.setattr("server.nodehub.hub._prepare_challenge", denied)
    conn = FakeConn([hello()])
    await asyncio.wait_for(h.serve_connection(conn), 2)
    assert conn.messages("error")[0]["code"] == "workspace"
    diagnostics = "\n".join(getattr(record, "error", "") for record in caplog.records)
    assert diagnostics
    assert "tok" not in caplog.text + diagnostics and "fleet-secret" not in caplog.text + diagnostics


@pytest.mark.asyncio
async def test_enrollment_issues_token(env):
    h = hub(env, enrollment_token="fleet-secret")
    greeting = hello(name=None, token="fleet-secret")
    greeting["capabilities"].update(cpu_model="Intel(R) Core(TM) i9-14900K", gpus=["Intel UHD", "NVIDIA RTX 4090"])
    c = FakeConn([greeting])
    task = await serve(h, c, enroll=True)
    try:
        enrolled = c.messages("enrolled")[0]
        assert enrolled["node_id"].startswith("node-")
        assert enrolled["token"] != "fleet-secret"
        async with env.factory() as session:
            node = await get_node(session, enrolled["node_id"])
            assert node is not None
            assert node.token_hash == hash_token(enrolled["token"])
            assert node.transcode_limit == 1
            assert node.display_name.startswith("i9-igpu-nvidia-")
            assert len(node.display_name.rsplit("-", 1)[-1]) == 4
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_enrollment_rejects_named(env):
    h = hub(env, enrollment_token="fleet-secret")
    c = FakeConn([hello(name="arbitrary", token="fleet-secret")])
    await h.serve_connection(c)
    assert c.messages("error")[0]["code"] == "auth"
    async with env.factory() as session:
        assert await get_node(session, "arbitrary") is None


@pytest.mark.asyncio
async def test_enrollment_workspace_failure(env):
    h = hub(env, enrollment_token="fleet-secret")
    c = FakeConn([hello(name=None, token="fleet-secret")])
    task = asyncio.create_task(h.serve_connection(c))
    await wait_for(lambda: c.messages("workspace_challenge"), c.changed)
    challenge = c.messages("workspace_challenge")[0]
    c.push(
        WorkspaceResponse(
            challenge_id=challenge["challenge_id"],
            response_path=challenge["response_path"],
            workspace_ready=False,
            workspace_free_bytes=0,
        ).model_dump(mode="json")
    )
    await asyncio.wait_for(task, 2)
    assert c.messages("error")[0]["code"] == "workspace"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["closure", "parse", "timeout", "wrong_node"])
async def test_enrollment_save_failure(env, caplog, failure):
    class EnrollmentConn(FakeConn):
        async def recv_json(self):
            if failure == "timeout" and self.messages("enrolled"):
                raise TimeoutError
            return await super().recv_json()

    h = hub(env, enrollment_token="fleet-secret")
    c = EnrollmentConn([hello(name=None, token="fleet-secret")])
    task = asyncio.create_task(h.serve_connection(c))
    try:
        await wait_for(lambda: c.messages("workspace_challenge"), c.changed)
        challenge = c.messages("workspace_challenge")[0]
        path = h.workspace_dir / challenge["response_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hashlib.sha256((h.workspace_dir / challenge["challenge_path"]).read_bytes()).hexdigest() + "\n")
        c.push(
            WorkspaceResponse(
                challenge_id=challenge["challenge_id"],
                response_path=challenge["response_path"],
                workspace_ready=True,
                workspace_free_bytes=10,
            ).model_dump(mode="json")
        )
        await wait_for(lambda: c.messages("enrolled"), c.changed)
        issued_id = c.messages("enrolled")[0]["node_id"]
        if failure == "parse":
            c.push({"v": 1, "type": "enrollment_saved"})
        elif failure == "wrong_node":
            c.push({"v": 1, "type": "enrollment_saved", "node_id": "other-node"})
        elif failure == "closure":
            c.push(None)
        await asyncio.wait_for(task, 2)
        async with env.factory() as session:
            assert await get_node(session, issued_id) is None
        records = [record for record in caplog.records if record.name == "server.nodehub.hub"]
        assert records and all(record.levelno == logging.WARNING for record in records)
        assert any(getattr(record, "error", None) for record in records)
    finally:
        c.disconnect()
        if not task.done():
            await asyncio.wait_for(task, 2)
    async with env.factory() as session:
        nodes = (await session.execute(select(NodeRow))).scalars().all()
        assert [node.name for node in nodes] == ["node-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,max_attempts", [("source", 1), ("layout", 3), ("partial", 3)])
async def test_staging_failure_diagnostics(env, monkeypatch, caplog, failure, max_attempts):
    jid = await file_job(env)
    h, conn = hub(env, max_attempts=max_attempts), FakeConn([hello()])
    connection = await serve(h, conn)
    denied_path = env.workspace / f"jobs/{jid}/attempts/1"
    layout = hub_module.create_attempt_layout

    def fail_layout(*_args):
        if failure == "partial":
            layout(*_args)
        raise PermissionError(13, "attempt directory denied", str(denied_path))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(hub_module, "staged_source" if failure == "source" else "create_attempt_layout", fail_layout)
            assert await h.dispatch_once() == 0
        current = await job(env, jid)
        assert current.status == (JobStatus.FAILED if max_attempts == 1 else JobStatus.STAGING)
        first = (await attempt(env, jid))[0]
        assert first.result == "failed" and first.finished_at is not None
        assert first.started_at is None and first.log_path is None and first.output_path is None
        assert current.warning
        assert str(denied_path) in current.warning
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not h.entries["node-a"].in_flight and not conn.messages("job")
        async with env.factory() as session:
            failure = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "offer_failed")
            )
            assert failure.payload["error"] in current.warning
            assert failure.payload["node"] == "node-a"
            assert failure.payload["media_file_id"] == current.media_file_id
            assert failure.payload["media_path"] == str(env.library / "movie.avi")
            assert failure.payload["workspace"] == str(env.workspace)
            assert jid not in await session.scalars(recovery_jobs())
            assert jid not in await session.scalars(protected_history_jobs())
        assert any(str(denied_path) in getattr(record, "error", "") for record in caplog.records)
        if max_attempts > 1:
            assert await h.staging.stage_job(jid) == 1
            for number in range(2, max_attempts + 1):
                assert await h.dispatch_once() == 1
                offer = conn.messages("job")[-1]["job"]
                assert offer["attempt_id"] != first.id
                (env.workspace / offer["log_path"]).write_bytes(b"encoding failed")
                await h._handle_finished(h.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"]))
                expected = JobStatus.STAGING if number < max_attempts else JobStatus.FAILED
                assert (await job(env, jid)).status == expected
                if number < max_attempts:
                    assert await h.dispatch_once() == 0
                    await asyncio.wait_for(h.staging.wait_idle(), 3)
            async with env.factory() as session:
                assert jid not in await session.scalars(protected_history_jobs())
        assert not (env.workspace / "jobs" / str(jid)).exists()
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 2)
        await h.aclose()


@pytest.mark.asyncio
async def test_dispatch_paths(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    assert await h.dispatch_once() == 1
    offer = c.messages("job")[0]["job"]
    assert offer["min_free_gb"] == h.staging.min_free_gb == 1
    assert offer["attempt_id"] > 0 and all(
        not p.startswith("/") for p in (offer["source_path"], offer["output_path"], offer["log_path"])
    )
    assert str(env.library) not in json.dumps(offer)
    a = (await attempt(env, jid))[0]
    assert a.source_path == offer["source_path"] and a.output_path == offer["output_path"]
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_dispatch_offers_subtitle(env):
    subtitle = env.library / "movie.srt"
    subtitle.write_bytes(b"subtitle")
    jid = await file_job(
        env,
        plan=FFmpegPlan(video=VideoTarget(encoder="libx264"), burn_subtitle={"filename": "movie.srt"}),
    )
    _stage_subtitle(
        env.workspace,
        jid,
        env.library / "movie.avi",
        "movie.srt",
    )
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    try:
        assert await h.dispatch_once() == 1
        offer = c.messages("job")[0]["job"]
        assert offer["subtitle_path"] == f"jobs/{jid}/subtitle.srt"
        assert not offer["subtitle_path"].startswith("/")
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_dispatch_batches_skip(writer_env, monkeypatch):
    env = writer_env
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    blocked = [await file_job(env, name=f"blocked-{index}.avi", requires=["missing"]) for index in range(3)]
    ready = await file_job(env, name="ready.avi")
    tail = [await file_job(env, name=f"tail-{index}.avi") for index in range(3)]
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    loaded = set()
    rows = []

    def record(current, _context):
        loaded.add(current.id)

    def returned_rows(_connection, cursor, _statement, _parameters, context, _many):
        if context.compiled is not None and context.compiled.statement.is_select:
            rows.append(cursor.rowcount)

    engine = env.factory.kw["bind"].sync_engine
    event.listen(Job, "load", record)
    event.listen(engine, "after_cursor_execute", returned_rows)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        assert [message["job"]["job_id"] for message in conn.messages("job")] == [ready]
        assert set(blocked).issubset(loaded) and not set(tail).intersection(loaded)
        assert rows and max(rows) <= hub_module.QUERY_BATCH
    finally:
        event.remove(Job, "load", record)
        event.remove(engine, "after_cursor_execute", returned_rows)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
    for jid in blocked:
        stored = await job(env, jid)
        assert stored.status == JobStatus.QUEUED and stored.warning is not None
        assert (await events(env, jid)).count("waiting") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_dispatch_skips_deletion(writer_env, monkeypatch, staged):
    env = writer_env
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    deleted = await file_job(env, name="deleted.avi", staged=staged)
    ready = await file_job(env, name="ready.avi")
    async with env.factory() as session:
        await session.execute(update(Job).where(Job.id == deleted).values(delete_requested=True))
        await session.commit()
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    loaded = set()

    def record(current, _context):
        loaded.add(current.id)

    event.listen(Job, "load", record)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        assert [message["job"]["job_id"] for message in conn.messages("job")] == [ready]
        assert deleted not in loaded and deleted not in h.staging.tasks
    finally:
        event.remove(Job, "load", record)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
    stored = await job(env, deleted)
    assert stored.delete_requested and stored.warning is None
    assert stored.status == (JobStatus.QUEUED if staged else JobStatus.STAGING)


@pytest.mark.asyncio
async def test_candidate_rounds_stable(writer_env, monkeypatch):
    env = writer_env
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 2)
    first = [
        await file_job(env, name=f"first-{index}.avi", now=T0 + timedelta(seconds=index * 3)) for index in range(4)
    ]
    second = [
        await file_job(env, name=f"second-{index}.avi", now=T0 + timedelta(seconds=index * 3 + 1)) for index in range(2)
    ]
    third = await file_job(env, name="third.avi", now=T0 + timedelta(seconds=2))
    higher = await file_job(env, name="higher.avi", priority=10, now=T0)
    lower = [await file_job(env, name=f"lower-{index}.avi", priority=-10, now=T0) for index in range(2)]
    async with env.factory() as session:
        first_library = (await session.get(Job, first[0])).library_id
        for name, ids in (("second", second), ("third", [third])):
            library = Library(name=name, roots=[str(env.library)])
            session.add(library)
            await session.flush()
            await session.execute(update(Job).where(Job.id.in_(ids)).values(library_id=library.id))
        await session.commit()
    h = hub(env)
    h._library_cursors[0] = first_library
    found = []
    async with h._dispatch_candidates() as candidates:
        async for jid in candidates:
            found.append(jid)
            async with env.factory() as session:
                await session.execute(update(Job).where(Job.id == jid).values(status=JobStatus.CANCELLED.value))
                await session.commit()
    assert found == [higher, second[0], third, first[0], first[1], second[1], first[2], first[3], *lower]


@pytest.mark.asyncio
async def test_candidate_upper_bound(env, monkeypatch):
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 1)
    first = await file_job(env, name="first.avi", now=T0)
    second = await file_job(env, name="second.avi", now=T0 + timedelta(seconds=1))
    h = hub(env)
    async with h._dispatch_candidates() as candidates:
        assert await anext(candidates) == first
        later = await file_job(env, name="later.avi", priority=100, now=T0 - timedelta(seconds=1))
        assert [jid async for jid in candidates] == [second]
    async with h._dispatch_candidates() as candidates:
        assert [jid async for jid in candidates] == [later, first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "delete"])
async def test_buffered_candidate_checks(env, monkeypatch, operation):
    monkeypatch.setattr(hub_module, "QUERY_BATCH", 2)
    blocked = await file_job(env, name="blocked.avi", requires=["missing"])
    skipped = await file_job(env, name="skipped.avi")
    ready = await file_job(env, name="ready.avi")
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    mark = h._mark_waiting

    async def mutate(session, current, reason, now):
        if current.id == blocked:
            async with env.factory() as writer:
                if operation == "cancel":
                    assert await cancel_pending(writer, skipped, now=env.clock()) is not None
                else:
                    await writer.execute(update(Job).where(Job.id == skipped).values(delete_requested=True))
                await writer.commit()
        return await mark(session, current, reason, now)

    monkeypatch.setattr(h, "_mark_waiting", mutate)
    try:
        assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
        assert [message["job"]["job_id"] for message in conn.messages("job")] == [ready]
        stored = await job(env, skipped)
        assert stored.attempts == 0
        assert stored.status == (JobStatus.CANCELLED if operation == "cancel" else JobStatus.QUEUED)
        assert (env.library / "skipped.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_paused_skips_dispatch(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    h.set_paused(h.entries["node-a"].node_db_id, True)
    assert await h.dispatch_once() == 0
    j = await job(env, jid)
    assert j.status == JobStatus.QUEUED
    assert j.warning is not None
    h.set_paused(h.entries["node-a"].node_db_id, False)
    assert await h.dispatch_once() == 1
    assert c.messages("job")[0]["job"]["job_id"] == jid
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_paused_survives_reconnect(env) -> None:
    async with env.factory() as session:
        node = await register_node(session, "node-a", "tok")
        node.paused = True
        await session.commit()

    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    try:
        assert h.entries["node-a"].paused is True
    finally:
        c.disconnect()
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_waiting_warning_clears(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello(encoders=("hevc_nvenc",))])
    task = await serve(h, c)
    assert await h.dispatch_once() == 0
    assert await h.dispatch_once() == 0
    waiting = await job(env, jid)
    assert waiting.status == JobStatus.QUEUED and waiting.warning is not None
    assert (await events(env, jid)).count("waiting") == 1
    c.disconnect()
    await asyncio.wait_for(task, 2)
    c2 = FakeConn([hello()])
    task2 = await serve(h, c2)
    await h.dispatch_once()
    j = await job(env, jid)
    assert j.warning is None and j.status == JobStatus.CLAIMED
    c2.disconnect()
    await asyncio.wait_for(task2, 2)


@pytest.mark.asyncio
async def test_progress_locks(env, monkeypatch):
    monkeypatch.setattr("server.events.EVENT_BATCH_INTERVAL", 60)
    jid = await file_job(env)
    bus = EventBus()
    h = hub(env, events=bus)
    c = FakeConn([hello()])
    task = await serve(h, c)
    try:
        assert await h.dispatch_once() == 1
        o = c.messages("job")[0]["job"]
        progress = (await job(env, jid)).progress
        await h._handle_progress(h.entries["node-a"], Progress(job_id=jid, attempt_id=o["attempt_id"] + 1, percent=9))
        assert (await job(env, jid)).progress == progress
        entry = h.entries["node-a"]
        subscription = bus.subscribe()
        await h._handle_progress(entry, Progress(job_id=jid, attempt_id=o["attempt_id"], percent=10))
        assert (await job(env, jid)).updated_at == T0
        bus.flush_bulk()
        frame = await asyncio.wait_for(subscription.next(), 2)
        assert frame is not None and frame.type == "events.batch"
        statuses = [item["payload"] for item in frame.payload["events"] if item["type"] == "job.status"]
        assert len(statuses) == 1
        assert statuses[0]["status"] == statuses[0]["job"]["status"] == "running"
        assert statuses[0]["job"]["latest_attempt"]["status"] == "running"
        assert statuses[0]["job"]["progress"] == {"attempt_id": o["attempt_id"], "percent": 10}
        env.clock.advance(1)
        await h._handle_progress(entry, Progress(job_id=jid, attempt_id=o["attempt_id"], percent=50))
        bus.flush_bulk()
        frame = await asyncio.wait_for(subscription.next(), 2)
        assert frame is not None and [item["type"] for item in frame.payload["events"]] == ["job.progress"]
        assert (await job(env, jid)).progress == {"attempt_id": o["attempt_id"], "percent": 50}
        assert (await job(env, jid)).updated_at == env.clock()
        assert (await events(env, jid)).count("started") == 1
        assert (await attempt(env, jid))[0].started_at == T0
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 2)
        await bus.aclose()


@pytest.mark.asyncio
async def test_cancel_attempt(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    assert await h.cancel_job(jid)
    msg = c.messages("cancel")[0]
    assert msg["attempt_id"] == (await attempt(env, jid))[0].id
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("workspace", [False, True])
async def test_reconnect_missing_attempt(env, restart, running, workspace):
    from server.workspace import cleanup_workspace

    jid = await file_job(env)
    original, old = hub(env), FakeConn([hello()])
    old_task = await serve(original, old)
    replacement = None
    replacement_task = None
    try:
        assert await original.dispatch_once() == 1
        offer = old.messages("job")[0]["job"]
        if workspace:
            materialize_offer(original, offer)
        if running:
            await original._handle_progress(
                original.entries["node-a"], Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=10)
            )
        old.disconnect()
        await asyncio.wait_for(old_task, 2)
        recovered = hub(env) if restart else original
        if restart:
            await recovered.recover_startup()
        replacement = FakeConn([hello()])
        replacement_task = await serve(recovered, replacement)
        current = await job(env, jid)
        stored = (await attempt(env, jid))[0]
        assert current.status == stored.status == JobStatus.FAILED
        assert stored.result == "failed" and stored.id == offer["attempt_id"]
        assert current.node_id is None and current.lease_expires_at is None
        assert stored.error is not None
        assert current.error is not None and stored.error in current.error
        assert current.warning is None
        assert "failed" in await events(env, jid)
        assert jid not in recovered.entries["node-a"].in_flight
        assert not (env.workspace / f"jobs/{jid}").exists()
        await recovered._handle_heartbeat(recovered.entries["node-a"], Heartbeat())
        assert (await job(env, jid)).lease_expires_at is None
        await cleanup_workspace(env.factory, env.workspace)
        if workspace:
            assert stored.log_data == b"log"
        assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert len(await attempt(env, jid)) == 1
        next_id = await file_job(env, name="next.avi")
        assert await recovered.dispatch_once() == 1
        assert replacement.messages("job")[0]["job"]["job_id"] == next_id
    finally:
        old.disconnect()
        if replacement is not None:
            replacement.disconnect()
        tasks = [old_task] + ([replacement_task] if replacement_task is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["cancelled", "succeeded"])
async def test_reconnect_completed_attempt(env, result):
    jid = await file_job(env)
    h, old = hub(env), FakeConn([hello()])
    old_task = await serve(h, old)
    replacement = None
    replacement_task = None
    try:
        assert await h.dispatch_once() == 1
        offer = old.messages("job")[0]["job"]
        materialize_offer(h, offer)
        report = finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            result,
            **(
                {
                    "output_path": offer["output_path"],
                    "output_probe": ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[]),
                }
                if result == "succeeded"
                else {}
            ),
        )
        append_message(env.workspace, offer["log_path"], report.model_dump(mode="json"))
        old.disconnect()
        await asyncio.wait_for(old_task, 2)
        replacement = FakeConn([hello()])
        replacement_task = await serve(h, replacement)
        assert (await job(env, jid)).status == result
        assert (await attempt(env, jid))[0].result == result
        assert jid not in h.entries["node-a"].in_flight
    finally:
        old.disconnect()
        if replacement is not None:
            replacement.disconnect()
        tasks = [old_task] + ([replacement_task] if replacement_task is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("warning", [None, "Existing diagnostic warning."])
async def test_cancel_missing_attempt(env, warning):
    import httpx
    from tests.helpers import api_client
    from tests.helpers import create_test_app as create_app

    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
        async with env.factory() as session:
            current = await get_job(session, jid)
            assert current is not None
            current.warning = warning
            await session.commit()
        app = create_app(env.factory, hub=h)
        async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/jobs/{jid}/cancel")
            assert response.status_code == 200
        assert conn.messages("cancel")[0]["attempt_id"] == offer["attempt_id"]
        conn.receiving.clear()
        conn.push(
            AttemptMissing(job_id=jid, attempt_id=offer["attempt_id"], log_path=offer["log_path"]).model_dump(
                mode="json"
            )
        )
        await asyncio.wait_for(conn.receiving.wait(), 2)
        await wait_for(lambda: jid not in h.entries["node-a"].in_flight, h._wake)
        current = await job(env, jid)
        assert current.status == JobStatus.CANCELLED and current.error
        assert current.warning == warning
        assert current.node_id is None and current.lease_expires_at is None
        stored = (await attempt(env, jid))[0]
        assert stored.status == JobStatus.CANCELLED and stored.result == "cancelled"
        assert stored.log_data == b"log" and stored.error == current.error
        assert "cancelled" in await events(env, jid)
        assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.CANCELLED])
async def test_reconnect_authorizes_current(env, active, historical, status):
    from server.workspace import create_attempt_layout, cleanup_workspace

    jid = await file_job(env)
    h, old = hub(env), FakeConn([hello()])
    old_task = await serve(h, old)
    replacement = None
    replacement_task = None
    try:
        assert await h.dispatch_once() == 1
        offer = old.messages("job")[0]["job"]
        output = materialize_offer(h, offer)
        old.disconnect()
        await asyncio.wait_for(old_task, 2)
        active_attempts = [{"job_id": jid, "attempt_id": offer["attempt_id"]}] if active else []
        async with env.factory() as session:
            current = await get_job(session, jid)
            stored = await session.get(JobAttempt, offer["attempt_id"])
            assert current is not None and stored is not None
            stored.status = status
            stored.result = "interrupted"
            stored.log_data = b"log"
            stored.error = "Original interruption."
            stored.finished_at = env.clock()
            current.status = JobStatus.RUNNING if historical else status
            current.error = "Original job error."
            current.warning = "Existing diagnostic warning."
            if historical:
                layout = create_attempt_layout(env.workspace, jid, 2, ".mkv")
                (env.workspace / layout["output"]).write_bytes(b"current output")
                current.attempts = 2
                newer = JobAttempt(
                    job_id=jid,
                    attempt_number=2,
                    node_id=stored.node_id,
                    workload="transcode",
                    status=JobStatus.RUNNING,
                    output_path=layout["output"],
                    log_path=layout["log"],
                )
                session.add(newer)
                await session.flush()
                active_attempts.append({"job_id": jid, "attempt_id": newer.id})
            else:
                current.node_id = None
                current.lease_expires_at = None
            await session.commit()
        history = await events(env, jid)
        replacement = FakeConn([hello(active_attempts=active_attempts)])
        replacement_task = await serve(h, replacement)
        authorized = replacement.messages("welcome")[0]["active_attempts"]
        assert authorized == ([{"job_id": jid, "attempt_id": newer.id}] if historical else [])
        await h.staging.prune_attempt(offer["attempt_id"])
        await cleanup_workspace(env.factory, env.workspace)
        stored = (await attempt(env, jid))[0]
        assert stored.result == "interrupted"
        assert stored.status == status and stored.error == "Original interruption."
        assert stored.finished_at == env.clock()
        assert stored.log_data == b"log"
        assert not output.exists()
        current = await job(env, jid)
        assert current.status == (JobStatus.RUNNING if historical else status)
        assert current.error == "Original job error." and current.warning == "Existing diagnostic warning."
        assert await events(env, jid) == history
        if historical:
            assert (env.workspace / layout["output"]).read_bytes() == b"current output"
            assert (env.workspace / offer["source_path"]).read_bytes() == b"source"
            assert (await attempt(env, jid))[1].result is None
        else:
            assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        old.disconnect()
        if replacement is not None:
            replacement.disconnect()
        tasks = [old_task] + ([replacement_task] if replacement_task is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["foreign", "healthcheck"])
async def test_reconnect_excludes_attempts(env, scope):
    from server.workspace import create_attempt_layout

    jid = await file_job(env, health=scope == "healthcheck")
    layout = create_attempt_layout(env.workspace, jid, 1, ".mkv")
    output = env.workspace / layout["output"]
    output.write_bytes(b"retained output")
    (env.workspace / layout["log"]).write_bytes(b"retained log")
    async with env.factory() as session:
        owner = (
            await register_node(session, "node-b", "other-token")
            if scope == "foreign"
            else await get_node(session, "node-a")
        )
        current = await get_job(session, jid)
        assert current is not None and owner is not None
        current.status = JobStatus.FAILED
        current.attempts = 1
        session.add(
            JobAttempt(
                job_id=jid,
                attempt_number=1,
                node_id=owner.id,
                workload="healthcheck" if scope == "healthcheck" else "transcode",
                status=JobStatus.FAILED,
                result="interrupted",
                error="Original interruption.",
                log_path=layout["log"],
                output_path=layout["output"],
            )
        )
        await session.commit()
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        stored = (await attempt(env, jid))[0]
        assert stored.result == "interrupted" and stored.log_data is None
        assert stored.error == "Original interruption."
        assert output.read_bytes() == b"retained output"
        assert (env.workspace / layout["log"]).read_bytes() == b"retained log"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["attempt", "stage"])
async def test_missing_attempt_checked(env, stale):
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
        entry = h.entries["node-a"]
        if stale == "stage":
            async with env.factory() as session:
                current = await session.get(JobAttempt, offer["attempt_id"])
                assert current is not None
                current.log_path = str(Path(offer["log_path"]).with_name("ffmpeg-2.log"))
                await session.commit()
        await h._finish_missing(
            entry,
            AttemptMissing(
                job_id=jid,
                attempt_id=offer["attempt_id"] + (stale == "attempt"),
                log_path=offer["log_path"],
            ),
        )
        assert (await job(env, jid)).status == JobStatus.CLAIMED
        assert jid in entry.in_flight
        assert (env.workspace / offer["output_path"]).read_bytes() == b"output"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_stall_cancel(env):
    jid = await file_job(env)
    h = hub(env, stall_timeout_s=1)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    env.clock.advance(2)
    await h.sweep_once()
    assert c.messages("cancel")[0]["attempt_id"] > 0
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_disconnect_retains_attempt(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    first = (await attempt(env, jid))[0].id
    c.disconnect()
    await asyncio.wait_for(task, 2)
    assert (await job(env, jid)).status == JobStatus.CLAIMED
    assert jid in h.entries["node-a"].in_flight
    c2 = FakeConn([hello(active_attempts=[{"job_id": jid, "attempt_id": first}])])
    task = await serve(h, c2)
    assert await h.dispatch_once() == 0
    rows = await attempt(env, jid)
    assert len(rows) == 1 and rows[0].id == first and rows[0].result is None
    assert jid in h.entries["node-a"].in_flight and not c2.messages("job")
    c2.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["failed", "aborted", "stalled"])
async def test_finished_failure(env, result):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    o = c.messages("job")[0]["job"]
    await h._handle_finished(h.entries["node-a"], finished(jid, o["attempt_id"], o["log_path"], result))
    status = (await job(env, jid)).status
    assert status == JobStatus.FAILED
    assert not (h.workspace_dir / f"jobs/{jid}").exists()
    assert (env.library / "movie.avi").read_bytes() == b"source"
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["failed", "aborted", "stalled"])
async def test_retry_fresh_attempt(env, result):
    jid = await file_job(env)
    async with env.factory() as session:
        await register_node(session, "node-b", "other-token")
        await session.commit()
    h = hub(env, max_attempts=3)
    first, other = FakeConn([hello()]), FakeConn([hello("node-b", "other-token")])
    connections = [await serve(h, first), await serve(h, other)]
    try:
        assert await h.dispatch_once() == 1
        original = first.messages("job")[0]["job"]
        materialize_offer(h, original).write_bytes(b"failed output")
        await h._handle_finished(
            h.entries["node-a"],
            finished(jid, original["attempt_id"], original["log_path"], result, error="encoder stopped"),
        )
        current = await job(env, jid)
        assert current.status == JobStatus.STAGING and current.attempts == 1
        assert current.node_id is None and current.lease_expires_at is None
        stored = (await attempt(env, jid))[0]
        assert stored.status == JobStatus.FAILED and stored.result == result
        assert stored.error == "encoder stopped" and stored.log_data == b"log"
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        retry = other.messages("job")[0]["job"]
        assert len(first.messages("job")) == 1
        assert retry["attempt_id"] != original["attempt_id"]
        assert retry["plan"] == original["plan"]
        assert retry["source_path"] == original["source_path"]
        assert retry["source_path"] != original["output_path"]
        assert (env.workspace / retry["source_path"]).read_bytes() == b"source"
        assert Path(retry["output_path"]).parent == Path(f"jobs/{jid}/attempts/2")
        assert not (env.workspace / retry["output_path"]).exists()
        await h._handle_finished(
            h.entries["node-a"],
            finished(
                jid,
                original["attempt_id"],
                original["log_path"],
                "succeeded",
                output_path=original["output_path"],
                output_probe=ProbeData.model_validate(original["probe"]).model_copy(
                    update={"file_path": original["output_path"]}
                ),
            ),
        )
        assert (await job(env, jid)).status == JobStatus.CLAIMED
        assert (await attempt(env, jid))[1].result is None
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
        assert "foreign_finished" in await events(env, jid)
    finally:
        first.disconnect()
        other.disconnect()
        await asyncio.wait_for(asyncio.gather(*connections, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["busy", "encoder", "paused"])
async def test_retry_reuses_capable(env, unavailable):
    jid = await file_job(env)
    if unavailable == "busy":
        await file_job(env, name="other.avi")
    async with env.factory() as session:
        await register_node(session, "node-b", "other-token")
        await session.commit()
    h = hub(env, max_attempts=3)
    first = FakeConn([hello()])
    other = FakeConn(
        [hello("node-b", "other-token", encoders=("libx265",) if unavailable == "encoder" else ("libx264",))]
    )
    connections = [await serve(h, first), await serve(h, other)]
    try:
        if unavailable == "paused":
            h.set_paused(h.entries["node-b"].node_db_id, True)
        assert await h.dispatch_once() == (2 if unavailable == "busy" else 1)
        original = first.messages("job")[0]["job"]
        materialize_offer(h, original)
        await h._handle_finished(
            h.entries["node-a"], finished(jid, original["attempt_id"], original["log_path"], error="encoder stopped")
        )
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        retry = first.messages("job")[-1]["job"]
        assert retry["job_id"] == jid and retry["attempt_id"] != original["attempt_id"]
        assert len(other.messages("job")) == (1 if unavailable == "busy" else 0)
        assert (env.workspace / retry["source_path"]).read_bytes() == b"source"
    finally:
        first.disconnect()
        other.disconnect()
        await asyncio.wait_for(asyncio.gather(*connections, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_retry_original_plan(env, monkeypatch):
    from server.flows.engine import FlowResult

    jid = await file_job(env, plan=FFmpegPlan())
    h, conn = hub(env, max_attempts=3), FakeConn([hello()])
    connection = await serve(h, conn)

    async def next_stage(*_args):
        return FlowResult(status="matched", plan=plan()), [], None, [], []

    monkeypatch.setattr(h, "_post_gate", next_stage)
    try:
        assert await h.dispatch_once() == 1
        original = conn.messages("job")[0]["job"]
        materialize_offer(h, original)
        await h._handle_finished(
            h.entries["node-a"],
            finished(
                jid,
                original["attempt_id"],
                original["log_path"],
                "succeeded",
                output_path=original["output_path"],
                output_probe=ProbeData.model_validate(original["probe"]).model_copy(
                    update={"file_path": original["output_path"]}
                ),
            ),
        )
        assert await h.dispatch_once() == 1
        stage = conn.messages("job")[-1]["job"]
        assert stage["attempt_id"] == original["attempt_id"]
        assert stage["source_path"] == original["output_path"] and stage["plan"] != original["plan"]
        materialize_offer(h, stage)
        await h._handle_finished(
            h.entries["node-a"], finished(jid, stage["attempt_id"], stage["log_path"], error="encode failed")
        )
        assert (await job(env, jid)).status == JobStatus.STAGING
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        retry = conn.messages("job")[-1]["job"]
        assert retry["attempt_id"] != original["attempt_id"]
        assert retry["plan"] == original["plan"] and retry["source_path"] == original["source_path"]
        assert retry["probe"]["size_bytes"] == original["probe"]["size_bytes"]
        assert (env.workspace / retry["source_path"]).read_bytes() == b"source"
        assert Path(retry["output_path"]).parent == Path(f"jobs/{jid}/attempts/2")
        assert not (env.workspace / retry["output_path"]).exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_retry_limit_stops(env):
    jid = await file_job(env)
    h, conn = hub(env, max_attempts=3), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        for number in range(1, 4):
            if number > 1:
                assert await h.dispatch_once() == 0
                await asyncio.wait_for(h.staging.wait_idle(), 3)
            assert await h.dispatch_once() == 1
            offer = conn.messages("job")[-1]["job"]
            materialize_offer(h, offer)
            reason = f"encoder failure {number}"
            await h._handle_finished(
                h.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"], error=reason)
            )
            current = await job(env, jid)
            assert current.status == (JobStatus.STAGING if number < 3 else JobStatus.FAILED)
            assert current.attempts == number
            stored = (await attempt(env, jid))[-1]
            assert stored.attempt_number == number and stored.error == reason
            assert stored.status == JobStatus.FAILED and stored.log_data == b"log"
        assert current.error is not None and reason in current.error
        assert current.node_id is None and current.lease_expires_at is None
        assert await h.dispatch_once() == 0
        assert len(conn.messages("job")) == 3
        assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
async def test_missing_retries_fresh(env, reconnect):
    jid = await file_job(env)
    h, conn = hub(env, max_attempts=3), FakeConn([hello()])
    connection = await serve(h, conn)
    replacement = None
    replacement_task = None
    try:
        assert await h.dispatch_once() == 1
        original = conn.messages("job")[0]["job"]
        materialize_offer(h, original)
        if reconnect:
            conn.disconnect()
            await asyncio.wait_for(connection, 3)
            replacement = FakeConn([hello()])
            replacement_task = await serve(h, replacement)
            receiver = replacement
            assert replacement.messages("welcome")[0]["active_attempts"] == []
        else:
            await h._finish_missing(
                h.entries["node-a"],
                AttemptMissing(job_id=jid, attempt_id=original["attempt_id"], log_path=original["log_path"]),
            )
            receiver = conn
        current = await job(env, jid)
        assert current.status == JobStatus.STAGING and current.attempts == 1
        stored = (await attempt(env, jid))[0]
        assert stored.status == JobStatus.FAILED and stored.result == "failed"
        assert stored.error is not None and stored.log_data == b"log"
        assert await h.dispatch_once() == 0
        await asyncio.wait_for(h.staging.wait_idle(), 3)
        assert await h.dispatch_once() == 1
        retry = receiver.messages("job")[-1]["job"]
        assert retry["attempt_id"] != original["attempt_id"]
        assert retry["plan"] == original["plan"] and retry["source_path"] == original["source_path"]
        assert (env.workspace / retry["source_path"]).read_bytes() == b"source"
        assert Path(retry["output_path"]).parent == Path(f"jobs/{jid}/attempts/2")
        assert not (env.workspace / retry["output_path"]).exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        if replacement is not None:
            replacement.disconnect()
        tasks = [connection] + ([replacement_task] if replacement_task is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["finalization_unprepared", "execution_log_unavailable"])
async def test_diagnostics_allow_cleanup(env, diagnostic):
    jid = await file_job(env)
    h, conn = hub(env), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
        async with env.factory() as session:
            session.add(
                JobEvent(job_id=jid, attempt_id=offer["attempt_id"], type=diagnostic, payload={"error": "diagnostic"})
            )
            await session.commit()
        await h._handle_finished(h.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"]))
        assert (await job(env, jid)).status == JobStatus.FAILED
        assert (await attempt(env, jid))[0].log_data == b"log"
        assert diagnostic in await events(env, jid)
        assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
async def test_expiry_lock_retry(env, connected):
    from server.workspace import cleanup_workspace

    jid = await file_job(env)
    h, conn = hub(env, max_attempts=3), FakeConn([hello()])
    connection = await serve(h, conn)
    replacement = None
    replacement_task = None
    try:
        assert await h.dispatch_once() == 1
        original = conn.messages("job")[0]["job"]
        output = materialize_offer(h, original)
        partial = output.with_suffix(".partial")
        partial.write_bytes(b"unreaped writer")
        with workspace_lock(output.parent):
            if not connected:
                conn.disconnect()
                await asyncio.wait_for(connection, 3)
            env.clock.advance(h.lease_ttl_s + 1)
            await h.sweep_once()
            assert (await job(env, jid)).status == JobStatus.STAGING
            stored = (await attempt(env, jid))[0]
            assert stored.status == JobStatus.FAILED and stored.result == "failed" and stored.log_data == b"log"
            assert stored.error is not None
            assert partial.read_bytes() == b"unreaped writer"
            replacement = FakeConn([hello()])
            replacement_task = await serve(h, replacement)
            assert await h.dispatch_once() == 0
            await asyncio.wait_for(h.staging.wait_idle(), 3)
            assert await h.dispatch_once() == 1
            retry = replacement.messages("job")[0]["job"]
            assert Path(retry["output_path"]).parent == Path(f"jobs/{jid}/attempts/2")
            assert retry["source_path"] != original["output_path"]
            assert (env.workspace / retry["source_path"]).read_bytes() == b"source"
            await h._handle_finished(
                h.entries["node-a"],
                finished(
                    jid,
                    original["attempt_id"],
                    original["log_path"],
                    "succeeded",
                    output_path=original["output_path"],
                    output_probe=ProbeData.model_validate(original["probe"]).model_copy(
                        update={"file_path": original["output_path"]}
                    ),
                ),
            )
            assert (await job(env, jid)).status == JobStatus.CLAIMED
            assert (await attempt(env, jid))[1].result is None
            assert not (env.library / "movie.mkv").exists()
            materialize_offer(h, retry)
            assert await h.cancel_job(jid)
            await h._handle_finished(
                h.entries["node-a"], finished(jid, retry["attempt_id"], retry["log_path"], "cancelled")
            )
            await cleanup_workspace(env.factory, env.workspace)
            assert output.read_bytes() == b"output" and partial.read_bytes() == b"unreaped writer"
        await cleanup_workspace(env.factory, env.workspace)
        assert not (env.workspace / f"jobs/{jid}").exists()
        rows = await attempt(env, jid)
        assert rows[0].result == "failed" and rows[0].log_data == b"log"
        assert rows[1].status == JobStatus.CANCELLED and (await job(env, jid)).status == JobStatus.CANCELLED
        async with env.factory() as session:
            retried = await session.scalar(select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "retried"))
            assert retried.attempt_id == original["attempt_id"]
            assert retried.payload == {"error": rows[0].error, "result": "failed"}
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        if replacement is not None:
            replacement.disconnect()
        tasks = [connection] + ([replacement_task] if replacement_task is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_cancel_expiry_terminal(env):
    jid = await file_job(env)
    h, conn = hub(env, max_attempts=3), FakeConn([hello()])
    connection = await serve(h, conn)
    try:
        assert await h.dispatch_once() == 1
        offer = conn.messages("job")[0]["job"]
        materialize_offer(h, offer)
        assert await h.cancel_job(jid)
        conn.disconnect()
        await asyncio.wait_for(connection, 3)
        env.clock.advance(h.lease_ttl_s + 1)
        await h.sweep_once()
        current = await job(env, jid)
        assert current.status == JobStatus.CANCELLED and current.attempts == 1
        assert current.error and current.node_id is None and current.lease_expires_at is None
        stored = (await attempt(env, jid))[0]
        assert stored.status == JobStatus.CANCELLED and stored.result == "cancelled" and stored.log_data == b"log"
        assert "retried" not in await events(env, jid)
        assert not (env.workspace / f"jobs/{jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        conn.disconnect()
        await asyncio.wait_for(connection, 3)


@pytest.mark.asyncio
async def test_finishes_cancelled(env):
    jid = await file_job(env)
    h = hub(env, max_attempts=3)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    offer = c.messages("job")[0]["job"]

    assert await h.cancel_job(jid)
    await h._handle_finished(
        h.entries["node-a"],
        finished(jid, offer["attempt_id"], offer["log_path"], "cancelled"),
    )

    assert (await job(env, jid)).status == JobStatus.CANCELLED
    assert (await attempt(env, jid))[0].status == JobStatus.CANCELLED
    assert await h.dispatch_once() == 0
    assert len(await attempt(env, jid)) == 1
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_duplicate_finished(env, monkeypatch):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    offer = c.messages("job")[0]["job"]
    await h._handle_finished(h.entries["node-a"], finished(jid, offer["attempt_id"], offer["log_path"], "cancelled"))
    calls = 0

    async def probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("duplicate was probed")

    monkeypatch.setattr("server.nodehub.hub.run_ffprobe", probe)
    duplicate = finished(
        jid,
        offer["attempt_id"],
        offer["log_path"],
        "succeeded",
        output_path=offer["output_path"],
        output_probe=ProbeData.model_validate(
            {"file_path": offer["output_path"], "duration_s": 1, "size_bytes": 1, "streams": []}
        ),
    )
    await h._handle_finished(h.entries["node-a"], duplicate)
    assert calls == 0 and (await job(env, jid)).status == JobStatus.CANCELLED
    assert "foreign_finished" in await events(env, jid)
    assert (await events(env, jid)).count("cancelled") == 1
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_reconnect_keeps_locks(env):
    jid = await file_job(env)
    h = hub(env)
    old = FakeConn([hello()])
    old_task = await serve(h, old)
    await h.dispatch_once()
    offer = old.messages("job")[0]["job"]
    new = FakeConn([hello(active_attempts=[{"job_id": jid, "attempt_id": offer["attempt_id"]}])])
    new_task = await serve(h, new)
    old.disconnect()
    await asyncio.wait_for(old_task, 2)
    assert h.entries["node-a"].conn is new and jid in h.entries["node-a"].in_flight
    assert (await job(env, jid)).status == JobStatus.CLAIMED
    assert "retried" not in await events(env, jid)
    new.disconnect()
    await asyncio.wait_for(new_task, 2)


@pytest.mark.asyncio
async def test_reconnect_recovers_stall(env):
    jid = await file_job(env)
    h = hub(env, stall_timeout_s=1, lease_ttl_s=30)
    old, new = FakeConn([hello()]), None
    old_task = await serve(h, old)
    new_task = None
    try:
        await h.dispatch_once()
        offer = old.messages("job")[0]["job"]
        materialize_offer(h, offer)
        new = FakeConn([hello(active_attempts=[{"job_id": jid, "attempt_id": offer["attempt_id"]}])])
        new_task = await serve(h, new)
        old.disconnect()
        await asyncio.wait_for(old_task, 2)
        assert not new.messages("job")
        env.clock.advance(2)
        await h.sweep_once()
        assert len(new.messages("cancel")) == 1
        assert jid in h.entries["node-a"].in_flight
        before = await job(env, jid)
        offer = old.messages("job")[0]["job"]
        await h._handle_heartbeat(h.entries["node-a"], Heartbeat(cpu_percent=1, memory_percent=1, gpus=[]))
        await h._handle_progress(h.entries["node-a"], Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=10))
        assert (await job(env, jid)).lease_expires_at == before.lease_expires_at
        env.clock.advance(60)
        await h.sweep_once()
        assert await h.dispatch_once() == 0
        current = await job(env, jid)
        assert current.status == JobStatus.FAILED
        assert (await attempt(env, jid))[0].result == "failed"
        assert jid not in h.entries["node-a"].in_flight
        assert not (env.workspace / offer["output_path"]).exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        old.disconnect()
        if new is not None:
            new.disconnect()
        tasks = [old_task] + ([new_task] if new_task is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_heartbeat_filters_locks(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    entry = h.entries["node-a"]
    before = await job(env, jid)
    env.clock.advance(1)
    await h._handle_heartbeat(entry, Heartbeat(cpu_percent=1, memory_percent=1, gpus=[]))
    renewed = await job(env, jid)
    assert renewed.lease_expires_at > before.lease_expires_at
    async with env.factory() as session:
        row = await session.get(Job, jid)
        row.status = JobStatus.QUEUED.value
        row.node_id = None
        await session.commit()
    await h._handle_heartbeat(entry, Heartbeat(cpu_percent=1, memory_percent=1, gpus=[]))
    assert jid not in entry.in_flight and jid not in entry.offered_at and jid not in entry.last_progress_at
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["connected", "disconnect", "replace"])
@pytest.mark.parametrize("cancellations", [0, 2])
async def test_reconnect_drains_locks(env, monkeypatch, transition, cancellations):
    jid = await file_job(env)
    h = hub(env, stall_timeout_s=1, lease_ttl_s=30)
    old, replacement = FakeConn([hello()]), FakeConn()
    old_task = await serve(h, old)
    owner = h.entries["node-a"]
    connections, tasks = [old, replacement], [old_task]
    started, release, replacement_teardown = asyncio.Event(), asyncio.Event(), asyncio.Event()
    disconnect = h._disconnect_entry

    async def controlled_disconnect(entry):
        if entry is owner:
            started.set()
            await release.wait()
        else:
            replacement_teardown.set()
        await disconnect(entry)

    monkeypatch.setattr(h, "_disconnect_entry", controlled_disconnect)
    try:
        assert await h.dispatch_once() == 1
        offer = old.messages("job")[0]["job"]
        output = materialize_offer(h, offer)
        replacement.push(hello(active_attempts=[{"job_id": jid, "attempt_id": offer["attempt_id"]}]))
        replacement_task = await serve(h, replacement)
        tasks.append(replacement_task)
        current = h.entries["node-a"]
        assert jid in current.in_flight and current.conn is replacement
        old.disconnect()
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(cancellations):
            old_task.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not old_task.done() and not owner.disconnected.is_set()
        env.clock.advance(2)
        before = await job(env, jid)
        await h._handle_heartbeat(current, Heartbeat(cpu_percent=1, memory_percent=1, gpus=[]))
        await h._handle_heartbeat(owner, Heartbeat(cpu_percent=1, memory_percent=1, gpus=[]))
        await h._handle_progress(current, Progress(job_id=jid, attempt_id=offer["attempt_id"], percent=90))
        assert (await job(env, jid)).lease_expires_at > before.lease_expires_at
        assert (await job(env, jid)).status == JobStatus.RUNNING
        assert len(await attempt(env, jid)) == 1
        await h._handle_finished(owner, finished(jid, offer["attempt_id"], offer["log_path"], "cancelled"))
        assert (await job(env, jid)).status == JobStatus.RUNNING and output.read_bytes() == b"output"
        env.clock.advance(2)
        await h.sweep_once()
        assert len(replacement.messages("cancel")) == 1
        if transition == "disconnect":
            replacement.disconnect()
            await asyncio.wait_for(replacement_teardown.wait(), 2)
            await asyncio.wait_for(replacement_task, 2)
            assert (await job(env, jid)).status == JobStatus.RUNNING
        elif transition == "replace":
            newest = FakeConn([hello(active_attempts=[{"job_id": jid, "attempt_id": offer["attempt_id"]}])])
            connections.append(newest)
            tasks.append(await serve(h, newest))
            current = h.entries["node-a"]
            assert jid in current.in_flight and current.conn is newest
            replacement.disconnect()
            await asyncio.wait_for(replacement_teardown.wait(), 2)
        assert output.read_bytes() == b"output"
        assert await h.dispatch_once() == 0
        if transition == "disconnect":
            env.clock.advance(h.lease_ttl_s + 1)
            await h.sweep_once()
            assert (await job(env, jid)).status == JobStatus.FAILED
            assert "node-a" not in h.entries
            original = (await attempt(env, jid))[0]
            assert original.result == "failed" and original.log_data == b"log"
            assert not output.exists()
        else:
            await h._handle_finished(current, finished(jid, offer["attempt_id"], offer["log_path"], "cancelled"))
            assert (await job(env, jid)).status == JobStatus.CANCELLED
            assert jid not in current.in_flight
            original = (await attempt(env, jid))[0]
            assert original.result == "cancelled" and original.log_data == b"log"
            assert not output.exists()
        assert not old_task.done(), "predecessor teardown must drain independently of current completion"
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), 2)
        assert isinstance(outcomes[0], asyncio.CancelledError) is bool(cancellations)
        assert len(await attempt(env, jid)) == 1
        assert (env.library / "movie.avi").read_bytes() == b"source"
    finally:
        release.set()
        for connection in connections:
            connection.disconnect()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_finished_post_veto(env):
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from server.plugins.host import PluginHost

    guard_flow = FlowDefinition.model_validate(
        {
            "entry": "guard",
            "nodes": [
                {
                    "type": "plugin",
                    "id": "guard",
                    "plugin": "inspector.output_ratio",
                    "inputs": {"metric": "size", "min_ratio": 0.5},
                },
                {"type": "terminal", "id": "done", "outcome": "done"},
            ],
            "edges": [{"src": "guard", "port": "match", "dst": "done"}],
        }
    )
    jid = await file_job(env, continuation=FlowContinuation(flow=guard_flow, node="guard"))
    from common.media.probe import ProbeData

    host = PluginHost([Path(__file__).resolve().parents[1] / "plugins"])
    c = FakeConn([hello()])
    task = None
    try:
        h = hub(env, plugin_host=host)
        task = await serve(h, c)
        await h.dispatch_once()
        o = c.messages("job")[0]["job"]
        output = h.workspace_dir / o["output_path"]
        output.write_bytes(b"out" * 34)
        (h.workspace_dir / o["log_path"]).write_bytes(b"log")
        async with env.factory() as s:
            row = (await s.execute(select(JobAttempt).where(JobAttempt.job_id == jid))).scalar_one()
            row.input_probe = probe_from_raw(Path("x.avi"), 1000, RAW).model_dump(mode="json")
            stage = (
                await s.scalars(select(JobEvent).where(JobEvent.attempt_id == row.id, JobEvent.type == "stage"))
            ).one()
            stage.payload = {**stage.payload, "probe": row.input_probe}
            await s.commit()
        probe = ProbeData.model_validate(
            {"file_path": o["output_path"], "container": "mkv", "duration_s": 60, "size_bytes": 900, "streams": []}
        )
        await h._handle_finished(
            h.entries["node-a"],
            finished(
                jid, o["attempt_id"], o["log_path"], "succeeded", output_path=o["output_path"], output_probe=probe
            ),
        )
        row = await job(env, jid)
        assert row.status == JobStatus.FAILED
        assert row.error is not None
        assert not list(env.trash.rglob("*"))
        assert not output.exists()
        async with env.factory() as s:
            media = (await s.execute(select(MediaFile).where(MediaFile.id == row.media_file_id))).scalar_one()
            assert media.path == str(env.library / "movie.avi")
            assert media.transcode_status == "failed"
            assert not (
                await s.execute(select(Job).where(Job.kind == "healthcheck", Job.media_file_id == media.id))
            ).scalar_one_or_none()
    finally:
        try:
            c.disconnect()
            if task is not None:
                await asyncio.wait_for(task, 2)
        finally:
            await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,trash_mode",
    [("removed", "all"), ("disabled", "none"), ("no_host", "all"), ("terminal_skip", "none"), ("terminal_skip", "all")],
)
async def test_post_inspector_failure(env, case, trash_mode):
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from server.plugins.host import PluginHost

    nodes = [{"type": "plugin", "id": "guard", "plugin": "inspector.output_ratio"}]
    edges = []
    entry = "guard"
    if case == "terminal_skip":
        nodes = [
            {"type": "terminal", "id": "skip", "outcome": "skip"},
            {"type": "plugin", "id": "bad", "plugin": "action.missing"},
        ]
        edges = [{"src": "skip", "port": "next", "dst": "bad"}]
        entry = "skip"
    flow = FlowDefinition.model_validate({"entry": entry, "nodes": nodes, "edges": edges})
    jid = await file_job(env, continuation=FlowContinuation(flow=flow, node=entry))
    async with env.factory() as session:
        library = (await session.execute(select(Library))).scalar_one()
        library.trash_mode = trash_mode
        await session.commit()
    dirs = [] if case == "removed" else [Path(__file__).resolve().parents[1] / "plugins"]
    host = PluginHost(dirs, disabled=["inspector.output_ratio"] if case == "disabled" else [])
    h = hub(env, plugin_host=None if case == "no_host" else host)
    conn = FakeConn([hello()])
    task = await serve(h, conn)
    try:
        await h.dispatch_once()
        offer = conn.messages("job")[0]["job"]
        output = materialize_offer(h, offer)
        probe = ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[])
        await asyncio.wait_for(
            h._handle_finished(
                h.entries["node-a"],
                finished(
                    jid,
                    offer["attempt_id"],
                    offer["log_path"],
                    "succeeded",
                    output_path=offer["output_path"],
                    output_probe=probe,
                ),
            ),
            5,
        )
        row = await job(env, jid)
        if case == "terminal_skip":
            assert row.status == JobStatus.SUCCEEDED
            assert (env.library / "movie.mkv").read_bytes() == b"output"
        else:
            assert row.status == JobStatus.FAILED
            assert row.error is not None
            if case != "no_host":
                assert row.decision_log[-1]["outcome"] == "error"
            assert (env.library / "movie.avi").read_bytes() == b"source"
            assert not (env.library / "movie.mkv").exists()
            assert not list(env.trash.rglob("*"))
        assert not output.exists()
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
async def test_finalization_failure_diagnostics(env, monkeypatch, caplog):
    async with post_action_case(env, succeed=False) as case:
        materialize_offer(case.hub, case.offer)
        cause = OSError(28, "destination storage is full", str(env.library / "movie.mkv"))
        failure = FinalizationError("could not publish verified output")

        async def fail_finalize(*_args, **_kwargs):
            raise failure from cause

        monkeypatch.setattr("server.nodehub.hub.finalize", fail_finalize)
        await case.hub._handle_finished(
            case.hub.entries["node-a"],
            finished(
                case.jid,
                case.offer["attempt_id"],
                case.offer["log_path"],
                "succeeded",
                output_path=case.offer["output_path"],
                output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
            ),
        )
        current = await job(env, case.jid)
        assert current.status == JobStatus.FAILED
        assert current.error is not None
        assert str(failure) in current.error and cause.strerror in current.error
        assert str(env.library / "movie.mkv") in current.error
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()
        assert any(
            record.levelno == logging.ERROR and record.exc_info is not None and record.exc_info[1] is failure
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_probe_failure(env, monkeypatch):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    offer = c.messages("job")[0]["job"]
    materialize_offer(h, offer)

    async def fail_probe(*args, **kwargs):
        raise RuntimeError("bad artifact")

    monkeypatch.setattr("server.nodehub.hub.run_ffprobe", fail_probe)
    node_probe = ProbeData.model_validate(
        {"file_path": offer["output_path"], "duration_s": 60, "size_bytes": 6, "streams": []}
    )
    await h._handle_finished(
        h.entries["node-a"],
        finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            "succeeded",
            output_path=offer["output_path"],
            output_probe=node_probe,
        ),
    )
    row = await job(env, jid)
    assert row.status == JobStatus.FAILED and row.error is not None
    assert not (h.workspace_dir / offer["output_path"]).exists()
    assert (env.library / "movie.avi").exists()
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_post_failure(env, monkeypatch):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    offer = c.messages("job")[0]["job"]
    output = materialize_offer(h, offer)

    async def fail_gate(*args, **kwargs):
        raise RuntimeError("plugin exploded")

    monkeypatch.setattr(h, "_post_gate", fail_gate)
    node_probe = ProbeData.model_validate(
        {"file_path": offer["output_path"], "duration_s": 60, "size_bytes": 6, "streams": []}
    )
    await h._handle_finished(
        h.entries["node-a"],
        finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            "succeeded",
            output_path=offer["output_path"],
            output_probe=node_probe,
        ),
    )
    row = await job(env, jid)
    assert row.status == JobStatus.FAILED and row.error is not None
    assert not output.exists()
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_artifact_backlog_handoff(writer_env, monkeypatch):
    from server.settings import ScannerConfig
    from server.fs import library as library_fs
    from server.probing import ProbeService
    from server.scanner.scan import scan_library
    from server.workspace import recovery_jobs

    env = writer_env
    probe_entered, probe_release = asyncio.Event(), asyncio.Event()
    queue_entered, queue_release = asyncio.Event(), asyncio.Event()
    enqueue = library_fs.enqueue_healthcheck_job

    async def probe(_binary, _path, **_kwargs):
        probe_entered.set()
        await probe_release.wait()
        return RAW

    async def queue(*args, **kwargs):
        queue_entered.set()
        await queue_release.wait()
        return await enqueue(*args, **kwargs)

    monkeypatch.setattr(library_fs, "run_ffprobe", probe)
    monkeypatch.setattr(library_fs, "enqueue_healthcheck_job", queue)
    async with post_action_case(env, succeed=False) as case:
        service = ProbeService(
            env.factory,
            FAKE_FFPROBE,
            "unused",
            workspace_dir=env.workspace,
            busy_jobs=case.hub.staging.busy_jobs,
        )
        output = materialize_offer(case.hub, case.offer)
        output.write_bytes(b"remux")
        output_size = output.stat().st_size
        completion = asyncio.create_task(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(
                        update={"file_path": case.offer["output_path"], "size_bytes": output_size}
                    ),
                ),
            )
        )
        try:
            await asyncio.wait_for(probe_entered.wait(), 5)
            async with env.factory() as session:
                current = await session.get(Job, case.jid)
                media = await session.get(MediaFile, current.media_file_id)
                assert current.status == "succeeded" and media.probe_id is None
                media_id = media.id
                assert media.size == output_size and media.source_identity == {"size": output_size}
                assert media.container == "avi"
                marker = await session.scalar(
                    select(JobEvent).where(
                        JobEvent.attempt_id == case.offer["attempt_id"], JobEvent.type == "artifact_check_pending"
                    )
                )
                assert marker is not None
                marker_id = marker.id
                backlog = await processing_backlog(session, healthcheck_slots=1, now=env.clock())
                assert backlog["remaining_files"] == 1 and backlog["remaining_s"] is None
                assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) == case.jid
                first_seen = media.first_seen
                library = await session.get(Library, current.library_id)
                report = await scan_library(
                    session,
                    library,
                    config=ScannerConfig(),
                    staging=case.hub.staging,
                    on_files_pending=service.files_pending,
                )
                assert not report.errors
                await session.refresh(media)
                assert media.first_seen == first_seen and media.probe_id is None

            await asyncio.wait_for(service.files_pending([media_id]), 3)
            await asyncio.wait_for(service._fill(), 3)
            async with env.factory() as session:
                assert await session.scalar(select(Job.id).where(Job.kind.in_(["probe", "mkv_stats"]))) is None

            probe_release.set()
            await asyncio.wait_for(queue_entered.wait(), 5)
            async with env.factory() as session:
                current = await session.get(Job, case.jid)
                media = await session.get(MediaFile, current.media_file_id)
                assert media.container == "mkv" and media.probe_id is not None
                assert await session.scalar(select(Job.id).where(Job.kind == "healthcheck")) is None
                assert (await session.get(JobEvent, marker_id)).type == "artifact_check_pending"
                backlog = await processing_backlog(session, healthcheck_slots=1, now=env.clock())
                assert backlog["remaining_files"] == 1 and backlog["remaining_s"] is None
                assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) == case.jid

            await asyncio.wait_for(service.files_pending([media_id]), 3)
            await asyncio.wait_for(service._fill(), 3)
            async with env.factory() as session:
                assert await session.scalar(select(Job.id).where(Job.kind.in_(["probe", "mkv_stats"]))) is None

            queue_release.set()
            await asyncio.wait_for(completion, 5)
            async with env.factory() as session:
                assert (await session.get(JobEvent, marker_id)).type == "artifact_check_finished"
                check = await session.scalar(select(Job).where(Job.kind == "healthcheck"))
                assert check is not None and check.status == "queued"
                assert (await processing_backlog(session, healthcheck_slots=1, now=env.clock()))["remaining_files"] == 1
                check.status = "succeeded"
                media = await session.get(MediaFile, check.media_file_id)
                media.health_status = "healthy"
                await session.commit()
                assert (await processing_backlog(session, healthcheck_slots=1, now=env.clock()))["remaining_files"] == 0
                assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
        finally:
            probe_release.set()
            queue_release.set()
            if not completion.done():
                completion.cancel()
            await asyncio.wait_for(asyncio.gather(completion, return_exceptions=True), 5)
            await asyncio.wait_for(service.close(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["current", "superseded", "failed"])
async def test_discovery_finishes_publication(writer_env, monkeypatch, owner):
    from server.settings import ScannerConfig
    from server.queue.store import requeue_startup
    from server.probing import ProbeService
    from server.scanner.scan import scan_library
    from server.staging import StagingService

    entered, release = asyncio.Event(), asyncio.Event()

    async def interrupted_refresh(_fs, _attempt_id):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)

    monkeypatch.setattr(LibraryFS, "refresh_published", interrupted_refresh)
    env = writer_env
    async with post_action_case(env, succeed=False) as case:
        async with env.factory() as session:
            current = await session.get(Job, case.jid)
            library = await session.get(Library, current.library_id)
            library.healthcheck_enabled = False
            await session.commit()
        materialize_offer(case.hub, case.offer)
        completion = asyncio.create_task(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            )
        )
        service = ProbeService(
            env.factory,
            FAKE_FFPROBE,
            "unused",
            workspace_dir=env.workspace,
            busy_jobs=case.hub.staging.busy_jobs,
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            completion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(completion, 5)
            assert await recover_incomplete(env.factory) == (0, 0)
            async with env.factory() as session:
                await requeue_startup(session, env.clock())
                await session.commit()
            await case.hub.recover_startup()
            async with env.factory() as session:
                current = await session.get(Job, case.jid)
                media_id = current.media_file_id
                assert current.status == "succeeded"
                marker = await session.scalar(
                    select(JobEvent).where(JobEvent.job_id == case.jid, JobEvent.type == "artifact_check_pending")
                )
                assert marker is not None
                marker_id = marker.id
                if owner == "superseded":
                    current.attempts += 1
                elif owner == "failed":
                    current.status = "failed"
                await session.commit()
                library = await session.get(Library, current.library_id)
                report = await scan_library(
                    session,
                    library,
                    config=ScannerConfig(),
                    staging=StagingService(env.factory, env.workspace, min_free_gb=1),
                )
                assert not report.errors
                media = await session.get(MediaFile, media_id)
                assert media.transcode_status == "transcoded" and media.probe_id is None
            await asyncio.wait_for(service._fill(), 3)
            assert len(service._tasks) == 1
            await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 5)
            async with env.factory() as session:
                media = await session.get(MediaFile, media_id)
                assert media.health_status == "healthy" and media.probe_id is not None
                assert media.health_source == {"path": media.path, **media.source_identity}
                marker = await session.get(JobEvent, marker_id)
                assert marker.type == ("artifact_check_finished" if owner == "current" else "artifact_check_pending")
                assert await session.scalar(select(Job.id).where(Job.kind == "healthcheck")) is None
                assert (await processing_backlog(session, healthcheck_slots=1, now=env.clock()))["remaining_files"] == 0
        finally:
            release.set()
            if not completion.done():
                completion.cancel()
            await asyncio.wait_for(asyncio.gather(completion, return_exceptions=True), 5)
            await asyncio.wait_for(service.close(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["probe", "request_result"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_artifact_refresh_completion(writer_env, monkeypatch, phase, cancelled):
    from server.fs import library as library_fs
    from server.probing import ProbeService
    from server.workspace import recovery_jobs

    env = writer_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("Refresh failed.")

    target = "run_ffprobe" if phase == "probe" else "enqueue_healthcheck_job"
    original = getattr(library_fs, target)
    monkeypatch.setattr(library_fs, target, fail)
    async with post_action_case(env, succeed=False) as case:
        materialize_offer(case.hub, case.offer)
        completion = asyncio.create_task(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if cancelled:
                completion.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(completion, 5)
            else:
                release.set()
                await asyncio.wait_for(completion, 5)
            async with env.factory() as session:
                current = await session.get(Job, case.jid)
                assert current.status == "succeeded"
                assert (
                    await session.scalar(
                        select(JobEvent.type).where(
                            JobEvent.attempt_id == case.offer["attempt_id"],
                            JobEvent.type.in_(["artifact_check_pending", "artifact_check_finished"]),
                        )
                    )
                    == "artifact_check_finished"
                )
                pending_probe = cancelled and phase == "probe"
                assert (await processing_backlog(session, healthcheck_slots=1, now=env.clock()))[
                    "remaining_files"
                ] == int(pending_probe)
                assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
                assert await session.scalar(select(Job.id).where(Job.kind == "healthcheck")) is None
            if pending_probe:
                monkeypatch.setattr(library_fs, target, original)
                service = ProbeService(env.factory, FAKE_FFPROBE, "unused", workspace_dir=env.workspace)
                try:
                    await asyncio.wait_for(service._fill(), 3)
                    assert len(service._tasks) == 1
                    await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 5)
                    async with env.factory() as session:
                        current = await session.get(Job, case.jid)
                        media = await session.get(MediaFile, current.media_file_id)
                        assert media.probe_id is not None and media.container == "mkv"
                        check = await session.scalar(select(Job).where(Job.kind == "healthcheck"))
                        assert check is not None and check.status == "queued"
                finally:
                    await asyncio.wait_for(service.close(), 5)
        finally:
            release.set()
            if not completion.done():
                completion.cancel()
            await asyncio.wait_for(asyncio.gather(completion, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_owner", [False, True])
async def test_artifact_cleanup_unconfirmed(writer_env, monkeypatch, lost_owner):
    from server.fs import library as library_fs
    from server.workspace import recovery_jobs

    async def fail_probe(*args):
        raise RuntimeError("Refresh failed.")

    async def invalidate(_operation):
        if lost_owner:
            return False
        raise RuntimeError("Cleanup failed.")

    monkeypatch.setattr(library_fs, "run_ffprobe", fail_probe)
    monkeypatch.setattr(library_fs._ProbeOperation, "_invalidate", invalidate)
    env = writer_env
    async with post_action_case(env, succeed=False) as case:
        materialize_offer(case.hub, case.offer)
        await asyncio.wait_for(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            ),
            5,
        )
        async with env.factory() as session:
            assert (
                await session.scalar(
                    select(JobEvent.type).where(
                        JobEvent.attempt_id == case.offer["attempt_id"], JobEvent.type == "artifact_check_pending"
                    )
                )
                == "artifact_check_pending"
            )
            assert (await processing_backlog(session, healthcheck_slots=1, now=env.clock()))["remaining_files"] == 1
            assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) == case.jid


@pytest.mark.asyncio
async def test_rescan_failure_warns(env, monkeypatch):
    jid = await file_job(env)
    import server.nodehub.hub as hub_module

    async def fail_rescan(*args, **kwargs):
        raise RuntimeError("refresh exploded")

    monkeypatch.setattr(hub_module.LibraryFS, "refresh_published", fail_rescan)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    o = c.messages("job")[0]["job"]
    output = materialize_offer(h, o)
    probe = ProbeData.model_validate(
        {"file_path": o["output_path"], "container": "mkv", "duration_s": 60, "size_bytes": 6, "streams": []}
    )
    await h._handle_finished(
        h.entries["node-a"],
        finished(jid, o["attempt_id"], o["log_path"], "succeeded", output_path=o["output_path"], output_probe=probe),
    )
    row = await job(env, jid)
    assert row.status == JobStatus.SUCCEEDED
    assert row.warning is not None
    async with env.factory() as session:
        assert (
            await session.scalar(
                select(JobEvent.type).where(
                    JobEvent.attempt_id == o["attempt_id"],
                    JobEvent.type.in_(["artifact_check_pending", "artifact_check_finished"]),
                )
            )
            == "artifact_check_finished"
        )
    assert (env.library / "movie.mkv").exists()
    assert not (env.library / "movie.avi").exists()
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_healthcheck_inherits_notifications(env, enabled: bool):
    actions = [
        Notify(targets=["failure"], on=["job.failed"]),
        Notify(targets=["success"], on=["job.succeeded"]),
        ExtractSubtitles(formats=["srt"], languages=["eng"]),
    ]
    jid = await file_job(env, pre_actions=actions)
    async with env.factory() as session:
        await session.execute(update(Library).values(healthcheck_enabled=enabled))
        await session.commit()
    source_size = (env.library / "movie.avi").stat().st_size
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    try:
        await h.dispatch_once()
        offer = c.messages("job")[0]["job"]
        materialize_offer(h, offer)
        probe = ProbeData.model_validate(
            {"file_path": offer["output_path"], "container": "mkv", "duration_s": 60, "size_bytes": 6, "streams": []}
        )
        await h._handle_finished(
            h.entries["node-a"],
            finished(
                jid,
                offer["attempt_id"],
                offer["log_path"],
                "succeeded",
                output_path=offer["output_path"],
                output_probe=probe,
                source_size=8,
                output_size=6,
            ),
        )
        async with env.factory() as session:
            healthcheck = (
                await session.execute(select(Job).where(Job.kind == "healthcheck").order_by(Job.id.desc()))
            ).scalar_one_or_none()
            if enabled:
                assert healthcheck is not None and healthcheck.pre_actions == [actions[0].model_dump(mode="json")]
            else:
                assert healthcheck is None
                media = await session.scalar(select(MediaFile).where(MediaFile.path == str(env.library / "movie.mkv")))
                assert media is not None and media.health_status == "healthy"
                assert media.health_source == {"path": media.path, **media.source_identity}
                assert (
                    await session.scalar(
                        select(JobEvent.id).where(JobEvent.job_id == jid, JobEvent.type == "artifact_check_pending")
                    )
                    is None
                )
            event = (
                await session.execute(select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "succeeded"))
            ).scalar_one()
            assert event.payload["source_size"] == source_size
            assert event.payload["output_size"] == 6
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_log_survives_cleanup(env):
    async with post_action_case(env, succeed=False) as case:
        log = case.hub.workspace_dir / case.offer["log_path"]
        log.write_bytes(b"exact log")
        await case.hub._handle_finished(
            case.hub.entries["node-a"],
            Finished(
                job_id=case.jid,
                attempt_id=case.offer["attempt_id"],
                result="failed",
                log_path=case.offer["log_path"],
            ),
        )
        assert (await attempt(env, case.jid))[0].log_data == b"exact log"
        assert not (case.hub.workspace_dir / f"jobs/{case.jid}").exists()


@pytest.mark.asyncio
async def test_promotes_action_artifact(env):
    async with post_action_case(env) as case:
        raw = {"format": {"filename": case.probe.file_path}}
        case.probe.raw = raw
        sidecar = (case.hub.workspace_dir / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        await resolve_action(case, artifacts=[str(sidecar.relative_to(case.hub.workspace_dir))])
        task = case.conn.messages("action_task")[0]
        assert task["probe"]["file_path"] == task["path"] == case.offer["output_path"]
        assert not Path(task["probe"]["file_path"]).is_absolute()
        assert task["probe"]["raw"] is None
        assert case.probe.file_path == str(env.library / "movie.mp4")
        assert case.probe.raw == raw
        assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert result["artifacts"] == [str(env.library / "movie.eng.srt")]
        assert (await job(env, case.jid)).status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_promotion_failure_completed(env, monkeypatch):
    import errno

    async with post_action_case(env) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")

        def fail_sync(path: Path) -> None:
            assert path == env.library and sidecar.is_file()
            raise OSError(errno.EIO, "directory sync failed")

        monkeypatch.setattr("server.finalization._fsync_directory", fail_sync)
        await resolve_action(case, artifacts=[str(sidecar.relative_to(env.workspace))])
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert not result["ok"] and result["error"] is not None
        assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
        assert not sidecar.exists()
        assert "actions_finished" in await events(env, case.jid)
        assert (await job(env, case.jid)).warning == result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sync", "conflict"])
async def test_failed_publication_finished(env, monkeypatch, failure):
    from server.workspace import recovery_jobs

    async with post_action_case(env) as case:
        output = env.workspace / case.offer["output_path"]
        sidecars = [output.with_name(f"output.eng.{suffix}") for suffix in ("srt", "ass")]
        destinations = [env.library / f"movie.eng.{suffix}" for suffix in ("srt", "ass")]
        for sidecar in sidecars:
            sidecar.write_bytes(sidecar.suffix.encode())
        if failure == "sync":
            synced = []

            def sync_directory(path):
                assert path == env.library
                synced.append(path)
                if len(synced) == 2:
                    raise OSError("directory sync failed")

            monkeypatch.setattr("server.finalization._fsync_directory", sync_directory)
        else:
            destinations[1].write_bytes(b"existing")
        await resolve_action(case, artifacts=[str(sidecar.relative_to(env.workspace)) for sidecar in sidecars])
        assert destinations[0].read_bytes() == b".srt"
        assert not sidecars[0].exists()
        assert destinations[1].read_bytes() == (b".ass" if failure == "sync" else b"existing")
        assert not sidecars[1].exists()
        stored = (await attempt(env, case.jid))[0]
        assert not stored.action_results[0]["ok"]
        assert stored.action_results[0]["artifacts"] == [str(destinations[0])]
        assert (await job(env, case.jid)).warning is not None
        async with env.factory() as session:
            assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
            report = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == case.jid, JobEvent.type == "action_report")
            )
            assert report.payload["results"][0]["artifacts"] == [
                str(sidecar.relative_to(env.workspace)) for sidecar in sidecars
            ]
            assert "actions_finished" in await events(env, case.jid)
        await hub(env)._cleanup_action_workspace(case.jid, case.offer["attempt_id"])
        assert not (env.workspace / f"jobs/{case.jid}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_publication_cancel_locks(env, monkeypatch, cancellations):
    from server.finalization import _move_artifact

    started, finished = asyncio.Event(), asyncio.Event()
    publication_done, cleanup_entered, cleanup_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    worker_errors = []

    def controlled_move(source, destination):
        try:
            assert source.read_bytes() == b"subtitle"
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            assert source.read_bytes() == b"subtitle"
            _move_artifact(source, destination)
        except Exception as exc:
            worker_errors.append(exc)
            raise
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr("server.finalization._move_artifact", controlled_move)
    async with post_action_case(env) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        cleanup = case.hub._cleanup_action_workspace

        async def observed_promote(*args, **kwargs):
            try:
                return await publish_artifacts(*args, **kwargs)
            finally:
                publication_done.set()

        async def observed_cleanup(*args):
            cleanup_entered.set()
            try:
                await cleanup(*args)
            finally:
                cleanup_done.set()

        monkeypatch.setattr("server.nodehub.hub.publish_artifacts", observed_promote)
        monkeypatch.setattr(case.hub, "_cleanup_action_workspace", observed_cleanup)
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            await case.hub._handle_report(
                case.hub.entries["node-a"].node_db_id,
                ActionReport(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    results=[
                        ActionOutcome(
                            kind=case.action.kind,
                            ok=True,
                            artifacts=[str(sidecar.relative_to(env.workspace))],
                        )
                    ],
                ),
            )
            await asyncio.wait_for(started.wait(), 5)
            for _ in range(cancellations):
                dispatch.cancel()
                checkpoint = asyncio.Event()
                loop.call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 5)
                if publication_done.is_set():
                    await asyncio.wait_for(cleanup_done.wait(), 5)
                assert not cleanup_entered.is_set(), "workspace cleanup overtook the publication worker"
                assert not dispatch.done() and not finished.is_set()
                assert sidecar.read_bytes() == b"subtitle"
        finally:
            release.set()
            if not dispatch.done() and not dispatch.cancelling():
                dispatch.cancel()
            outcomes = await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)
            if started.is_set():
                await asyncio.wait_for(finished.wait(), 5)

        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert not worker_errors
        assert cleanup_done.is_set() and not (env.workspace / f"jobs/{case.jid}").exists()
        assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
        assert (env.library / "movie.avi").read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_publication_batch_locks(env, monkeypatch, cancellations):
    from server.finalization import _move_artifact

    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def controlled_move(source, destination):
        if source.name != "output.eng.srt":
            return _move_artifact(source, destination)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            assert source.read_bytes() == b"srt"
            _move_artifact(source, destination)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr("server.finalization._move_artifact", controlled_move)
    async with post_action_case(env) as case:
        output = env.workspace / case.offer["output_path"]
        sidecars = [output.with_name(f"output.eng.{suffix}") for suffix in ("srt", "ass")]
        published = [env.library / f"movie.eng.{suffix}" for suffix in ("srt", "ass")]
        for sidecar in sidecars:
            sidecar.write_bytes(sidecar.suffix[1:].encode())
        log = env.workspace / case.offer["log_path"]
        log.write_bytes(b"encode\nsidecars\n")
        async with env.factory() as session:
            current = await session.get(JobAttempt, case.offer["attempt_id"])
            current.log_data = b"encode\n"
            await session.commit()
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(
                    kind=case.action.kind,
                    ok=True,
                    artifacts=[str(sidecar.relative_to(env.workspace)) for sidecar in sidecars],
                )
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, report)
            await asyncio.wait_for(started.wait(), 5)
            for _ in range(cancellations):
                dispatch.cancel()
                checkpoint = asyncio.Event()
                loop.call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 5)
                assert not dispatch.done() and not finished.is_set()
                assert all(sidecar.is_file() for sidecar in sidecars)
        finally:
            release.set()
            if not dispatch.done() and not dispatch.cancelling():
                dispatch.cancel()
            outcomes = await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)
            if started.is_set():
                await asyncio.wait_for(finished.wait(), 5)

        assert isinstance(outcomes[0], asyncio.CancelledError)
        for sidecar, destination in zip(sidecars, published, strict=True):
            assert sidecar.exists() or destination.exists(), (
                "workspace cleanup discarded an unpromoted reported artifact"
            )
            assert destination.read_bytes() == sidecar.suffix[1:].encode()
        stored = (await attempt(env, case.jid))[0]
        expected = (
            report.results[0]
            .model_copy(update={"artifacts": [str(path) for path in published]})
            .model_dump(mode="json")
        )
        assert stored.action_results == [expected]
        assert stored.log_data == b"encode\nsidecars\n"
        async with env.factory() as session:
            event = await session.scalar(
                select(JobEvent).where(JobEvent.job_id == case.jid, JobEvent.type == "action_results")
            )
            assert event.payload == {"phase": "post", "results": [expected]}
        assert not (env.workspace / f"jobs/{case.jid}").exists()
        assert (env.library / "movie.avi").read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["load", "logs"])
async def test_report_cancel_locks(env, monkeypatch, phase):
    from server.workspace import read_attempt_logs

    started, release = asyncio.Event(), asyncio.Event()
    get = AsyncSession.get
    async with post_action_case(env) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        (env.workspace / case.offer["log_path"]).write_bytes(b"encode\nsidecars\n")
        async with env.factory() as session:
            current = await session.get(JobAttempt, case.offer["attempt_id"])
            current.log_data = b"encode\n"
            await session.commit()

        async def controlled_get(session, entity, identifier, *args, **kwargs):
            result = await get(session, entity, identifier, *args, **kwargs)
            if (
                phase == "load"
                and entity is JobAttempt
                and identifier == case.offer["attempt_id"]
                and not started.is_set()
            ):
                started.set()
                await release.wait()
            return result

        async def controlled_logs(*args):
            if phase == "logs":
                started.set()
                await release.wait()
            return await read_attempt_logs(*args)

        monkeypatch.setattr(AsyncSession, "get", controlled_get)
        monkeypatch.setattr("server.nodehub.hub.read_attempt_logs", controlled_logs)
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))])
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, report)
            await asyncio.wait_for(started.wait(), 5)
            dispatch.cancel()
            # Let the interrupted await run before releasing the controlled boundary.
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
        finally:
            release.set()
            if not dispatch.done() and not dispatch.cancelling():
                dispatch.cancel()
            outcomes = await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)

        assert isinstance(outcomes[0], asyncio.CancelledError)
        published = env.library / "movie.eng.srt"
        assert sidecar.exists() or published.exists(), "a finished report lost its artifact before publication began"
        stored = (await attempt(env, case.jid))[0]
        expected = report.results[0].model_copy(update={"artifacts": [str(published)]}).model_dump(mode="json")
        assert stored.action_results == [expected], "cleanup discarded the accepted report's saved history"
        assert stored.log_data == b"encode\nsidecars\n"
        assert published.read_bytes() == b"subtitle"
        assert (env.library / "movie.avi").read_bytes() == b"source"


@pytest.mark.asyncio
async def test_history_preserves_actions(env):
    from server.retention import delete_eligible_job

    async with post_action_case(env) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            async with env.factory() as session:
                deleted = await delete_eligible_job(session, case.jid)
                await session.commit()
            assert not deleted, "history deletion must not revoke a live post-action completion"
            sidecar.write_bytes(b"subtitle")
            await case.hub._handle_report(
                case.hub.entries["node-a"].node_db_id,
                ActionReport(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    results=[
                        ActionOutcome(
                            kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))]
                        )
                    ],
                ),
            )
            outcomes = await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)
        finally:
            if not dispatch.done():
                report = case.hub._action_reports.get((case.jid, case.offer["attempt_id"]))
                if report is not None and not report.done():
                    report.set_result([ActionOutcome(kind=case.action.kind, ok=False, error="test teardown")])
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)
        published = env.library / "movie.eng.srt"
        assert sidecar.exists() or published.exists(), "history deletion lost an active worker's generated artifact"
        assert published.is_file(), "deleting history prevented the active worker's successful artifact publication"
        assert not deleted, "terminal media publication does not mean post-actions are finished"
        assert outcomes == [None]
        assert published.read_bytes() == b"subtitle"
        assert (env.library / "movie.avi").read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["load", "logs", "commit"])
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_report_transaction_cancel(env, monkeypatch, phase, cancellations):
    from server.workspace import read_attempt_logs

    started, release = asyncio.Event(), asyncio.Event()
    get, commit = AsyncSession.get, AsyncSession.commit
    async with post_action_case(env) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        (env.workspace / case.offer["log_path"]).write_bytes(b"encode\nsidecars\n")
        async with env.factory() as session:
            current = await session.get(JobAttempt, case.offer["attempt_id"])
            current.log_data = b"encode\n"
            await session.commit()

        async def controlled_get(session, entity, identifier, *args, **kwargs):
            result = await get(session, entity, identifier, *args, **kwargs)
            if phase == "load" and entity is JobAttempt and not started.is_set():
                started.set()
                await release.wait()
            return result

        async def controlled_logs(*args):
            if phase == "logs":
                started.set()
                await release.wait()
            return await read_attempt_logs(*args)

        async def controlled_commit(session):
            if phase == "commit" and any(
                isinstance(event, JobEvent) and event.type == "actions_finished" for event in session.new
            ):
                started.set()
                await release.wait()
            await commit(session)

        monkeypatch.setattr(AsyncSession, "get", controlled_get)
        monkeypatch.setattr(AsyncSession, "commit", controlled_commit)
        monkeypatch.setattr("server.nodehub.hub.read_attempt_logs", controlled_logs)
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))])
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, report)
            await asyncio.wait_for(started.wait(), 5)
            for _ in range(cancellations):
                dispatch.cancel()
                checkpoint = asyncio.Event()
                asyncio.get_running_loop().call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 5)
                assert not dispatch.done()
                assert sidecar.exists() or (env.library / "movie.eng.srt").exists()
                assert (env.workspace / case.offer["log_path"]).is_file()
        finally:
            release.set()
            if not dispatch.done() and not dispatch.cancelling():
                dispatch.cancel()
            outcomes = await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        stored = (await attempt(env, case.jid))[0]
        assert stored.log_data == b"encode\nsidecars\n"
        published = env.library / "movie.eng.srt"
        expected = report.results[0].model_copy(update={"artifacts": [str(published)]}).model_dump(mode="json")
        assert stored.action_results == [expected]
        async with env.factory() as session:
            event = await session.scalar(select(JobEvent).where(JobEvent.type == "action_results"))
            assert event.payload == {"phase": "post", "results": [expected]}
            assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "actions_finished")) is not None
        assert published.read_bytes() == b"subtitle"
        assert not (env.workspace / f"jobs/{case.jid}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["load", "commit", "report_commit"])
async def test_report_failure_recovers(env, monkeypatch, phase):
    from server.workspace import post_action_jobs, cleanup_workspace

    get, commit = AsyncSession.get, AsyncSession.commit
    async with post_action_case(env) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        (env.workspace / case.offer["log_path"]).write_bytes(b"action log")

        async def failed_get(session, entity, identifier, *args, **kwargs):
            if phase == "load" and entity is JobAttempt:
                raise RuntimeError("completion load failed")
            return await get(session, entity, identifier, *args, **kwargs)

        async def failed_commit(session):
            target = "action_report" if phase == "report_commit" else "actions_finished"
            if phase != "load" and any(isinstance(event, JobEvent) and event.type == target for event in session.new):
                raise RuntimeError("completion commit failed")
            await commit(session)

        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))])
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            append_message(env.workspace, case.offer["log_path"], report.model_dump(mode="json"))
            with monkeypatch.context() as patch:
                patch.setattr(AsyncSession, "get", failed_get)
                patch.setattr(AsyncSession, "commit", failed_commit)
                await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, report)
                with pytest.raises(RuntimeError):
                    await asyncio.wait_for(dispatch, 5)
            async with env.factory() as session:
                saved = await session.scalar(select(JobEvent).where(JobEvent.type == "action_report"))
                if phase != "report_commit":
                    assert saved.payload["results"] == [outcome.model_dump(mode="json") for outcome in report.results]
                    assert saved.payload["path"] == case.offer["output_path"]
                await session.commit()
            await cleanup_workspace(env.factory, env.workspace)
            async with env.factory() as session:
                assert await session.scalar(post_action_jobs().where(Job.id == case.jid)) is None
                assert await session.scalar(select(JobEvent.id).where(JobEvent.type == "actions_finished")) is not None
                stored = await session.get(JobAttempt, case.offer["attempt_id"])
                assert stored.log_data == b"action log"
            assert not (env.workspace / f"jobs/{case.jid}").exists()
            assert (env.library / "movie.avi").read_bytes() == b"source"
        finally:
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_terminal_actions_tracked(env, monkeypatch):
    from server.retention import delete_eligible_job
    from server.workspace import post_action_jobs

    started, release = asyncio.Event(), asyncio.Event()
    async with post_action_case(
        env, succeed=False, job_kwargs={"post_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])]}
    ) as case:
        materialize_offer(case.hub, case.offer)
        publish = case.hub._publish_status
        send = case.conn.send_json

        async def controlled_publish(job):
            if job.id == case.jid and job.status == JobStatus.SUCCEEDED and not started.is_set():
                started.set()
                await release.wait()
            await publish(job)

        async def report_send(data):
            await send(data)
            if data["type"] == "action_task":
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                    ),
                )

        monkeypatch.setattr(case.hub, "_publish_status", controlled_publish)
        monkeypatch.setattr(case.conn, "send_json", report_send)
        finishing = asyncio.create_task(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert not case.conn.messages("action_task")
            async with env.factory() as session:
                assert await session.scalar(post_action_jobs().where(Job.id == case.jid)) == case.jid
                assert not await delete_eligible_job(session, case.jid)
                await session.commit()
            assert (await case.hub.staging.prune_job(case.jid))["outcome"] == "skipped"
        finally:
            release.set()
            if not finishing.done() and not started.is_set():
                finishing.cancel()
            await asyncio.wait_for(asyncio.gather(finishing, return_exceptions=True), 5)
            await asyncio.wait_for(asyncio.gather(*tuple(case.hub._action_waits), return_exceptions=True), 5)
        assert not finishing.cancelled() and finishing.exception() is None
        async with env.factory() as session:
            assert await session.scalar(post_action_jobs().where(Job.id == case.jid)) is None
            media = await session.get(MediaFile, (await session.get(Job, case.jid)).media_file_id)
            assert Path(media.path).read_bytes() == b"output"
            assert await delete_eligible_job(session, case.jid)
            await session.commit()


@pytest.mark.asyncio
async def test_post_log_retained(env):
    async with post_action_case(env) as case:
        log = case.hub.workspace_dir / case.offer["log_path"]
        log.write_bytes(b"encode\naction log\n")
        async with env.factory() as session:
            current = await session.get(JobAttempt, case.offer["attempt_id"])
            assert current is not None
            current.log_data = b"encode\n"
            await session.commit()
        await resolve_action(case)
        assert (await attempt(env, case.jid))[0].log_data == b"encode\naction log\n"
        async with env.factory() as session:
            stage = await case.hub._stage_event(session, current)
            assert stage is not None and stage.payload["log"] == "encode\naction log\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["succeeded", "failed", "veto", "collision"])
async def test_pre_sidecars_survive(env, monkeypatch, result):
    monkeypatch.setitem(
        RAW,
        "streams",
        [*RAW["streams"], {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}}],
    )
    async with post_action_case(
        env,
        succeed=False,
        job_kwargs={
            "plan": FFmpegPlan(streams=StreamPolicy(subtitle=StreamSelection(keep_languages=[], keep_forced=False))),
            "pre_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])],
        },
    ) as case:
        output = materialize_offer(case.hub, case.offer)
        sidecar = output.with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        destination = env.library / "movie.eng.srt"
        if result == "collision":
            destination.write_bytes(b"old-bytes")
        if result == "veto":
            from server.flows.engine import FlowResult

            async def veto(*args):
                return FlowResult(status="vetoed", reason="rejected output"), [], None, [], []

            monkeypatch.setattr(case.hub, "_post_gate", veto)
        await asyncio.wait_for(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                Finished(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    result="failed" if result == "failed" else "succeeded",
                    log_path=case.offer["log_path"],
                    output_path=None if result == "failed" else case.offer["output_path"],
                    output_probe=None
                    if result == "failed"
                    else case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                    action_results=[
                        ActionOutcome(
                            kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(case.hub.workspace_dir))]
                        )
                    ],
                ),
            ),
            5,
        )
        recorded = (await attempt(env, case.jid))[0].action_results[0]
        assert recorded["ok"] is (result == "succeeded")
        assert recorded["artifacts"] == (
            [str(destination)]
            if result == "succeeded"
            else [str(sidecar.relative_to(case.hub.workspace_dir))]
            if result == "collision"
            else []
        )
        if result in {"succeeded", "collision"}:
            assert destination.read_bytes() == (b"subtitle" if result == "succeeded" else b"old-bytes")
        else:
            assert not destination.exists()
            assert (await job(env, case.jid)).status == JobStatus.FAILED
        if result == "collision":
            assert (env.library / "movie.avi").read_bytes() == b"source"
            assert not (env.library / "movie.mkv").exists()
            assert (await job(env, case.jid)).status == JobStatus.FINALIZING
            assert not output.parent.exists()
            async with env.factory() as session:
                required = await session.scalar(select(JobEvent).where(JobEvent.type == "required_artifacts"))
                assert Path(required.payload["entries"][0]["copy"]).read_bytes() == b"subtitle"
            assert await recover_incomplete(env.factory) == (0, 1)
        else:
            assert not output.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("report", ["failed", "missing"])
async def test_extraction_report_rejected(env, monkeypatch, report):
    monkeypatch.setitem(
        RAW,
        "streams",
        [*RAW["streams"], {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}}],
    )
    async with post_action_case(
        env,
        succeed=False,
        job_kwargs={
            "plan": FFmpegPlan(streams=StreamPolicy(subtitle=StreamSelection(keep_languages=[], keep_forced=False))),
            "pre_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])],
        },
    ) as case:
        materialize_offer(case.hub, case.offer)
        await asyncio.wait_for(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    action_results=[ActionOutcome(kind="extract_subtitles", ok=False, error="ENOSPC")]
                    if report == "failed"
                    else [],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            ),
            2,
        )
        row = await job(env, case.jid)
        assert row.status == JobStatus.FAILED and row.error is not None
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists() and not (env.library / "movie.eng.srt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["succeeded", "failed", "collision"])
@pytest.mark.parametrize("trash_mode", ["none", "transcode"])
async def test_multistage_subtitle_safety(env, monkeypatch, result, trash_mode):
    from server.flows.engine import FlowResult
    from server.queue.store import ActiveJobError, check_file_idle
    from server.workspace import cleanup_workspace

    subtitle_stream = {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}}
    monkeypatch.setitem(RAW, "streams", [*RAW["streams"], subtitle_stream])
    async with post_action_case(
        env,
        succeed=False,
        job_kwargs={
            "plan": FFmpegPlan(),
            "pre_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])],
        },
    ) as case:
        async with env.factory() as session:
            library = (await session.scalars(select(Library))).one()
            library.trash_mode = trash_mode
            await session.commit()

        async def probe(_binary, path, **_kwargs):
            return {**RAW, "streams": RAW["streams"] if path.name == "output.mkv" else RAW["streams"][:2]}

        async def gate(_session, _job, _output, _source, stage):
            next_plan = (
                FFmpegPlan(streams=StreamPolicy(subtitle=StreamSelection(keep_languages=[], keep_forced=False)))
                if stage["number"] == 1
                else FFmpegPlan()
            )
            return FlowResult(status="matched", plan=next_plan) if stage["number"] < 3 else None, [], None, [], []

        monkeypatch.setattr("server.nodehub.hub.run_ffprobe", probe)
        monkeypatch.setattr("server.finalization.run_ffprobe", probe)
        monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
        monkeypatch.setattr(case.hub, "_post_gate", gate)
        sidecar = (env.workspace / case.offer["output_path"]).with_suffix(".eng.srt")
        sidecar.write_bytes(b"subtitle")
        final_sidecar = env.library / "movie.eng.srt"
        if result == "collision":
            final_sidecar.write_bytes(b"old-bytes")
        for number in range(1, 4):
            offer = case.conn.messages("job")[-1]["job"]
            materialize_offer(case.hub, offer)
            await asyncio.wait_for(
                case.hub._handle_finished(
                    case.hub.entries["node-a"],
                    finished(
                        case.jid,
                        offer["attempt_id"],
                        offer["log_path"],
                        "failed" if number == 3 and result == "failed" else "succeeded",
                        output_path=None if number == 3 and result == "failed" else offer["output_path"],
                        output_probe=None
                        if number == 3 and result == "failed"
                        else ProbeData.model_validate(offer["probe"]).model_copy(
                            update={"file_path": offer["output_path"]}
                        ),
                        action_results=[
                            ActionOutcome(
                                kind="extract_subtitles", ok=True, artifacts=[str(sidecar.relative_to(env.workspace))]
                            )
                        ]
                        if number == 1
                        else [],
                    ),
                ),
                2,
            )
            if number < 3:
                assert (env.library / "movie.avi").read_bytes() == b"source"
                assert not (env.library / "movie.mkv").exists()
                assert final_sidecar.exists() is (result == "collision")
                await case.hub.dispatch_once()
        row = await job(env, case.jid)
        if result == "succeeded":
            assert row.status == JobStatus.SUCCEEDED and final_sidecar.read_bytes() == b"subtitle"
            assert not (env.library / "movie.avi").exists()
        else:
            assert (env.library / "movie.avi").read_bytes() == b"source"
            assert not (env.library / "movie.mkv").exists()
            if result == "collision":
                assert row.status == JobStatus.FINALIZING
                assert final_sidecar.read_bytes() == b"old-bytes" and not sidecar.exists()
                async with env.factory() as session:
                    required = await session.scalar(select(JobEvent).where(JobEvent.type == "required_artifacts"))
                    assert Path(required.payload["entries"][0]["copy"]).read_bytes() == b"subtitle"
                assert await recover_incomplete(env.factory) == (0, 1)
                async with env.factory() as session:
                    with pytest.raises(ActiveJobError):
                        await check_file_idle(session, row.media_file_id)
                await cleanup_workspace(env.factory, env.workspace)
                assert not sidecar.exists()
                assert final_sidecar.read_bytes() == b"old-bytes"
                assert (env.library / "movie.avi").read_bytes() == b"source"
            else:
                assert row.status == JobStatus.FAILED and not final_sidecar.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["existing", "racing", "cross_device"])
async def test_sidecar_collision_preserved(env, monkeypatch, collision):
    import errno
    import os

    existing_file = env.library / "movie.eng.srt"
    if collision != "racing":
        existing_file.write_bytes(b"old-bytes")
    async with post_action_case(env) as case:
        sidecar = (case.hub.workspace_dir / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"replacement")
        link = os.link

        def collide(source, destination, **kwargs):
            from tests.helpers import fd_path

            if (
                fd_path(source, kwargs.get("src_dir_fd")) == sidecar
                and fd_path(destination, kwargs.get("dst_dir_fd")) == existing_file
            ):
                if collision == "cross_device":
                    raise OSError(errno.EXDEV, "cross-device")
                if collision == "racing":
                    existing_file.write_bytes(b"old-bytes")
            return link(source, destination, **kwargs)

        monkeypatch.setattr("common.fs.effects.os.link", collide)
        await resolve_action(case, artifacts=[str(sidecar.relative_to(case.hub.workspace_dir))])
        assert existing_file.read_bytes() == b"old-bytes"
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert result["ok"] is False and result["artifacts"] == []
        assert result["error"] is not None
        row = await job(env, case.jid)
        assert row.status == JobStatus.SUCCEEDED and row.warning == result["error"]
        async with env.factory() as session:
            event = (
                await session.execute(
                    select(JobEvent).where(JobEvent.job_id == case.jid, JobEvent.type == "action_results")
                )
            ).scalar_one()
            assert event.payload["results"] == [result]
        assert not sidecar.exists()
        assert "actions_finished" in await events(env, case.jid)
        assert not list(env.library.glob(".*.part"))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pre", "post"])
@pytest.mark.parametrize("collision", [False, True])
async def test_publication_transaction_boundary(env, monkeypatch, phase, collision):
    from sqlalchemy import text, update

    from server.finalization import finalize

    warning = (
        "Publication cleanup pending. Workspace retained."
        if phase == "pre"
        else "Post-action cancellation did not finish. Workspace retained."
    )

    async def warned_finalize(*args, **kwargs):
        publication = await finalize(*args, **kwargs)
        publication.warning = warning
        return publication

    if phase == "pre":
        monkeypatch.setattr("server.nodehub.hub.finalize", warned_finalize)
    async with post_action_case(
        env,
        succeed=phase == "post",
        job_kwargs={"pre_actions": [ExtractSubtitles(formats=["srt"], languages=["eng", "spa"])]}
        if phase == "pre"
        else None,
    ) as case:
        sidecars = [
            (env.workspace / case.offer["output_path"]).with_name(f"output.{language}.srt")
            for language in ("eng", "spa")
        ]
        for sidecar in sidecars:
            sidecar.write_bytes(b"subtitle")
            if collision:
                (env.library / sidecar.name.replace("output.", "movie.")).write_bytes(b"old-bytes")
        results = [
            ActionOutcome(kind="extract_subtitles", ok=True, artifacts=[str(path.relative_to(env.workspace))])
            for path in sidecars
        ]
        observed = []

        async def publish(factory, attempt_id, outcomes, *, workspace_dir):
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '1s'"))
                current = await session.get(Job, case.jid)
                assert current is not None and current.status == JobStatus.SUCCEEDED and current.warning == warning
                pending = (
                    await session.scalars(
                        select(JobEvent).where(JobEvent.job_id == case.jid, JobEvent.type == "actions_pending")
                    )
                ).one()
                if phase == "pre":
                    assert pending.payload["raw_results"] == [result.model_dump(mode="json") for result in results]
                else:
                    report = (
                        await session.scalars(
                            select(JobEvent).where(JobEvent.attempt_id == attempt_id, JobEvent.type == "action_report")
                        )
                    ).one()
                    assert report.payload["results"] == [result.model_dump(mode="json") for result in results]
                await session.execute(update(Job).where(Job.id == case.jid).values(updated_at=Job.updated_at))
                await session.commit()
            durable = await publish_artifacts(factory, attempt_id, outcomes, workspace_dir=workspace_dir)
            observed.extend(durable)
            return durable

        monkeypatch.setattr("server.nodehub.hub.publish_artifacts", publish)
        if phase == "pre":
            materialize_offer(case.hub, case.offer)
            await asyncio.wait_for(
                case.hub._handle_finished(
                    case.hub.entries["node-a"],
                    finished(
                        case.jid,
                        case.offer["attempt_id"],
                        case.offer["log_path"],
                        "succeeded",
                        output_path=case.offer["output_path"],
                        output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                        action_results=results,
                    ),
                ),
                3,
            )
        else:
            assert await case.hub._prepare_post_actions(case.jid, case.offer["attempt_id"], [case.action])
            assert not await asyncio.wait_for(
                case.hub._finish_post_actions(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["output_path"],
                    case.probe,
                    results,
                    [],
                    accepted=True,
                    finished=False,
                    prerequisite_error=None,
                    node_id=case.hub.entries["node-a"].node_db_id,
                ),
                3,
            )
        assert len(observed) == 2
        current = await job(env, case.jid)
        assert current.status == JobStatus.SUCCEEDED
        assert current.warning == (observed[-1].error if collision else warning)
        assert (await attempt(env, case.jid))[0].action_results == [
            result.model_dump(mode="json") for result in observed
        ]
        if collision:
            assert observed[0].error != observed[-1].error
            assert all(not result.ok for result in observed)
        else:
            assert all(result.ok for result in observed)


@pytest.mark.asyncio
async def test_cleanup_warning_persists(env, monkeypatch):
    def fail_cleanup(*args, **kwargs):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr("server.staging._remove_job", fail_cleanup)
    async with post_action_case(env) as case:
        sidecar = (case.hub.workspace_dir / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        await resolve_action(case, artifacts=[str(sidecar.relative_to(case.hub.workspace_dir))])
        row = await job(env, case.jid)
        assert row.status == JobStatus.SUCCEEDED and row.warning is not None
        assert (await events(env, case.jid)).count("cleanup_failed") == 1
        assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"


@pytest.mark.asyncio
async def test_rejects_foreign_artifact(env):
    async with post_action_case(env) as case:
        foreign = case.hub.workspace_dir / f"jobs/{case.jid}/attempts/999/output.eng.srt"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign")
        await resolve_action(case, artifacts=[f"jobs/{case.jid}/attempts/999/output.eng.srt"])
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert result["ok"] is False and result["artifacts"] == []
        assert not (env.library / "movie.eng.srt").exists()


@pytest.mark.asyncio
async def test_rejects_artifact_symlink(env):
    outside = make_test_temp_dir("artifact-target") / "output.eng.srt"
    outside.write_bytes(b"keep")
    async with post_action_case(env) as case:
        sidecar = (case.hub.workspace_dir / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.symlink_to(outside)
        await resolve_action(case, artifacts=[str(sidecar.relative_to(case.hub.workspace_dir))])
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert result["ok"] is False
        assert outside.read_bytes() == b"keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("finishes", [True, False])
async def test_action_timeout_cleans(env, finishes):
    class ReportingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "cancel":
                assert output.exists()
                if not finishes:
                    raise OSError("node disconnected")
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                    ),
                )

    async with post_action_case(env, conn=ReportingConn([hello()]), hub_kwargs={"action_task_timeout_s": 0}) as case:
        output = case.hub.workspace_dir / case.offer["output_path"]
        await asyncio.wait_for(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            ),
            2,
        )
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert result["ok"] is False
        assert result["error"] is not None
        assert "actions_finished" in await events(env, case.jid)
        assert bool(case.conn.messages("cancel"))
        assert not output.exists()
        assert (await job(env, case.jid)).status == JobStatus.SUCCEEDED
        assert (case.jid, case.offer["attempt_id"]) not in case.hub._action_reports


@pytest.mark.asyncio
async def test_action_send_finishes(env):
    from server.workspace import cleanup_workspace

    async with post_action_case(env) as case:
        attempt_id = case.offer["attempt_id"]
        entry = case.hub.entries["node-a"]
        entry.conn = FailingConn(RuntimeError("send failed"))
        await asyncio.wait_for(
            case.hub._dispatch_post_actions(
                entry, case.jid, attempt_id, case.offer["output_path"], case.probe, [case.action], [], []
            ),
            2,
        )
        result = (await attempt(env, case.jid))[0].action_results[0]
        assert not result["ok"] and result["error"] is not None
        assert (await job(env, case.jid)).status == JobStatus.SUCCEEDED
        assert not (case.hub.workspace_dir / f"jobs/{case.jid}").exists()
        assert "actions_pending" in await events(env, case.jid)
        assert "actions_finished" in await events(env, case.jid)
        assert (case.jid, attempt_id) not in case.hub._action_reports
        await cleanup_workspace(env.factory, env.workspace)
        assert not (env.workspace / f"jobs/{case.jid}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "disconnect"])
async def test_action_delivery_locked(env, failure):
    delivered = asyncio.Event()
    release = asyncio.Event()

    class AmbiguousConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                delivered.set()
                await release.wait()
                raise OSError("connection lost after delivery")
            if data.get("type") == "cancel":
                raise OSError("worker completion unavailable")

    async with post_action_case(env, conn=AmbiguousConn([hello()])) as case:
        output = case.hub.workspace_dir / case.offer["output_path"]
        with workspace_lock(output.parent):
            dispatch = asyncio.create_task(
                case.hub._dispatch_post_actions(
                    case.hub.entries["node-a"],
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["output_path"],
                    case.probe,
                    [case.action],
                    [],
                    [],
                )
            )
            try:
                await asyncio.wait_for(delivered.wait(), 2)
                if failure == "cancel":
                    dispatch.cancel()
                else:
                    release.set()
                await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
                assert output.exists()
            finally:
                release.set()
                if not dispatch.done():
                    dispatch.cancel()
                await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
        assert (await case.hub.staging.prune_job(case.jid))["outcome"] == "removed"
        assert not (env.workspace / f"jobs/{case.jid}").exists()
        assert "actions_finished" in await events(env, case.jid)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "disconnect"])
@pytest.mark.parametrize("completion", ["delivery", "cancel"])
async def test_action_delivery_completed(env, failure, completion):
    delivered, release = asyncio.Event(), asyncio.Event()

    class ReportingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                if completion == "delivery":
                    await case.hub._handle_report(
                        case.hub.entries["node-a"].node_db_id,
                        ActionReport(
                            job_id=case.jid,
                            attempt_id=case.offer["attempt_id"],
                            results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                        ),
                    )
                delivered.set()
                await release.wait()
                raise OSError("connection lost after delivery")
            if data.get("type") == "cancel":
                assert output.read_bytes() == b"output"
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                    ),
                )
                raise OSError("connection lost after cancellation report")

    async with post_action_case(env, conn=ReportingConn([hello()])) as case:
        output = case.hub.workspace_dir / case.offer["output_path"]
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await asyncio.wait_for(delivered.wait(), 2)
            if failure == "cancel":
                dispatch.cancel()
            else:
                release.set()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
            assert dispatch.cancelled() is (failure == "cancel")
            assert not (env.workspace / f"jobs/{case.jid}").exists()
            assert not case.hub._action_reports
            assert (env.library / "movie.avi").read_bytes() == b"source"
        finally:
            release.set()
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["send", "report"])
@pytest.mark.parametrize("cancel_phase", ["send", "report", "completed"])
async def test_action_recancel_locks(env, phase, cancel_phase):
    delivered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                delivered.set()
                if phase == "send":
                    await release.wait()
            if data.get("type") == "cancel":
                if cancel_phase == "completed":
                    await case.hub._handle_report(
                        case.hub.entries["node-a"].node_db_id,
                        ActionReport(
                            job_id=case.jid,
                            attempt_id=case.offer["attempt_id"],
                            results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                        ),
                    )
                cancelling.set()
                if cancel_phase != "report":
                    await release.wait()

    async with post_action_case(env, conn=BlockingConn([hello()])) as case:
        output = case.hub.workspace_dir / case.offer["output_path"]
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await asyncio.wait_for(delivered.wait(), 2)
            dispatch.cancel()
            await asyncio.wait_for(cancelling.wait(), 2)
            assert output.read_bytes() == b"output"
            dispatch.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not dispatch.done() and output.read_bytes() == b"output"
            if cancel_phase != "completed":
                pending = case.hub._action_reports[(case.jid, case.offer["attempt_id"])]
                pending.set_exception(OSError("worker completion unavailable"))
            release.set()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
            assert dispatch.cancelled()
            assert not (env.workspace / f"jobs/{case.jid}").exists()
            assert "actions_finished" in await events(env, case.jid)
            assert not case.hub._action_reports
        finally:
            release.set()
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "ok"), [("send", True), ("pending", False)])
async def test_action_report_accepted(env, phase, ok):
    cancelling = asyncio.Event()

    class ReportingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                if phase == "send":
                    await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, report)
                raise OSError("task send failed after delivery")
            if data.get("type") == "cancel":
                cancelling.set()

    async with post_action_case(env, conn=ReportingConn([hello()])) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(
                    kind=case.action.kind,
                    ok=ok,
                    error=None if ok else "worker extraction failed",
                    artifacts=[str(sidecar.relative_to(env.workspace))],
                )
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            if phase == "pending":
                await asyncio.wait_for(cancelling.wait(), 2)
                assert sidecar.read_bytes() == b"subtitle" and not dispatch.done()
                case.conn.push(report.model_dump(mode="json"))
            await asyncio.wait_for(dispatch, 2)
            result = (await attempt(env, case.jid))[0].action_results[0]
            published = env.library / "movie.eng.srt"
            assert result == report.results[0].model_copy(
                update={"artifacts": [str(published)] if ok else []}
            ).model_dump(mode="json")
            assert published.exists() is ok
            if ok:
                assert published.read_bytes() == b"subtitle"
            assert not (env.workspace / f"jobs/{case.jid}").exists()
            assert not case.hub._action_reports
            assert (env.library / "movie.avi").read_bytes() == b"source"
        finally:
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True])
async def test_action_cancel_publishes(env, repeat):
    delivered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ReportingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                delivered.set()
                await release.wait()
            if data.get("type") == "cancel":
                await case.hub._handle_report(case.hub.entries["node-a"].node_db_id, report)
                cancelling.set()
                await release.wait()

    async with post_action_case(env, conn=ReportingConn([hello()])) as case:
        sidecar = (env.workspace / case.offer["output_path"]).with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        report = ActionReport(
            job_id=case.jid,
            attempt_id=case.offer["attempt_id"],
            results=[
                ActionOutcome(kind=case.action.kind, ok=True, artifacts=[str(sidecar.relative_to(env.workspace))])
            ],
        )
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await asyncio.wait_for(delivered.wait(), 2)
            dispatch.cancel()
            await asyncio.wait_for(cancelling.wait(), 2)
            assert sidecar.read_bytes() == b"subtitle"
            if repeat:
                dispatch.cancel()
                checkpoint = asyncio.Event()
                asyncio.get_running_loop().call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 2)
                assert not dispatch.done() and sidecar.read_bytes() == b"subtitle"
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
            assert isinstance(outcomes[0], asyncio.CancelledError)
            assert (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
            assert (await attempt(env, case.jid))[0].action_results[0]["ok"] is True
            assert not (env.workspace / f"jobs/{case.jid}").exists()
            assert not case.hub._action_reports
        finally:
            release.set()
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["send", "report", "cancel"])
@pytest.mark.parametrize("failure", ["cancelled", "error"])
async def test_action_report_finishes(env, phase, failure):
    delivered = asyncio.Event()

    def teardown_report():
        report = case.hub._action_reports[(case.jid, case.offer["attempt_id"])]
        if failure == "cancelled":
            report.cancel()
        else:
            report.set_exception(OSError("connection teardown"))

    class TeardownConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                if phase == "send":
                    teardown_report()
                delivered.set()
            if data.get("type") == "cancel":
                teardown_report()

    async with post_action_case(env, conn=TeardownConn([hello()])) as case:
        output = case.hub.workspace_dir / case.offer["output_path"]
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                case.probe,
                [case.action],
                [],
                [],
            )
        )
        try:
            await asyncio.wait_for(delivered.wait(), 2)
            if phase == "report":
                teardown_report()
            elif phase == "cancel":
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
            assert not output.exists()
            assert "actions_finished" in await events(env, case.jid)
            assert not case.hub._action_reports
        finally:
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["workspace", "requirements", "path", "validation"])
async def test_action_prerequisite_cleans(env, failure):
    async with post_action_case(env) as case:
        entry = case.hub.entries["node-a"]
        path = case.offer["output_path"]
        requires = []
        if failure == "workspace":
            entry.workspace_ready = False
        elif failure == "requirements":
            requires = ["missing"]
        elif failure == "path":
            path = None
        else:
            path = "/outside/output.mkv"
        await asyncio.wait_for(
            case.hub._dispatch_post_actions(
                entry, case.jid, case.offer["attempt_id"], path, case.probe, [case.action], requires, []
            ),
            2,
        )
        assert not (env.workspace / f"jobs/{case.jid}").exists()
        assert not case.conn.messages("action_task") and not case.conn.messages("cancel")
        assert not case.hub._action_reports
        assert (await attempt(env, case.jid))[0].action_results[0]["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("requirement", ["missing_encoder", "missing_tag", "encoder_subset", "matched", "server_only"])
async def test_final_action_requirements(env, terminal, requirement):
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from tests.helpers import plugin_host

    nodes = [
        {"id": "subs", "type": "plugin", "plugin": "action.extract_subs", "inputs": {"formats": ["vtt"]}},
        {"id": "dates", "type": "plugin", "plugin": "action.keep_dates"},
        {"id": "done", "type": "terminal", "outcome": "done"},
    ]
    start = "dates" if requirement == "server_only" else "subs"
    flow = FlowDefinition.model_validate(
        {
            "entry": "done" if terminal else start,
            "nodes": nodes,
            "edges": [
                {"src": "subs", "port": "next", "dst": "dates"},
                *([{"src": "done", "port": "next", "dst": start}] if terminal else []),
            ],
        }
    )
    jid = await file_job(env, plan=FFmpegPlan(), continuation=FlowContinuation(flow=flow, node=flow.entry))
    stamp = (env.library / "movie.avi").stat().st_mtime_ns
    compatible = requirement in {"matched", "server_only"}
    encoders = ["libx264", "webvtt"] if requirement in {"matched", "missing_tag", "encoder_subset"} else ["libx264"]

    class ReportingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                await h._handle_report(
                    h.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=data["job_id"],
                        attempt_id=data["attempt_id"],
                        results=[ActionOutcome(kind="extract_subtitles", ok=True)],
                    ),
                )

    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        manifest = host.describe("action.keep_dates" if requirement == "server_only" else "action.extract_subs")
        assert manifest is not None
        manifest.requires = ["gpu", "cpu"]
        manifest.requires_encoders = ["webvtt", "srt"] if requirement == "encoder_subset" else ["webvtt"]
        h = hub(env, plugin_host=host)
        conn = ReportingConn([hello(encoders=encoders, tags=[] if requirement == "missing_tag" else ["cpu"])])
        receiver = await serve(h, conn)
        try:
            assert await asyncio.wait_for(h.dispatch_once(), 3) == 1
            offer = conn.messages("job")[0]["job"]
            materialize_offer(h, offer)
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        offer["attempt_id"],
                        offer["log_path"],
                        "succeeded",
                        output_path=offer["output_path"],
                        output_probe=ProbeData.model_validate(offer["probe"]).model_copy(
                            update={"file_path": offer["output_path"]}
                        ),
                    ),
                ),
                3,
            )
            await asyncio.wait_for(asyncio.gather(*list(h._action_waits)), 3)
            row = await job(env, jid)
            assert row.status == JobStatus.SUCCEEDED
            assert (env.library / "movie.mkv").read_bytes() == b"output"
            assert (env.library / "movie.mkv").stat().st_mtime_ns == stamp
            assert bool(conn.messages("action_task")) is (requirement == "matched")
            for task in conn.messages("action_task"):
                assert task["probe"]["file_path"] == task["path"]
                assert not Path(task["probe"]["file_path"]).is_absolute()
                assert task["probe"]["raw"] is None
            results = (await attempt(env, jid))[0].action_results
            if requirement == "server_only":
                assert not results and row.warning is None
            else:
                assert results[-1]["ok"] is compatible
                if not compatible:
                    assert results[-1]["error"] is not None
                    assert results[-1]["error"] == row.warning
                async with env.factory() as session:
                    event = (
                        await session.scalars(
                            select(JobEvent).where(JobEvent.job_id == jid, JobEvent.type == "post_actions")
                        )
                    ).one()
                    assert event.attempt_id == offer["attempt_id"]
                    assert event.payload["requires"] == sorted(manifest.requires)
                    assert event.payload["requires_encoders"] == sorted(manifest.requires_encoders)
            assert not h._action_reports
            assert not (env.workspace / f"jobs/{jid}").exists()
        finally:
            conn.disconnect()
            await asyncio.wait_for(receiver, 2)
            await h.aclose()


@pytest.mark.asyncio
async def test_deferred_action_requirements(env):
    from server.flows.definition import FlowDefinition
    from server.flows.engine import FlowContinuation
    from tests.helpers import plugin_host

    flow = FlowDefinition.model_validate(
        {
            "entry": "dates",
            "nodes": [
                {"id": "dates", "type": "plugin", "plugin": "action.keep_dates"},
                {"id": "encode", "type": "plugin", "plugin": "transcode.video", "inputs": {"encoder": "libx264"}},
                {"id": "done", "type": "terminal", "outcome": "done"},
            ],
            "edges": [
                {"src": "dates", "port": "next", "dst": "encode"},
                {"src": "encode", "port": "plan", "dst": "done"},
            ],
        }
    )
    jid = await file_job(env, plan=FFmpegPlan(), continuation=FlowContinuation(flow=flow, node="dates"))
    async with plugin_host([Path(__file__).resolve().parents[1] / "plugins"]) as host:
        manifest = host.describe("action.keep_dates")
        assert manifest is not None
        manifest.requires = ["cpu"]
        manifest.requires_encoders = ["webvtt"]
        h, conn = hub(env, plugin_host=host), FakeConn([hello(encoders=["libx264", "webvtt"], tags=["cpu"])])
        receiver = await serve(h, conn)
        try:
            assert await h.dispatch_once() == 1
            first = conn.messages("job")[0]["job"]
            materialize_offer(h, first)
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        first["attempt_id"],
                        first["log_path"],
                        "succeeded",
                        output_path=first["output_path"],
                        output_probe=ProbeData.model_validate(first["probe"]).model_copy(
                            update={"file_path": first["output_path"]}
                        ),
                    ),
                ),
                3,
            )
            async with env.factory() as session:
                stage = await h._stage_event(session, (await attempt(env, jid))[0])
                assert stage is not None
                assert stage.payload["post_requires"] == ["cpu"]
                assert stage.payload["post_requires_encoders"] == ["webvtt"]
            entry = h.entries["node-a"]
            entry.capabilities.encoders = ["libx264"]
            assert await h.dispatch_once() == 0
            assert (await job(env, jid)).warning is not None
            entry.capabilities.encoders = ["libx264", "webvtt"]
            assert await h.dispatch_once() == 1
            second = conn.messages("job")[1]["job"]
            materialize_offer(h, second)
            await asyncio.wait_for(
                h._handle_finished(
                    h.entries["node-a"],
                    finished(
                        jid,
                        second["attempt_id"],
                        second["log_path"],
                        "succeeded",
                        output_path=second["output_path"],
                        output_probe=ProbeData.model_validate(second["probe"]).model_copy(
                            update={"file_path": second["output_path"]}
                        ),
                    ),
                ),
                3,
            )
            async with env.factory() as session:
                event = (
                    await session.scalars(
                        select(JobEvent)
                        .where(JobEvent.job_id == jid, JobEvent.type == "post_actions")
                        .order_by(JobEvent.id.desc())
                    )
                ).first()
                assert event is not None
                assert event.attempt_id == second["attempt_id"]
                assert event.payload["requires"] == ["cpu"]
                assert event.payload["requires_encoders"] == ["webvtt"]
            assert (await job(env, jid)).status == JobStatus.SUCCEEDED
            assert not conn.messages("action_task")
        finally:
            conn.disconnect()
            await asyncio.wait_for(receiver, 2)
            await h.aclose()


@pytest.mark.asyncio
async def test_action_cancel_cleans(env):
    send_started = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    class SignalingConn(FakeConn):
        async def send_json(self, data):
            await super().send_json(data)
            if data.get("type") == "action_task":
                send_started.set()
            elif data.get("type") == "cancel":
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind=case.action.kind, ok=False, error="action cancelled")],
                    ),
                )

    outside = make_test_temp_dir("action-outside") / "published.txt"
    outside.write_bytes(b"keep")
    async with post_action_case(env, conn=SignalingConn([hello()])) as case:
        cleanup = case.hub._cleanup_workspace

        async def controlled_cleanup(job_id, attempts):
            cleanup_entered.set()
            await cleanup_release.wait()
            await cleanup(job_id, attempts)

        case.hub._cleanup_workspace = controlled_cleanup
        dispatch = asyncio.create_task(
            case.hub._dispatch_post_actions(
                case.hub.entries["node-a"],
                case.jid,
                case.offer["attempt_id"],
                case.offer["output_path"],
                None,
                [case.action],
                [],
                [],
            )
        )
        case.hub._action_waits.add(dispatch)
        await asyncio.wait_for(send_started.wait(), 2)
        assert (case.jid, case.offer["attempt_id"]) in case.hub._action_reports
        close = asyncio.create_task(case.hub.aclose())
        try:
            await asyncio.wait_for(cleanup_entered.wait(), 2)
            assert not close.done()
            cleanup_release.set()
            await asyncio.wait_for(close, 2)
        finally:
            cleanup_release.set()
            if not close.done():
                close.cancel()
            await asyncio.gather(close, return_exceptions=True)
            if not dispatch.done():
                dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)
        assert dispatch.cancelled()
        assert (case.jid, case.offer["attempt_id"]) not in case.hub._action_reports
        assert outside.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_log_parent_symlink(env):
    jid = await file_job(env)
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    o = c.messages("job")[0]["job"]
    p = h.workspace_dir / o["log_path"]
    external = make_test_temp_dir("outside")
    p.parent.rename(external / "moved")
    p.parent.symlink_to(external / "moved")
    await h._handle_finished(h.entries["node-a"], finished(jid, o["attempt_id"], o["log_path"]))
    assert (await job(env, jid)).status == JobStatus.FAILED
    c.disconnect()
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.parametrize("missing_log", [False, True])
@pytest.mark.asyncio
async def test_finished_transcode(env, monkeypatch, preserve, missing_log):
    monkeypatch.setattr("server.fs.library.utcnow", env.clock)
    monkeypatch.setattr("server.queue.store.utcnow", env.clock)
    first_seen = env.clock() - timedelta(hours=3)
    jid = await file_job(env, pre_actions=[PreserveTimestamps()] if preserve else None)
    async with env.factory() as s:
        library = (await s.execute(select(Library).where(Library.name == "library"))).scalar_one()
        library.trash_mode = "all"
        library.cooldown_s = 7200
        media = (await s.scalars(select(MediaFile))).one()
        media.first_seen = first_seen
        await s.commit()
    h = hub(env)
    c = FakeConn([hello()])
    task = await serve(h, c)
    await h.dispatch_once()
    o = c.messages("job")[0]["job"]
    output = materialize_offer(h, o)
    output.write_bytes(b"remux")
    if missing_log:
        (h.workspace_dir / o["log_path"]).unlink()
    from common.media.probe import ProbeData

    probe = ProbeData.model_validate(
        {"file_path": o["output_path"], "container": "mkv", "duration_s": 60, "size_bytes": 5, "streams": []}
    )
    await h._handle_finished(
        h.entries["node-a"],
        finished(jid, o["attempt_id"], o["log_path"], "succeeded", output_path=o["output_path"], output_probe=probe),
    )
    assert (await job(env, jid)).status == JobStatus.SUCCEEDED
    assert list(env.trash.rglob("*"))
    async with env.factory() as s:
        event = await s.scalar(
            select(JobEvent).where(JobEvent.attempt_id == o["attempt_id"], JobEvent.type == "publication")
        )
        assert event.payload["preserve_timestamps"] is preserve
        media = (
            await s.execute(select(MediaFile).where(MediaFile.path == str(env.library / "movie.mkv")))
        ).scalar_one()
        assert media.size == 5
        assert media.container == "mkv"
        assert media.video_codec == "h264"
        assert media.health_status == HealthStatus.PENDING.value
        assert media.transcode_status == "transcoded"
        attempt = await s.get(JobAttempt, o["attempt_id"])
        assert attempt is not None and attempt.output_probe is not None
        assert attempt.output_probe["file_path"] == str(env.library / "movie.mkv")
        assert attempt.output_probe["container"] == "mkv"
        assert attempt.output_probe["streams"][0]["codec"] == "h264"
        probe = await s.get(ProbeRow, media.probe_id)
        assert json.loads(probe.raw_json)["format"]["duration"] == "600.0"
        healthcheck = (
            await s.execute(select(Job).where(Job.kind == "healthcheck", Job.media_file_id == media.id))
        ).scalar_one()
    c.disconnect()
    await asyncio.wait_for(task, 2)
    assert media.first_seen.replace(tzinfo=UTC) == first_seen
    assert healthcheck.status == JobStatus.QUEUED and healthcheck.hold_until is None


@pytest.mark.asyncio
@pytest.mark.parametrize("modified", [False, True])
async def test_dates_survive_reads(env, monkeypatch, modified):
    from common.fs import effects
    from server.workspace import staged_dates

    source, destination = env.library / "movie.avi", env.library / "movie.mkv"
    original_atime, original_mtime = 1_500_000_000_000_000_000, 1_600_000_000_000_000_000
    read_atime = 1_700_000_000_000_000_000
    write, stat, utime, copy = Path.write_bytes, Path.stat, os.utime, effects.copy_file
    reads = set()
    copied = False
    refreshed = False

    def read(path):
        path.read_bytes()
        reads.add(path)

    def read_stat(path, value):
        if path not in reads:
            return value
        # Model read-atime on noatime test storage.
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_atime_ns": read_atime, "st_atime": read_atime / 1e9})

    def restore_dates(path, **kwargs):
        utime(path, **kwargs)
        if not isinstance(path, int):
            reads.discard(Path(path))

    def write_source(path, data):
        result = write(path, data)
        if path == source:
            os.utime(path, ns=(original_atime, original_mtime))
        return result

    def byte_copy(src, dst, **kwargs):
        nonlocal copied
        result = copy(src, dst, **kwargs)
        if src == source:
            read(source)
            copied = True
        return result

    async def reading_probe(_binary, path, **_kwargs):
        read(path)
        return RAW

    refresh_published = LibraryFS.refresh_published

    async def refresh(fs, attempt_id):
        nonlocal refreshed
        # Publication refresh restores the staged dates even after a metadata edit.
        if modified:
            os.utime(destination, ns=(original_atime, original_mtime + 1))
        refreshed = True
        return await refresh_published(fs, attempt_id)

    monkeypatch.setattr(Path, "write_bytes", write_source)
    monkeypatch.setattr(Path, "stat", lambda path, *args, **kwargs: read_stat(path, stat(path, *args, **kwargs)))
    monkeypatch.setattr("os.utime", restore_dates)
    monkeypatch.setattr("common.fs.effects.fcntl.ioctl", lambda *_: (_ for _ in ()).throw(OSError(22, "unsupported")))
    monkeypatch.setattr(effects, "copy_file", byte_copy)
    monkeypatch.setattr("server.workspace.copy_file", byte_copy)
    monkeypatch.setattr("server.nodehub.hub.run_ffprobe", reading_probe)
    monkeypatch.setattr("server.finalization.run_ffprobe", reading_probe)
    monkeypatch.setattr("server.fs.library.run_ffprobe", reading_probe)
    monkeypatch.setattr("server.fs.library.LibraryFS.refresh_published", refresh)
    async with post_action_case(env, succeed=False, job_kwargs={"pre_actions": [PreserveTimestamps()]}) as case:
        assert copied and source.stat().st_atime_ns == read_atime
        assert staged_dates(env.workspace, case.jid) == {"atime_ns": original_atime, "mtime_ns": original_mtime}
        materialize_offer(case.hub, case.offer)
        await asyncio.wait_for(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            ),
            2,
        )
        assert (await job(env, case.jid)).status == JobStatus.SUCCEEDED
        assert refreshed
        assert destination.stat().st_atime_ns == original_atime
        assert destination.stat().st_mtime_ns == original_mtime
        async with env.factory() as session:
            media = (await session.scalars(select(MediaFile).where(MediaFile.path == str(destination)))).one()
            assert media.mtime_ns == original_mtime
            assert media.size == 6 and media.probe_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("trash_mode", ["all", "none"])
async def test_publication_pending(env, monkeypatch, trash_mode):
    jid = await file_job(env, post_actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])])
    async with env.factory() as session:
        library = (await session.scalars(select(Library))).one()
        library.trash_mode = trash_mode
        await session.commit()
    h, c = hub(env), FakeConn([hello()])
    task = await serve(h, c)
    source, destination = env.library / "movie.avi", env.library / "movie.mkv"
    real_link = os.link

    def fail_publication(src, dst, **kwargs):
        from tests.helpers import fd_path

        if fd_path(dst, kwargs.get("dst_dir_fd")) == destination:
            raise OSError("publication interrupted")
        real_link(src, dst, **kwargs)

    try:
        await asyncio.wait_for(h.dispatch_once(), 3)
        offer = c.messages("job")[0]["job"]
        output = materialize_offer(h, offer)
        probe = ProbeData(file_path=offer["output_path"], duration_s=60, size_bytes=6, streams=[])
        msg = finished(
            jid,
            offer["attempt_id"],
            offer["log_path"],
            "succeeded",
            output_path=offer["output_path"],
            output_probe=probe,
        )
        monkeypatch.setattr("server.finalization.os.link", fail_publication)
        await asyncio.wait_for(h._handle_finished(h.entries["node-a"], msg), 3)
        await asyncio.wait_for(h._handle_finished(h.entries["node-a"], msg), 3)
        async with env.factory() as session:
            row = await session.get(Job, jid)
            attempt_row = await session.get(JobAttempt, offer["attempt_id"])
            journal = (await session.scalars(select(FinalizationJournal))).one()
            media = await session.get(MediaFile, row.media_file_id)
            assert row.status == attempt_row.status == JobStatus.FINALIZING
            assert row.finished_at is None and attempt_row.result is None and attempt_row.finished_at is None
            assert row.warning is not None
            assert media.path == str(source) and media.transcode_status != "transcoded"
            assert journal.state == ("original_moved" if trash_mode == "all" else "discard_original_moved")
            assert Path(journal.trash_path).read_bytes() == b"source"
            assert Path(journal.partial_path).read_bytes() == b"output"
        assert not (env.workspace / f"jobs/{jid}").exists()
        assert attempt_row.log_data == b"log"
        assert not source.exists() and not destination.exists()
        assert jid not in h.entries["node-a"].in_flight and not h._action_waits

        monkeypatch.setattr("server.finalization.os.link", real_link)
        assert await asyncio.wait_for(recover_incomplete(env.factory), 3) == (1, 0)
        async with env.factory() as session:
            row = await session.get(Job, jid)
            attempt_row = await session.get(JobAttempt, offer["attempt_id"])
            media = await session.get(MediaFile, row.media_file_id)
            assert row.status == attempt_row.status == JobStatus.SUCCEEDED
            assert attempt_row.result == "succeeded" and row.warning is None
            assert media.path == str(destination) and media.size == 6 and media.transcode_status == "transcoded"
        assert destination.read_bytes() == b"output"
    finally:
        c.disconnect()
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["collision", "sync", "batch"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_pre_promotion_finished(env, monkeypatch, failure, deferred):
    from server.finalization import _move_artifact
    from server.workspace import recovery_jobs, cleanup_workspace

    monkeypatch.setitem(
        RAW,
        "streams",
        [
            *RAW["streams"],
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
            *(
                [{"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fra"}}]
                if failure == "batch"
                else []
            ),
        ],
    )

    async def probe(_binary, _path, **_kwargs):
        return RAW

    monkeypatch.setattr("server.nodehub.hub.run_ffprobe", probe)
    monkeypatch.setattr("server.finalization.run_ffprobe", probe)
    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    async with post_action_case(
        env,
        succeed=False,
        job_kwargs={
            "plan": FFmpegPlan(),
            "pre_actions": [
                ExtractSubtitles(formats=["srt"], languages=["eng", "fra"] if failure == "batch" else ["eng"])
            ],
            "post_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])] if deferred else None,
        },
    ) as case:
        output = materialize_offer(case.hub, case.offer)
        sidecar = output.with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        destination = env.library / "movie.eng.srt"
        artifacts = [str(sidecar.relative_to(env.workspace))]
        if failure == "batch":
            first = sidecar
            sidecar = output.with_name("output.fra.srt")
            sidecar.write_bytes(b"subtitle")
            destination = env.library / "movie.fra.srt"
            artifacts.append(str(sidecar.relative_to(env.workspace)))
        if failure == "collision":
            destination.write_bytes(b"old-bytes")
        else:

            def failed_move(source, target):
                if failure == "batch" and source == first:
                    return _move_artifact(source, target)

                def failed_sync(_path):
                    raise OSError("sidecar sync failed")

                with monkeypatch.context() as patch:
                    patch.setattr("server.finalization._fsync_directory", failed_sync)
                    _move_artifact(source, target)

            monkeypatch.setattr("server.finalization._move_artifact", failed_move)
        finishing = asyncio.create_task(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                    action_results=[
                        ActionOutcome(kind="extract_subtitles", ok=True, artifacts=[artifact]) for artifact in artifacts
                    ],
                ),
            )
        )
        try:
            if deferred:
                await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
                pending = case.hub._action_reports[(case.jid, case.offer["attempt_id"])]
                await case.hub._handle_report(
                    case.hub.entries["node-a"].node_db_id,
                    ActionReport(
                        job_id=case.jid,
                        attempt_id=case.offer["attempt_id"],
                        results=[ActionOutcome(kind="extract_subtitles", ok=True)],
                    ),
                )
                assert pending.done() and not pending.cancelled(), "publication protection rejected worker completion"
            await asyncio.wait_for(finishing, 5)
            if deferred:
                await asyncio.wait_for(asyncio.gather(*case.hub._action_waits), 3)
                assert "action_report" in await events(env, case.jid)
        finally:
            if not finishing.done():
                finishing.cancel()
            await asyncio.wait_for(asyncio.gather(finishing, return_exceptions=True), 5)
            tasks = tuple(case.hub._action_waits)
            pending = case.hub._action_reports.get((case.jid, case.offer["attempt_id"]))
            if pending is not None and not pending.done():
                pending.set_exception(OSError("worker completion unavailable during teardown"))
            for task in tasks:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        stored = (await attempt(env, case.jid))[0]
        assert (await job(env, case.jid)).status == JobStatus.SUCCEEDED
        failed_result = stored.action_results[1 if failure == "batch" else 0]
        assert not failed_result["ok"]
        assert failed_result["error"] is not None
        assert destination.read_bytes() == (b"old-bytes" if failure == "collision" else b"subtitle")
        assert not sidecar.exists()
        if failure == "batch":
            assert not first.exists() and (env.library / "movie.eng.srt").read_bytes() == b"subtitle"
            assert stored.action_results[0]["ok"]
        assert bool(case.conn.messages("action_task")) is deferred
        if deferred:
            assert "actions_finished" in await events(env, case.jid)
        async with env.factory() as session:
            assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
        await cleanup_workspace(env.factory, env.workspace)
        assert not (env.workspace / f"jobs/{case.jid}").exists()


@pytest.mark.asyncio
async def test_refresh_dates_drained(env, monkeypatch):
    started, drained = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    utime = os.utime
    dates = {"atime_ns": 1_500_000_000_000_000_000, "mtime_ns": 1_600_000_000_000_000_000}
    target = env.library / "movie.mkv"
    target.write_bytes(b"x" * 2048)

    def controlled_utime(path, **kwargs):
        assert Path(path) == target
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            utime(path, **kwargs)
        finally:
            loop.call_soon_threadsafe(drained.set)

    async def probe(_binary, _path, **_kwargs):
        return RAW

    os.utime(target, ns=(dates["atime_ns"], dates["mtime_ns"]))
    jid = await file_job(env)
    async with env.factory() as session:
        job = await session.get(Job, jid)
        assert job is not None
        library = await session.get(Library, job.library_id)
        media = await session.get(MediaFile, job.media_file_id)
        assert library is not None and media is not None
        source = Path(media.path)
        attempt = JobAttempt(
            job_id=jid,
            attempt_number=1,
            status=JobStatus.SUCCEEDED,
            result="succeeded",
            source_size=source.stat().st_size,
            output_size=target.stat().st_size,
        )
        session.add(attempt)
        await session.flush()
        job.attempts = 1
        job.status = JobStatus.SUCCEEDED
        journal = FinalizationJournal(
            attempt_id=attempt.id,
            source_path=str(source),
            destination_path=str(target),
            partial_path=str(env.workspace / "partial"),
            trash_path=str(env.trash / "original"),
            state="completed",
        )
        session.add(journal)
        session.add(
            JobEvent(
                job_id=jid,
                attempt_id=attempt.id,
                type="publication",
                payload={
                    "library_id": library.id,
                    "roots": library.roots,
                    "preserve_timestamps": True,
                    "dates": dates,
                },
            )
        )
        await session.commit()
        attempt_id = attempt.id
    os.utime(source, ns=(dates["atime_ns"], dates["mtime_ns"]))
    stage_fixture(env.workspace, jid, source)
    monkeypatch.setattr("server.fs.library.run_ffprobe", probe)
    monkeypatch.setattr("server.fs.source.os.utime", controlled_utime)
    task = asyncio.create_task(
        LibraryFS(
            library.id,
            sessionmaker=env.factory,
            ffprobe_bin="ffprobe",
            mkvpropedit_bin="mkvpropedit",
            workspace_dir=env.workspace,
        ).refresh_published(attempt_id)
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done(), "publication refresh returned while its timestamp writer was still running"
            assert not drained.is_set()
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(drained.wait(), 5)
    assert isinstance(outcomes[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_publication_roots_checked(env, monkeypatch):
    from common.fs import effects
    from server.queue.store import update_library

    started, drained = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    copy = effects.copy_file
    async with post_action_case(env, succeed=False) as case:
        output = materialize_offer(case.hub, case.offer)

        def controlled_copy(src, dst, **kwargs):
            if src != output:
                return copy(src, dst, **kwargs)
            loop.call_soon_threadsafe(started.set)
            try:
                assert release.wait(5)
                return copy(src, dst, **kwargs)
            finally:
                loop.call_soon_threadsafe(drained.set)

        monkeypatch.setattr(effects, "copy_file", controlled_copy)
        task = asyncio.create_task(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            current_root = env.library / "current"
            current_root.mkdir()
            async with env.factory() as session:
                library = (await session.scalars(select(Library))).one()
                await update_library(session, library.id, {"roots": [str(current_root)]})
                await session.commit()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            await asyncio.wait_for(drained.wait(), 5)
        source = env.library / "movie.avi"
        assert source.exists(), "publication used revoked library roots to discard the original"
        assert source.read_bytes() == b"source" and not (env.library / "movie.mkv").exists()


@pytest.mark.asyncio
async def test_sidecar_roots_checked(env, monkeypatch):
    from common.media.probe import probe_from_raw
    from common.fs import effects
    from server.finalization import FinalizationError, finalize
    from server.queue.store import update_library

    started, drained = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    copy = effects.copy_file
    async with post_action_case(env, succeed=False) as case:
        output = materialize_offer(case.hub, case.offer)
        sidecar = output.with_name("output.eng.srt")
        sidecar.write_bytes(b"subtitle")
        target = env.library / "movie.eng.srt"

        def controlled_copy(src, dst, **kwargs):
            if src != sidecar:
                return copy(src, dst, **kwargs)
            loop.call_soon_threadsafe(started.set)
            try:
                assert release.wait(5)
                return copy(src, dst, **kwargs)
            finally:
                loop.call_soon_threadsafe(drained.set)

        async def probe(_binary, _path, **_kwargs):
            return RAW

        async def publishing():
            async with env.factory() as session:
                row = await session.get(JobAttempt, case.offer["attempt_id"])
                current = await session.get(Job, case.jid)
                current.status = JobStatus.FINALIZING
                source = env.library / "movie.avi"
                raw_input = {
                    **RAW,
                    "streams": RAW["streams"] + [{"index": 2, "codec_name": "subrip", "codec_type": "subtitle"}],
                }
                stage = await session.scalar(
                    select(JobEvent).where(JobEvent.attempt_id == row.id, JobEvent.type == "stage")
                )
                stage.payload = {
                    **stage.payload,
                    "result": "succeeded",
                    "probe": probe_from_raw(source, 6, raw_input).model_dump(mode="json"),
                    "plan": FFmpegPlan(streams=StreamPolicy(subtitle=StreamSelection(keep_languages=[]))).model_dump(
                        mode="json"
                    ),
                    "action_results": [
                        ActionOutcome(
                            kind="extract_subtitles", ok=True, artifacts=[str(sidecar.relative_to(env.workspace))]
                        ).model_dump(mode="json")
                    ],
                }
                await session.commit()
            return await finalize(
                env.factory, row.id, workspace_dir=env.workspace, trash_root=None, ffprobe_bin="ffprobe"
            )

        monkeypatch.setattr(effects, "copy_file", controlled_copy)
        monkeypatch.setattr("server.finalization.run_ffprobe", probe)
        task = asyncio.create_task(publishing())
        try:
            await asyncio.wait_for(started.wait(), 5)
            current_root = env.library / "current"
            current_root.mkdir()
            async with env.factory() as session:
                library = (await session.scalars(select(Library))).one()
                await update_library(session, library.id, {"roots": [str(current_root)]})
                await session.commit()
        finally:
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            if started.is_set():
                await asyncio.wait_for(drained.wait(), 5)
            else:
                assert started.is_set(), outcomes
        assert not isinstance(outcomes[0], BaseException)
        assert outcomes[0].pending and not outcomes[0].committed
        assert not target.exists(), "required sidecar publication used revoked library roots"
        assert sidecar.read_bytes() == b"subtitle"
        assert (env.library / "movie.avi").read_bytes() == b"source"
        assert not (env.library / "movie.mkv").exists()


@pytest.mark.asyncio
async def test_finish_cleans_partial(env, monkeypatch):
    commit = AsyncSession.commit
    committed = False
    async with post_action_case(env, succeed=False) as case:
        materialize_offer(case.hub, case.offer)
        partial = env.library / f".{case.offer['attempt_id']}.part"

        async def observe_commit(session):
            nonlocal committed
            journal = await session.scalar(
                select(FinalizationJournal).where(FinalizationJournal.attempt_id == case.offer["attempt_id"])
            )
            finishing = not committed and journal is not None and journal.state in {"completed", "discard_completed"}
            if finishing:
                assert partial.read_bytes() == b"output"
            await commit(session)
            if finishing:
                assert partial.read_bytes() == b"output"
                committed = True

        monkeypatch.setattr(AsyncSession, "commit", observe_commit)
        await asyncio.wait_for(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            ),
            5,
        )
        assert committed
        assert (env.library / "movie.mkv").read_bytes() == b"output"
        assert not partial.exists()


@pytest.mark.asyncio
async def test_action_roots_checked(env):
    from server.queue.store import update_library
    from server.workspace import recovery_jobs, cleanup_workspace

    async with post_action_case(
        env, succeed=False, job_kwargs={"post_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])]}
    ) as case:
        output = materialize_offer(case.hub, case.offer)
        await asyncio.wait_for(
            case.hub._handle_finished(
                case.hub.entries["node-a"],
                finished(
                    case.jid,
                    case.offer["attempt_id"],
                    case.offer["log_path"],
                    "succeeded",
                    output_path=case.offer["output_path"],
                    output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                ),
            ),
            5,
        )
        try:
            await wait_for(lambda: case.conn.messages("action_task"), case.conn.changed)
            sidecar = output.with_name("output.eng.srt")
            sidecar.write_bytes(b"subtitle")
            current_root = env.library / "current"
            current_root.mkdir()
            async with env.factory() as session:
                library = (await session.scalars(select(Library))).one()
                await update_library(session, library.id, {"roots": [str(current_root)]})
                await session.commit()
            await case.hub._handle_report(
                case.hub.entries["node-a"].node_db_id,
                ActionReport(
                    job_id=case.jid,
                    attempt_id=case.offer["attempt_id"],
                    results=[
                        ActionOutcome(
                            kind="extract_subtitles", ok=True, artifacts=[str(sidecar.relative_to(env.workspace))]
                        )
                    ],
                ),
            )
            await asyncio.wait_for(asyncio.gather(*case.hub._action_waits), 5)
            assert not (env.library / "movie.eng.srt").exists(), "deferred artifact publication used revoked roots"
            assert not sidecar.exists()
            stored = (await attempt(env, case.jid))[0]
            assert not stored.action_results[0]["ok"] and stored.action_results[0]["error"] is not None
            async with env.factory() as session:
                assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
            await cleanup_workspace(env.factory, env.workspace)
            assert not (env.workspace / f"jobs/{case.jid}").exists()
        finally:
            report = case.hub._action_reports.get((case.jid, case.offer["attempt_id"]))
            if report is not None and not report.done():
                report.set_result([ActionOutcome(kind="extract_subtitles", ok=False, error="test teardown")])
            tasks = tuple(case.hub._action_waits)
            if report is None:
                for task in tasks:
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("trash_mode", ["none", "all"])
async def test_finish_commit_recovery(env, monkeypatch, deferred, trash_mode):
    from server.retention import delete_eligible_job
    from server.workspace import recovery_jobs, cleanup_workspace

    commit = AsyncSession.commit
    failed = False
    async with post_action_case(
        env,
        succeed=False,
        job_kwargs={"post_actions": [ExtractSubtitles(formats=["srt"], languages=["eng"])] if deferred else None},
    ) as case:
        async with env.factory() as session:
            library = (await session.scalars(select(Library))).one()
            library.trash_mode = trash_mode
            await session.commit()
        output = materialize_offer(case.hub, case.offer)

        async def failed_commit(session):
            nonlocal failed
            current = await session.get(Job, case.jid)
            if not failed and current is not None and current.status == JobStatus.SUCCEEDED:
                failed = True
                raise RuntimeError("finish commit failed")
            await commit(session)

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", failed_commit)
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(
                    case.hub._handle_finished(
                        case.hub.entries["node-a"],
                        finished(
                            case.jid,
                            case.offer["attempt_id"],
                            case.offer["log_path"],
                            "succeeded",
                            output_path=case.offer["output_path"],
                            output_probe=case.probe.model_copy(update={"file_path": case.offer["output_path"]}),
                        ),
                    ),
                    5,
                )
        assert failed
        case.conn.disconnect()
        await asyncio.wait_for(case.task, 5)
        async with env.factory() as session:
            row = await session.get(Job, case.jid)
            journal = (await session.scalars(select(FinalizationJournal))).one()
            assert row.status == JobStatus.FINALIZING
            event = await session.scalar(
                select(JobEvent).where(JobEvent.attempt_id == journal.attempt_id, JobEvent.type == "publication")
            )
            assert event.payload["roots"] == [str(env.library)]
            assert not Path(journal.partial_path).exists()
            attempt = await session.get(JobAttempt, journal.attempt_id)
            assert attempt is not None
            assert (env.library / "movie.mkv").stat().st_size == attempt.output_size
            assert journal.state == ("completed" if trash_mode == "all" else "discard_completed")
            assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
            assert not await delete_eligible_job(session, case.jid)
            await session.commit()
        assert not output.exists() and not case.hub._action_waits
        assert await asyncio.wait_for(recover_incomplete(env.factory), 5) == (1, 0)
        assert await asyncio.wait_for(recover_incomplete(env.factory), 5) == (0, 0)
        assert (await job(env, case.jid)).status == JobStatus.SUCCEEDED
        assert (env.library / "movie.mkv").read_bytes() == b"output"
        assert Path(journal.trash_path).exists() == (trash_mode == "all")
        assert not Path(journal.partial_path).exists()
        await cleanup_workspace(env.factory, env.workspace)
        assert not output.exists()
        async with env.factory() as session:
            assert await session.scalar(recovery_jobs().where(Job.id == case.jid)) is None
