from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

CHUNK = 256 * 1024
CACHE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}


class MediaRangeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__("invalid range")
        self.size = size


def parse_range(header: str | None, size: int) -> tuple[int, int, int]:
    """Return (start, end, status) for a single bytes range."""
    start, end, status = 0, max(size - 1, 0), 200
    if size == 0:
        return 0, -1, 200
    if header is None:
        return start, end, status
    if not header.startswith("bytes=") or "," in header:
        raise MediaRangeError(size)
    left, separator, right = header[len("bytes=") :].partition("-")
    if not separator:
        raise MediaRangeError(size)
    try:
        if left:
            start = int(left)
            end = int(right) if right else size - 1
        elif right:
            length = int(right)
            start, end = max(size - length, 0), size - 1
        else:
            raise MediaRangeError(size)
    except ValueError as exc:
        raise MediaRangeError(size) from exc
    if start < 0 or start > end or start >= size:
        raise MediaRangeError(size)
    return start, min(end, size - 1), 206


def stream_range(fd: int, start: int, end: int) -> Generator[bytes]:
    """Yield a byte range from an open file."""
    remaining = end + 1 - start
    position = start
    while remaining > 0:
        chunk = os.pread(fd, min(CHUNK, remaining), position)
        if not chunk:
            break
        position += len(chunk)
        remaining -= len(chunk)
        yield chunk
    if remaining > 0:
        raise FileNotFoundError("media truncated during playback")


def head_headers(mimetype: str, size: int) -> dict[str, str]:
    return {
        "Content-Type": mimetype,
        "Content-Length": str(size),
        "Accept-Ranges": "bytes",
        **CACHE_HEADERS,
    }
