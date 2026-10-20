from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from common.actions import ActionOutcome, ExtractSubtitles
from common.journal import read_messages
from common.protocol import (
    ActionReport,
    ActionTask,
    ActiveAttempt,
    Error,
    Finished,
    JobOffer,
    NodeLog,
    NodeLogs,
    OfferedJob,
    Progress,
    SetLimits,
    Welcome,
)
from common.enums import ExecutionResult
from common.workspace import workspace_lock
from node import agent as agent_module
from node.agent import Agent
from node.executor import JobResult, JobSpec, ProgressUpdate
from node.logging import HubLogHandler
from tests.test_agent import AgentEnv, FakeHubConn, make_config, offer, probe_data, wait_until


@pytest.fixture
def job(tmp_path: Path) -> OfferedJob:
    attempt = "jobs/1/attempts/2"
    (tmp_path / attempt).mkdir(parents=True)
    return offer(
        tmp_path / "source.mkv",
        tmp_path / "output.mkv",
        attempt_id=2,
        source_path=f"{attempt}/source.mkv",
        output_path=f"{attempt}/output.mkv",
        log_path=f"{attempt}/ffmpeg.log",
        probe=probe_data(tmp_path / "source.mkv").model_copy(update={"file_path": f"{attempt}/source.mkv"}),
    )


@pytest.mark.asyncio
async def test_journal_precedes_enqueue(tmp_path: Path, job: OfferedJob, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(make_config(AgentEnv(tmp_path), node_id="node-a"))
    update = ProgressUpdate(percent=25.0, fps=48.0, out_time_us=2_500_000, total_size=100, speed="2x")
    progress = Progress(
        job_id=1, attempt_id=2, percent=25.0, fps=48.0, out_time_us=2_500_000, total_size=100, speed="2x"
    ).model_dump(mode="json")
    finished = Finished(
        job_id=1,
        attempt_id=2,
        result="succeeded",
        output_path=job.output_path,
        output_size=100,
        source_size=1000,
        output_probe=job.probe.model_copy(update={"file_path": job.output_path}),
        log_path=job.log_path,
    ).model_dump(mode="json")
    report = ActionReport(
        job_id=1,
        attempt_id=2,
        results=[ActionOutcome(kind="extract_subtitles", ok=True, artifacts=["jobs/1/attempts/2/output.eng.srt"])],
    ).model_dump(mode="json")
    enqueued: list[dict] = []
    put = agent._outbox.put_nowait

    def enqueue(message: dict) -> None:
        assert read_messages(tmp_path, job.log_path) == [*enqueued, message]
        enqueued.append(message)
        put(message)

    monkeypatch.setattr(agent._outbox, "put_nowait", enqueue)
    try:
        agent._progress_sink(job.job_id, job.attempt_id, job.log_path)(update)
        agent._queue_message(finished, job.log_path)
        agent._queue_message(report, job.log_path)
        assert enqueued == [progress, finished, report]
        assert [agent._outbox.get_nowait() for _ in enqueued] == enqueued
        assert agent._outbox.empty()
    finally:
        await asyncio.wait_for(agent.aclose(), 2)


@pytest.mark.asyncio
async def test_logs_group_attempts(tmp_path: Path, job: OfferedJob) -> None:
    agent = Agent(make_config(AgentEnv(tmp_path), node_id="node-a"))
    other_log = "jobs/1/attempts/3/ffmpeg.log"
    (tmp_path / other_log).parent.mkdir(parents=True)
    agent._log_paths.update({(1, 2): job.log_path, (1, 3): other_log})
    handler = HubLogHandler(asyncio.get_running_loop(), agent._queue_logs)
    agent._log_handler = handler
    entries = [
        NodeLog(logger="node.executor", level="INFO", message="first", job_id=1, attempt_id=2),
        NodeLog(logger="node.executor", level="INFO", message="unscoped"),
        NodeLog(logger="node.executor", level="INFO", message="other attempt", job_id=1, attempt_id=3),
        NodeLog(logger="node.executor", level="INFO", message="ambiguous job", job_id=1),
        NodeLog(logger="node.executor", level="INFO", message="inactive", job_id=9, attempt_id=2),
        NodeLog(logger="node.executor", level="INFO", message="ambiguous attempt", attempt_id=2),
        NodeLog(logger="node.executor", level="INFO", message="first again", job_id=1, attempt_id=2),
        NodeLog(logger="node.executor", level="INFO", message="unscoped again"),
    ]
    expected = [
        NodeLogs(entries=[entries[index] for index in group]).model_dump(mode="json")
        for group in ([0, 6], [1, 7], [2], [3], [4], [5])
    ]
    try:
        for entry in entries:
            handler.emit(
                logging.makeLogRecord(
                    {
                        "name": entry.logger,
                        "levelno": logging.INFO,
                        "levelname": entry.level,
                        "msg": entry.message,
                        "job_id": entry.job_id,
                        "attempt_id": entry.attempt_id,
                    }
                )
            )
        handler.flush()
        assert [agent._outbox.get_nowait() for _ in expected] == expected
        assert read_messages(tmp_path, job.log_path) == [expected[0]]
        assert read_messages(tmp_path, other_log) == [expected[2]]
        assert len(list(tmp_path.rglob("job.ndjson"))) == 2
        assert agent._outbox.empty()
    finally:
        await asyncio.wait_for(agent.aclose(), 2)


@pytest.mark.asyncio
async def test_journal_failure_succeeds(
    tmp_path: Path, job: OfferedJob, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = Agent(make_config(AgentEnv(tmp_path), node_id="node-a"))
    result = JobResult(
        job_id="1",
        status=ExecutionResult.SUCCEEDED,
        argv=["ffmpeg", "-i", "source.mkv", "output.mkv"],
        stream_log=[],
        output_path=str(tmp_path / job.output_path),
        output_size=100,
        source_size=1000,
        output_probe=job.probe.model_copy(update={"file_path": str(tmp_path / job.output_path)}),
    )
    attempted: list[dict] = []

    def fail_append(root: Path, log_path: str, message: dict) -> None:
        assert root == tmp_path and log_path == job.log_path
        attempted.append(message)
        raise OSError("journal disk unavailable")

    async def execute(
        spec: JobSpec, _ffmpeg: str, _ffprobe: str, on_progress: Callable[[ProgressUpdate], None], *, prepared=None
    ) -> JobResult:
        assert spec.attempt_id == job.attempt_id
        on_progress(ProgressUpdate(percent=100.0))
        return result

    monkeypatch.setattr(agent_module, "append_message", fail_append)
    monkeypatch.setattr(agent_module, "execute", execute)
    caplog.set_level(logging.INFO, logger="node.agent")
    handler = HubLogHandler(asyncio.get_running_loop(), agent._queue_logs)
    agent._log_handler = handler
    logging.getLogger().addHandler(handler)
    try:
        await asyncio.wait_for(agent._run_job(job), 2)
        messages = [agent._outbox.get_nowait() for _ in range(agent._outbox.qsize())]
        assert messages == attempted
        assert [message for message in messages if message["type"] == "progress"] == [
            Progress(job_id=1, attempt_id=2, percent=100.0).model_dump(mode="json")
        ]
        assert [message for message in messages if message["type"] == "finished"] == [
            agent._finished_msg(1, 2, result, [], job.log_path)
        ]
        assert next(message for message in messages if message["type"] == "finished")["result"] == "succeeded"
        errors = [record for record in caplog.records if record.levelno == logging.ERROR and record.exc_info]
        assert len(errors) == len(attempted)
        assert all(getattr(record, "_hub_log_skip", False) for record in errors)
        assert not (tmp_path / job.log_path).with_name("job.ndjson").exists()
    finally:
        await asyncio.wait_for(agent.aclose(), 2)


@pytest.mark.asyncio
async def test_reconnect_retains_execution(tmp_path: Path, job: OfferedJob, monkeypatch: pytest.MonkeyPatch) -> None:
    started, offline, queued, release = (asyncio.Event() for _ in range(4))
    handlers = set(logging.getLogger().handlers)
    calls = 0

    async def execute(
        spec: JobSpec, _ffmpeg: str, _ffprobe: str, on_progress: Callable[[ProgressUpdate], None], *, prepared=None
    ) -> JobResult:
        nonlocal calls
        calls += 1
        started.set()
        await offline.wait()
        on_progress(ProgressUpdate(percent=50.0))
        queued.set()
        await release.wait()
        return JobResult(
            job_id=spec.job_id,
            status=ExecutionResult.SUCCEEDED,
            argv=[],
            stream_log=[],
            output_path=spec.dst,
            output_probe=spec.probe.model_copy(update={"file_path": spec.dst}),
        )

    monkeypatch.setattr(agent_module, "execute", execute)
    agent = Agent(make_config(AgentEnv(tmp_path), node_id="node-a"))
    loop = asyncio.get_running_loop()
    call_later = loop.call_later
    timers: list[asyncio.TimerHandle] = []

    def schedule(delay, callback, *args, context=None):
        if callback == agent._expire:
            timer = asyncio.TimerHandle(loop.time() + delay, callback, args, loop, context=context)
            timers.append(timer)
            return timer
        return call_later(delay, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_later", schedule)
    first = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[], lease_ttl_s=30).model_dump(mode="json"),
            JobOffer(job=job).model_dump(mode="json"),
        ]
    )
    sessions = [asyncio.create_task(agent._session(first))]
    second = FakeHubConn(
        [
            Welcome(
                timezone="UTC", active_attempts=[ActiveAttempt(job_id=job.job_id, attempt_id=job.attempt_id)]
            ).model_dump(mode="json"),
            JobOffer(job=job).model_dump(mode="json"),
            SetLimits(max_transcodes=2, max_healthchecks=1, max_mkv_stats=1).model_dump(mode="json"),
        ]
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        task = agent._jobs[(1, 2)]
        outbox = agent._outbox
        handler = agent._log_handler
        first.disconnect()
        assert await asyncio.wait_for(sessions[0], 2)
        assert agent._jobs[(1, 2)] is task and not task.done() and not task.cancelling()
        with pytest.raises(BlockingIOError), workspace_lock((tmp_path / job.log_path).parent, exclusive=True):
            pytest.fail("disconnected execution released its workspace lock")
        assert agent._active_transcodes == 1 and agent._log_handler is handler
        assert len(timers) == 1 and agent._reconnect_timer is timers[0]
        offline.set()
        await asyncio.wait_for(queued.wait(), 2)
        assert agent._outbox.qsize() >= 1 and not first.sent_of("progress")
        sessions.append(asyncio.create_task(agent._session(second)))
        await wait_until(lambda: agent._transcode_limit == 2 and second.sent_of("progress"), second.activity, 2)
        assert agent._jobs[(1, 2)] is task and calls == 1
        assert agent._outbox is outbox and agent._log_handler is handler
        assert timers[0].cancelled() and agent._reconnect_timer is None
        progress = Progress(job_id=1, attempt_id=2, percent=50.0).model_dump(mode="json")
        assert second.sent_of("progress") == [progress]
        release.set()
        await wait_until(lambda: second.sent_of("finished"), second.activity, 2)
        assert len(second.sent_of("finished")) == 1
        assert second.sent_of("finished")[0]["result"] == "succeeded"
        with workspace_lock((tmp_path / job.log_path).parent, exclusive=True):
            pass
        journal = read_messages(tmp_path, job.log_path)
        assert [message for message in journal if message["type"] == "progress"] == [progress]
        assert [message for message in journal if message["type"] == "finished"] == second.sent_of("finished")
        second.disconnect()
        assert await asyncio.wait_for(sessions[1], 2)
    finally:
        offline.set()
        release.set()
        first.disconnect()
        second.disconnect()
        for session in sessions:
            if not session.done():
                session.cancel()
        await asyncio.wait_for(asyncio.gather(*sessions, return_exceptions=True), 2)
        await asyncio.wait_for(agent.aclose(), 2)
        assert all(timer.cancelled() for timer in timers)
        assert agent._log_handler is None and agent._reconnect_timer is None
        assert set(logging.getLogger().handlers) == handlers


@pytest.mark.asyncio
async def test_expiry_drains_work(
    tmp_path: Path, job: OfferedJob, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    job_started, action_started, job_draining, action_draining, release = (asyncio.Event() for _ in range(5))
    handlers = set(logging.getLogger().handlers)
    caplog.set_level(logging.INFO, logger="node.agent")

    async def execute(*_args, prepared=None) -> JobResult:
        job_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            job_draining.set()
            await release.wait()
            raise
        pytest.fail("Lease expiry must cancel execution.")

    async def actions(*_args) -> list[ActionOutcome]:
        action_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            action_draining.set()
            await release.wait()
            raise
        pytest.fail("Lease expiry must cancel actions.")

    monkeypatch.setattr(agent_module, "execute", execute)
    monkeypatch.setattr(agent_module, "run_actions", actions)
    action = ActionTask(
        job_id=1,
        attempt_id=3,
        path="jobs/1/attempts/3/output.mkv",
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )
    (tmp_path / action.path).parent.mkdir(parents=True)
    agent = Agent(make_config(AgentEnv(tmp_path), node_id="node-a"))
    loop = asyncio.get_running_loop()
    call_later = loop.call_later
    timers: list[asyncio.TimerHandle] = []
    callbacks: list[Callable[[], None]] = []

    def schedule(delay, callback, *args, context=None):
        if callback == agent._expire:
            timer = asyncio.TimerHandle(loop.time() + delay, callback, args, loop, context=context)
            timers.append(timer)
            callbacks.append(callback)
            return timer
        return call_later(delay, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_later", schedule)
    conn = FakeHubConn(
        [
            Welcome(timezone="UTC", active_attempts=[], lease_ttl_s=30).model_dump(mode="json"),
            JobOffer(job=job).model_dump(mode="json"),
            action.model_dump(mode="json"),
        ]
    )
    session = asyncio.create_task(agent._session(conn))
    try:
        await asyncio.wait_for(job_started.wait(), 2)
        await asyncio.wait_for(action_started.wait(), 2)
        tasks = [agent._jobs[(1, 2)], agent._action_tasks[(1, 3)]]
        conn.disconnect()
        assert await asyncio.wait_for(session, 2)
        assert len(timers) == 1
        deadline = timers[0].when()
        for reply in (Error(code="auth", message="rejected").model_dump(mode="json"), None):
            failed = FakeHubConn([reply])
            assert await asyncio.wait_for(agent._session(failed), 2) is False
            assert agent._reconnect_timer is timers[0]
            assert len(timers) == 1 and timers[0].when() == deadline and not timers[0].cancelled()
            assert all(not task.done() and not task.cancelling() for task in tasks)
            await failed.close()
        callbacks[0]()
        await asyncio.wait_for(job_draining.wait(), 2)
        await asyncio.wait_for(action_draining.wait(), 2)
        assert all(not task.done() for task in tasks)
        assert agent._jobs and agent._action_tasks and agent._active_transcodes == 1
        for path in (job.log_path, action.path):
            with pytest.raises(BlockingIOError), workspace_lock((tmp_path / path).parent, exclusive=True):
                pytest.fail("workspace lock released before expiry drain completed")
        assert agent._reconnect_timer is None
        assert all(message["type"] != "finished" for message in read_messages(tmp_path, job.log_path))
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        messages = [agent._outbox.get_nowait() for _ in range(agent._outbox.qsize())]
        finished = [message for message in messages if message["type"] == "finished"]
        reports = [message for message in messages if message["type"] == "action_report"]
        assert len(finished) == len(reports) == 1
        assert (finished[0]["job_id"], finished[0]["attempt_id"], finished[0]["result"], finished[0]["log_path"]) == (
            1,
            2,
            "failed",
            job.log_path,
        )
        assert (reports[0]["job_id"], reports[0]["attempt_id"]) == (1, 3)
        assert [(result["kind"], result["ok"]) for result in reports[0]["results"]] == [("extract_subtitles", False)]
        assert [
            message for message in read_messages(tmp_path, job.log_path) if message["type"] == "finished"
        ] == finished
        assert read_messages(tmp_path, "jobs/1/attempts/3/ffmpeg.log") == reports
        assert not agent._jobs and not agent._action_tasks and agent._active_transcodes == 0
        for path in (job.log_path, action.path):
            with workspace_lock((tmp_path / path).parent, exclusive=True):
                pass
    finally:
        release.set()
        conn.disconnect()
        if not session.done():
            session.cancel()
        await asyncio.wait_for(asyncio.gather(session, return_exceptions=True), 2)
        await asyncio.wait_for(agent.aclose(), 2)
        for timer in timers:
            timer.cancel()
        assert agent._log_handler is None and agent._reconnect_timer is None
        assert set(logging.getLogger().handlers) == handlers
