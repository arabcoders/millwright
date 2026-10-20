from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server import cache as cache_module
from server.cache import CacheEntry, JsonCache
from server.workspace import wait_workspace_io

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def cache(tmp_path: Path) -> JsonCache:
    return JsonCache(tmp_path / "cache", clock=lambda: T0)


@pytest.fixture
def entry() -> CacheEntry:
    return CacheEntry(generated_at=T0, expires_at=T0 + timedelta(hours=1), payload={"value": [1, "é", None]})


def seed(path: Path, data: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def test_sharded_keys(cache: JsonCache, tmp_path: Path) -> None:
    keys = ["", "a/b", "a\\b", "../outside", "a|bc", "ab|c", "1", "01", "é", "e\u0301", "\0"]
    paths = [cache.path("probe", key) for key in keys]
    assert len(set(paths)) == len(keys)
    for key, path in zip(keys, paths, strict=True):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        assert path == tmp_path / "cache" / "objects" / "probe" / digest[:2] / digest[2:4] / f"{digest[:10]}.json"
        assert cache.path("probe", key) == path
        assert cache.path("stats", key) != path


@pytest.mark.parametrize("namespace", ["", ".", "..", "../probe", "/probe", "a/b", "a\\b", "Probe", "a\0b"])
def test_namespace_confinement(cache: JsonCache, namespace: str) -> None:
    with pytest.raises(ValueError):
        cache.path(namespace, "key")


@pytest.mark.asyncio
async def test_reuses_entries(cache: JsonCache, entry: CacheEntry) -> None:
    await cache.write("probe", "a|bc", entry)
    other = entry.model_copy(update={"payload": {"value": 2}})
    await cache.write("probe", "ab|c", other)
    await cache.write("stats", "a|bc", other)
    reopened = JsonCache(cache.root.parent, clock=lambda: T0)
    assert await reopened.read("probe", "a|bc") == entry
    assert await reopened.read("probe", "ab|c") == other
    assert await reopened.read("stats", "a|bc") == other

    replacement = entry.model_copy(update={"payload": {"value": 3}})
    await cache.write("probe", "a|bc", replacement)
    assert await reopened.read("probe", "a|bc") == replacement
    await cache.delete("probe", "a|bc")
    await cache.delete("probe", "a|bc")
    assert await reopened.read("probe", "a|bc") is None
    assert await reopened.read("probe", "ab|c") == other
    assert await reopened.read("stats", "a|bc") == other


@pytest.mark.asyncio
async def test_missing_entries(cache: JsonCache, entry: CacheEntry) -> None:
    assert await cache.read("probe", "missing") is None
    await cache.delete("probe", "missing")
    assert await cache.cleanup() == 0
    assert not cache.root.exists()
    await cache.write("probe", "present", entry)
    missing = cache.path("probe", "missing")
    missing.parent.mkdir(parents=True, exist_ok=True)
    assert await cache.read("probe", "missing") is None


@pytest.mark.asyncio
async def test_expiry_boundary(tmp_path: Path, entry: CacheEntry) -> None:
    now = entry.expires_at - timedelta(microseconds=1)
    cache = JsonCache(tmp_path / "cache", clock=lambda: now)
    await cache.write("probe", "key", entry)
    assert await cache.read("probe", "key") == entry
    now = entry.expires_at
    assert await cache.read("probe", "key") is None
    now += timedelta(seconds=1)
    assert await cache.read("probe", "key") is None
    assert cache.path("probe", "key").exists()
    assert await cache.cleanup() == 1
    assert not cache.path("probe", "key").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"generated_at":"bad","expires_at":"bad","payload":{}}',
        b'{"generated_at":"2026-01-01T00:00:00","expires_at":"2026-01-02T00:00:00Z","payload":{}}',
    ],
)
async def test_malformed_misses(cache: JsonCache, data: bytes) -> None:
    path = seed(cache.path("probe", "key"), data)
    assert await cache.read("probe", "key") is None
    assert path.read_bytes() == data
    assert await cache.cleanup() == 1
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write", "delete", "cleanup"])
@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "disk error")])
async def test_operational_errors(
    cache: JsonCache, entry: CacheEntry, monkeypatch: pytest.MonkeyPatch, operation: str, error: OSError
) -> None:
    def denied(_path: Path, *, create: bool = False) -> AbstractContextManager[int]:
        raise error

    monkeypatch.setattr(cache_module, "directory_fd", denied)
    with pytest.raises(OSError) as caught:
        if operation == "read":
            await cache.read("probe", "key")
        elif operation == "write":
            await cache.write("probe", "key", entry)
        elif operation == "delete":
            await cache.delete("probe", "key")
        else:
            await cache.cleanup()
    assert caught.value is error


@pytest.mark.asyncio
async def test_failed_open(cache: JsonCache, entry: CacheEntry, monkeypatch: pytest.MonkeyPatch) -> None:
    await cache.write("probe", "key", entry)
    path = cache.path("probe", "key")
    error = OSError(errno.ENOSPC, "disk full")

    open_file = os.open

    def fail(path, flags, *args, **kwargs):
        if flags & os.O_WRONLY:
            raise error
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(cache_module.os, "open", fail)
    with pytest.raises(OSError) as caught:
        await cache.write("probe", "key", entry.model_copy(update={"payload": {"changed": True}}))
    assert caught.value is error
    assert await cache.read("probe", "key") == entry
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.asyncio
async def test_cleanup_scope(cache: JsonCache, entry: CacheEntry, tmp_path: Path) -> None:
    live = seed(cache.path("probe", "live"), entry.model_dump_json())
    expired = seed(cache.path("probe", "expired"), entry.model_copy(update={"expires_at": T0}).model_dump_json())
    malformed = seed(cache.path("stats", "malformed"), "{")

    unknown = [
        seed(live.with_name("notes.json"), "{"),
        seed(live.with_name(f".{live.stem}.part"), "unfinished"),
        seed(live.with_name(f".{live.stem}.{'z' * 32}.part"), "unfinished"),
        seed(live.parent / "nested" / live.name, "{"),
        seed(cache.root / "probe" / live.name, "{"),
        seed(cache.root / "Bad" / "aa" / "bb" / f"{'a' * 10}.json", "{"),
        seed(cache.root / "probe" / "zz" / "aa" / f"{'a' * 10}.json", "{"),
        seed(cache.root.parent / "downloads" / "probe" / live.parent.parent.name / live.parent.name / live.name, "{"),
        seed(tmp_path / "outside" / live.name, "{"),
    ]
    before = {path: path.read_bytes() for path in unknown}

    assert await cache.cleanup() == 2
    assert live.read_text(encoding="utf-8") == entry.model_dump_json()
    assert all(not path.exists() for path in (expired, malformed))
    assert {path: path.read_bytes() for path in unknown} == before
    assert await cache.cleanup() == 0


@pytest.mark.asyncio
async def test_cleanup_symlinks(cache: JsonCache, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    target = seed(outside / "entry.json", "{")
    path = cache.path("probe", "linked")
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    partial = path.with_name(f".{path.stem}.{'a' * 32}.part")
    partial.symlink_to(target)
    namespace = cache.root / "linked"
    namespace.symlink_to(outside, target_is_directory=True)
    shard = cache.root / "probe" / ("00" if path.parent.parent.name != "00" else "01")
    shard.symlink_to(outside, target_is_directory=True)
    second_shard = path.parent.parent / ("00" if path.parent.name != "00" else "01")
    second_shard.symlink_to(outside, target_is_directory=True)
    external = [
        seed(outside / "aa" / "bb" / f"aabb{'a' * 6}.json", "{"),
        seed(outside / "aa" / f"{shard.name}aa{'a' * 6}.json", "{"),
        seed(outside / f"{path.parent.parent.name}{second_shard.name}{'a' * 6}.json", "{"),
    ]
    digest = path.stem[:4] + "a" * 6
    fifo = path.with_name(f"{digest}.json")
    os.mkfifo(fifo)

    assert await cache.cleanup() == 0
    assert all(link.is_symlink() for link in (path, partial, namespace, shard, second_shard))
    assert target.read_text() == "{"
    assert all(item.read_text() == "{" for item in external)
    assert fifo.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["root", "namespace", "shard", "second_shard"])
async def test_symlink_confinement(cache: JsonCache, entry: CacheEntry, tmp_path: Path, level: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    path = cache.path("probe", "key")
    linked = {
        "root": cache.root,
        "namespace": path.parent.parent.parent,
        "shard": path.parent.parent,
        "second_shard": path.parent,
    }[level]
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(outside, target_is_directory=True)
    for operation in ("read", "write", "delete"):
        with pytest.raises(OSError):
            if operation == "read":
                await cache.read("probe", "key")
            elif operation == "write":
                await cache.write("probe", "key", entry)
            else:
                await cache.delete("probe", "key")
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cleanup", "delete"])
async def test_cancellation_drains(
    cache: JsonCache, entry: CacheEntry, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    attempting = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    open_file = os.open
    writer: asyncio.Task[None] | None = None
    contender: asyncio.Task[int | None] | None = None

    def blocked_open(path, flags, *args, **kwargs):
        if flags & os.O_WRONLY:
            loop.call_soon_threadsafe(writing.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release the cache writer.")
        return open_file(path, flags, *args, **kwargs)

    async def observed_io[T](action: Callable[[], T]) -> T:
        if asyncio.current_task() is not writer:
            assert finished.is_set(), "Cache released its lock before the synchronous writer finished."
            return await wait_workspace_io(action)

        def tracked() -> T:
            try:
                return action()
            finally:
                finished.set()

        return await wait_workspace_io(tracked)

    async def contend() -> int | None:
        attempting.set()
        if operation == "cleanup":
            return await cache.cleanup()
        await cache.delete("probe", "key")
        return None

    monkeypatch.setattr(cache_module.os, "open", blocked_open)
    monkeypatch.setattr(cache_module, "wait_workspace_io", observed_io)
    expired = entry.model_copy(update={"expires_at": T0})
    try:
        writer = asyncio.create_task(cache.write("probe", "key", expired))
        await asyncio.wait_for(writing.wait(), 2)
        writer.cancel()
        contender = asyncio.create_task(contend())
        await asyncio.wait_for(attempting.wait(), 2)
        writer.cancel()
        assert not writer.done()
        assert not contender.done()
        path = cache.path("probe", "key")
        assert not path.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(writer), 2)
        result = await asyncio.wait_for(asyncio.shield(contender), 2)
        assert result == (1 if operation == "cleanup" else None)
        assert finished.is_set()
        assert not path.exists()
        assert not path.parent.exists() or list(path.parent.iterdir()) == []
    finally:
        release.set()
        tasks = [task for task in (writer, contender) if task is not None]
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
