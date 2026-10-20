from __future__ import annotations

import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path

import pytest
import structlog
import httpx
from fastapi import HTTPException

from server.core.logging import configure_access_logging, setup_logging
from server.core.log_history import install_log_file, query_logs
from server.api.app import AccessLogMiddleware, create_app
from server.core.config import ServerConfig
from tests.helpers import database_context
from common.logging import THIRD_PARTY_LEVELS, setup_logging as setup_common_logging


pytestmark = pytest.mark.usefixtures("logging_state")


async def receive() -> dict[str, str]:
    return {"type": "http.request"}


def rendered_record(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    err = capsys.readouterr().err
    return json.loads(err.strip().splitlines()[-1])


def test_json_record_shape(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(timezone="UTC")
    logging.getLogger("server.test").info("hello %s", "world")
    record = rendered_record(capsys)
    assert record["event"] == "hello world"
    assert record["level"] == "info"
    assert record["logger"] == "server.test"
    timestamp = record["timestamp"]
    assert isinstance(timestamp, str)
    datetime.fromisoformat(timestamp)


@pytest.mark.parametrize("configure", [setup_logging, setup_common_logging])
def test_shared_log_format(configure, monkeypatch, capsys):
    timestamp = "2026-01-01T23:30:00Z"

    def stamp(_logger, _name, event_dict):
        return {**event_dict, "timestamp": timestamp}

    monkeypatch.setattr(structlog.processors, "TimeStamper", lambda **_kwargs: stamp)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    configure(timezone="Asia/Kuwait")
    template, value = "fixture:%s", "worker"
    logging.getLogger("node.agent").info(
        template, value, extra={"operation": "node.handshake.accepted", "node_id": value, "color_message": "colored"}
    )
    record = rendered_record(capsys)
    assert record["timestamp"] == timestamp
    assert record["logger"] == "node.agent" and record["level"] == "info"
    assert record["operation"] == "node.handshake.accepted" and record["node_id"] == value
    assert record["event"] == template % value
    assert "color_message" not in record


def test_dependency_log_levels(capsys):
    setup_common_logging(timezone="UTC", level=logging.DEBUG)
    for name, level in THIRD_PARTY_LEVELS:
        logger = logging.getLogger(name)
        logger.debug("fixture")
        logger.info("fixture")
        logger.warning("fixture")
        records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert [record["level"] for record in records] == (
            ["info", "warning"] if level == logging.INFO else ["warning"]
        )
        assert logger.getEffectiveLevel() == level


@pytest.mark.parametrize("enabled", [False, True])
def test_websocket_access_switch(tmp_path: Path, capsys: pytest.CaptureFixture[str], enabled: bool) -> None:
    setup_logging(timezone="UTC")
    configure_access_logging(enabled=not enabled)
    configure_access_logging(enabled=enabled)
    logger = logging.getLogger("uvicorn.error")
    handler = install_log_file(tmp_path)
    access_messages = [
        ('%s - "WebSocket %s" [accepted]', ("client", "/api/events?ticket=fixture")),
        ('%s - "WebSocket %s" 403', ("client", "/api/events")),
        ('%s - "WebSocket %s" %d', ("client", "/api/events", 401)),
        ("connection open", ()),
        ("connection closed", ()),
        ("connection rejected (%d %s)", (403, "Forbidden")),
    ]
    retained = ["Application startup complete.", "Shutting down", "opening handshake failed", "connection open"]
    try:
        for message, args in access_messages:
            logger.info(message, *args)
        logger.info(retained[0])
        logger.info(retained[1])
        logger.error(retained[2])
        logger.warning(retained[3])
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    expected = ([message % args for message, args in access_messages] if enabled else []) + retained
    console = [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]
    assert [record["event"] for record in console] == expected
    history = query_logs(tmp_path)["entries"]
    assert [entry["message"] for entry in history] == expected


@pytest.mark.asyncio
async def test_request_id_header() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(message):
        sent.append(message)

    await AccessLogMiddleware(app)({"type": "http", "method": "GET", "path": "/x", "headers": []}, receive, send)
    headers = dict(sent[0]["headers"])
    assert len(headers[b"x-request-id"]) == 32


@pytest.mark.asyncio
async def test_request_id_isolation() -> None:
    entered = {value: asyncio.Event() for value in ("one", "two")}

    async def app(scope, receive, send):
        value = scope["path"]
        entered[value].set()
        await asyncio.wait_for(entered["two" if value == "one" else "one"].wait(), 2)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def request(value):
        sent = []

        async def send(message):
            sent.append(message)

        await AccessLogMiddleware(app)(
            {"type": "http", "method": "GET", "path": value, "headers": [(b"x-request-id", value.encode())]},
            receive,
            send,
        )
        return dict(sent[0]["headers"])[b"x-request-id"]

    async with asyncio.TaskGroup() as group:
        one = group.create_task(request("one"))
        two = group.create_task(request("two"))
    assert [one.result(), two.result()] == [b"one", b"two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("access_log", [False, True])
async def test_access_log_switch(tmp_path: Path, caplog: pytest.LogCaptureFixture, access_log: bool) -> None:
    context = structlog.contextvars.get_contextvars()
    sent = []

    async def app(scope, receive, send):
        assert structlog.contextvars.get_contextvars()["request_id"] == "request-fixture"
        logging.getLogger("server.test").info("Application event")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    handler = install_log_file(tmp_path)
    try:
        with caplog.at_level(logging.INFO):
            await AccessLogMiddleware(app, access_log=access_log)(
                {"type": "http", "method": "GET", "path": "/x", "headers": [(b"x-request-id", b"request-fixture")]},
                receive,
                send,
            )
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert dict(sent[0]["headers"])[b"x-request-id"] == b"request-fixture"
    assert structlog.contextvars.get_contextvars() == context
    assert len([record for record in caplog.records if record.name == "server.access"]) == int(access_log)
    entries = query_logs(tmp_path)["entries"]
    assert len([entry for entry in entries if entry["logger"] == "server.access"]) == int(access_log)
    application = next(entry for entry in entries if entry["logger"] == "server.test")
    assert application["message"] == "Application event"
    assert application["fields"]["request_id"] == "request-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("access_log", [False, True])
async def test_request_failure_response(caplog: pytest.LogCaptureFixture, access_log: bool) -> None:
    async def app(scope, receive, send):
        raise RuntimeError("disk unavailable for movie.mkv. Credential: secret")

    sent = []

    async def send(message):
        sent.append(message)

    await AccessLogMiddleware(app, access_log=access_log, secrets=("secret",))(
        {"type": "http", "method": "GET", "path": "/failure", "query_string": b"token=secret", "headers": []},
        receive,
        send,
    )
    assert sent[0]["status"] == 500
    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"]
    assert int(headers[b"content-length"]) == len(sent[-1]["body"])
    assert b"secret" not in sent[-1]["body"]
    assert b"disk unavailable for movie.mkv" in sent[-1]["body"]
    failures = [record for record in caplog.records if record.name == "server.api.app"]
    assert len(failures) == 1
    assert "disk unavailable for movie.mkv" in getattr(failures[0], "error")
    assert "secret" not in str(failures[0].__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize("handled", [False, True])
async def test_request_failure_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handled: bool) -> None:
    monkeypatch.setenv("MW_UI_PATH", str(tmp_path / "no-ui"))
    async with database_context("request-failure") as factory:
        app = create_app(factory, config=ServerConfig(api_key="credential-fixture", access_log=False))

        @app.get("/diagnostic")
        async def diagnostic() -> None:
            if handled:
                raise HTTPException(503, "Encoder slot unavailable. Credential: credential-fixture")
            raise RuntimeError("Workspace offline. Credential: credential-fixture")

        handler = install_log_file(tmp_path)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/diagnostic", headers={"x-request-id": "failure-fixture"})
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
    assert response.status_code == (503 if handled else 500)
    detail = response.json()["detail"]
    assert ("Encoder slot unavailable" if handled else "Workspace offline") in detail
    entries = query_logs(tmp_path)["entries"]
    assert len(entries) == 1
    assert entries[0]["level"] == "error"
    assert entries[0]["fields"]["request_id"] == "failure-fixture"
    assert detail in entries[0]["fields"]["error"]
    assert "credential-fixture" not in response.text
    assert "credential-fixture" not in json.dumps(entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("access_log", [False, True])
async def test_started_failure_reraises(access_log: bool) -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late failure")

    sent = []

    async def send(message):
        sent.append(message)

    with pytest.raises(RuntimeError):
        await AccessLogMiddleware(app, access_log=access_log)(
            {"type": "http", "method": "GET", "path": "/late", "headers": []}, receive, send
        )
    assert [message["type"] for message in sent] == ["http.response.start"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/logs", "/logs/", "/api/logs", "/api/logs/", "/api/logs/stream"])
async def test_viewer_access_quiet(path: str, caplog: pytest.LogCaptureFixture) -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        pass

    with caplog.at_level(logging.INFO):
        await AccessLogMiddleware(app)({"type": "http", "method": "GET", "path": path, "headers": []}, receive, send)
    assert not [record for record in caplog.records if record.name == "server.access"]
