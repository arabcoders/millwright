from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
from typing import TYPE_CHECKING

from common.fs.acquire import DIRECTORY_FLAGS, directory_fd, regular_fd
from pydantic import AwareDatetime, BaseModel, JsonValue, ValidationError

from server.models import utcnow
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

_NAMESPACE = re.compile(r"[a-z][a-z0-9_-]*")
_SHARD = re.compile(r"[0-9a-f]{2}")
_ENTRY = re.compile(r"[0-9a-f]{10}\.json")


class CacheEntry(BaseModel):
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    payload: JsonValue


class JsonCache:
    def __init__(self, root: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.root = root / "objects"
        self.clock = clock if clock is not None else utcnow
        # Cleanup waits for writes, including writes draining through cancellation.
        self._lock = asyncio.Lock()

    def path(self, namespace: str, key: str) -> Path:
        if _NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("Cache namespace must be a safe internal name.")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / namespace / digest[:2] / digest[2:4] / f"{digest[:10]}.json"

    @staticmethod
    def _read(directory: int, name: str) -> CacheEntry | None:
        try:
            with regular_fd(name, directory) as fd, os.fdopen(os.dup(fd), "rb") as handle:
                return CacheEntry.model_validate_json(handle.read())
        except FileNotFoundError, ValidationError:
            return None

    async def read(self, namespace: str, key: str) -> CacheEntry | None:
        path = self.path(namespace, key)

        def read() -> CacheEntry | None:
            try:
                with directory_fd(path.parent) as directory:
                    entry = self._read(directory, path.name)
            except FileNotFoundError:
                return None
            return entry if entry is not None and entry.expires_at > self.clock() else None

        async with self._lock:
            return await wait_workspace_io(read)

    async def write(self, namespace: str, key: str, entry: CacheEntry) -> None:
        path = self.path(namespace, key)
        data = entry.model_dump_json()

        def write() -> None:
            with directory_fd(path.parent, create=True) as directory:
                fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(data)

        async with self._lock:
            await wait_workspace_io(write)

    async def delete(self, namespace: str, key: str) -> None:
        path = self.path(namespace, key)

        def delete() -> None:
            try:
                with directory_fd(path.parent) as directory:
                    os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass

        async with self._lock:
            await wait_workspace_io(delete)

    async def cleanup(self) -> int:
        now = self.clock()

        def walk(directory: int, level: int) -> int:
            removed = 0
            with os.scandir(directory) as entries:
                for item in entries:
                    if level < 3:
                        pattern = _NAMESPACE if level == 0 else _SHARD
                        if pattern.fullmatch(item.name) is None or not item.is_dir(follow_symlinks=False):
                            continue
                        child = os.open(item.name, DIRECTORY_FLAGS, dir_fd=directory)
                        try:
                            removed += walk(child, level + 1)
                        finally:
                            os.close(child)
                        try:
                            os.rmdir(item.name, dir_fd=directory)
                        except OSError as exc:
                            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                                raise
                    elif _ENTRY.fullmatch(item.name) and item.is_file(follow_symlinks=False):
                        entry = self._read(directory, item.name)
                        if entry is None or entry.expires_at <= now:
                            os.unlink(item.name, dir_fd=directory)
                            removed += 1
            return removed

        def cleanup() -> int:
            try:
                with directory_fd(self.root) as directory:
                    return walk(directory, 0)
            except FileNotFoundError:
                return 0

        async with self._lock:
            return await wait_workspace_io(cleanup)
