from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from common.dates import Dates
from common.diagnostics import error_traceback
from common.protocol import NodeCapabilities, NodeLog, NodeLogs, Welcome, parse_message
from common.protocol.messages import NODE_LOG_TRACEBACK_LIMIT
from node.agent import Agent, AgentConfig
from node.logging import HubLogHandler
from server.core.log_history import query_logs
from server.nodehub.hub import NodeConnection, NodeEntry, NodeHub
from tests.test_agent import FakeHubConn
from tests.test_log_history import file_logs


@pytest.mark.usefixtures("logging_state")
@pytest.mark.parametrize("timezone", [None, "Asia/Kuwait"])
def test_startup_timezone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], timezone: str | None
) -> None:
    from node import cli

    if timezone is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", timezone)

    async def start(_args, dates):
        assert str(dates.timezone) == (timezone or "UTC")
        logging.getLogger("node.cli").info("Startup diagnostic")
        return 0

    monkeypatch.setattr(cli, "_amain", start)
    with pytest.raises(SystemExit) as exited:
        cli.main([])
    assert exited.value.code == 0
    record = json.loads(capsys.readouterr().err)
    assert record["logger"] == "node.cli" and record["event"] == "Startup diagnostic"
    assert datetime.fromisoformat(record["timestamp"]).tzinfo == UTC


@pytest.mark.asyncio
@pytest.mark.usefixtures("logging_state")
async def test_session_timezone(tmp_path: Path) -> None:
    dates = Dates("Europe/Berlin")
    agent = Agent(AgentConfig(token="token", server_url="ws://hub", workspace_dir=str(tmp_path)), dates=dates)
    conn = FakeHubConn([Welcome(timezone="Asia/Kuwait", active_attempts=[]).model_dump(mode="json"), None])

    try:
        assert await asyncio.wait_for(agent._session(conn), 2)
        assert str(dates.timezone) == "Asia/Kuwait"
        reconnect = FakeHubConn([Welcome(timezone="Europe/Berlin", active_attempts=[]).model_dump(mode="json"), None])
        assert await asyncio.wait_for(agent._session(reconnect), 2)
        assert str(dates.timezone) == "Europe/Berlin"
    finally:
        await asyncio.wait_for(agent.aclose(), 2)


class Loop:
    def __init__(self) -> None:
        self.callbacks: list[tuple[object, tuple[object, ...]]] = []

    def call_soon_threadsafe(self, callback: object, *args: object) -> None:
        self.callbacks.append((callback, args))

    def call_soon(self, callback: object) -> None:
        self.callbacks.append((callback, ()))

    def run(self) -> None:
        while self.callbacks:
            callback, args = self.callbacks.pop(0)
            assert isinstance(callback, Callable)
            callback(*args)


def test_protocol_validates_node_logs() -> None:
    parsed = parse_message(NodeLogs(entries=[NodeLog(logger="node.agent", level="INFO", message="ready")]).model_dump())
    assert isinstance(parsed, NodeLogs)
    with pytest.raises(ValueError):
        parse_message({"v": 1, "type": "node_logs", "entries": [{"logger": "server", "level": "INFO", "message": "x"}]})


def test_handler_bounds_callbacks(caplog: pytest.LogCaptureFixture) -> None:
    loop = Loop()
    sent: list[dict] = []
    handler = HubLogHandler(loop, sent.append)
    logger = logging.getLogger("node.agent")
    logger.addHandler(handler)
    capacity = 100
    messages = [f"local diagnostic {index}" for index in range(capacity + 1)]

    def emit() -> None:
        for message in messages:
            logger.info(message)

    worker = threading.Thread(target=emit)
    try:
        with caplog.at_level(logging.INFO, logger="node.agent"):
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive()
        assert len(loop.callbacks) == capacity
        assert not sent
        loop.run()
        assert [entry["message"] for message in sent for entry in message["entries"]] == messages[:capacity]
        assert [record.getMessage() for record in caplog.records] == messages
    finally:
        worker.join(timeout=2)
        logger.removeHandler(handler)
        handler.close()
        assert not worker.is_alive()


def test_handler_ignores_remote(caplog: pytest.LogCaptureFixture) -> None:
    loop = Loop()
    sent: list[dict] = []
    handler = HubLogHandler(loop, sent.append)
    record = logging.LogRecord("node.remote", logging.INFO, "", 0, "forwarded", (), None)
    record._from_node = True  # type: ignore[attr-defined]
    handler.emit(record)
    handler.close()
    loop.run()
    assert not sent


@pytest.mark.parametrize("completion_type", ["finished", "action_report"])
def test_agent_bounds_logs(tmp_path: Path, completion_type: str) -> None:
    agent = Agent(AgentConfig(token="token", server_url="ws://hub", workspace_dir=str(tmp_path)))
    capacity = agent._outbox.maxsize
    messages = [
        {"type": "node_logs", "entries": [{"logger": "node.agent", "level": "INFO", "message": f"log {index}"}]}
        for index in range(capacity + 1)
    ]
    for message in messages:
        agent._queue_logs(message)
    assert agent._outbox.qsize() == capacity
    (tmp_path / "logs").mkdir()
    completion = {"type": completion_type}
    agent._queue_message(completion, "logs/job.log")
    assert agent._outbox.qsize() == capacity
    assert [agent._outbox.get_nowait() for _ in range(capacity)] == [*messages[1:capacity], completion]
    assert agent._outbox.empty()


@pytest.mark.parametrize("repetitions", [500, 2000])
def test_forwarded_causes_structured(repetitions: int) -> None:
    loop = Loop()
    sent: list[dict] = []
    handler = HubLogHandler(loop, sent.append)
    try:
        try:
            try:
                raise OSError("storage denied for jobs/1/attempts/2/source.mkv " + "diagnostic " * repetitions)
            except OSError as cause:
                raise RuntimeError("job failed") from cause
        except RuntimeError as exc:
            expected = error_traceback(exc)
            handler.emit(
                logging.makeLogRecord(
                    {
                        "name": "node.agent",
                        "levelno": logging.ERROR,
                        "levelname": "ERROR",
                        "msg": "job failed",
                        "exc_info": (type(exc), exc, exc.__traceback__),
                        "job_id": 1,
                        "attempt_id": 2,
                    }
                )
            )
        loop.run()
        entries = []
        for message in sent:
            parsed = parse_message(message)
            assert isinstance(parsed, NodeLogs)
            entries.extend(parsed.entries)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.message == "job failed"
        assert entry.job_id == 1 and entry.attempt_id == 2
        assert entry.traceback is not None
        assert "jobs/1/attempts/2/source.mkv" in entry.traceback
        if len(expected) > NODE_LOG_TRACEBACK_LIMIT:
            assert len(entry.traceback) == NODE_LOG_TRACEBACK_LIMIT
            assert entry.traceback.endswith("\n[traceback truncated]")
            assert expected.startswith(entry.traceback.removesuffix("\n[traceback truncated]"))
        else:
            assert entry.traceback == expected
    finally:
        handler.close()


@pytest.mark.parametrize("traceback_source", ["exc_info", "explicit", "both"])
def test_forwarding_records_file(tmp_path: Path, caplog: pytest.LogCaptureFixture, traceback_source: str) -> None:
    loop = Loop()
    entry = NodeEntry(
        name="node-test",
        node_db_id=1,
        capabilities=NodeCapabilities(),
        workspace_ready=True,
        conn=Mock(spec=NodeConnection),
    )

    def forward(message: dict) -> None:
        parsed = parse_message(message)
        assert isinstance(parsed, NodeLogs)
        NodeHub._handle_node_logs(entry, parsed)

    handler = HubLogHandler(loop, forward)
    try:
        with file_logs(tmp_path):
            try:
                raise OSError("storage denied for jobs/1/attempts/2/source.mkv token-value " + "diagnostic " * 500)
            except OSError as exc:
                expected = error_traceback(exc, secrets=("token-value",) if traceback_source != "exc_info" else ())
                record = logging.LogRecord(
                    "node.agent",
                    logging.ERROR,
                    __file__,
                    1,
                    "node task failed",
                    (),
                    None if traceback_source == "explicit" else (type(exc), exc, exc.__traceback__),
                )
                if traceback_source != "exc_info":
                    record.traceback = expected
                handler.emit(record)
            loop.run()
        entries = [record for record in query_logs(tmp_path)["entries"] if record["logger"] == "node.node-test"]
        assert len(entries) == 1
        assert entries[0]["message"] == "node task failed"
        assert entries[0]["fields"]["traceback"] == expected
        assert entries[0]["fields"]["source_logger"] == "node.agent"
        replayed = [record for record in caplog.records if record.name == "node.node-test"]
        assert len(replayed) == 1
        assert replayed[0].getMessage() == "node task failed"
        assert getattr(replayed[0], "traceback") == expected
        stored = json.loads((tmp_path / "logs" / "millwright.log").read_text())
        assert stored["message"] == "node task failed"
        assert "jobs/1/attempts/2/source.mkv" in stored["fields"]["traceback"]
    finally:
        handler.close()
