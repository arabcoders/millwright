from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from server.core import log_history
import structlog

from server.core.log_history import install_log_file, query_logs

if TYPE_CHECKING:
    from collections.abc import Generator
    from logging.handlers import RotatingFileHandler


@contextmanager
def file_logs(path: Path, *, max_bytes: int = 10_000_000) -> Generator[RotatingFileHandler]:
    handler = install_log_file(path, max_bytes=max_bytes)
    try:
        yield handler
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def emit(handler: RotatingFileHandler, message: str, **fields: object) -> None:
    record = logging.LogRecord("server.test", logging.INFO, "", 0, message, (), None)
    record.created = 1000
    record.__dict__.update(fields)
    handler.handle(record)


def test_logs_persist(tmp_path: Path) -> None:
    with file_logs(tmp_path) as handler:
        emit(handler, "ordinary message", job_id=4, custom_value="preserved")
    first = query_logs(tmp_path)["entries"][0]
    assert first["message"] == "ordinary message"
    assert first["fields"] == {"job_id": 4, "custom_value": "preserved"}
    with file_logs(tmp_path) as handler:
        emit(handler, "restored", entry_id="entry")
    entries = query_logs(tmp_path)["entries"]
    assert entries[0] == first
    assert entries[1]["id"] > first["id"]


def test_formatter_fields(tmp_path: Path) -> None:
    with file_logs(tmp_path) as handler:
        record = logging.LogRecord("server.test", logging.INFO, "", 0, "formatted %s", ("message",), None)
        record.custom_value = "preserved"
        logging.Formatter("%(asctime)s %(message)s").format(record)
        handler.handle(record)
    entry = query_logs(tmp_path)["entries"][0]
    assert entry["message"] == "formatted message"
    assert entry["fields"] == {"custom_value": "preserved"}


def test_formatter_exception(tmp_path: Path) -> None:
    with file_logs(tmp_path) as handler:
        try:
            raise RuntimeError("failure")
        except RuntimeError:
            logging.getLogger("server.test").exception("operation failed")
    entry = query_logs(tmp_path)["entries"][0]
    assert entry["source"]["path"].endswith("tests/test_log_history.py")
    assert entry["exception"]["stack"][-1]["path"].endswith("tests/test_log_history.py")


def test_cursor_pages(tmp_path: Path) -> None:
    with file_logs(tmp_path) as handler:
        for index in range(5):
            emit(handler, str(index))
    newest = query_logs(tmp_path, limit=2)
    assert [entry["message"] for entry in newest["entries"]] == ["3", "4"]
    assert not newest["truncated"]
    older = query_logs(tmp_path, limit=2, before=newest["next_before"])
    assert [entry["message"] for entry in older["entries"]] == ["1", "2"]
    catchup = query_logs(tmp_path, limit=1, after=older["entries"][0]["id"])
    assert catchup["entries"][0]["message"] == "2"
    assert catchup["latest_id"] == newest["latest_id"]
    assert older["oldest_id"] == newest["oldest_id"]


def test_rotation_history(tmp_path: Path) -> None:
    with file_logs(tmp_path, max_bytes=1) as handler:
        for index in range(6):
            emit(handler, str(index))
    entries = query_logs(tmp_path)["entries"]
    assert [entry["message"] for entry in entries] == ["2", "3", "4", "5"]
    assert query_logs(tmp_path, after=1)["truncated"]


def test_log_context(tmp_path: Path) -> None:
    tokens = structlog.contextvars.bind_contextvars(request_id="request")
    try:
        with file_logs(tmp_path) as handler:
            emit(handler, "restored", entry_id="entry", custom_value="preserved", source_logger="node.agent")
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
    assert query_logs(tmp_path)["entries"][0]["fields"] == {
        "request_id": "request",
        "entry_id": "entry",
        "custom_value": "preserved",
        "source_logger": "node.agent",
    }


def test_polling_excluded(tmp_path: Path) -> None:
    with file_logs(tmp_path) as handler:
        record = logging.LogRecord("server.access", logging.INFO, "", 0, "http request", (), None)
        record.__dict__["route"] = "/api/logs"
        handler.handle(record)
        emit(handler, "restored")
    assert [entry["message"] for entry in query_logs(tmp_path)["entries"]] == ["restored"]


def test_large_cursor_reads(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "logs"
    directory.mkdir()
    records = [
        {"id": index, "level": "warning" if index < 201 else "info", "logger": "test", "message": str(index)}
        for index in range(1, 20001)
    ]
    (directory / "millwright.log").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    parsed = 0
    original = log_history._parse

    def parse(line):
        nonlocal parsed
        parsed += 1
        return original(line)

    monkeypatch.setattr(log_history, "_parse", parse)
    newest = query_logs(tmp_path)
    assert [entry["id"] for entry in newest["entries"]] == list(range(19901, 20001))
    assert parsed < 150
    for cursor in (0, 103, 9950, 19900):
        parsed = 0
        page = query_logs(tmp_path, after=cursor)
        assert [entry["id"] for entry in page["entries"]] == list(range(cursor + 1, cursor + 101))
        assert parsed < 150
    warning = query_logs(tmp_path, levels={"warning"})
    assert [entry["id"] for entry in warning["entries"]] == list(range(101, 201))
    older = query_logs(tmp_path, before=warning["next_before"], levels={"warning"})
    assert [entry["id"] for entry in older["entries"]] == list(range(1, 101))
    assert older["next_before"] is None


def test_filtered_rotation(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    directory.mkdir()
    for number, ids in ((2, range(1, 5)), (1, range(5, 9)), (0, range(9, 13))):
        path = directory / (f"millwright.log.{number}" if number else "millwright.log")
        path.write_text(
            "".join(
                json.dumps(
                    {"id": id_, "level": "critical", "logger": "test", "fields": {"value": "Café"}},
                    ensure_ascii=False,
                )
                + "\n"
                for id_ in ids
            ),
            encoding="utf-8",
        )
    page = query_logs(tmp_path, before=10, limit=3, levels={"error"}, logger="test", search="CAFÉ")
    assert [entry["id"] for entry in page["entries"]] == [7, 8, 9]
    assert page["next_before"] == 7
    assert [entry["id"] for entry in query_logs(tmp_path, after=7, limit=3)["entries"]] == [8, 9, 10]


def test_large_record_boundaries(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    directory.mkdir()
    records = [
        {"id": id_, "level": "info", "logger": "test", "message": "é" * size}
        for id_, size in ((1, 70000), (2, 1), (3, 140000))
    ]
    (directory / "millwright.log").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records) + '{"id":4,',
        encoding="utf-8",
    )
    assert query_logs(tmp_path, before=3)["entries"] == records[:2]
    assert query_logs(tmp_path, after=1)["entries"] == records[1:]
    assert query_logs(tmp_path, before=2)["entries"] == records[:1]
    assert query_logs(tmp_path, after=3)["entries"] == []
