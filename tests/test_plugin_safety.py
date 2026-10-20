from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from server.plugins.host import PluginHost
from server.plugins import host as plugin_host_module
from server.plugins import upload as plugin_upload
from server.plugins.upload import UploadError, install_plugin
from tests.test_plugin_host import ctx_dict, stage_all, stop_runner, video
from tests.test_plugin_lifecycle import MANIFEST_TOML, zip_bytes


class ObservedLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()
        self.lifecycle_started = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.waiting.set()
            task = asyncio.current_task()
            if task is not None and task.get_name() == "plugin-lifecycle":
                self.lifecycle_started.set()
        return await super().acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "disable", "restart", "reload", "watch", "close"])
@pytest.mark.parametrize("describe", [False, True], ids=["decide", "describe"])
async def test_waiter_obeys_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, describe: bool
) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    spawn = AsyncMock(wraps=host._spawn)
    monkeypatch.setattr(host, "_spawn", spawn)
    lock = ObservedLock()
    state.lock = lock
    await lock.acquire()
    task = asyncio.create_task(
        host.describe_inputs("filter.echo")
        if describe
        else host.evaluate("filter.echo", ctx_dict(streams=[video()]), {})
    )
    stop_state = host._stop_runner

    async def observed_stop(state: Any) -> None:
        current = asyncio.current_task()
        if current is not None and (
            current is host._close_task if operation == "close" else current.get_name() == "plugin-lifecycle"
        ):
            lock.lifecycle_started.set()
        await stop_state(state)

    monkeypatch.setattr(host, "_stop_runner", observed_stop)

    async def mutate() -> None:
        if operation == "stop":
            await asyncio.wait_for(host.stop("filter.echo"), 5)
        elif operation == "disable":
            await asyncio.wait_for(host.set_enabled("filter.echo", False), 5)
        elif operation == "close":
            await asyncio.wait_for(host.aclose(), 5)
        else:
            (tmp_path / "filter.echo" / "plugin.py").write_text(
                'def decide(ctx, inputs):\n    return {"type": "veto", "reason": "updated policy"}\n',
                encoding="utf-8",
            )
            if operation == "restart":
                await asyncio.wait_for(host.restart("filter.echo"), 5)
            elif operation == "reload":
                await asyncio.wait_for(host.hot_load(tmp_path / "filter.echo"), 5)
            else:
                await asyncio.wait_for(host._apply_changes({tmp_path / "filter.echo" / "plugin.py"}), 5)

    control = None
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        control = asyncio.create_task(mutate(), name="plugin-lifecycle")
        await asyncio.wait_for(lock.lifecycle_started.wait(), 5)
        completed_before_dispatch = control.done()
        lock.release()
        result = await asyncio.wait_for(task, 5)
        await asyncio.wait_for(control, 5)
        await asyncio.wait_for(host.aclose(), 5)
        assert state.runner is None or state.runner.returncode is not None
        if operation in {"stop", "disable", "close"} and (completed_before_dispatch or operation == "close"):
            if describe:
                assert result is None
            else:
                assert result.type == "error"
                assert result.error is not None
        if operation == "close":
            assert host._closed and state.runner is None
            spawn.assert_not_awaited()
    finally:
        if lock.locked():
            lock.release()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        if control is not None:
            if not control.done():
                control.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(control, 10)
        await asyncio.wait_for(host.aclose(), 5)
        if state.runner is not None:
            await stop_runner(state.runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("describe", [False, True], ids=["decide", "describe"])
async def test_protocol_forgets_response(tmp_path: Path, describe: bool) -> None:
    stage_all(tmp_path, "filter.echo")
    code = (
        "def decide(ctx, inputs):\n"
        '    if inputs.get("debug"):\n'
        '        print("diagnostic", flush=True)\n'
        '    return {"type": "match", "facets": {"mode": inputs["mode"]}}\n'
    )
    if describe:
        code = 'print("diagnostic", flush=True)\n' + code
    (tmp_path / "filter.echo" / "plugin.py").write_text(code, encoding="utf-8")
    host = PluginHost([tmp_path])
    try:
        if describe:
            assert await asyncio.wait_for(host.describe_inputs("filter.echo"), 5) is None
        else:
            first = await asyncio.wait_for(
                host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "old", "debug": True}), 5
            )
            assert first.type == "error"
        for mode in ("new", "latest"):
            decision = await asyncio.wait_for(
                host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": mode}), 5
            )
            assert decision.type == "error" or decision.facets == {"mode": mode}
    finally:
        await asyncio.wait_for(host.aclose(), 5)


def test_upload_retains_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_code = b'def decide(ctx, inputs):\n    return {"type": "match", "facets": {"policy": "original"}}\n'
    original = zip_bytes({"plugin.toml": MANIFEST_TOML, "plugin.py": original_code})
    updated = zip_bytes({"plugin.toml": MANIFEST_TOML, "plugin.py": b"updated policy\n"})
    _, installed = install_plugin(original, tmp_path, bundled_taken=lambda _: False)
    move = plugin_upload.move_plugin_directory

    def fail_publish(root: Path, root_fd: int, source: str, destination: str, directory_fd: int) -> None:
        if source.endswith(".partial"):
            raise OSError("publish failed")
        move(root, root_fd, source, destination, directory_fd)

    with monkeypatch.context() as patch:
        patch.setattr(plugin_upload, "move_plugin_directory", fail_publish)
        with pytest.raises(OSError):
            install_plugin(updated, tmp_path, bundled_taken=lambda _: False)
    assert (installed / "plugin.py").read_bytes() == original_code
    assert not (tmp_path / ".filter.uploaded.replaced").exists()
    partial = tmp_path / ".filter.uploaded.partial"
    assert (partial / "plugin.py").read_bytes() == b"updated policy\n"
    assert (partial / "plugin.toml").read_bytes() == MANIFEST_TOML
    copied = False

    def fail_copy(*_args: object) -> tuple[int, tuple[int, ...]]:
        nonlocal copied
        copied = True
        raise OSError("extraction failed")

    with monkeypatch.context() as patch:
        patch.setattr("server.plugins.upload._copy_entry", fail_copy)
        with pytest.raises(UploadError):
            install_plugin(updated, tmp_path, bundled_taken=lambda _: False)
    assert not copied
    assert (partial / "plugin.py").read_bytes() == b"updated policy\n"
    assert (partial / "plugin.toml").read_bytes() == MANIFEST_TOML
    retained = [path for path in tmp_path.rglob("plugin.py") if path.read_bytes() == original_code]
    assert retained, "failed upload and retry discarded the last installed plugin"
    assert (installed / "plugin.py").read_bytes() == original_code


@pytest.mark.asyncio
@pytest.mark.parametrize("watch", [False, True], ids=["discover", "watch"])
async def test_upload_partial_ignored(tmp_path: Path, watch: bool) -> None:
    code = b'def decide(ctx, inputs):\n    return {"type": "match", "facets": {"policy": "original"}}\n'
    _, installed = install_plugin(
        zip_bytes({"plugin.toml": MANIFEST_TOML, "plugin.py": code}), tmp_path, bundled_taken=lambda _: False
    )
    partial = tmp_path / ".filter.uploaded.partial"
    host = PluginHost([], uploaded_dir=tmp_path)
    runner = None
    try:
        if watch:
            decision = await host.evaluate("filter.uploaded", ctx_dict(streams=[video()]), {})
            assert decision.facets == {"policy": "original"}
            runner = host._plugins["filter.uploaded"].runner
        shutil.copytree(installed, partial)
        if watch:
            await host._apply_changes({partial / "plugin.toml"})
            assert host._plugins["filter.uploaded"].dir == installed
        else:
            shutil.rmtree(installed)
            await host.aclose()
            host = PluginHost([], uploaded_dir=tmp_path)
            assert host.describe("filter.uploaded") is None
        await host.aclose()
        assert runner is None or runner.returncode is not None
    finally:
        await host.aclose()
        if runner is not None:
            await stop_runner(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "disable", "restart", "reload", "watch", "close"])
@pytest.mark.parametrize("call", ["decide", "describe", "validate"])
async def test_spawn_obeys_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, call: str
) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    spawned = asyncio.Event()
    release = asyncio.Event()
    changing = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    spawn = host._spawn

    async def paused_spawn(state: Any) -> asyncio.subprocess.Process:
        process = await spawn(state)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(host, "_spawn", paused_spawn)

    async def mutate() -> None:
        changing.set()
        if operation == "stop":
            await host.stop("filter.echo")
        elif operation == "disable":
            await host.set_enabled("filter.echo", False)
        elif operation == "restart":
            await host.restart("filter.echo")
        elif operation == "reload":
            await host.hot_load(tmp_path / "filter.echo")
        elif operation == "watch":
            await host._apply_changes({tmp_path / "filter.echo" / "plugin.py"})
        else:
            await host.aclose()

    control = None
    if call == "decide":
        task = asyncio.create_task(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {}))
    elif call == "describe":
        task = asyncio.create_task(host.describe_inputs("filter.echo"))
    else:
        task = asyncio.create_task(host.validate_inputs("filter.echo", {}))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        control = asyncio.create_task(mutate())
        await asyncio.wait_for(changing.wait(), 5)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, control), 10)
        assert all(process.returncode is not None for process in processes), (
            f"{operation} completed but a runner queued before the lifecycle change survived"
        )
    finally:
        release.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, *([control] if control is not None else []), return_exceptions=True), 10
            )
        finally:
            try:
                await asyncio.wait_for(host.aclose(), 5)
            finally:
                for process in processes:
                    await stop_runner(process)


@pytest.mark.asyncio
async def test_validation_avoids_decide(tmp_path: Path) -> None:
    stage_all(tmp_path, "filter.echo")
    (tmp_path / "filter.echo" / "plugin.py").write_text(
        "from pydantic import BaseModel, model_validator\n"
        "class Inputs(BaseModel):\n"
        "    lower: int = 0\n"
        "    upper: int = 10\n"
        "    @model_validator(mode='after')\n"
        "    def ordered(self):\n"
        "        if self.lower >= self.upper:\n"
        "            raise ValueError('lower must precede upper')\n"
        "        return self\n"
        "INPUTS = Inputs\n"
        "def decide(ctx, inputs):\n"
        "    raise AssertionError('validation must not invoke decide')\n",
        encoding="utf-8",
    )
    host = PluginHost([tmp_path])
    try:
        assert await asyncio.wait_for(host.validate_inputs("filter.echo", {}), 5) == []
        errors = await asyncio.wait_for(host.validate_inputs("filter.echo", {"lower": 10, "upper": 5}), 5)
        assert len(errors) == 1
        assert await asyncio.wait_for(host.validate_inputs("filter.echo", {"lower": 1, "upper": 2}), 5) == []
    finally:
        await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
async def test_validation_discards_response(tmp_path: Path) -> None:
    stage_all(tmp_path, "filter.echo")
    (tmp_path / "filter.echo" / "plugin.py").write_text(
        "from pydantic import BaseModel, model_validator\n"
        "class Inputs(BaseModel):\n"
        "    debug: bool = False\n"
        "    @model_validator(mode='after')\n"
        "    def diagnostic(self):\n"
        "        if self.debug:\n"
        "            print('diagnostic', flush=True)\n"
        "        return self\n"
        "INPUTS = Inputs\n"
        "def decide(ctx, inputs):\n"
        "    return {'type': 'match', 'facets': {}}\n",
        encoding="utf-8",
    )
    host = PluginHost([tmp_path])
    try:
        assert await asyncio.wait_for(host.validate_inputs("filter.echo", {"debug": True}), 5)
        errors = await asyncio.wait_for(host.validate_inputs("filter.echo", {"unknown": True}), 5)
        assert errors
        assert await asyncio.wait_for(host.validate_inputs("filter.echo", {}), 5) == []
    finally:
        await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["decide", "describe", "validate"])
async def test_acquisition_drains_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    spawned, stopping = asyncio.Event(), asyncio.Event()
    release_spawn, release_stop = asyncio.Event(), asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    requests: list[dict] = []
    spawn = host._spawn
    kill = plugin_host_module._kill_runner
    roundtrip = host._roundtrip

    async def paused_spawn(state: Any) -> asyncio.subprocess.Process:
        process = await spawn(state)
        processes.append(process)
        spawned.set()
        await release_spawn.wait()
        return process

    async def paused_kill(process: asyncio.subprocess.Process) -> None:
        stopping.set()
        await release_stop.wait()
        await kill(process)

    async def observed_roundtrip(process: asyncio.subprocess.Process, request: dict) -> bytes:
        requests.append(request)
        return await roundtrip(process, request)

    monkeypatch.setattr(host, "_spawn", paused_spawn)
    monkeypatch.setattr(plugin_host_module, "_kill_runner", paused_kill)
    monkeypatch.setattr(host, "_roundtrip", observed_roundtrip)
    if call == "decide":
        task = asyncio.create_task(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {}))
    elif call == "describe":
        task = asyncio.create_task(host.describe_inputs("filter.echo"))
    else:
        task = asyncio.create_task(host.validate_inputs("filter.echo", {}))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        for _ in range(3):
            task.cancel()
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
            await checkpoint
            assert not task.done(), "cancellation abandoned an unfinished spawn"
        release_spawn.set()
        await asyncio.wait_for(stopping.wait(), 5)
        for _ in range(3):
            task.cancel()
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
            await checkpoint
            assert not task.done(), "repeated cancellation abandoned acquisition cleanup"
        release_stop.set()
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        assert isinstance(result[0], asyncio.CancelledError)
        assert all(process.returncode is not None for process in processes)
        assert requests == [], "cancelled acquisition dispatched a runner request"
        assert host._plugins["filter.echo"].runner is None
        assert not host._plugins["filter.echo"].retiring
    finally:
        release_spawn.set()
        release_stop.set()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        finally:
            try:
                await asyncio.wait_for(host.aclose(), 5)
            finally:
                for process in processes:
                    await stop_runner(process)


class RequestPipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass


class PendingProcess:
    def __init__(self) -> None:
        self.pid = 123
        self.returncode: int | None = None
        self.signalled = asyncio.Event()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.stdin = RequestPipe()
        self.stdout = asyncio.StreamReader()

    def kill(self) -> None:
        self.signalled.set()

    async def wait(self) -> int:
        self.waiting.set()
        await self.release.wait()
        self.returncode = -9
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close", "reload", "protocol"])
async def test_teardown_waits_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    process = PendingProcess()
    monkeypatch.setattr(state, "runner", process)

    async def malformed(*_args: object) -> bytes:
        return b"diagnostic\n"

    monkeypatch.setattr(host, "_roundtrip", malformed)
    if operation == "close":
        task = asyncio.create_task(host.aclose())
    elif operation == "reload":
        task = asyncio.create_task(host.hot_load(tmp_path / "filter.echo"))
    else:
        task = asyncio.create_task(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {}))
    abandoned = False
    try:
        await asyncio.wait_for(process.waiting.wait(), 5)
        assert process.signalled.is_set()
        for _ in range(3):
            task.cancel()
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
            await checkpoint
            abandoned |= task.done() and process.returncode is None and state.runner is not process
        process.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        assert not abandoned, f"cancelled {operation} stopped tracking the runner before exit was confirmed"
        assert state.runner is None, "confirmed teardown retained the exited runner"
        assert not state.retiring, "confirmed teardown left the registration quarantined"
    finally:
        process.release.set()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        finally:
            await asyncio.wait_for(process.wait(), 5)
            await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
async def test_unconfirmed_runner_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    process = PendingProcess()
    monkeypatch.setattr(state, "runner", process)

    async def unconfirmed() -> int:
        raise TimeoutError("runner exit was not confirmed")

    monkeypatch.setattr(process, "wait", unconfirmed)
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(host.stop("filter.echo"), 5)
        assert process.signalled.is_set()
        assert process.returncode is not None or state.runner is process, "unconfirmed runner exit lost its owner"
    finally:
        process.release.set()
        monkeypatch.setattr(process, "wait", lambda: PendingProcess.wait(process))
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["decide", "validate"])
async def test_unconfirmed_protocol_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str) -> None:
    stage_all(tmp_path, "filter.echo")
    (tmp_path / "filter.echo" / "plugin.py").write_text(
        "from pydantic import BaseModel\n"
        "class Inputs(BaseModel):\n"
        "    mode: str = 'old'\n"
        "INPUTS = Inputs\n"
        "def decide(ctx, inputs):\n"
        "    return {'type': 'match', 'facets': {'mode': inputs.mode}}\n",
        encoding="utf-8",
    )
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    process = PendingProcess()
    monkeypatch.setattr(state, "runner", process)
    responses = iter(
        [b"diagnostic\n", b'{"decision":{"type":"match","facets":{"mode":"old"}}}\n']
        if call == "decide"
        else [b"diagnostic\n", b'{"valid":true}\n']
    )
    sent: list[object] = []

    async def response(*_args: object) -> bytes:
        sent.append(_args)
        return next(responses)

    async def unconfirmed() -> int:
        raise TimeoutError("runner exit was not confirmed")

    monkeypatch.setattr(host, "_roundtrip", response)
    monkeypatch.setattr(process, "wait", unconfirmed)
    try:
        with pytest.raises(RuntimeError):
            if call == "decide":
                await asyncio.wait_for(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "old"}), 5)
            else:
                await asyncio.wait_for(host.validate_inputs("filter.echo", {}), 5)
        assert state.runner is process, "unconfirmed termination stopped tracking the runner"
        try:
            if call == "decide":
                result = await asyncio.wait_for(
                    host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "new"}), 5
                )
                assert result.type == "error" or result.facets == {"mode": "new"}, (
                    "unconfirmed protocol teardown reused the previous request's decision"
                )
            else:
                errors = await asyncio.wait_for(host.validate_inputs("filter.echo", {"unknown": True}), 5)
                assert errors, "unconfirmed protocol teardown reused the previous request's valid response"
        except RuntimeError as exc:
            assert isinstance(exc, RuntimeError)
        assert len(sent) == 1, "another request was sent to a runner with an untrusted stream and unconfirmed exit"
    finally:
        process.release.set()
        monkeypatch.setattr(process, "wait", lambda: PendingProcess.wait(process))
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
async def test_retirement_recovers_afterstop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    process = PendingProcess()
    wait = process.wait
    roundtrip = host._roundtrip
    monkeypatch.setattr(state, "runner", process)

    async def unconfirmed() -> int:
        raise TimeoutError("runner exit was not confirmed")

    async def forbidden(*_args: object) -> bytes:
        raise AssertionError("retiring runner received another request")

    monkeypatch.setattr(process, "wait", unconfirmed)
    monkeypatch.setattr(host, "_roundtrip", forbidden)
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(host.hot_load(tmp_path / "filter.echo"), 5)
        assert host._plugins["filter.echo"] is state and state.runner is process and state.retiring
        blocked = await asyncio.wait_for(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {}), 5)
        assert blocked.type == "error" and blocked.error is not None
        assert await asyncio.wait_for(host.validate_inputs("filter.echo", {}), 5)
        assert await asyncio.wait_for(host.describe_inputs("filter.echo"), 5) is None
        monkeypatch.setattr(process, "wait", wait)
        process.release.set()
        await asyncio.wait_for(host.stop("filter.echo"), 5)
        assert process.returncode is not None and state.runner is None and not state.retiring
        await asyncio.wait_for(host.restart("filter.echo"), 5)
        assert host._plugins["filter.echo"] is not state
        monkeypatch.setattr(host, "_roundtrip", roundtrip)
        resumed = await asyncio.wait_for(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {}), 5)
        assert resumed.type == "match" and resumed.facets == {"mode": "x"}
    finally:
        monkeypatch.setattr(process, "wait", wait)
        process.release.set()
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["decide", "describe", "validate"])
async def test_oversized_protocol_discards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    process = PendingProcess()
    process.release.set()
    monkeypatch.setattr(state, "runner", process)
    unread = {
        "decide": b'{"decision":{"type":"match","facets":{"mode":"old"}}}\n',
        "describe": b'{"inputs":[]}\n',
        "validate": b'{"valid":true}\n',
    }[call]
    process.stdout.feed_data(b"x" * 70000 + b"\n" + unread)
    try:
        with contextlib.suppress(ValueError):
            if call == "decide":
                await asyncio.wait_for(host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "old"}), 5)
            elif call == "describe":
                await asyncio.wait_for(host.describe_inputs("filter.echo"), 5)
            else:
                await asyncio.wait_for(host.validate_inputs("filter.echo", {}), 5)
        assert state.runner is None or state.retiring, "oversized stdout line left the runner stream reusable"
        assert len(process.stdin.sent) == 1
    finally:
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(host.aclose(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["decide", "describe", "validate"])
@pytest.mark.parametrize("failure", ["reset", "utf8"])
async def test_failed_protocol_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str, failure: str
) -> None:
    stage_all(tmp_path, "filter.echo")
    host = PluginHost([tmp_path])
    state = host._plugins["filter.echo"]
    process = PendingProcess()
    process.release.set()
    monkeypatch.setattr(state, "runner", process)
    unread = {
        "decide": b'{"decision":{"type":"match","facets":{"mode":"old"}}}\n',
        "describe": b'{"inputs":[]}\n',
        "validate": b'{"valid":true}\n',
    }[call]
    if failure == "reset":
        process.stdout.feed_data(unread)
        process.stdout.set_exception(ConnectionResetError("runner pipe reset"))
    else:
        process.stdout.feed_data(b"\xff\n" + unread)
    try:
        if call == "decide":
            decision = await asyncio.wait_for(
                host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "old"}), 5
            )
            assert decision.type == "error" and decision.error
        elif call == "describe":
            assert await asyncio.wait_for(host.describe_inputs("filter.echo"), 5) is None
        else:
            assert await asyncio.wait_for(host.validate_inputs("filter.echo", {}), 5)
        assert state.runner is None and not state.retiring
        assert process.returncode is not None and process.signalled.is_set()
        assert len(process.stdin.sent) == 1
    finally:
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.wait_for(host.aclose(), 5)
