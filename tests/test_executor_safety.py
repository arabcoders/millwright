from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

import pytest

import node.process as process
from node.executor import _Log, _Run, _probe_json, execute
from common.process import ChildNotReapedError
from tests.test_executor import FFMPEG, FFPROBE, spec


@pytest.mark.asyncio
@pytest.mark.parametrize("abort", [False, True])
async def test_occupied_output_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, abort: bool) -> None:
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)
    output.write_bytes(b"unexpected recovery files")
    if abort:
        Path(job.src).unlink()
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
    assert output.exists(), "prepare failure deleted an output the executor never acquired"
    assert output.read_bytes() == b"unexpected recovery files", "encoding overwrote an occupied output"
    assert result.status != "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["terminate", "kill"])
async def test_signal_failure_retains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal: str) -> None:
    class Output(asyncio.StreamReader):
        async def readline(self) -> bytes:
            raise TimeoutError

    class Process:
        returncode = None

        def __init__(self) -> None:
            self.stdout = Output()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            if signal == "terminate":
                raise PermissionError("termination denied")

        def send_signal(self, _signal) -> None:
            raise PermissionError("kill denied")

        async def wait(self) -> int:
            raise TimeoutError

    proc = Process()
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)

    async def spawn(*_args, **_kwargs):
        output.write_bytes(b"live child output")
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    outcome = (await asyncio.wait_for(asyncio.gather(execute(job, FFMPEG, FFPROBE), return_exceptions=True), 2))[0]
    assert output.exists(), "signal failure deleted output while child exit was unconfirmed"
    assert output.read_bytes() == b"live child output"
    assert isinstance(outcome, ChildNotReapedError), f"unconfirmed exit was treated as completed: {outcome!r}"
    assert proc.returncode is None


@pytest.mark.asyncio
async def test_probe_io_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            raise OSError("probe pipe failed")

        def terminate(self) -> None:
            pass

        def send_signal(self, _signal) -> None:
            pass

        async def wait(self) -> int:
            self.returncode = -15
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    log = _Log(tmp_path / "probe.log")
    (tmp_path / "output.mkv").write_bytes(b"probe input")
    try:
        with pytest.raises(OSError):
            await asyncio.wait_for(_probe_json("ffprobe", tmp_path / "output.mkv", log), 2)
        assert proc.returncode is not None, "probe I/O failure finished without stopping its child"
    finally:
        log.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["audio", "subtitle", "real_video"])
async def test_retained_streams_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    from common.media.probe import ProbeStream

    job = spec(tmp_path, min_free_gb=0)
    job.probe.streams += [
        ProbeStream(index=1, codec="aac", codec_type="audio"),
        ProbeStream(index=2, codec="subrip", codec_type="subtitle"),
        ProbeStream(index=3, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
    ]

    async def encoded(*_args, **_kwargs):
        Path(job.dst).write_bytes(b"encoded output")
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    async def probe(*_args):
        return {
            "format": {"duration": "10"},
            "streams": [
                {
                    "index": stream.index,
                    "codec_name": stream.codec,
                    "codec_type": stream.codec_type,
                    "disposition": {"attached_pic": int(stream.disposition_attached_pic)},
                }
                for stream in job.probe.streams
                if (
                    "real_video"
                    if stream.codec_type == "video" and not stream.disposition_attached_pic
                    else stream.codec_type
                )
                != missing
            ],
        }

    monkeypatch.setattr("node.executor._run_ffmpeg", encoded)
    monkeypatch.setattr("node.executor._probe_json", probe)
    result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
    assert result.status != "succeeded", f"executor accepted output missing retained {missing}"
    assert result.output_path is None and result.output_probe is None
    assert Path(job.src).read_bytes() == b"x" * 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["nan", "inf"])
async def test_invalid_duration_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duration: str) -> None:
    job = spec(tmp_path, min_free_gb=0)

    async def encoded(*_args, **_kwargs):
        Path(job.dst).write_bytes(b"unverifiable output")
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    async def probe(*_args):
        return {
            "format": {"duration": duration},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}],
        }

    monkeypatch.setattr("node.executor._run_ffmpeg", encoded)
    monkeypatch.setattr("node.executor._probe_json", probe)
    result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
    assert result.status != "succeeded", f"output duration {duration!r} bypassed verification"
    assert result.output_path is None and result.output_probe is None
    assert Path(job.src).read_bytes() == b"x" * 1000


@pytest.mark.asyncio
async def test_eof_stall_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waiting, release, stopped = (asyncio.Event() for _ in range(3))

    class Process:
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.stopped = False

        def terminate(self) -> None:
            self.stopped = True
            stopped.set()
            release.set()

        def send_signal(self, _signal) -> None:
            self.stopped = True
            stopped.set()
            release.set()

        async def wait(self) -> int:
            waiting.set()
            await release.wait()
            self.returncode = -15
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    timeout = asyncio.timeout
    stalls: list[asyncio.Timeout] = []
    deadlines: list[asyncio.TimerHandle] = []
    loop = asyncio.get_running_loop()
    call_at = loop.call_at

    def capture_timeout(delay):
        deadline = timeout(delay)
        if delay == 123:
            stalls.append(deadline)
        return deadline

    def capture_deadline(when, callback, *args, **kwargs):
        handle = call_at(when, callback, *args, **kwargs)
        if getattr(callback, "__self__", None) in stalls:
            deadlines.append(handle)
        return handle

    monkeypatch.setattr(process, "create_tool", spawn)
    monkeypatch.setattr("asyncio.timeouts.timeout", capture_timeout)
    monkeypatch.setattr(loop, "call_at", capture_deadline)
    job = spec(tmp_path, min_free_gb=0, stall_timeout_s=123)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        active = [handle for handle in deadlines if not handle.cancelled()]
        assert active, "progress EOF disabled the stall deadline while the child remained alive"
        for handle in active:
            handle._run()
            handle.cancel()
        await asyncio.wait_for(stopped.wait(), 2)
        result = await asyncio.wait_for(task, 2)
        assert result.status == "stalled"
        assert proc.returncode == -15
        assert not Path(job.dst).exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["encode", "probe", "extract", "capability"])
async def test_spawn_cancel_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    import common.process as child_process
    from common.actions import ExtractSubtitles
    from node.actions import run_actions
    from common.capabilities import probe_capabilities
    from tests.test_node_actions import probe as action_probe
    from tests.test_node_actions import spec as action_spec

    started, release_spawn, reaping, release_exit = (asyncio.Event() for _ in range(4))
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)

    class Process:
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def send_signal(self, _signal) -> None:
            pass

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        async def wait(self) -> int:
            reaping.set()
            await release_exit.wait()
            self.returncode = -9
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        output.write_bytes(b"starting child output")
        started.set()
        try:
            await release_spawn.wait()
        except asyncio.CancelledError:
            # Model asyncio's subprocess startup: close its transport, then
            # await child exit before propagating the cancellation.
            proc.kill()
            await proc.wait()
            raise
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    monkeypatch.setattr(child_process, "create_subprocess_exec", spawn)
    log = _Log(tmp_path / "probe.log")
    operations = {
        "encode": lambda: execute(job, FFMPEG, FFPROBE),
        "probe": lambda: _probe_json("ffprobe", Path(job.src), log),
        "extract": lambda: run_actions(
            action_spec(
                Path(job.src), [ExtractSubtitles(formats=["srt"], languages=[])], action_probe(Path(job.src), "eng")
            ),
            "ffmpeg",
        ),
        "capability": lambda: probe_capabilities("ffmpeg"),
    }
    task = asyncio.create_task(operations[operation]())
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
        assert output.exists(), "cancellation deleted output before the starting subprocess confirmed exit"
        assert not task.done(), f"{operation} released subprocess tracking before child exit was confirmed"
        release_spawn.set()
        await asyncio.wait_for(reaping.wait(), 2)
        assert proc.returncode is None
        release_exit.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert proc.returncode == -9
    finally:
        release_spawn.set()
        release_exit.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        log.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["disk", "duration", "plan_size", "hard_size", "spawn", "missing", "json"])
async def test_guard_failure_preserves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: str) -> None:
    from types import SimpleNamespace

    job = spec(tmp_path, min_free_gb=0)
    if guard == "disk":
        job.min_free_gb = 1
        monkeypatch.setattr("node.files.shutil.disk_usage", lambda _path: SimpleNamespace(free=0))
    elif guard == "plan_size":
        job.plan.expected.size_ceiling_ratio = 0.05
    elif guard == "hard_size":
        job.hard_size_ceiling = 0.05

    async def encoded(*_args, **_kwargs):
        if guard == "spawn":
            raise OSError("cannot start encoder")
        if guard != "missing":
            Path(job.dst).write_bytes(b"x" * 100)
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    async def probe(*_args):
        if guard == "json":
            raise ValueError("invalid probe JSON")
        return {
            "format": {"duration": "1" if guard == "duration" else "10"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}],
        }

    monkeypatch.setattr("node.executor._run_ffmpeg", encoded)
    monkeypatch.setattr("node.executor._probe_json", probe)
    result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
    assert result.status in {"aborted", "failed"} and result.error
    assert result.output_path is None and result.output_probe is None and not Path(job.dst).exists()
    assert Path(job.src).read_bytes() == b"x" * 1000


@pytest.mark.asyncio
async def test_log_failure_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    job = spec(tmp_path)
    failure = OSError("execution log unavailable")

    def fail(_path):
        raise failure

    monkeypatch.setattr("node.executor.open_log", fail)
    caplog.set_level(logging.ERROR, logger="node.executor")
    result = await execute(job, FFMPEG, FFPROBE)
    assert result.status == "failed" and result.error == str(failure)
    record = next(record for record in caplog.records if record.name == "node.executor")
    assert getattr(record, "error") == result.error
    assert getattr(record, "job_id", None) == job.job_id and getattr(record, "attempt_id", None) == job.attempt_id
    assert Path(job.src).read_bytes() == b"x" * 1000 and not Path(job.dst).exists()


@pytest.mark.asyncio
async def test_cleanup_failure_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import node.executor as executor

    job = spec(tmp_path, min_free_gb=0)
    logs: list[_Log] = []
    opening, unlink = _Log.__init__, executor.os.unlink

    def open_log(log, path):
        opening(log, path)
        logs.append(log)

    def failed_unlink(path, *args, **kwargs):
        if Path(path) == Path(job.dst):
            raise PermissionError("output cleanup denied")
        return unlink(path, *args, **kwargs)

    async def failed(_job, argv, *_args):
        Path(job.dst).write_bytes(b"failed output")
        return _Run(returncode=1, stalled=False, cancelled=False, progress=None)

    monkeypatch.setattr(_Log, "__init__", open_log)
    monkeypatch.setattr(executor.os, "unlink", failed_unlink)
    monkeypatch.setattr(executor, "_run_ffmpeg", failed)
    try:
        with pytest.raises(PermissionError):
            await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
        assert logs and logs[0].file.closed, "output cleanup failure bypassed log finalization"
        assert Path(job.dst).read_bytes() == b"failed output"
        assert Path(job.src).read_bytes() == b"x" * 1000
    finally:
        for log in logs:
            if not log.file.closed:
                log.finish()


@pytest.mark.asyncio
async def test_eof_stderr_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eof, waiting, stopped = (asyncio.Event() for _ in range(3))

    class Output(asyncio.StreamReader):
        async def readline(self) -> bytes:
            line = await super().readline()
            if not line:
                eof.set()
            return line

    class Process:
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = Output()
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()

        def terminate(self) -> None:
            stopped.set()
            self.stderr.feed_eof()

        def send_signal(self, _signal) -> None:
            stopped.set()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            waiting.set()
            await stopped.wait()
            self.returncode = -15
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    timeout = asyncio.timeout
    stalls: list[asyncio.Timeout] = []
    deadlines: list[asyncio.TimerHandle] = []
    loop = asyncio.get_running_loop()
    call_at = loop.call_at

    def capture_timeout(delay):
        deadline = timeout(delay)
        if delay == 123:
            stalls.append(deadline)
        return deadline

    def capture_deadline(when, callback, *args, **kwargs):
        handle = call_at(when, callback, *args, **kwargs)
        if getattr(callback, "__self__", None) in stalls:
            deadlines.append(handle)
        return handle

    monkeypatch.setattr(process, "create_tool", spawn)
    monkeypatch.setattr("asyncio.timeouts.timeout", capture_timeout)
    monkeypatch.setattr(loop, "call_at", capture_deadline)
    task = asyncio.create_task(execute(spec(tmp_path, min_free_gb=0, stall_timeout_s=123), FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(eof.wait(), 2)
        await asyncio.wait_for(waiting.wait(), 2)
        active = [handle for handle in deadlines if not handle.cancelled()]
        assert active, "progress EOF disabled the stall deadline while stderr and the child remained open"
        for handle in active:
            handle._run()
            handle.cancel()
        await asyncio.wait_for(stopped.wait(), 2)
        result = await asyncio.wait_for(task, 2)
        assert result.status == "stalled" and proc.returncode == -15
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, OSError])
async def test_wait_failure_escalates(failure: type[Exception], monkeypatch: pytest.MonkeyPatch) -> None:
    import common.process as child_process
    from common.process import spawn_child, stop_child

    calls: list[str] = []

    class Process:
        returncode: int | None = None

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        async def wait(self) -> int:
            calls.append("wait")
            if calls == ["terminate", "wait"]:
                raise failure("TERM wait failed")
            self.returncode = -9
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(child_process, "create_subprocess_exec", spawn)
    child = await asyncio.wait_for(spawn_child("ffmpeg"), 2)
    assert await asyncio.wait_for(stop_child(child), 2) == -9
    assert calls == ["terminate", "wait", "kill", "wait"]
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_start_return_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, acquired, reaping, release = (asyncio.Event() for _ in range(4))
    job = spec(tmp_path, min_free_gb=0)

    class Process:
        returncode: int | None = None

        def terminate(self) -> None:
            pass

        def send_signal(self, _signal) -> None:
            pass

        async def wait(self) -> int:
            reaping.set()
            await release.wait()
            self.returncode = -15
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        Path(job.dst).write_bytes(b"child output")
        started.set()
        await acquired.wait()
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        acquired.set()
        await asyncio.wait_for(reaping.wait(), 2)
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done() and proc.returncode is None
        assert Path(job.dst).read_bytes() == b"child output"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert proc.returncode == -15 and not Path(job.dst).exists()
        assert Path(job.src).read_bytes() == b"x" * 1000
    finally:
        acquired.set()
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_start_error_retains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    job = spec(tmp_path, min_free_gb=0)

    async def spawn(*_args, **_kwargs):
        Path(job.dst).write_bytes(b"unconfirmed child output")
        started.set()
        await release.wait()
        raise OSError("transport cleanup failed")

    monkeypatch.setattr(process, "create_tool", spawn)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(ChildNotReapedError):
            await asyncio.wait_for(task, 2)
        assert Path(job.dst).read_bytes() == b"unconfirmed child output"
        assert Path(job.src).read_bytes() == b"x" * 1000
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_handoff_failure_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    finishing, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    job = spec(tmp_path, min_free_gb=0)
    logs: list[_Log] = []
    original = _Log.finish

    async def encoded(_job, argv, *_args):
        Path(argv[-1]).write_bytes(b"child output")
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    async def probe(*_args):
        return {
            "format": {"duration": "10"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}],
        }

    def blocked_finish(log):
        logs.append(log)
        loop.call_soon_threadsafe(finishing.set)
        try:
            assert release.wait(5)
            original(log)
            if failure == "error":
                raise OSError("log flush failed")
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr("node.executor._run_ffmpeg", encoded)
    monkeypatch.setattr("node.executor._probe_json", probe)
    monkeypatch.setattr(_Log, "finish", blocked_finish)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(finishing.wait(), 2)
        assert not logs[0].file.closed
        if failure == "cancel":
            for _ in range(2):
                task.cancel()
                checkpoint = asyncio.Event()
                loop.call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 2)
                assert not task.done() and not finished.is_set()
        release.set()
        if failure == "error":
            result = await asyncio.wait_for(task, 2)
            assert result.status == "succeeded" and result.output_path == job.dst
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        assert finished.is_set() and logs[0].file.closed
        assert Path(job.src).read_bytes() == b"x" * 1000
        assert Path(job.dst).read_bytes() == b"child output"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "aborted", "unreaped"])
async def test_log_fsync_preserves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    job = spec(tmp_path, min_free_gb=0)
    logs: list[_Log] = []
    opening, syncing = _Log.__init__, os.fsync
    fsync_failed = False

    def open_log(log, path):
        opening(log, path)
        logs.append(log)

    def failed_fsync(fd):
        nonlocal fsync_failed
        if logs and not logs[0].file.closed and fd == logs[0].file.fileno():
            fsync_failed = True
            raise OSError("NFS log fsync rejected")
        return syncing(fd)

    async def encoded(_job, argv, *_args, **_kwargs):
        Path(argv[-1]).write_bytes(b"child output")
        if outcome == "unreaped":
            raise ChildNotReapedError("encoder exit unconfirmed")
        return _Run(returncode=1 if outcome == "failed" else 0, stalled=False, cancelled=False, progress=None)

    async def probe(*_args):
        return {
            "format": {"duration": "10"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}],
        }

    if outcome == "aborted":
        Path(job.src).unlink()
    monkeypatch.setattr(_Log, "__init__", open_log)
    monkeypatch.setattr(os, "fsync", failed_fsync)
    monkeypatch.setattr("node.executor._run_ffmpeg", encoded)
    monkeypatch.setattr("node.executor._probe_json", probe)
    if outcome == "unreaped":
        with pytest.raises(ChildNotReapedError):
            await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
    else:
        result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
        assert result.status == outcome
        if outcome == "succeeded":
            assert result.output_path == job.dst and result.output_probe is not None
        elif outcome == "aborted":
            assert result.error and "src.mkv" in result.error
    assert fsync_failed
    assert logs and logs[0].file.closed
    assert Path(job.dst).exists() == (outcome in {"succeeded", "unreaped"})
