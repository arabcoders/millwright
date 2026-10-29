from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.client import ClientProtocol
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response
from websockets.uri import parse_uri
from common.actions import Action, ActionOutcome, ExtractSubtitles
from common.media.probe import ProbeData, ProbeStream
from common.plan.models import FFmpegPlan, VideoTarget
from common.protocol import (
    ActionTask,
    ActiveAttempt,
    AttemptMissing,
    Cancel,
    Enrolled,
    Error,
    Finished,
    JobOffer,
    NodeCapabilities,
    OfferedJob,
    SetLimits,
    Welcome,
    WorkspaceChallenge,
    parse_message,
)
from common.enums import ExecutionResult
from common.process import ChildNotReapedError, stop_child
from common.workspace import workspace_lock
from node import agent as agent_module
from node import actions as actions_module
from node.actions import ActionsSpec
from node.agent import Agent, AgentConfig, safe_endpoint
from node.executor import JobResult
from node.repair import PreparedInput
from tests.helpers import make_test_temp_dir

FAKE_FFMPEG = Path(__file__).parent / "fakes" / "ffmpeg.py"
FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"


@dataclass
class AgentEnv:
    workspace: Path


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> AgentEnv:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    FAKE_FFMPEG.chmod(0o755)
    FAKE_FFPROBE.chmod(0o755)
    workspace = make_test_temp_dir("agent-workspace")
    (workspace / "logs").mkdir()
    return AgentEnv(workspace=workspace)


class FakeHubConn:
    def __init__(self, incoming: list[dict | None] | None = None) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[dict | None] = asyncio.Queue()
        for msg in incoming or []:
            self.incoming.put_nowait(msg)
        self.closed = False
        self.activity = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        self.activity.set()

    async def recv_json(self) -> dict | None:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True

    def push(self, msg: dict) -> None:
        self.incoming.put_nowait(msg)

    def disconnect(self) -> None:
        self.incoming.put_nowait(None)

    def sent_of(self, msg_type: str) -> list[dict]:
        return [msg for msg in self.sent if msg.get("type") == msg_type]


class FailingHubConn(FakeHubConn):
    def __init__(self, incoming: list[dict | None] | None = None) -> None:
        super().__init__(incoming)
        self.send_failed = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        if data.get("type") == "finished":
            self.send_failed.set()
            raise OSError("send failed")
        await super().send_json(data)


def make_config(env: AgentEnv, **overrides) -> AgentConfig:
    fields: dict = {
        "node_id": None,
        "token": "enroll-secret",
        "enrollment_token": "enroll-secret",
        "credentials_path": env.workspace / "node-credentials.json",
        "server_url": "ws://hub/api/hub",
        "workspace_dir": str(env.workspace),
        "min_free_gb": 1,
        "ffmpeg_bin": str(FAKE_FFMPEG),
        "ffprobe_bin": str(FAKE_FFPROBE),
        "capabilities": NodeCapabilities(ffmpeg_version="7.1", encoders=["libx265"]),
    }
    fields.update(overrides)
    return AgentConfig(**fields)


def probe_data(src: Path) -> ProbeData:
    return ProbeData(
        file_path=src.name,
        duration_s=10.0,
        size_bytes=1000,
        streams=[
            ProbeStream(index=0, codec="h264", codec_type="video", bit_rate=400_000),
            ProbeStream(index=1, codec="ac3", codec_type="audio", language="eng", channels=6),
        ],
    )


def offer(src: Path, dst: Path, *, job_id: int = 1, **overrides) -> OfferedJob:
    fields: dict = {
        "job_id": job_id,
        "min_free_gb": 1,
        "attempt_id": 1,
        "source_path": src.name,
        "output_path": dst.name,
        "log_path": f"logs/{job_id}.log",
        "plan": FFmpegPlan(video=VideoTarget(encoder="libx265")),
        "probe": probe_data(src),
    }
    fields.update(overrides)
    return OfferedJob(**fields)


def verify_sidecar(env: AgentEnv, duration: str = "10.0") -> None:
    (env.workspace / "1.partial.probe.json").write_text(
        json.dumps({"streams": [], "format": {"duration": duration}}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("limits", "expected"),
    [((50.0, 1.05, 90.0), (50.0, 1.05, 90.0)), ((1.0, 2.0, 900.0), (10.0, 1.25, 600.0))],
)
def test_spec_combines_limits(
    env: AgentEnv, limits: tuple[float, float, float], expected: tuple[float, float, float]
) -> None:
    agent = Agent(make_config(env, min_free_gb=limits[0], hard_size_ceiling=limits[1], stall_timeout_s=limits[2]))
    spec = agent._build_spec(offer(env.workspace / "source.mkv", env.workspace / "output.mkv", min_free_gb=10))

    assert (spec.min_free_gb, spec.hard_size_ceiling, spec.stall_timeout_s) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stop", [False, True])
async def test_stop_blocking_close(env: AgentEnv, cancel_stop: bool) -> None:
    started, cleaned, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    class BlockingConn(FakeHubConn):
        async def close(self) -> None:
            closing.set()
            await asyncio.wait_for(release.wait(), 2)

    async def run() -> None:
        try:
            started.set()
            await asyncio.wait_for(release.wait(), 2)
        finally:
            cleaned.set()

    agent = Agent(make_config(env))
    agent._conn = BlockingConn()
    running = asyncio.create_task(run())
    agent._run_task = running
    await asyncio.wait_for(started.wait(), 2)
    stopping = asyncio.create_task(agent.aclose())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        assert cleaned.is_set() and running.done()
        if cancel_stop:
            stopping.cancel()
    finally:
        release.set()
        if cancel_stop:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopping, 2)
        else:
            await asyncio.wait_for(stopping, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["hello", "job", "action"])
async def test_close_shares_drain(env: AgentEnv, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    started, draining, release_work, closing, release_close = (asyncio.Event() for _ in range(5))

    class ClosingConn(FakeHubConn):
        async def close(self) -> None:
            closing.set()
            await release_close.wait()
            self.closed = True

    async def execute(spec, *_args, prepared=None):
        started.set()
        try:
            await release_work.wait()
        except asyncio.CancelledError:
            draining.set()
            await release_work.wait()
            return JobResult(job_id=spec.job_id, status=ExecutionResult.CANCELLED, argv=[], stream_log=[])
        pytest.fail("shutdown must cancel execution")

    async def actions(*_args):
        started.set()
        try:
            await release_work.wait()
        except asyncio.CancelledError:
            draining.set()
            await release_work.wait()
            raise
        pytest.fail("shutdown must cancel actions")

    monkeypatch.setattr(agent_module, "execute", execute)
    monkeypatch.setattr(agent_module, "run_actions", actions)
    incoming: list[dict | None] = []
    if phase != "hello":
        incoming.append(Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"))
        if phase == "job":
            incoming.append(
                JobOffer(job=offer(env.workspace / "source", env.workspace / "output")).model_dump(mode="json")
            )
        else:
            incoming.append(ActionTask(job_id=1, attempt_id=1, path="output.mkv").model_dump(mode="json"))
    conn = ClosingConn(incoming)

    async def connect(_url):
        return conn

    agent = Agent(make_config(env), connect=connect)
    handlers = set(logging.getLogger().handlers)
    running = asyncio.create_task(agent.run())
    closers: list[asyncio.Task] = []
    try:
        await asyncio.wait_for(conn.activity.wait() if phase == "hello" else started.wait(), 2)
        closers.append(asyncio.create_task(agent.aclose()))
        await asyncio.wait_for(closing.wait() if phase == "hello" else draining.wait(), 2)
        closers.append(asyncio.create_task(agent.aclose()))
        for caller in (closers[0], closers[0], closers[1]):
            caller.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not any(task.done() for task in closers)
        release_work.set()
        await asyncio.wait_for(closing.wait(), 2)
        assert not any(task.done() for task in closers)
        release_close.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*closers, return_exceptions=True), 2)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        await asyncio.wait_for(agent.aclose(), 2)
        assert running.done() and conn.closed
        assert not agent._jobs and not agent._action_tasks and agent._active_transcodes == 0
        assert not conn.sent_of("finished") and not conn.sent_of("action_report")
        assert set(logging.getLogger().handlers) == handlers
    finally:
        release_work.set()
        release_close.set()
        conn.disconnect()
        if not running.done():
            running.cancel()
        await asyncio.wait_for(asyncio.gather(running, *closers, agent.aclose(), return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_cancel_before_start(env: AgentEnv) -> None:
    agent = Agent(make_config(env))
    job = offer(env.workspace / "source", env.workspace / "output")
    conn = FakeHubConn(
        [
            JobOffer(job=job).model_dump(mode="json"),
            Cancel(job_id=job.job_id, attempt_id=job.attempt_id, log_path=job.log_path).model_dump(mode="json"),
            None,
        ]
    )
    await asyncio.wait_for(agent._receive_loop(conn), 2)
    await asyncio.wait_for(asyncio.gather(*list(agent._jobs.values())), 2)
    message = agent._outbox.get_nowait()
    assert message["type"] == "finished" and message["result"] == "cancelled"
    assert agent._outbox.empty() and not agent._jobs and agent._active_transcodes == 0


@pytest.mark.asyncio
async def test_close_failure(env: AgentEnv) -> None:
    class FailingConn(FakeHubConn):
        async def close(self) -> None:
            raise OSError("close failed")

    agent = Agent(make_config(env))
    agent._conn = FailingConn()
    with pytest.raises(OSError):
        await asyncio.wait_for(agent.aclose(), 2)


@pytest.mark.asyncio
async def test_disconnect_retains_job(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(spec, *_args, prepared=None):
        started.set()
        try:
            await asyncio.wait_for(release.wait(), 3)
        except asyncio.CancelledError:
            return JobResult(job_id=spec.job_id, status=ExecutionResult.CANCELLED, argv=[], stream_log=[])
        pytest.fail("execution must be cancelled")

    monkeypatch.setattr("node.agent.execute", execute)
    job = offer(env.workspace / "source", env.workspace / "output")
    conn = FakeHubConn(
        [Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"), JobOffer(job=job).model_dump(mode="json")]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    replacement = None
    replacement_session = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        conn.disconnect()
        await asyncio.wait_for(session, 3)
        assert not conn.sent_of("finished")
        assert agent._jobs and agent._active_transcodes == 1
        replacement = FakeHubConn(
            [
                Welcome(
                    timezone="UTC", active_attempts=[ActiveAttempt(job_id=job.job_id, attempt_id=job.attempt_id)]
                ).model_dump(mode="json")
            ]
        )
        replacement_session = asyncio.create_task(agent._session(replacement))
        await wait_until(lambda: replacement.sent_of("hello"), replacement.activity)
        assert replacement.sent_of("hello")[0]["active_attempts"] == [
            {"job_id": job.job_id, "attempt_id": job.attempt_id}
        ]
        replacement.push(
            Cancel(job_id=job.job_id, attempt_id=job.attempt_id, log_path="earlier-stage.log").model_dump(mode="json")
        )
        await wait_until(lambda: replacement.sent_of("attempt_missing"), replacement.activity)
        assert not agent._jobs[(job.job_id, job.attempt_id)].cancelling()
        assert agent._active_transcodes == 1
        await agent.aclose()
        assert not agent._jobs and agent._active_transcodes == 0
    finally:
        release.set()
        conn.disconnect()
        if replacement is not None:
            replacement.disconnect()
        await asyncio.wait_for(agent.aclose(), 3)
        tasks = [session] + ([replacement_session] if replacement_session is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pre", "job", "action"])
@pytest.mark.parametrize(("authorized", "unreaped"), [(False, False), (True, False), (False, True)])
async def test_welcome_authorizes_work(
    env: AgentEnv, monkeypatch: pytest.MonkeyPatch, phase: str, authorized: bool, unreaped: bool
) -> None:
    started, draining, release, welcomed = (asyncio.Event() for _ in range(4))
    attempt = env.workspace / "attempt"
    attempt.mkdir()
    job = offer(
        attempt / "source.mkv",
        attempt / "output.mkv",
        source_path="attempt/source.mkv",
        output_path="attempt/output.mkv",
        log_path="attempt/ffmpeg.log",
        probe=probe_data(attempt / "source.mkv").model_copy(update={"file_path": "attempt/source.mkv"}),
    )
    action = ActionTask(
        job_id=job.job_id,
        attempt_id=job.attempt_id,
        path=job.output_path,
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )

    async def work() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            draining.set()
            await release.wait()
            if unreaped:
                raise ChildNotReapedError("child exit unconfirmed")

    async def execute(spec, *_args, prepared=None):
        await work()
        return JobResult(
            job_id=spec.job_id,
            status=ExecutionResult.SUCCEEDED,
            argv=[],
            stream_log=[],
            output_path=spec.dst,
            output_probe=job.probe.model_copy(update={"file_path": spec.dst}),
        )

    async def actions(*_args):
        await work()
        return [ActionOutcome(kind="extract_subtitles", ok=True)]

    async def heartbeat(_interval):
        welcomed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent_module, "execute", execute)
    monkeypatch.setattr(agent_module, "run_actions", actions)
    agent = Agent(make_config(env))
    monkeypatch.setattr(agent, "_heartbeat_loop", heartbeat)
    if phase == "pre":

        async def pre_actions(*_args):
            await work()
            return []

        monkeypatch.setattr(agent, "_run_pre_actions", pre_actions)
    key = (job.job_id, job.attempt_id)
    running = asyncio.create_task(agent._run_job(job) if phase != "action" else agent._run_action_task(action))
    owners = agent._jobs if phase != "action" else agent._action_tasks
    owners[key] = running
    conn = FakeHubConn(
        [
            Welcome(
                timezone="UTC",
                active_attempts=[ActiveAttempt(job_id=job.job_id, attempt_id=job.attempt_id)] if authorized else [],
            ).model_dump(mode="json")
        ]
    )
    session = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        agent._disconnecting = True
        timer = asyncio.get_running_loop().call_later(30, agent._expire)
        agent._reconnect_timer = timer
        session = asyncio.create_task(agent._session(conn))
        if authorized:
            await asyncio.wait_for(welcomed.wait(), 2)
            assert not running.cancelling() and not draining.is_set()
            assert agent._welcomed and timer.cancelled() and agent._reconnect_timer is None
        else:
            await asyncio.wait_for(draining.wait(), 2)
            assert not running.done() and not welcomed.is_set() and not agent._welcomed
            assert agent._disconnecting and agent._reconnect_timer is timer and not timer.cancelled()
        assert conn.sent_of("hello")[0]["active_attempts"] == [{"job_id": job.job_id, "attempt_id": job.attempt_id}]
        with pytest.raises(BlockingIOError), workspace_lock(attempt, exclusive=True):
            pytest.fail("attempt unlocked before execution drain")
        release.set()
        if unreaped:
            with pytest.raises(ChildNotReapedError):
                await asyncio.wait_for(running, 2)
            with pytest.raises(ChildNotReapedError):
                await asyncio.wait_for(session, 2)
            assert conn.closed and not agent._welcomed and not welcomed.is_set()
            assert agent._disconnecting and agent._reconnect_timer is timer and not timer.cancelled()
            assert not conn.sent_of("finished") and not conn.sent_of("action_report")
            return
        await asyncio.wait_for(running, 2)
        await asyncio.wait_for(welcomed.wait(), 2)
        report_type = "finished" if phase != "action" else "action_report"
        await wait_until(lambda: conn.sent_of(report_type), conn.activity)
        report = conn.sent_of(report_type)[0]
        if phase != "action":
            assert report["result"] == ("succeeded" if authorized else "cancelled")
            if not authorized:
                assert report["output_path"] is None and report["output_probe"] is None
                assert report["error"]
        else:
            assert report["results"][0]["ok"] is authorized
            if not authorized:
                assert report["results"][0]["error"]
        with workspace_lock(attempt, exclusive=True):
            pass
    finally:
        release.set()
        conn.disconnect()
        await asyncio.wait_for(agent.aclose(), 2)
        await asyncio.wait_for(
            asyncio.gather(running, *([session] if session is not None else []), return_exceptions=True), 2
        )


@pytest.mark.asyncio
async def test_hello_includes_actions(env: AgentEnv) -> None:
    release = asyncio.Event()
    active = asyncio.create_task(release.wait())
    action_only = asyncio.create_task(release.wait())

    async def complete() -> None:
        return

    finished = asyncio.create_task(complete())
    agent = Agent(make_config(env))
    agent._jobs[(1, 2)] = active
    agent._jobs[(3, 4)] = finished
    agent._action_tasks[(1, 2)] = active
    agent._action_tasks[(5, 6)] = action_only
    attempts = [ActiveAttempt(job_id=1, attempt_id=2), ActiveAttempt(job_id=5, attempt_id=6)]
    conn = FakeHubConn([Welcome(timezone="UTC", active_attempts=attempts).model_dump(mode="json"), None])
    try:
        await asyncio.wait_for(finished, 2)
        assert await asyncio.wait_for(agent._session(conn), 2)
        assert conn.sent_of("hello")[0]["active_attempts"] == [attempt.model_dump() for attempt in attempts]
        assert not active.cancelling()
    finally:
        release.set()
        await asyncio.wait_for(agent.aclose(), 2)
        await asyncio.wait_for(asyncio.gather(active, action_only, finished, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["job", "action"])
@pytest.mark.parametrize("missing", [False, True])
async def test_workspace_open_fails(env: AgentEnv, monkeypatch: pytest.MonkeyPatch, phase: str, missing: bool) -> None:
    attempt = env.workspace / "attempt"
    if not missing:
        attempt.mkdir()

    async def execute(*_args, prepared=None):
        pytest.fail("workspace open failure must not execute work")

    monkeypatch.setattr(agent_module, "execute", execute)
    monkeypatch.setattr(agent_module, "run_actions", execute)
    agent = Agent(make_config(env))
    with contextlib.ExitStack() as scope:
        if not missing:
            scope.enter_context(workspace_lock(attempt, exclusive=True))
        if phase == "job":
            await asyncio.wait_for(
                agent._run_job(
                    offer(env.workspace / "source", env.workspace / "output", log_path="attempt/ffmpeg.log")
                ),
                2,
            )
        else:
            await asyncio.wait_for(
                agent._run_action_task(
                    ActionTask(
                        job_id=1,
                        attempt_id=1,
                        path="attempt/output.mkv",
                        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
                    )
                ),
                2,
            )
        report = agent._outbox.get_nowait()
        assert report["result"] == "failed" if phase == "job" else not report["results"][0]["ok"]
        assert not agent._started_jobs and agent._active_transcodes == 0
        assert not (attempt / "job.ndjson").exists()
    if missing:
        assert not attempt.exists()


@pytest.mark.asyncio
async def test_cancel_missing_attempt(env: AgentEnv) -> None:
    conn = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            Cancel(job_id=7, attempt_id=12, reason="cancelled", log_path="jobs/7/attempts/1/ffmpeg.log").model_dump(
                mode="json"
            ),
        ]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    try:
        await wait_until(lambda: conn.sent_of("attempt_missing"), conn.activity)
        assert conn.sent_of("hello")[0]["active_attempts"] == []
        assert parse_message(conn.sent_of("attempt_missing")[0]) == AttemptMissing(
            job_id=7, attempt_id=12, log_path="jobs/7/attempts/1/ffmpeg.log"
        )
    finally:
        conn.disconnect()
        await asyncio.wait_for(agent.aclose(), 3)
        await asyncio.wait_for(asyncio.gather(session, return_exceptions=True), 3)


@pytest.mark.asyncio
async def test_stop_drains_actions(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    job_started, action_started, draining, recancelled, release_job, release_action, action_stopped = (
        asyncio.Event() for _ in range(7)
    )

    async def execute(spec, *_args, prepared=None):
        job_started.set()
        try:
            await release_job.wait()
        except asyncio.CancelledError:
            draining.set()
            drain = asyncio.create_task(release_job.wait())
            try:
                while not drain.done():
                    try:
                        await asyncio.shield(drain)
                    except asyncio.CancelledError:
                        recancelled.set()
            finally:
                await drain
            return JobResult(job_id=spec.job_id, status=ExecutionResult.CANCELLED, argv=[], stream_log=[])
        pytest.fail("disconnect must cancel execution")

    async def actions(*_args):
        action_started.set()
        try:
            await release_action.wait()
            return []
        finally:
            action_stopped.set()

    monkeypatch.setattr(agent_module, "execute", execute)
    monkeypatch.setattr(agent_module, "run_actions", actions)
    job = offer(env.workspace / "source", env.workspace / "output")
    action = ActionTask(
        job_id=2,
        attempt_id=2,
        path="other-output.mkv",
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )
    conn = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            JobOffer(job=job).model_dump(mode="json"),
            action.model_dump(mode="json"),
        ]
    )
    agent = Agent(make_config(env))
    existing_tasks = asyncio.all_tasks()
    existing_handlers = set(logging.getLogger().handlers)
    session = asyncio.create_task(agent._session(conn))
    agent._run_task = session
    stopping = None
    tasks: list[asyncio.Task] = [session]
    try:
        await asyncio.wait_for(job_started.wait(), 2)
        await asyncio.wait_for(action_started.wait(), 2)
        tasks += list(asyncio.all_tasks() - existing_tasks - {session})
        conn.disconnect()
        # A disconnect keeps active tasks for reconnect. Full close performs the drain.
        stopping = asyncio.create_task(agent.aclose())
        tasks.append(stopping)
        await asyncio.wait_for(draining.wait(), 2)
        assert not stopping.done() and action_stopped.is_set()
        release_job.set()
        await asyncio.wait_for(stopping, 2)
        assert action_stopped.is_set(), "shutdown returned while a post-action task was still using workspace files"
        assert not agent._jobs and not agent._action_tasks and agent._active_transcodes == 0
        assert not conn.sent_of("finished") and not conn.sent_of("action_report")
        assert all(task.done() for task in tasks)
        assert set(logging.getLogger().handlers) == existing_handlers
    finally:
        release_job.set()
        release_action.set()
        conn.disconnect()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        for handler in set(logging.getLogger().handlers) - existing_handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_disconnect_drains_termination(env: AgentEnv, monkeypatch: pytest.MonkeyPatch, stop: bool) -> None:
    started, terminating, draining, release = (asyncio.Event() for _ in range(4))
    src, dst = env.workspace / "source.mkv", env.workspace / "output.mkv"
    src.write_bytes(b"x" * 1000)

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            terminating.set()

        def send_signal(self, _signal) -> None:
            pytest.fail("termination must finish before the grace period expires")

        async def wait(self) -> int:
            await release.wait()
            self.returncode = -15
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        dst.write_bytes(b"child output")
        started.set()
        return proc

    wait_for = asyncio.wait_for

    async def wait_draining(awaitable, timeout):
        if timeout == agent_module._JOB_DRAIN_TIMEOUT_S:
            draining.set()
        return await wait_for(awaitable, timeout)

    monkeypatch.setattr("node.process.create_tool", spawn)
    monkeypatch.setattr(asyncio, "wait_for", wait_draining)
    job = offer(src, dst, min_free_gb=0)
    conn = FakeHubConn(
        [Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"), JobOffer(job=job).model_dump(mode="json")]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    agent._run_task = session
    stopping = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        conn.push(Cancel(job_id=job.job_id, attempt_id=job.attempt_id, log_path=job.log_path).model_dump(mode="json"))
        await asyncio.wait_for(terminating.wait(), 2)
        if stop:
            stopping = asyncio.create_task(agent.aclose())
        else:
            conn.disconnect()
            stopping = asyncio.create_task(agent.aclose())
        await asyncio.wait_for(draining.wait(), 2)
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not session.done() and proc.returncode is None
        assert dst.read_bytes() == b"child output"
        assert agent._jobs and agent._active_transcodes == 1
        release.set()
        if stop:
            assert stopping is not None
            await asyncio.wait_for(stopping, 2)
        else:
            assert stopping is not None
            await asyncio.wait_for(stopping, 2)
        assert proc.returncode == -15 and not dst.exists()
        assert not agent._jobs and agent._active_transcodes == 0
        assert src.read_bytes() == b"x" * 1000
        assert not conn.sent_of("finished")
    finally:
        release.set()
        conn.disconnect()
        tasks = [session, *([stopping] if stopping is not None else [])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_shutdown_drains_offer(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    started, terminating, release = (asyncio.Event() for _ in range(3))
    processes = []
    src = env.workspace / "source.mkv"
    src.write_bytes(b"x" * 1000)

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            terminating.set()

        def send_signal(self, _signal) -> None:
            pytest.fail("termination must finish before the grace period expires")

        async def wait(self) -> int:
            await release.wait()
            self.returncode = -15
            return self.returncode

    async def spawn(*args, **_kwargs):
        Path(args[-1]).write_bytes(b"child output")
        proc = Process()
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr("node.process.create_tool", spawn)
    first = offer(src, env.workspace / "first.mkv", min_free_gb=0)
    late = offer(src, env.workspace / "late.mkv", job_id=2, min_free_gb=0)
    conn = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            JobOffer(job=first).model_dump(mode="json"),
        ]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    agent._run_task = session
    stopping = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        stopping = asyncio.create_task(agent.aclose())
        await asyncio.wait_for(terminating.wait(), 2)
        conn.push(JobOffer(job=late).model_dump(mode="json"))
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not stopping.done() and agent._active_transcodes == 1
        release.set()
        await asyncio.wait_for(stopping, 2)
        assert not agent._jobs and agent._active_transcodes == 0, (
            "shutdown returned with a late-offer job still owning a transcode slot: "
            f"active={agent._active_transcodes}, child exits={[proc.returncode for proc in processes]}, "
            f"late output exists={(env.workspace / 'late.mkv').exists()}"
        )
        assert all(proc.returncode is not None for proc in processes)
        assert not (env.workspace / "late.mkv").exists()
        assert src.read_bytes() == b"x" * 1000
        assert not conn.sent_of("finished")
    finally:
        release.set()
        conn.disconnect()
        tasks = [session, *agent._jobs.values(), *([stopping] if stopping is not None else [])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_unfinished_blocks_queue(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    started, fail_first, closing, release_close = (asyncio.Event() for _ in range(4))
    processes = []
    src = env.workspace / "source.mkv"
    src.write_bytes(b"x" * 1000)

    class Output(asyncio.StreamReader):
        async def readline(self) -> bytes:
            await fail_first.wait()
            raise TimeoutError

    class Process:
        def __init__(self, *, unreaped: bool) -> None:
            self.unreaped = unreaped
            self.returncode: int | None = None
            self.stdout = Output() if unreaped else asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self.killed = False

        def terminate(self) -> None:
            pass

        def send_signal(self, _signal) -> None:
            self.killed = True

        async def wait(self) -> int:
            if self.unreaped:
                raise TimeoutError
            self.returncode = -15
            return self.returncode

    class BlockingConn(FakeHubConn):
        async def close(self) -> None:
            closing.set()
            await release_close.wait()
            self.closed = True
            self.disconnect()

    async def spawn(*args, **_kwargs):
        Path(args[-1]).write_bytes(b"child output")
        proc = Process(unreaped=not processes)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr("node.process.create_tool", spawn)
    first = offer(src, env.workspace / "first.mkv", min_free_gb=0)
    queued = offer(src, env.workspace / "queued.mkv", job_id=2, min_free_gb=0)
    conn = BlockingConn(
        [
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            JobOffer(job=first).model_dump(mode="json"),
            JobOffer(job=queued).model_dump(mode="json"),
        ]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (queued.job_id, queued.attempt_id) in agent._jobs
        assert len(processes) == 1 and agent._active_transcodes == 1
        fail_first.set()
        await asyncio.wait_for(closing.wait(), 2)
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert agent._disconnecting and processes[0].killed and processes[0].returncode is None
        assert (env.workspace / "first.mkv").read_bytes() == b"child output"
        assert src.read_bytes() == b"x" * 1000
        assert len(processes) == 1, (
            "queued encoding started after child exit was unconfirmed and disconnecting was set: "
            f"child exits={[proc.returncode for proc in processes]}, active={agent._active_transcodes}"
        )
        assert not (env.workspace / "queued.mkv").exists()
        release_close.set()
        await asyncio.wait_for(session, 2)
        await asyncio.wait_for(agent.aclose(), 2)
        assert not agent._jobs and agent._active_transcodes == 0
        assert not conn.sent_of("finished")
    finally:
        fail_first.set()
        release_close.set()
        conn.disconnect()
        tasks = [session, *agent._jobs.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


SUBTITLE_SIDECAR_STREAMS = [
    {"index": 0, "codec_name": "h264", "codec_type": "video", "disposition": {}, "tags": {}},
    {"index": 1, "codec_name": "subrip", "codec_type": "subtitle", "disposition": {}, "tags": {"language": "eng"}},
    {"index": 2, "codec_name": "subrip", "codec_type": "subtitle", "disposition": {}, "tags": {"language": "fre"}},
]


def verify_sidecar_with_subs(env: AgentEnv) -> None:
    (env.workspace / "1.partial.probe.json").write_text(
        json.dumps({"streams": SUBTITLE_SIDECAR_STREAMS, "format": {"duration": "10.0"}}), encoding="utf-8"
    )


async def wait_until(predicate: Callable[[], object], wake: asyncio.Event, timeout_s: float = 5.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():
            await wake.wait()
            wake.clear()


@asynccontextmanager
async def driven_session(agent: Agent, conn: FakeHubConn) -> AsyncGenerator[asyncio.Task]:
    task = asyncio.create_task(agent._session(conn))
    try:
        yield task
    finally:
        conn.disconnect()
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_session_runs_offer(env: AgentEnv) -> None:
    src = env.workspace / "movie.avi"
    src.write_bytes(b"x" * 1000)
    os.utime(src, (1_234_567_890, 1_234_567_890))
    dst = env.workspace / "movie.mkv"
    verify_sidecar(env)
    conn = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[], heartbeat_s=15.0).model_dump(mode="json"),
            JobOffer(job=offer(src, dst)).model_dump(mode="json"),
        ]
    )
    agent = Agent(make_config(env))
    async with driven_session(agent, conn) as session:
        await wait_until(lambda: conn.sent_of("finished"), conn.activity)
    assert session.result() is True
    assert conn.sent[0]["type"] == "hello"
    assert conn.sent[0]["node_id"] is None
    assert conn.sent[0]["capabilities"]["max_transcodes"] == 1
    progress = conn.sent_of("progress")
    finished = conn.sent_of("finished")
    assert len(progress) >= 1
    assert len(finished) == 1
    assert conn.sent.index(progress[0]) < conn.sent.index(finished[0])
    assert finished[0]["result"] == "succeeded"
    assert finished[0]["output_size"] == 100
    assert finished[0]["source_size"] == 1000
    assert dst.is_file() and dst.stat().st_size == 100
    assert not (env.workspace / "1.partial").exists()


@pytest.mark.asyncio
async def test_heartbeat_flows(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    waiting, tick = asyncio.Event(), asyncio.Event()

    async def sleep(interval: float) -> None:
        assert interval == 15.0
        waiting.set()
        await tick.wait()
        tick.clear()

    monkeypatch.setattr("node.agent.asyncio.sleep", sleep)
    conn = FakeHubConn([Welcome(timezone="UTC", active_attempts=[], heartbeat_s=15.0).model_dump(mode="json")])
    agent = Agent(make_config(env))
    async with driven_session(agent, conn) as session:
        await asyncio.wait_for(waiting.wait(), 5)
        tick.set()
        await wait_until(lambda: conn.sent_of("heartbeat"), conn.activity)
    assert session.result() is True
    assert conn.sent_of("heartbeat")[0]["type"] == "heartbeat"
    heartbeat = conn.sent_of("heartbeat")[0]
    assert heartbeat["cpu_percent"] is None or 0 <= heartbeat["cpu_percent"] <= 100
    assert heartbeat["memory_percent"] is None or 0 <= heartbeat["memory_percent"] <= 100
    assert isinstance(heartbeat["gpus"], list)


@pytest.mark.asyncio
async def test_enrollment_persists_pair(env: AgentEnv) -> None:
    conn = FakeHubConn(
        [
            Enrolled(node_id="node-a", token="node-token").model_dump(mode="json"),
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            None,
        ]
    )
    config = make_config(env)
    agent = Agent(config)
    assert await asyncio.wait_for(agent._session(conn), timeout=5)
    assert conn.sent[0]["node_id"] is None
    assert conn.sent[0]["token"] == "enroll-secret"
    assert json.loads((env.workspace / "node-credentials.json").read_text()) == {
        "node_id": "node-a",
        "token": "node-token",
    }


@pytest.mark.asyncio
async def test_auth_rejection_enrolls(env: AgentEnv) -> None:
    conn = FakeHubConn([Error(code="auth", message="rejected").model_dump(mode="json")])
    config = make_config(env, node_id="node-a", token="old-token")
    agent = Agent(config)
    assert await agent._session(conn) is False
    assert config.node_id == "node-a" and config.token == "old-token"
    assert agent._enroll is True

    next_conn = FakeHubConn(
        [
            Enrolled(node_id="node-new", token="new-token").model_dump(mode="json"),
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            None,
        ]
    )
    assert await asyncio.wait_for(agent._session(next_conn), 5)
    assert next_conn.sent[0]["node_id"] is None
    assert next_conn.sent[0]["token"] == "enroll-secret"
    assert next_conn.sent[1] == {"v": 1, "type": "enrollment_saved", "node_id": "node-new"}
    assert json.loads((env.workspace / "node-credentials.json").read_text()) == {
        "node_id": "node-new",
        "token": "new-token",
    }

    reconnect = FakeHubConn([Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"), None])
    assert await asyncio.wait_for(agent._session(reconnect), 5)
    assert reconnect.sent[0]["node_id"] == "node-new"
    assert reconnect.sent[0]["token"] == "new-token"


@pytest.mark.asyncio
async def test_uncommitted_credentials_reenroll(env: AgentEnv) -> None:
    config = make_config(env)
    agent = Agent(config)
    issued = FakeHubConn([Enrolled(node_id="node-issued", token="issued-token").model_dump(mode="json"), None])
    assert await agent._session(issued) is False
    assert (config.node_id, config.token) == ("node-issued", "issued-token")

    rejected = FakeHubConn([Error(code="auth", message="not committed").model_dump(mode="json")])
    assert await agent._session(rejected) is False
    assert agent._enroll is True

    replacement = FakeHubConn(
        [
            Enrolled(node_id="node-replaced", token="replacement-token").model_dump(mode="json"),
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            None,
        ]
    )
    assert await asyncio.wait_for(agent._session(replacement), timeout=5)
    assert json.loads((env.workspace / "node-credentials.json").read_text()) == {
        "node_id": "node-replaced",
        "token": "replacement-token",
    }


@pytest.mark.asyncio
async def test_network_failure_keeps_identity(env: AgentEnv) -> None:
    conn = FakeHubConn([None])
    config = make_config(env, node_id="node-a", token="old-token")
    assert await Agent(config)._session(conn) is False
    assert config.node_id == "node-a" and config.token == "old-token"


@pytest.mark.asyncio
async def test_workspace_failure_keeps_credentials(env: AgentEnv) -> None:
    path = env.workspace / "node-credentials.json"
    path.write_text('{"node_id":"node-old","token":"old-token"}\n', encoding="utf-8")
    conn = FakeHubConn(
        [
            WorkspaceChallenge(challenge_id="c", challenge_path="challenge", response_path="response").model_dump(
                mode="json"
            ),
        ]
    )
    config = make_config(env, node_id="node-old", token="old-token")
    assert await Agent(config)._session(conn) is False
    assert path.read_text(encoding="utf-8") == '{"node_id":"node-old","token":"old-token"}\n'


@pytest.mark.parametrize("failure", ["missing", "permission"])
def test_workspace_failure_diagnostics(env, monkeypatch, caplog, failure):
    config = make_config(env, node_id="node-a", token="node-secret")
    agent = Agent(config)
    challenge = WorkspaceChallenge(challenge_id="c", challenge_path="challenge", response_path="response")
    if failure == "permission":

        def denied(_path):
            raise PermissionError("Access denied for node-secret and enroll-secret")

        monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.chdir(env.workspace.parent)
    config.workspace_dir = env.workspace.name

    response = agent._workspace_response(challenge)

    assert not response["workspace_ready"]
    assert str(env.workspace.resolve()) in caplog.text
    assert str(env.workspace / "challenge") in caplog.text
    failed = next(record for record in caplog.records if getattr(record, "operation", None) == "node.workspace.failed")
    assert getattr(failed, "error") == response["error"]
    assert failed.levelno == logging.WARNING
    for secret in (config.token, config.enrollment_token):
        assert secret not in caplog.text + getattr(failed, "error")
        assert secret not in response["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial", "workspace", "enrollment"])
@pytest.mark.parametrize("failure", ["missing", "unexpected", "invalid", "timeout", "auth"])
async def test_handshake_failure_reason(env, caplog, phase, failure):
    incoming = []
    if phase == "workspace":
        (env.workspace / "challenge").write_bytes(b"challenge")
        incoming.append(
            WorkspaceChallenge(challenge_id="c", challenge_path="challenge", response_path="response").model_dump(
                mode="json"
            )
        )
    elif phase == "enrollment":
        incoming.append(Enrolled(node_id="node-issued", token="issued-secret").model_dump(mode="json"))
    reply = {
        "missing": None,
        "unexpected": SetLimits(max_transcodes=1, max_healthchecks=1, max_mkv_stats=1).model_dump(mode="json"),
        "invalid": {"v": 1, "type": "welcome", "heartbeat_s": "node-secret enroll-secret issued-secret"},
        "timeout": None,
        "auth": Error(
            code="auth",
            message="Authentication revoked. node-secret enroll-secret"
            + (" issued-secret" if phase == "enrollment" else ""),
        ).model_dump(mode="json"),
    }[failure]
    incoming.append(reply)

    class DiagnosticConn(FakeHubConn):
        async def recv_json(self):
            data = await super().recv_json()
            if failure == "timeout" and self.incoming.empty():
                raise TimeoutError
            return data

    config = make_config(env, token="node-secret")
    if phase != "enrollment":
        config.node_id = "node-a"
    agent = Agent(config)
    assert await asyncio.wait_for(agent._session(DiagnosticConn(incoming)), 2) is False
    assert not agent._welcomed
    records = [record for record in caplog.records if record.name == "node.agent"]
    assert records and all(record.levelno == logging.WARNING for record in records)
    if failure == "invalid":
        assert any(getattr(record, "error", None) for record in records)
    elif failure == "unexpected":
        rejected = next(record for record in records if getattr(record, "operation", None) == "node.handshake.rejected")
        assert getattr(rejected, "outcome") == "invalid"
    if failure == "auth":
        rejected = next(record for record in records if getattr(record, "operation", None) == "node.handshake.rejected")
        assert getattr(rejected, "outcome") == "auth"
    for secret in ("node-secret", "enroll-secret", "issued-secret"):
        assert secret not in caplog.text + "".join(getattr(record, "error", "") for record in records)


@pytest.mark.asyncio
async def test_enrolled_reply_unexpected(env, caplog):
    conn = FakeHubConn([Enrolled(node_id="node-a", token="issued-secret").model_dump(mode="json")])
    agent = Agent(make_config(env, node_id="node-a"))
    assert await agent._session(conn) is False
    records = [record for record in caplog.records if record.name == "node.agent"]
    assert records and all(record.levelno == logging.WARNING for record in records)
    assert "issued-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Normal shutdown.", "Rejected old-secret node-secret enroll-secret"])
async def test_websocket_close_diagnostics(env, monkeypatch, caplog, reason):
    frame = Close(1000, reason)

    async def closed():
        raise ConnectionClosedOK(frame, frame, True)

    config = make_config(env, token="old-secret")
    ws = ClientConnection(ClientProtocol(parse_uri(config.server_url)))
    monkeypatch.setattr(ws, "recv", closed)
    conn = agent_module._WebSocketConnection(ws, config)
    config.token = "node-secret"
    forwarded: list[dict] = []
    handler = agent_module.HubLogHandler(asyncio.get_running_loop(), forwarded.append)
    logging.getLogger().addHandler(handler)
    try:
        with caplog.at_level(logging.INFO, logger="node.agent"):
            assert await conn.recv_json() is None
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    closed_record = next(
        record for record in caplog.records if getattr(record, "operation", None) == "node.connection.closed"
    )
    assert closed_record.levelno == logging.INFO
    assert str(frame.code) in closed_record.getMessage()
    assert forwarded[0]["entries"][0]["message"] == closed_record.getMessage()
    if reason == "Normal shutdown.":
        assert reason in closed_record.getMessage()
    for secret in ("old-secret", "node-secret", "enroll-secret"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_enrollment_save_diagnostics(env, monkeypatch, caplog):
    agent = Agent(make_config(env, token="node-secret"))

    def denied(*_args):
        raise PermissionError("Credentials denied for node-secret enroll-secret issued-secret")

    monkeypatch.setattr(agent, "_save_credentials", denied)
    conn = FakeHubConn([Enrolled(node_id="node-a", token="issued-secret").model_dump(mode="json")])
    assert await agent._session(conn) is False
    records = [record for record in caplog.records if record.name == "node.agent"]
    assert any(getattr(record, "error", None) for record in records)
    assert records and all(record.levelno == logging.WARNING for record in records)
    for secret in ("node-secret", "enroll-secret", "issued-secret"):
        assert secret not in caplog.text + "".join(getattr(record, "error", "") for record in records)


@pytest.mark.asyncio
async def test_error_ends(env: AgentEnv) -> None:
    conn = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            {"v": 1, "type": "error", "code": "protocol", "message": "invalid"},
        ]
    )
    agent = Agent(make_config(env))
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(agent._session(conn), timeout=5)
    assert [msg["type"] for msg in conn.sent] == ["hello"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["upgrade", "timeout", "tcp"])
async def test_connection_failure_diagnostics(env: AgentEnv, caplog: pytest.LogCaptureFixture, failure: str) -> None:
    url = "wss://proxy-user:proxy-password@example.test:8443/api/hub?mode=operator&attempt=2"
    attempted = []
    delays = []

    async def connect(value: str) -> FakeHubConn:
        attempted.append(value)
        if failure == "upgrade":
            raise InvalidStatus(Response(403, "Forbidden", Headers(), b"private-response"))
        if failure == "timeout":
            raise TimeoutError
        raise ConnectionRefusedError(f"Connection refused at {value}")

    async def stop_after_retry(seconds: float) -> None:
        delays.append(seconds)
        agent._closed = True

    agent = Agent(make_config(env, server_url=url), connect=connect, sleep=stop_after_retry)
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(task, 2)
    finally:
        await asyncio.wait_for(agent.aclose(), 2)
    assert attempted == [url]
    assert "wss://example.test:8443/api/hub?mode=operator&attempt=2" in caplog.text
    failed = [record for record in caplog.records if getattr(record, "operation", None) == "node.connect.failed"]
    assert getattr(failed[0], "error")
    if failure == "upgrade":
        assert "403" in getattr(failed[0], "error")
    assert delays == [1.0]
    for value in ("proxy-user", "proxy-password", "private-response"):
        assert value not in caplog.text + "".join(getattr(record, "error", "") for record in failed)
    assert failed and all(record.levelno == logging.WARNING for record in failed)
    assert failed[0].__dict__["endpoint"] == "wss://example.test:8443/api/hub?mode=operator&attempt=2"
    assert failed[0].__dict__["backoff_s"] == delays[0]


@pytest.mark.asyncio
async def test_unavailable_hub_retries(env: AgentEnv, caplog: pytest.LogCaptureFixture) -> None:
    attempts = []
    delays = []

    async def connect(url: str) -> FakeHubConn:
        attempts.append(url)
        raise InvalidStatus(Response(503, "Service Unavailable", Headers(), b""))

    async def retry(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 2:
            agent._closed = True

    agent = Agent(make_config(env), connect=connect, sleep=retry)
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(task, 2)
    finally:
        await asyncio.wait_for(agent.aclose(), 2)
    assert attempts == [agent._config.server_url] * 2
    assert delays == [1.0, 2.0]
    assert not [
        record for record in caplog.records if record.name == "node.agent" and record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("wss://example.test/api/hub?mode=operator", "wss://example.test/api/hub?mode=operator"),
        ("wss://user:password@example.test/api/hub?mode=operator", "wss://example.test/api/hub?mode=operator"),
        ("ws://[::1]:8000/api/hub?label=a%20b&attempt=2", "ws://[::1]:8000/api/hub?label=a%20b&attempt=2"),
        ("ws://example.test/api/hub", "ws://example.test/api/hub"),
    ],
)
def test_endpoint_keeps_query(value: str, expected: str) -> None:
    assert safe_endpoint(value) == expected


@pytest.mark.asyncio
async def test_backoff_resets(env: AgentEnv) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        wake.set()

    attempts = 0
    conns: list[FakeHubConn] = []
    wake = asyncio.Event()

    async def connect(url: str) -> FakeHubConn:
        nonlocal attempts
        attempts += 1
        if attempts in (1, 2, 3, 5):
            msg = "no hub"
            raise ConnectionError(msg)
        conn = FakeHubConn([Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"), None])
        conns.append(conn)
        return conn

    agent = Agent(make_config(env, backoff_base_s=1.0, backoff_max_s=4.0), connect=connect, sleep=fake_sleep)
    task = asyncio.create_task(agent.run())
    try:
        await wait_until(lambda: len(conns) >= 2 and len(delays) >= 5, wake)
    finally:
        await agent.aclose()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    assert delays[:5] == [1.0, 2.0, 4.0, 4.0, 1.0]
    assert conns[0].closed


@pytest.mark.asyncio
async def test_writer_fails(env: AgentEnv) -> None:
    src = env.workspace / "movie.avi"
    src.write_bytes(b"x" * 1000)
    conn = FailingHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"),
            JobOffer(job=offer(src, src)).model_dump(mode="json"),
        ]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    try:
        await asyncio.wait_for(conn.send_failed.wait(), timeout=5)
        with pytest.raises(OSError):
            await asyncio.wait_for(session, timeout=5)
    finally:
        if not session.done():
            conn.disconnect()
            await asyncio.wait_for(session, timeout=5)


@pytest.mark.asyncio
async def test_action_task_roundtrip(env: AgentEnv) -> None:
    src = env.workspace / "movie.avi"
    src.write_bytes(b"x" * 1000)
    probe = ProbeData(
        file_path=src.name,
        duration_s=10.0,
        size_bytes=1000,
        streams=[ProbeStream(index=1, codec="subrip", codec_type="subtitle", language="eng")],
    )
    task = ActionTask(
        job_id=1,
        attempt_id=1,
        path=src.name,
        probe=probe,
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )
    conn = FakeHubConn(
        [Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"), task.model_dump(mode="json")]
    )
    agent = Agent(make_config(env))
    async with driven_session(agent, conn) as session:
        await wait_until(lambda: conn.sent_of("action_report"), conn.activity)
    assert session.result() is True
    report = conn.sent_of("action_report")[0]
    assert report["job_id"] == 1
    assert report["results"] == [
        {"kind": "extract_subtitles", "ok": True, "error": None, "artifacts": ["movie.eng.srt"]}
    ]
    assert (env.workspace / "movie.eng.srt").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["", "-2"])
async def test_post_preserves_media(env: AgentEnv, stage: str) -> None:
    src = env.workspace / "jobs" / "1" / "attempts" / "2" / f"output{stage}.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"published media")
    log = src.with_name(f"ffmpeg{stage}.log")
    initial_log = "transcode log\n"
    log.write_text(initial_log, encoding="utf-8")
    agent = Agent(make_config(env))
    task = ActionTask(
        job_id=1,
        attempt_id=9,
        path=str(src.relative_to(env.workspace)),
        actions=[ExtractSubtitles(formats=["srt"], languages=[])],
    )

    await asyncio.wait_for(agent._run_action_task(task), timeout=5)

    assert src.read_bytes() == b"published media"
    assert log.read_text().startswith(initial_log)
    assert log.stat().st_size > len(initial_log)
    report = agent._outbox.get_nowait()
    assert report["results"][0]["kind"] == "extract_subtitles"
    assert report["results"][0]["ok"] is False
    assert report["results"][0]["artifacts"] == []


@pytest.mark.asyncio
async def test_actions_precede_encode(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from node.executor import _Run

    src, dst = env.workspace / "output.mkv", env.workspace / "output-2.mkv"
    src.write_bytes(b"x" * 1000)
    probe = probe_data(src)
    probe.streams.append(ProbeStream(index=2, codec="subrip", codec_type="subtitle", language="eng"))
    job = offer(
        src,
        dst,
        probe=probe,
        log_path="ffmpeg-2.log",
        min_free_gb=0,
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )

    async def ffmpeg(spec, argv, *_args):
        assert spec.src == str(src) and spec.probe == probe
        assert dst.with_suffix(".eng.srt").exists()
        assert not src.with_suffix(".eng.srt").exists()
        assert Path(argv[argv.index("-i") + 1]) == src
        assert Path(argv[-1]) == dst
        Path(argv[-1]).write_bytes(b"x" * 100)
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    async def output_probe(*_args):
        return {
            "format": {"duration": "10"},
            "streams": [
                {"index": stream.index, "codec_type": stream.codec_type, "codec_name": stream.codec}
                for stream in probe.streams
            ],
        }

    monkeypatch.setattr("node.executor._run_ffmpeg", ffmpeg)
    monkeypatch.setattr("node.executor._probe_json", output_probe)
    message = await asyncio.wait_for(Agent(make_config(env))._execute_job(job), 5)
    assert message["result"] == "succeeded"
    assert message["action_results"][0]["artifacts"] == ["output-2.eng.srt"]
    assert src.read_bytes() == b"x" * 1000 and dst.read_bytes() == b"x" * 100


@pytest.mark.asyncio
async def test_repair_precedes_extraction(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    src, dst = env.workspace / "source.mkv", env.workspace / "output.mkv"
    src.write_bytes(b"x" * 1000)
    original = probe_data(src).model_copy(update={"container": "mkv"})
    original.streams.append(ProbeStream(index=2, codec="", codec_type="subtitle", language="eng"))
    normalized = original.model_copy(deep=True)
    normalized.streams[2].codec = "subrip"
    repaired = env.workspace / "normalized.mkv"

    @asynccontextmanager
    async def normalize(source, probe, _repair_dir, **_kwargs):
        assert source == src and probe == original
        repaired.write_bytes(b"repaired")
        try:
            yield PreparedInput(path=repaired, probe=normalized)
        finally:
            repaired.unlink()

    async def extract(spec: ActionsSpec, _binary):
        assert spec.path == repaired and spec.probe == normalized and spec.output_path == dst
        assert repaired.exists()
        return [ActionOutcome(kind="extract_subtitles", ok=True, artifacts=["output.eng.srt"])]

    async def verify(_binary, _path, _log):
        assert repaired.exists()
        return {
            "format": {"duration": "10"},
            "streams": [
                {"index": stream.index, "codec_name": stream.codec, "codec_type": stream.codec_type}
                for stream in normalized.streams
            ],
        }

    monkeypatch.setattr(agent_module, "prepare_input", normalize)
    monkeypatch.setattr(agent_module, "run_actions", extract)
    monkeypatch.setattr("node.executor._probe_json", verify)
    job = offer(src, dst, probe=original, min_free_gb=0, actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])])
    result = await Agent(make_config(env, min_free_gb=0))._execute_job(job)
    assert result["result"] == "succeeded" and result["source_size"] == 1000
    assert not repaired.exists() and src.read_bytes() == b"x" * 1000


@pytest.mark.asyncio
async def test_extraction_blocks_encode(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    src, dst = env.workspace / "source.mkv", env.workspace / "output.mkv"
    src.write_bytes(b"original")
    probe = probe_data(src)
    probe.streams.append(ProbeStream(index=2, codec="subrip", codec_type="subtitle", language="eng"))
    job = offer(src, dst, probe=probe, actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])])
    agent = Agent(make_config(env))

    async def failed_extraction(*_args):
        return [ActionOutcome(kind="extract_subtitles", ok=False, error="ENOSPC")]

    async def encode(*_args):
        pytest.fail("encoding must not run after a failed prerequisite")

    monkeypatch.setattr(agent, "_run_pre_actions", failed_extraction)
    monkeypatch.setattr("node.agent.execute", encode)
    message = await asyncio.wait_for(agent._execute_job(job), 2)
    assert message["result"] == "failed"
    assert message["action_results"][0]["error"] == "ENOSPC"
    assert src.read_bytes() == b"original" and not dst.exists()


def test_finished_hides_argv(env: AgentEnv) -> None:
    agent = Agent(make_config(env))
    result = JobResult(
        job_id="1",
        status=ExecutionResult.SUCCEEDED,
        argv=[str(env.workspace / "secret.mkv")],
        stream_log=[],
        output_path=str(env.workspace / "output.mkv"),
        output_probe=probe_data(env.workspace / "output.mkv").model_copy(
            update={"file_path": str(env.workspace / "output.mkv")}
        ),
    )
    message = agent._finished_msg(1, 1, result, [], "job.log")
    assert message["argv"] == []
    assert str(env.workspace) not in json.dumps(message)


def test_resolve_symlink(env: AgentEnv) -> None:
    target = env.workspace / "target"
    target.write_text("x", encoding="utf-8")
    (env.workspace / "link").symlink_to(target)
    with pytest.raises(ValueError):
        Agent(make_config(env))._resolve("link")


@pytest.mark.asyncio
async def test_heartbeat_failure_disconnects(
    env: AgentEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sampled = asyncio.Event()
    error = OSError("resource sample failed enroll-secret")

    async def sample(_previous):
        sampled.set()
        raise error

    async def sleep(_interval):
        return

    monkeypatch.setattr(agent_module, "resource_sample", sample)
    monkeypatch.setattr(agent_module.asyncio, "sleep", sleep)
    caplog.set_level(logging.ERROR, logger="node.agent")
    agent = Agent(make_config(env))
    conn = FakeHubConn([Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json")])
    try:
        with pytest.raises(OSError) as raised:
            await asyncio.wait_for(agent._session(conn), 2)
        assert raised.value is error
        assert sampled.is_set() and agent._disconnecting
        failed = next(
            record for record in caplog.records if getattr(record, "operation", None) == "node.heartbeat.failed"
        )
        assert failed.levelno == logging.ERROR
        assert "resource sample failed" in getattr(failed, "error")
        traceback = getattr(failed, "traceback")
        assert traceback and traceback not in failed.getMessage()
        assert "enroll-secret" not in traceback
        assert "enroll-secret" not in caplog.text + getattr(failed, "error")
    finally:
        await asyncio.wait_for(agent.aclose(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("unreaped", [False, True])
async def test_heartbeat_shutdown_drains(env: AgentEnv, monkeypatch: pytest.MonkeyPatch, unreaped: bool) -> None:
    started, stopping, release, completed = (asyncio.Event() for _ in range(4))
    connections = 0
    conn = FakeHubConn([Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json")])

    async def wait():
        await release.wait()
        completed.set()
        if unreaped:
            raise OSError("child exit unavailable")
        process.returncode = -9
        return -9

    process = Mock(spec=asyncio.subprocess.Process)
    process.returncode = None
    process.kill.side_effect = stopping.set
    process.wait = AsyncMock(side_effect=wait)

    async def sample(_previous):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await stop_child(process, force=True)

    async def sleep(_interval):
        return

    async def connect(_url):
        nonlocal connections
        connections += 1
        return conn

    monkeypatch.setattr(agent_module, "resource_sample", sample)
    monkeypatch.setattr(agent_module.asyncio, "sleep", sleep)
    agent = Agent(make_config(env), connect=connect)
    running = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        conn.disconnect()
        await asyncio.wait_for(stopping.wait(), 2)
        for _ in range(2):
            running.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not running.done() and not completed.is_set()
        release.set()
        with pytest.raises(ChildNotReapedError if unreaped else asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        assert completed.is_set() and connections == 1
        assert process.returncode == (None if unreaped else -9)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 2)
        closing = await asyncio.wait_for(asyncio.gather(agent.aclose(), return_exceptions=True), 2)
        assert isinstance(closing[0], ChildNotReapedError) if unreaped else closing == [None]


@pytest.mark.asyncio
async def test_log_failure_path(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    source = env.workspace / "source.mkv"
    source.write_bytes(b"original")
    job = offer(source, env.workspace / "output.mkv")

    def fail(_path):
        raise OSError("execution log unavailable")

    monkeypatch.setattr("node.executor.open_log", fail)
    message = await Agent(make_config(env))._execute_job(job)
    assert message["result"] == "failed" and message["error"] == "execution log unavailable"
    assert message["log_path"] == job.log_path
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_failure_completion_details(
    env: AgentEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = env.workspace / "source.mkv"
    source.write_bytes(b"original")
    job = offer(source, env.workspace / "output.mkv", job_id=7, attempt_id=3, min_free_gb=0)
    reason = "Unknown encoder 'missing_encoder'."
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    monkeypatch.setenv("FAKE_FFMPEG_ERR", reason)
    caplog.set_level(logging.INFO, logger="node.agent")
    agent = Agent(make_config(env, node_id="node-a", min_free_gb=0))
    try:
        await asyncio.wait_for(agent._run_job(job), 5)
        completion = next(record for record in caplog.records if getattr(record, "operation", None) == "job.finished")
        assert completion.levelno == logging.WARNING
        assert completion.__dict__["job_id"] == job.job_id and completion.__dict__["attempt_id"] == job.attempt_id
        assert completion.__dict__["node_id"] == "node-a" and completion.__dict__["outcome"] == "failed"
        message = completion.getMessage()
        assert job.source_path in message and job.log_path in message and reason in getattr(completion, "error")
        failure = next(
            record for record in caplog.records if getattr(record, "operation", None) == "executor.ffmpeg.failed"
        )
        warning = failure.getMessage()
        assert failure.levelno == logging.WARNING
        assert failure.__dict__["job_id"] == str(job.job_id) and failure.__dict__["attempt_id"] == job.attempt_id
        assert failure.__dict__["returncode"] == 3 and failure.__dict__["outcome"] == "failed"
        assert job.source_path in warning and reason in getattr(failure, "error")
        assert str(env.workspace / job.log_path) in warning
        messages = [agent._outbox.get_nowait() for _ in range(agent._outbox.qsize())]
        finished = next(message for message in messages if message["type"] == "finished")
        assert finished["result"] == "failed" and reason in finished["error"]
        assert reason in (env.workspace / job.log_path).read_text()
        assert source.read_bytes() == b"original" and not (env.workspace / job.output_path).exists()
    finally:
        await asyncio.wait_for(agent.aclose(), 2)


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_credentials_preserves_neighbor(env: AgentEnv, tmp_path: Path, link: str) -> None:
    neighbor = tmp_path / "unrelated.json"
    neighbor.write_bytes(b"unrelated stored data\n")
    config = make_config(env)
    agent = Agent(config)
    partial = env.workspace / ".node-credentials.json.partial"
    if link == "symlink":
        partial.symlink_to(neighbor)
    else:
        partial.hardlink_to(neighbor)

    try:
        agent._save_credentials("node-new", "test-issued-token")
    except OSError, ValueError:
        pass

    assert neighbor.read_bytes() == b"unrelated stored data\n", "creating a partial file overwrote an unrelated file"


@pytest.mark.parametrize("failure", ["write", "fsync", "replace", "occupied"])
def test_credentials_failure_retains(env: AgentEnv, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    config = make_config(env)
    path = env.workspace / "node-credentials.json"
    partial = env.workspace / ".node-credentials.json.partial"
    path.write_bytes(b"existing credentials\n")

    def failed(*_args, **_kwargs):
        raise OSError("credential write failed")

    if failure == "occupied":
        partial.write_bytes(b"unexpected partial\n")
    elif failure == "write":
        monkeypatch.setattr(agent_module.json, "dump", failed)
    elif failure == "fsync":
        monkeypatch.setattr(agent_module.os, "fsync", failed)
    else:
        monkeypatch.setattr(agent_module.os, "replace", failed)
    with pytest.raises(OSError):
        Agent(config)._save_credentials("node-new", "issued-token")
    assert path.read_bytes() == b"existing credentials\n"
    if failure == "occupied":
        assert partial.read_bytes() == b"unexpected partial\n"
    else:
        assert not partial.exists()


@pytest.mark.asyncio
async def test_cancel_action_attempts(env: AgentEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            raise
        return []

    monkeypatch.setattr(agent_module, "run_actions", delayed)
    src = env.workspace / "movie.avi"
    src.write_bytes(b"x")
    action = ActionTask(
        job_id=1,
        attempt_id=2,
        path=src.name,
        probe=probe_data(src).model_copy(update={"file_path": src.name}),
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )
    conn = FakeHubConn(
        [Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"), action.model_dump(mode="json")]
    )
    agent = Agent(make_config(env))
    session = asyncio.create_task(agent._session(conn))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        conn.push(Cancel(job_id=1, attempt_id=1, log_path=None).model_dump(mode="json"))
        assert (1, 2) in agent._action_tasks
        conn.push(Cancel(job_id=1, attempt_id=2, log_path=None).model_dump(mode="json"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        assert (1, 2) in agent._action_tasks
        assert not conn.sent_of("action_report")
        release.set()
        await wait_until(lambda: conn.sent_of("action_report"), conn.activity)
        result = conn.sent_of("action_report")[0]["results"][0]
        assert result["kind"] == "extract_subtitles"
        assert result["ok"] is False
        assert result["artifacts"] == []
        conn.disconnect()
        await asyncio.wait_for(session, timeout=5)
    finally:
        release.set()
        if not session.done():
            conn.disconnect()
            await asyncio.wait_for(session, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pre", "post"])
async def test_unreaped_child_disconnects(env: AgentEnv, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    src = env.workspace / "output.mkv"
    src.write_bytes(b"output")
    partial = env.workspace / "output.eng.srt.partial"
    calls: list[str] = []

    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"incomplete")
            raise asyncio.CancelledError

        def send_signal(self, _signal) -> None:
            calls.append("kill")

        async def wait(self) -> int:
            calls.append("wait")
            raise TimeoutError

    async def spawn(*args: str, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr("node.process.create_tool", spawn)
    agent = Agent(make_config(env))
    conn = FakeHubConn()
    agent._conn = conn
    probe = probe_data(src).model_copy(
        update={"streams": [ProbeStream(index=1, codec="subrip", codec_type="subtitle", language="eng")]}
    )
    actions: list[Action] = [ExtractSubtitles(formats=["srt"], languages=["eng"])]

    async def execute_job(offer: OfferedJob) -> dict:
        await actions_module.run_actions(
            ActionsSpec(
                job_id=offer.job_id,
                path=src,
                probe=probe,
                actions=actions,
                workspace_dir=env.workspace,
                log_path=env.workspace / "job.log",
            ),
            "ffmpeg",
        )
        pytest.fail("unreaped child must not finish")

    monkeypatch.setattr(agent, "_execute_job", execute_job)
    if phase == "pre":
        await asyncio.wait_for(agent._run_job(offer(src, src)), timeout=5)
    else:
        await asyncio.wait_for(
            agent._run_action_task(
                ActionTask(
                    job_id=1,
                    attempt_id=1,
                    path=src.name,
                    probe=probe.model_copy(update={"file_path": src.name}),
                    actions=actions,
                )
            ),
            timeout=5,
        )
    assert calls == ["kill", "wait"]
    assert conn.closed and agent._disconnecting
    assert agent._outbox.empty()
    assert not agent._jobs and not agent._action_tasks
    assert partial.read_bytes() == b"incomplete"
