from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from common.diagnostics import error_traceback
from common.protocol import NodeLog, NodeLogs
from common.protocol.messages import NODE_LOG_ERROR_LIMIT, NODE_LOG_TRACEBACK_LIMIT

if TYPE_CHECKING:
    from collections.abc import Callable


_FIELDS = ("job_id", "attempt_id", "stage", "outcome", "duration_ms", "count", "error_class")


class HubLogHandler(logging.Handler):
    def __init__(self, loop: Any, send: Callable[[dict], None]) -> None:
        super().__init__(level=logging.DEBUG)
        self._loop = loop
        self._thread = threading.get_ident()
        self._send = send
        self._closed = False
        self._pending: list[dict] = []
        self._scheduled = False
        self._reservations = 0
        self._reservation_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if (
            self._closed
            or not record.name.startswith("node.")
            or getattr(record, "_from_node", False)
            or getattr(record, "_hub_log_skip", False)
        ):
            return
        message = record.getMessage()
        fields = {field: getattr(record, field) for field in _FIELDS if hasattr(record, field)}
        traceback = getattr(record, "traceback", None)
        if traceback is None and record.exc_info and record.exc_info[1] is not None:
            traceback = error_traceback(record.exc_info[1])
        for name, value, limit in (
            ("error", getattr(record, "error", None), NODE_LOG_ERROR_LIMIT),
            ("traceback", traceback, NODE_LOG_TRACEBACK_LIMIT),
        ):
            if value is None:
                continue
            # Keep oversized diagnostics from rejecting the entire log entry.
            marker = f"\n[{name} truncated]"
            if len(value) > limit:
                value = value[: limit - len(marker)] + marker
            fields[name] = value
        for offset in range(0, len(message), 2048):
            self._emit_chunk(record, message[offset : offset + 2048], fields)

    def _emit_chunk(self, record: logging.LogRecord, message: str, fields: dict) -> None:
        try:
            entry = NodeLog(
                logger=record.name,
                level=record.levelname
                if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                else "INFO",
                message=message,
                **fields,
            ).model_dump(mode="json")
        except TypeError, ValueError:
            return
        with self._reservation_lock:
            if self._reservations >= 100:
                return
            self._reservations += 1
        try:
            if threading.get_ident() == self._thread:
                self._enqueue(entry)
            else:
                self._loop.call_soon_threadsafe(self._enqueue, entry)
        except RuntimeError:
            with self._reservation_lock:
                self._reservations -= 1

    def _enqueue(self, entry: dict) -> None:
        with self._reservation_lock:
            self._reservations -= 1
        if self._closed:
            return
        if len(self._pending) >= 100:
            return
        self._pending.append(entry)
        if not self._scheduled:
            self._scheduled = True
            self._loop.call_soon(self.flush)

    def flush(self) -> None:
        self._scheduled = False
        if not self._pending or self._closed:
            return
        entries, self._pending = self._pending, []
        groups: dict[tuple[int | None, int | None], list[dict]] = {}
        for entry in entries:
            key = entry.get("job_id"), entry.get("attempt_id")
            groups.setdefault(key, []).append(entry)
        for group in groups.values():
            for offset in range(0, len(group), 50):
                self._send(NodeLogs(entries=group[offset : offset + 50]).model_dump(mode="json"))

    def close(self) -> None:
        self.flush()
        self._closed = True
        self._pending.clear()
        super().close()
