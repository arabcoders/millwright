from __future__ import annotations

import asyncio
import io
import os
import shutil
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from fastapi import HTTPException, Request, UploadFile

from server.api import plugins as plugins_api
from server.api.plugins import upload_plugin
from server.core.auth import Principal
from server.plugins import upload as plugin_upload
from server.plugins.host import PluginHost
from server.plugins.manifest import PluginManifest, load_manifest
from server.plugins.upload import PluginInstallation, UploadError, install_plugin
from tests.test_plugin_lifecycle import MANIFEST_TOML, zip_bytes


def package(policy: str) -> bytes:
    return zip_bytes(
        {
            "plugin.toml": MANIFEST_TOML.replace(b"1.0.0", policy.encode()),
            "helper.py": f'POLICY = "{policy}"\n'.encode(),
            "plugin.py": (
                f'POLICY = "{policy}"\n'
                'def decide(ctx, inputs):\n    return {"type": "match", "facets": {"policy": POLICY}}\n'
            ).encode(),
        }
    )


def contents(path: Path) -> tuple[str, bytes, bytes]:
    return load_manifest(path).version, (path / "helper.py").read_bytes(), (path / "plugin.py").read_bytes()


def expected(data: bytes) -> tuple[str, bytes, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        helper = archive.read("helper.py")
        policy = helper.decode().split('"')[1]
        return policy, helper, archive.read("plugin.py")


class ObservedUploadLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


class UploadHost(PluginHost):
    upload_lock: ObservedUploadLock

    def __init__(self, root: Path) -> None:
        super().__init__([], uploaded_dir=root)
        self.upload_lock = ObservedUploadLock()
        self.registered: list[tuple[str, bytes, bytes]] = []

    @property
    def manifest(self) -> PluginManifest | None:
        state = self._plugins.get("filter.uploaded")
        return state.manifest if state is not None else None

    async def hot_load(self, plugin_dir: Path) -> PluginManifest:
        self.registered.append(contents(plugin_dir))
        self._register(plugin_dir)
        return load_manifest(plugin_dir)

    async def describe_inputs(self, plugin_id: str) -> list[dict]:
        return []


async def upload(host: UploadHost, data: bytes) -> dict:
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(plugin_host=host))})
    with io.BytesIO(data) as stream:
        file = UploadFile(filename="plugin.zip", file=stream)
        return await upload_plugin(request, Principal(user_id=1, username="operator"), file)


@pytest.mark.asyncio
async def test_uploads_keep_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = "alpha", "beta"
    archives = {policy: package(policy) for policy in ("original", first, second)}
    _, installed = install_plugin(archives["original"], tmp_path, bundled_taken=lambda _: False)
    host = UploadHost(tmp_path)
    loop = asyncio.get_running_loop()
    waiting = {policy: asyncio.Event() for policy in (first, second)}
    release = {policy: threading.Event() for policy in (first, second)}
    started = {policy: threading.Event() for policy in (first, second)}
    completed = {policy: threading.Event() for policy in (first, second)}
    copy = plugin_upload._copy_entry

    def paused_copy(
        archive: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path, written: int
    ) -> tuple[int, tuple[int, ...]]:
        if entry.filename != "plugin.py":
            return copy(archive, entry, target, written)
        policy = archive.read("helper.py").decode().split('"')[1]
        loop.call_soon_threadsafe(waiting[policy].set)
        if not release[policy].wait(5):
            raise TimeoutError("upload copy was not released")
        return copy(archive, entry, target, written)

    monkeypatch.setattr("server.plugins.upload._copy_entry", paused_copy)
    install = plugins_api.install_plugin_pending

    def tracked_install(data: bytes, root: Path, *, bundled_taken: Callable[[str], bool]) -> PluginInstallation:
        policy = next(name for name in (first, second) if data == archives[name])
        started[policy].set()
        try:
            return install(data, root, bundled_taken=bundled_taken)
        finally:
            completed[policy].set()

    monkeypatch.setattr("server.api.plugins.install_plugin_pending", tracked_install)
    tasks = [asyncio.create_task(upload(host, archives[first]))]
    outcomes: list[dict | BaseException] = []
    try:
        await asyncio.wait_for(waiting[first].wait(), 5)
        tasks.append(asyncio.create_task(upload(host, archives[second])))
        await asyncio.wait_for(host.upload_lock.waiting.wait(), 5)
        assert not started[second].is_set(), "second installer entered while the first still owned upload_lock"
        release[first].set()
        outcomes.append(await asyncio.wait_for(tasks[0], 5))
        await asyncio.wait_for(waiting[second].wait(), 5)
        release[second].set()
        outcomes.extend(await asyncio.wait_for(asyncio.gather(tasks[1], return_exceptions=True), 5))
        valid = {expected(archives[first]), expected(archives[second])}
        assert all(snapshot in valid for snapshot in host.registered), (
            f"upload registered code from mixed archives: registered={host.registered!r}, "
            f"installed={contents(installed)!r}, outcomes={outcomes!r}"
        )
        assert contents(installed) in valid, "installed plugin combines files from overlapping uploads"
        assert not any(isinstance(outcome, OSError) for outcome in outcomes)
    finally:
        for event in release.values():
            event.set()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        finally:
            for policy in (first, second):
                if started[policy].is_set():
                    assert await asyncio.to_thread(completed[policy].wait, 5)
            await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
async def test_cancel_drains_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, updated = package("original"), package("updated")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    host = UploadHost(tmp_path)
    loop = asyncio.get_running_loop()
    waiting = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    copy = plugin_upload._copy_entry

    def paused_copy(
        archive: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path, written: int
    ) -> tuple[int, tuple[int, ...]]:
        if entry.filename != "plugin.py":
            return copy(archive, entry, target, written)
        loop.call_soon_threadsafe(waiting.set)
        try:
            if not release.wait(5):
                raise TimeoutError("upload copy was not released")
            return copy(archive, entry, target, written)
        finally:
            exited.set()

    monkeypatch.setattr("server.plugins.upload._copy_entry", paused_copy)
    install = plugins_api.install_plugin_pending
    completed = threading.Event()

    def tracked_install(data: bytes, root: Path, *, bundled_taken: Callable[[str], bool]) -> PluginInstallation:
        try:
            return install(data, root, bundled_taken=bundled_taken)
        finally:
            completed.set()

    monkeypatch.setattr("server.api.plugins.install_plugin_pending", tracked_install)
    task = asyncio.create_task(upload(host, updated))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        checkpoint = loop.create_future()
        loop.call_soon(checkpoint.set_result, None)
        await checkpoint
        abandoned = task.done() and not completed.is_set()
        assert contents(installed) == expected(original)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert await asyncio.to_thread(completed.wait, 5)
        assert contents(installed) in {expected(original), expected(updated)}
        assert not abandoned, (
            "cancelled upload returned while its filesystem worker still owned an unfinished mutation: "
            f"installed={contents(installed)!r}, registered={host.registered!r}"
        )
    finally:
        release.set()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        finally:
            if waiting.is_set():
                assert await asyncio.to_thread(exited.wait, 5)
                assert await asyncio.to_thread(completed.wait, 5)
            await asyncio.wait_for(host.aclose(), 2)


def test_upload_restores_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = package("original")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    replaced = tmp_path / ".filter.uploaded.replaced"
    move = plugin_upload.move_plugin_directory

    def fail_publication(root: Path, root_fd: int, source: str, destination: str, directory_fd: int) -> None:
        if source.endswith(".partial"):
            raise OSError("publication failed")
        move(root, root_fd, source, destination, directory_fd)

    monkeypatch.setattr(plugin_upload, "move_plugin_directory", fail_publication)
    with pytest.raises(OSError):
        install_plugin(package("updated"), tmp_path, bundled_taken=lambda _: False)
    assert contents(installed) == expected(original)
    assert not replaced.exists()
    assert contents(tmp_path / ".filter.uploaded.partial") == expected(package("updated"))


@pytest.mark.parametrize("role", ["partial", "replaced"])
def test_upload_refuses_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    original = package("original")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    recovery = tmp_path / f".filter.uploaded.{role}"
    os.replace(installed, recovery)

    def fail_copy(*_args: object):
        raise OSError("extraction failed")

    monkeypatch.setattr(plugin_upload, "_copy_entry", fail_copy)
    with pytest.raises(UploadError):
        install_plugin(package("updated"), tmp_path, bundled_taken=lambda _: False)
    assert not installed.exists()
    assert contents(recovery) == expected(original)


def test_upload_extraction_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = package("original")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)

    def fail_copy(*_args: object):
        raise OSError("extraction failed")

    monkeypatch.setattr(plugin_upload, "_copy_entry", fail_copy)
    with pytest.raises(OSError):
        install_plugin(package("updated"), tmp_path, bundled_taken=lambda _: False)
    assert contents(installed) == expected(original)
    assert (tmp_path / ".filter.uploaded.partial").is_dir()


def test_upload_foreign_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, updated = package("original"), package("updated")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    move = plugin_upload.move_plugin_directory

    def occupy_destination(root: Path, root_fd: int, source: str, destination: str, directory_fd: int) -> None:
        if source.endswith(".partial"):
            installed.mkdir()
            (installed / "personal.txt").write_bytes(b"foreign old-bytes")
        move(root, root_fd, source, destination, directory_fd)

    monkeypatch.setattr(plugin_upload, "move_plugin_directory", occupy_destination)
    with pytest.raises(FileExistsError):
        install_plugin(updated, tmp_path, bundled_taken=lambda _: False)
    assert (installed / "personal.txt").read_bytes() == b"foreign old-bytes"
    assert contents(tmp_path / ".filter.uploaded.replaced") == expected(original)
    assert contents(tmp_path / ".filter.uploaded.partial") == expected(updated)


def test_upload_blocks_ambiguous(tmp_path: Path) -> None:
    original, existing_package = package("original"), package("existing")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    replaced = tmp_path / ".filter.uploaded.replaced"
    shutil.copytree(installed, replaced)
    for name in ("plugin.toml", "helper.py", "plugin.py"):
        with zipfile.ZipFile(io.BytesIO(existing_package)) as archive:
            (installed / name).write_bytes(archive.read(name))
    with pytest.raises(UploadError):
        install_plugin(package("updated"), tmp_path, bundled_taken=lambda _: False)
    assert contents(installed) == expected(existing_package)
    assert contents(replaced) == expected(original)


def test_upload_retains_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = package("original")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    replaced = tmp_path / ".filter.uploaded.replaced"
    move = plugin_upload.move_plugin_directory

    def fail_replace(root: Path, root_fd: int, source: str, destination: str, directory_fd: int) -> None:
        if source.endswith((".partial", ".replaced")):
            raise OSError("publication or rollback failed")
        move(root, root_fd, source, destination, directory_fd)

    with monkeypatch.context() as patch:
        patch.setattr(plugin_upload, "move_plugin_directory", fail_replace)
        with pytest.raises(OSError):
            install_plugin(package("updated"), tmp_path, bundled_taken=lambda _: False)
    assert not installed.exists()
    assert contents(replaced) == expected(original)

    def fail_copy(*_args: object) -> tuple[int, tuple[int, ...]]:
        raise OSError("retry extraction failed")

    monkeypatch.setattr("server.plugins.upload._copy_entry", fail_copy)
    with pytest.raises(UploadError):
        install_plugin(package("updated"), tmp_path, bundled_taken=lambda _: False)
    assert not installed.exists()
    assert contents(replaced) == expected(original)
    assert contents(tmp_path / ".filter.uploaded.partial") == expected(package("updated"))


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["hot_load", "payload"])
async def test_cancel_waits_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    original, first, second = package("original"), package("alpha"), package("beta")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    host = UploadHost(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    installed_data: list[bytes] = []
    install = plugins_api.install_plugin_pending

    def tracked_install(data: bytes, root: Path, *, bundled_taken: Callable[[str], bool]) -> PluginInstallation:
        installed_data.append(data)
        return install(data, root, bundled_taken=bundled_taken)

    monkeypatch.setattr("server.api.plugins.install_plugin_pending", tracked_install)
    hot_load = host.hot_load
    describe = host.describe_inputs

    async def paused_hot_load(path: Path) -> PluginManifest:
        if load_manifest(path).version == "alpha":
            entered.set()
            await release.wait()
        return await hot_load(path)

    async def paused_describe(plugin_id: str) -> list[dict]:
        if host.manifest is not None and host.manifest.version == "alpha":
            entered.set()
            await release.wait()
        return await describe(plugin_id)

    if boundary == "hot_load":
        monkeypatch.setattr(host, "hot_load", paused_hot_load)
    else:
        monkeypatch.setattr(host, "describe_inputs", paused_describe)
    tasks = [asyncio.create_task(upload(host, first))]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        tasks.append(asyncio.create_task(upload(host, second)))
        await asyncio.wait_for(host.upload_lock.waiting.wait(), 5)
        for _ in range(3):
            tasks[0].cancel()
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
            await checkpoint
            assert not tasks[0].done(), "repeated cancellation abandoned upload registration"
            assert host.upload_lock.locked()
            assert installed_data == [first], "queued upload entered before registration and payload completed"
            assert contents(installed) == expected(first)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], dict)
        assert host.registered == [expected(first), expected(second)]
        assert contents(installed) == expected(second)
        assert not host.upload_lock.locked()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["hot_load", "payload"])
async def test_upload_rolls_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    original, updated = package("original"), package("updated")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    host = UploadHost(tmp_path)
    host._plugins["filter.uploaded"].stopped = True
    host._disabled.add("filter.uploaded")
    acquired: list[PluginInstallation] = []
    install = plugins_api.install_plugin_pending

    def acquire(data: bytes, root: Path, *, bundled_taken: Callable[[str], bool]) -> PluginInstallation:
        installation = install(data, root, bundled_taken=bundled_taken)
        acquired.append(installation)
        return installation

    monkeypatch.setattr(plugins_api, "install_plugin_pending", acquire)
    load = host.hot_load
    stop = host._stop_runner
    stopped: list[str] = []

    async def stop_before_rollback(state):
        stopped.append(state.manifest.version)
        if state.manifest.version == "updated":
            assert contents(installed) == expected(updated)
            assert contents(tmp_path / ".filter.uploaded.replaced") == expected(original)
        await stop(state)

    async def fail_load(path: Path) -> PluginManifest:
        await load(path)
        raise RuntimeError("load failed")

    async def fail_payload(_host, _info):
        raise RuntimeError("payload failed")

    monkeypatch.setattr(host, "_stop_runner", stop_before_rollback)
    if boundary == "hot_load":
        monkeypatch.setattr(host, "hot_load", fail_load)
    else:
        monkeypatch.setattr(plugins_api, "_plugin_payload", fail_payload)
    try:
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(upload(host, updated), 5)
        assert error.value.status_code == 500
        assert contents(installed) == expected(original)
        assert host.manifest is not None and host.manifest.version == "original"
        assert host._plugins["filter.uploaded"].stopped
        assert "filter.uploaded" in host._disabled
        assert stopped == ["original", "updated"]
        assert not (tmp_path / ".filter.uploaded.partial").exists()
        assert not (tmp_path / ".filter.uploaded.replaced").exists()
        assert len(acquired) == 1
        for fd in (acquired[0].root_fd, acquired[0].installed_fd, acquired[0].previous_fd):
            assert fd is not None
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["previous", "loaded"])
async def test_upload_retains_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    original, updated = package("original"), package("updated")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    host = UploadHost(tmp_path)
    stop = host._stop_runner

    async def unconfirmed_stop(state):
        if state.manifest.version == ("original" if boundary == "previous" else "updated"):
            state.retiring = True
            raise RuntimeError("runner exit unconfirmed")
        await stop(state)

    async def fail_payload(_host, _info):
        raise RuntimeError("payload failed")

    monkeypatch.setattr(host, "_stop_runner", unconfirmed_stop)
    monkeypatch.setattr(plugins_api, "_plugin_payload", fail_payload)
    try:
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(upload(host, updated), 5)
        assert error.value.status_code == 500
        assert "filter.uploaded" in host._disabled
        assert host._plugins["filter.uploaded"].retiring
        if boundary == "previous":
            assert contents(installed) == expected(original)
            assert not (tmp_path / ".filter.uploaded.partial").exists()
            assert not (tmp_path / ".filter.uploaded.replaced").exists()
        else:
            assert contents(installed) == expected(updated)
            assert contents(tmp_path / ".filter.uploaded.replaced") == expected(original)
            assert host.manifest is not None and host.manifest.version == "updated"
    finally:
        monkeypatch.setattr(host, "_stop_runner", stop)
        await asyncio.wait_for(host.aclose(), 2)


@pytest.mark.asyncio
async def test_upload_commit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, updated = package("original"), package("updated")
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    host = UploadHost(tmp_path)
    remove = plugin_upload.remove_plugin_directory

    def partial_cleanup(root, root_fd, name, directory_fd, snapshot):
        if name.endswith(".replaced"):
            os.unlink("helper.py", dir_fd=directory_fd)
            raise OSError("cleanup failed after a partial deletion")
        remove(root, root_fd, name, directory_fd, snapshot)

    monkeypatch.setattr(plugin_upload, "remove_plugin_directory", partial_cleanup)
    try:
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(upload(host, updated), 5)
        assert error.value.status_code == 500
        assert contents(installed) == expected(updated)
        assert host.manifest is not None and host.manifest.version == "updated"
        assert "filter.uploaded" not in host._disabled
        replaced = tmp_path / ".filter.uploaded.replaced"
        assert load_manifest(replaced).version == "original"
        assert not (replaced / "helper.py").exists()
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            assert (replaced / "plugin.py").read_bytes() == archive.read("plugin.py")
        assert not (tmp_path / ".filter.uploaded.partial").exists()
    finally:
        await asyncio.wait_for(host.aclose(), 2)
