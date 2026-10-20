from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import stat
import traceback
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, override

import structlog
from common.fs.acquire import directory_fd

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import BinaryIO

_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _open_file(directory: int, name: str, *, append: bool = False) -> BinaryIO:
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_WRONLY | os.O_APPEND | os.O_CREAT if append else os.O_RDONLY)
    handle = os.fdopen(os.open(name, flags, 0o600, dir_fd=directory), "ab" if append else "rb")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        handle.close()
        raise ValueError("log must be an independent regular file")
    return handle


def open_log(state_dir: Path, name: str = "millwright.log") -> BinaryIO:
    if name != "millwright.log" and not (
        name.startswith("millwright.log.")
        and (suffix := name.removeprefix("millwright.log.")).isascii()
        and suffix.isdecimal()
        and int(suffix) > 0
        and str(int(suffix)) == suffix
    ):
        raise ValueError("invalid operational log name")
    with directory_fd(state_dir / "logs") as directory:
        return _open_file(directory, name)


class _ConfinedRotatingHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, state_dir: Path, *, max_bytes: int, backup_count: int) -> None:
        self._directory: int | None = None
        super().__init__(
            state_dir / "logs" / "millwright.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        try:
            with directory_fd(state_dir / "logs", create=True) as directory:
                self._directory = os.dup(directory)
            self.stream = self._open()
        except BaseException:
            self.close()
            raise

    @override
    def _open(self) -> io.TextIOWrapper:
        if self._directory is None:
            raise ValueError("log handler is closed")
        handle = _open_file(self._directory, "millwright.log", append=True)
        return io.TextIOWrapper(handle, encoding=self.encoding, errors=self.errors)

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            if self.stream is not None and os.fstat(self.stream.fileno()).st_nlink != 1:
                raise ValueError("log sink acquired an unsafe alias")
            super().emit(record)
        finally:
            self.release()

    @override
    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.stream is None:
            self.stream = self._open()
        position = self.stream.tell()
        return self.maxBytes > 0 and position > 0 and position + len(f"{self.format(record)}\n") >= self.maxBytes

    @override
    def doRollover(self) -> None:
        if self._directory is None:
            raise ValueError("log handler is closed")
        directory = self._directory
        with ExitStack() as stack:
            files = {}
            for index in range(self.backupCount + 1):
                name = "millwright.log" + (f".{index}" if index else "")
                try:
                    handle = stack.enter_context(_open_file(directory, name))
                except FileNotFoundError:
                    continue
                files[name] = os.fstat(handle.fileno())
            if self.stream:
                info = os.fstat(self.stream.fileno())
                current = files.get("millwright.log")
                if current is None or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("log sink changed before rotation")
                self.stream.close()
                self.stream = None
            if self.backupCount > 0:
                for index in range(self.backupCount, 0, -1):
                    source = "millwright.log" + (f".{index - 1}" if index > 1 else "")
                    destination = f"millwright.log.{index}"
                    if source not in files:
                        continue
                    expected = files[source]
                    if destination in files:
                        self._check_name(destination, files[destination])
                        os.unlink(destination, dir_fd=directory)
                    self._check_name(source, expected)
                    # Linking refuses a newly occupied destination instead of overwriting it.
                    os.link(source, destination, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    self._check_name(destination, expected, links=2)
                    self._check_name(source, expected, links=2)
                    os.unlink(source, dir_fd=directory)
                    del files[source]
                    files[destination] = expected
            if not self.delay:
                self.stream = self._open()

    def _check_name(self, name: str, expected: os.stat_result, *, links: int = 1) -> None:
        info = os.stat(name, dir_fd=self._directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_nlink) != (
            expected.st_dev,
            expected.st_ino,
            links,
        ):
            raise ValueError("log changed during rotation")

    def close(self) -> None:
        self.acquire()
        try:
            try:
                super().close()
            finally:
                if self._directory is not None:
                    directory, self._directory = self._directory, None
                    os.close(directory)
        finally:
            self.release()


class JsonLogFormatter(logging.Formatter):
    def __init__(self, last_id: int = 0) -> None:
        super().__init__()
        self._last_id = last_id

    def format(self, record: logging.LogRecord) -> str:
        values = {**structlog.contextvars.get_contextvars(), **record.__dict__}
        fields = {
            key: value
            for key, value in values.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        event_id = max(int(record.created * 1_000_000), self._last_id + 1)
        self._last_id = event_id
        result: dict[str, Any] = {
            "id": event_id,
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "fields": fields,
            "source": {
                "path": record.pathname,
                "file": record.filename,
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            },
            "process": {"id": record.process, "name": record.processName},
            "thread": {"id": record.thread, "name": record.threadName},
        }
        if record.exc_info and record.exc_info[0] is not None:
            frames = traceback.extract_tb(record.exc_info[2])
            result["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "stack": [
                    {
                        "path": frame.filename,
                        "file": frame.filename.rsplit("/", 1)[-1],
                        "function": frame.name,
                        "line": frame.lineno,
                    }
                    for frame in frames
                ],
            }
        return json.dumps(result, separators=(",", ":"), default=str, ensure_ascii=False)


class _ViewerFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        route = getattr(record, "path", getattr(record, "route", ""))
        return not (record.name == "server.access" and str(route).startswith("/api/logs"))


def install_log_file(
    state_dir: Path, *, max_bytes: int = 10_000_000, backup_count: int = 3
) -> logging.handlers.RotatingFileHandler:
    handler = _ConfinedRotatingHandler(state_dir, max_bytes=max_bytes, backup_count=backup_count)
    try:
        handler.setFormatter(JsonLogFormatter(query_logs(state_dir, limit=1)["latest_id"]))
    except BaseException:
        handler.close()
        raise
    handler.addFilter(_ViewerFilter())
    logging.getLogger().addHandler(handler)
    return handler


def _parse(line: bytes) -> dict[str, Any] | None:
    try:
        return json.loads(line) if line.strip() else None
    except json.JSONDecodeError, UnicodeDecodeError:
        return None


def _reverse_lines(handle: BinaryIO, end: int) -> Iterator[bytes]:
    partial = b""
    while end:
        start = max(0, end - 65536)
        handle.seek(start)
        lines = (handle.read(end - start) + partial).split(b"\n")
        partial = lines.pop(0)
        yield from reversed(lines)
        end = start
    if partial:
        yield partial


def _seek_id(handle: BinaryIO, size: int, cursor: int) -> int:
    # IDs increase with append order. Seek to the first record at or above the cursor.
    low, high = 0, size
    while low < high:
        middle = (low + high) // 2
        handle.seek(middle)
        if middle:
            handle.readline()
        start = handle.tell()
        entry = _parse(handle.readline())
        if entry is None or int(entry["id"]) >= cursor:
            high = middle
        else:
            low = handle.tell()
        if start >= size:
            high = middle
    handle.seek(low)
    if low:
        handle.seek(low - 1)
        if handle.read(1) != b"\n":
            handle.readline()
    while handle.tell() < size:
        start = handle.tell()
        entry = _parse(handle.readline())
        if entry is not None and int(entry["id"]) >= cursor:
            return start
    return size


def query_logs(
    state_dir: Path,
    *,
    limit: int = 100,
    before: int | None = None,
    after: int | None = None,
    levels: set[str] | None = None,
    logger: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    with ExitStack() as stack:
        bounds: list[tuple[BinaryIO, int, int, int]] = []
        for name in ["millwright.log", *[f"millwright.log.{n}" for n in range(1, 4)]]:
            try:
                handle = stack.enter_context(open_log(state_dir, name))
                size = os.fstat(handle.fileno()).st_size
                first = next((entry for line in handle if (entry := _parse(line)) is not None), None)
                last = next(
                    (entry for line in _reverse_lines(handle, size) if (entry := _parse(line)) is not None), None
                )
                if first is not None and last is not None:
                    bounds.append((handle, size, int(first["id"]), int(last["id"])))
            except FileNotFoundError:
                continue
        latest_id = max((last for _, _, _, last in bounds), default=0)
        oldest_id = min((first for _, _, first, _ in bounds), default=None)
        entries: list[dict[str, Any]] = []
        seen: set[int] = set()
        needle = search.casefold() if search else None
        for handle, size, first, last in sorted(bounds, key=lambda bound: bound[2], reverse=after is None):
            if (before is not None and first >= before) or (after is not None and last <= after):
                continue
            cursor = after + 1 if after is not None else before
            offset = _seek_id(handle, size, cursor) if cursor is not None else size
            if after is not None:
                handle.seek(offset)
                lines = iter(lambda handle=handle, size=size: handle.readline(max(0, size - handle.tell())), b"")
            else:
                lines = _reverse_lines(handle, offset)
            for line in lines:
                entry = _parse(line)
                if entry is None:
                    continue
                id_ = int(entry["id"])
                if id_ in seen or (before is not None and id_ >= before) or (after is not None and id_ <= after):
                    continue
                if levels and not (entry["level"] in levels or (entry["level"] == "critical" and "error" in levels)):
                    continue
                if logger and entry["logger"] != logger:
                    continue
                if needle and needle not in line.decode("utf-8").casefold():
                    continue
                seen.add(id_)
                entries.append(entry)
                if len(entries) > limit:
                    break
            if len(entries) > limit:
                break
    has_older = after is None and len(entries) > limit
    truncated = after is not None and oldest_id is not None and after < oldest_id
    entries = entries[:limit]
    if after is None:
        entries.reverse()
    return {
        "entries": entries,
        "next_before": int(entries[0]["id"]) if has_older and entries else None,
        "latest_id": latest_id,
        "oldest_id": oldest_id,
        "truncated": truncated,
    }
