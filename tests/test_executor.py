from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

import node.process as process
from common.media.probe import ProbeData, ProbeStream
from common.plan.models import FFmpegPlan, StreamPolicy, StreamSelection, VideoTarget
from common.enums import ExecutionResult
from node.executor import JobSpec, ProgressUpdate, _Emitter, _Log, _Run, _probe_json, execute
from node.repair import PreparedInput
from common.process import ChildNotReapedError
from tests.helpers import make_test_temp_dir

FFMPEG = Path(__file__).parent / "fakes" / "ffmpeg.py"
FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"


def spec(root: Path, **overrides: object) -> JobSpec:
    src = root / "src.mkv"
    src.write_bytes(b"x" * 1000)
    fields: dict[str, Any] = {
        "job_id": "job-1",
        "min_free_gb": 1,
        "attempt_id": 2,
        "src": str(src),
        "dst": str(root / "out.mkv"),
        "log": str(root / "attempt.log"),
        "plan": FFmpegPlan(video=VideoTarget(encoder="libx265")),
        "probe": ProbeData(
            file_path=str(src),
            duration_s=10,
            size_bytes=1000,
            streams=[ProbeStream(index=0, codec="h264", codec_type="video")],
        ),
    }
    fields.update(overrides)
    return JobSpec(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cleanup", "encode", "unchanged", "extras"])
async def test_cleanup_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    job = spec(tmp_path, min_free_gb=0)
    job.probe.duration_s = 3047
    job.probe.streams += [
        ProbeStream(index=1, codec="ac3", codec_type="audio", language="jpn"),
        ProbeStream(index=2, codec="ass", codec_type="subtitle", language="eng"),
        ProbeStream(index=3, codec="ass", codec_type="subtitle", language="ger"),
    ]
    job.plan = FFmpegPlan(
        streams=StreamPolicy(subtitle=StreamSelection(keep_languages=["eng"])) if mode != "unchanged" else None,
        video=VideoTarget(encoder="libx265") if mode == "encode" else None,
        extras=["-t", "1391"] if mode == "extras" else [],
    )
    Path(f"{job.dst}.probe.json").write_text(
        json.dumps(
            {
                "format": {"duration": "1391.06"},
                "streams": [
                    {"index": stream.index, "codec_type": stream.codec_type, "codec_name": stream.codec}
                    for stream in job.probe.streams
                    if mode == "unchanged" or stream.index != 3
                ],
            }
        )
    )
    result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 5)
    assert result.status == (ExecutionResult.SUCCEEDED if mode == "cleanup" else ExecutionResult.FAILED)
    assert Path(job.dst).exists() == (mode == "cleanup")
    assert Path(job.src).read_bytes() == b"x" * 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("output_bytes", [100, 1500])
async def test_repaired_input_accounting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_bytes: int) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", str(output_bytes))
    job = spec(tmp_path)
    Path(f"{job.dst}.probe.json").write_text(
        json.dumps(
            {"format": {"duration": "10"}, "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]}
        )
    )
    repaired = tmp_path / "normalized.mkv"
    repaired.write_bytes(b"x" * 5000)
    prepared = PreparedInput(
        path=repaired, probe=job.probe.model_copy(update={"file_path": str(repaired), "size_bytes": 5000})
    )
    result = await execute(job, FFMPEG, FFPROBE, prepared=prepared)
    assert result.source_size == 1000
    assert result.status == (ExecutionResult.SUCCEEDED if output_bytes == 100 else ExecutionResult.FAILED)
    assert Path(job.dst).exists() == (output_bytes == 100)
    assert Path(job.src).read_bytes() == b"x" * 1000 and repaired.stat().st_size == 5000


def emit(*lines: str, duration: float = 10, fps: float | None = 25) -> ProgressUpdate:
    emitter = _Emitter(duration, fps, None)
    for line in (*lines, "progress=end"):
        emitter.feed(line)
    assert emitter.latest is not None
    return emitter.latest


def test_progress_percent() -> None:
    assert emit("frame=125").percent == 50
    assert emit("frame=125", "out_time_us=2000000").percent == 50
    timed = emit("frame=125", "out_time_us=8000000", "fps=50", "speed=3.1x")
    assert timed.percent == 80 and timed.speed == "3.1x"
    without_fps = emit("frame=125", "out_time_us=2000000", "fps=30", "speed=0.5x", fps=None)
    assert without_fps.percent == 20 and without_fps.speed == "0.5x"
    assert emit("frame=300", duration=10, fps=25).percent == 100


def test_subtitle_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr("node.executor.time.monotonic", lambda: now)
    updates: list[ProgressUpdate] = []
    emitter = _Emitter(100, 25, updates.append)
    for frame in (125, 250):
        for line in (f"frame={frame}", "fps=50", "out_time_us=1001000", "speed=0.002x", "progress=continue"):
            emitter.feed(line)
        now += 1
    assert [update.percent for update in updates] == [5, 10]
    assert [update.speed for update in updates] == ["2x", "2x"]
    assert all(update.out_time_us == 1001000 for update in updates)


@pytest.mark.parametrize("duration,fps,frame", [(0, 25, 1), (10, 0, 1), (10, None, 1), (10, 25, 0)])
def test_missing_progress_inputs(duration: float, fps: float | None, frame: int) -> None:
    assert emit(f"frame={frame}", duration=duration, fps=fps).percent is None


@pytest.mark.asyncio
@pytest.mark.parametrize("artwork", [False, True])
async def test_output_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artwork: bool) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    job = spec(tmp_path)
    job.probe.streams[0].fps = 25
    streams: list[dict[str, Any]] = [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]
    payload = {"format": {"duration": "10"}, "streams": streams}
    if artwork:
        job.probe.streams.insert(
            0, ProbeStream(index=1, codec="mjpeg", codec_type="video", disposition_attached_pic=True, fps=1)
        )
        streams.append(
            {
                "index": 1,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 0},
                "tags": {"FILENAME": "cover.jpg", "MIMETYPE": "image/jpeg"},
            }
        )
    Path(f"{job.dst}.probe.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    spawn = process.create_tool
    calls: list[tuple[str, ...]] = []

    async def spawn_tool(*args, **kwargs):
        assert "shell" not in kwargs
        if str(args[0]) == str(FFMPEG):
            calls.append(args)
            source, output = args[args.index("-i") + 1], args[-1]
            assert source == job.src and output == job.dst
            assert Path(source).read_bytes() == b"x" * 1000
            assert Path(output).read_bytes() == b""
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(process, "create_tool", spawn_tool)
    progress: list[ProgressUpdate] = []
    result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE, on_progress=progress.append), timeout=10)
    assert result.status == "succeeded"
    assert result.output_path == str(tmp_path / "out.mkv")
    assert Path(result.output_path or "").stat().st_size == 100
    assert len(calls) == 1 and result.argv == list(calls[0])
    assert result.output_probe is not None and result.output_probe.file_path == str(tmp_path / "out.mkv")
    assert len(result.output_probe.real_video) == 1
    assert progress and progress[0].percent == 50
    if artwork:
        assert result.output_probe.video[1].disposition_attached_pic
        assert (
            result.output_probe.raw is not None
            and result.output_probe.raw["streams"][1]["disposition"]["attached_pic"] == 0
        )
    assert (tmp_path / "src.mkv").read_bytes() == b"x" * 1000


@pytest.mark.asyncio
async def test_complete_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    text = "stderr marker\n" + "z" * 100_000
    monkeypatch.setenv("FAKE_FFMPEG_ERR", text)
    result = await execute(spec(tmp_path), FFMPEG, FFPROBE)
    assert result.status == "failed"
    assert result.error is not None and result.error.endswith("z" * 2048)
    assert text.encode() in (tmp_path / "attempt.log").read_bytes()
    assert not (tmp_path / "out.mkv").exists()


@pytest.mark.asyncio
async def test_probe_keeps_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", "out.mkv")
    result = await execute(spec(tmp_path), FFMPEG, FFPROBE)
    assert result.status == "failed"
    assert (tmp_path / "src.mkv").is_file()
    assert not (tmp_path / "out.mkv").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("unreaped", [False, True])
async def test_log_failure_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreaped: bool) -> None:
    reader_completed = asyncio.Event()

    class Output(asyncio.StreamReader):
        async def readline(self) -> bytes:
            try:
                return await super().readline()
            finally:
                reader_completed.set()

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = Output()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"diagnostic")
            self.stderr.feed_eof()
            self.terminated = self.killed = False
            self.waits = 0

        def terminate(self) -> None:
            self.terminated = True

        def send_signal(self, _signal) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waits += 1
            if unreaped:
                raise TimeoutError
            self.returncode = -15
            return -15

    proc = Process()

    async def create(*_args, **_kwargs):
        (tmp_path / "out.mkv").write_bytes(b"output")
        return proc

    def fail_log(_self, _chunk):
        raise OSError("disk full")

    monkeypatch.setattr(process, "create_tool", create)
    monkeypatch.setattr(_Log, "stderr", fail_log)
    job = spec(tmp_path, min_free_gb=0)
    if unreaped:

        def fail_finish(log):
            log.file.close()
            raise OSError("disk full")

        monkeypatch.setattr(_Log, "finish", fail_finish)
        with pytest.raises(ChildNotReapedError):
            await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
        assert proc.killed and proc.waits == 2
        assert (tmp_path / "out.mkv").read_bytes() == b"output"
    else:
        result = await asyncio.wait_for(execute(job, FFMPEG, FFPROBE), 2)
        assert result.status == "failed" and result.error is not None
        assert proc.returncode == -15 and proc.waits == 1
        assert not (tmp_path / "out.mkv").exists()
    assert proc.terminated
    assert reader_completed.is_set()


@pytest.mark.asyncio
async def test_cancel_cleans_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "hang")
    started = asyncio.Event()
    original = process.create_tool

    async def create_process(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr(process, "create_tool", create_process)
    task = asyncio.create_task(execute(spec(tmp_path), FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        result = await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait_for(task, timeout=10)
    assert result.status == "cancelled"
    assert not (tmp_path / "out.mkv").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("unreaped", [False, True])
async def test_repeated_cancel_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreaped: bool) -> None:
    started, terminating, killed, release_term, release_kill = (asyncio.Event() for _ in range(5))

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self.waits = 0

        def terminate(self) -> None:
            terminating.set()

        def send_signal(self, _signal) -> None:
            killed.set()

        async def wait(self) -> int:
            self.waits += 1
            if self.waits == 1:
                await release_term.wait()
                raise TimeoutError
            await release_kill.wait()
            if unreaped:
                raise TimeoutError
            self.returncode = -9
            return self.returncode

    proc = Process()
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)

    async def spawn(*_args, **_kwargs):
        output.write_bytes(b"child-owned output")
        started.set()
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(terminating.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done() and proc.returncode is None
            assert output.read_bytes() == b"child-owned output"
        release_term.set()
        await asyncio.wait_for(killed.wait(), 2)
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done() and output.is_file()
        release_kill.set()
        error = ChildNotReapedError if unreaped else asyncio.CancelledError
        with pytest.raises(error):
            await asyncio.wait_for(task, 2)
        assert output.exists() is unreaped
        assert proc.returncode == (None if unreaped else -9)
        assert Path(job.src).read_bytes() == b"x" * 1000
    finally:
        release_term.set()
        release_kill.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_probe_cancel_terminates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    terminated = False
    waited = False

    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

        async def wait(self) -> int:
            nonlocal waited
            waited = True
            self.returncode = -15
            return self.returncode

    async def create_process(*_: object, **__: object) -> Process:
        return Process()

    monkeypatch.setattr(process, "create_tool", create_process)
    log = _Log(tmp_path / "probe.log")
    (tmp_path / "input.mkv").write_bytes(b"probe input")
    task = asyncio.create_task(_probe_json("ffprobe", tmp_path / "input.mkv", log))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        log.finish()
    assert terminated
    assert waited


@pytest.mark.asyncio
async def test_reader_drain_preserves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writing, draining, written = (asyncio.Event() for _ in range(3))
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_stderr, original_gather = _Log.stderr, asyncio.gather

    class Output(asyncio.StreamReader):
        async def readline(self) -> bytes:
            await writing.wait()
            raise TimeoutError

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = Output()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"diagnostic")
            self.stderr.feed_eof()
            self.killed = False

        def terminate(self) -> None:
            pass

        def send_signal(self, _signal) -> None:
            self.killed = True

        async def wait(self) -> int:
            raise TimeoutError

    proc = Process()
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)

    async def spawn(*_args, **_kwargs):
        output.write_bytes(b"child-owned output")
        return proc

    def blocked_write(log: _Log, chunk: bytes) -> None:
        original_stderr(log, chunk)
        loop.call_soon_threadsafe(writing.set)
        try:
            assert release.wait(5)
        finally:
            loop.call_soon_threadsafe(written.set)

    def gather_draining(*tasks, **kwargs):
        if len(tasks) == 2 and kwargs.get("return_exceptions"):
            draining.set()
        return original_gather(*tasks, **kwargs)

    monkeypatch.setattr(process, "create_tool", spawn)
    monkeypatch.setattr(asyncio, "gather", gather_draining)
    monkeypatch.setattr(_Log, "stderr", blocked_write)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(draining.wait(), 2)
        assert proc.killed and proc.returncode is None and not task.done()
        assert output.read_bytes() == b"child-owned output"
        task.cancel()
        release.set()
        outcome = (await asyncio.wait_for(original_gather(task, return_exceptions=True), 2))[0]
        assert proc.returncode is None
        assert Path(job.src).read_bytes() == b"x" * 1000
        assert output.is_file(), (
            f"unconfirmed child output was deleted during reader cleanup ({type(outcome).__name__})"
        )
        assert isinstance(outcome, ChildNotReapedError)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(original_gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(written.wait(), 2)


@pytest.mark.asyncio
async def test_unreaped_log_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finishing, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_finish = _Log.finish
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)

    async def unreaped(*_args, **_kwargs):
        output.write_bytes(b"child-owned output")
        raise ChildNotReapedError("exit unconfirmed")

    def blocked_finish(log: _Log) -> None:
        loop.call_soon_threadsafe(finishing.set)
        try:
            assert release.wait(5)
            original_finish(log)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr("node.executor._run_ffmpeg", unreaped)
    monkeypatch.setattr(_Log, "finish", blocked_finish)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(finishing.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done() and not finished.is_set()
            assert output.read_bytes() == b"child-owned output"
        release.set()
        with pytest.raises(ChildNotReapedError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set() and output.is_file()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize("unreaped", [False, True])
async def test_probe_repeated_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timed_out: bool, unreaped: bool):
    started, killed, release = (asyncio.Event() for _ in range(3))

    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            if timed_out:
                raise TimeoutError
            await asyncio.Event().wait()
            return b"", b""

        def terminate(self) -> None:
            pass

        def send_signal(self, _signal) -> None:
            killed.set()

        async def wait(self) -> int:
            if not killed.is_set():
                raise TimeoutError
            await release.wait()
            if unreaped:
                raise TimeoutError
            self.returncode = -9
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    log = _Log(tmp_path / "probe.log")
    (tmp_path / "input.mkv").write_bytes(b"probe input")
    task = asyncio.create_task(_probe_json("ffprobe", tmp_path / "input.mkv", log))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if not timed_out:
            task.cancel()
        await asyncio.wait_for(killed.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done() and proc.returncode is None
        release.set()
        error = ChildNotReapedError if unreaped else asyncio.CancelledError
        with pytest.raises(error):
            await asyncio.wait_for(task, 2)
        assert proc.returncode == (None if unreaped else -9)
    finally:
        release.set()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        log.finish()


@pytest.mark.asyncio
async def test_probe_write_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writing, written = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_stderr = _Log.stderr
    logs = []
    job = spec(tmp_path, min_free_gb=0)
    output = Path(job.dst)

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"{}", b"probe diagnostic"

    async def spawn(*_args, **_kwargs):
        return Process()

    async def encoded(*_args, **_kwargs):
        output.write_bytes(b"encoded output")
        return _Run(returncode=0, stalled=False, cancelled=False, progress=None)

    def blocked_write(log: _Log, chunk: bytes) -> None:
        logs.append(log)
        loop.call_soon_threadsafe(writing.set)
        try:
            assert release.wait(5)
            original_stderr(log, chunk)
        finally:
            loop.call_soon_threadsafe(written.set)

    monkeypatch.setattr("node.executor._run_ffmpeg", encoded)
    monkeypatch.setattr(process, "create_tool", spawn)
    monkeypatch.setattr(_Log, "stderr", blocked_write)
    task = asyncio.create_task(execute(job, FFMPEG, FFPROBE))
    try:
        await asyncio.wait_for(writing.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done() and not written.is_set()
            assert not logs[0].file.closed and output.is_file()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert written.is_set() and logs[0].file.closed
        assert not output.exists()
        assert b"probe diagnostic" in Path(job.log).read_bytes()
        assert Path(job.src).read_bytes() == b"x" * 1000
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(written.wait(), 2)
