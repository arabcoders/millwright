from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from common.capabilities import Capabilities, CapabilitiesError

import common.capabilities as capability_checks
import common.healthcheck as healthcheck
from server.healthlogs import DiagnosticLog, create_healthcheck_log


class Proc:
    def __init__(
        self,
        code: int = 0,
        *,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        blocked: bool = False,
    ) -> None:
        self.returncode = code
        line_type = BlockingLines if blocked else Lines
        self.stdout: Lines = line_type(stdout or [])
        self.stderr: Lines = line_type(stderr or [])

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


class Lines:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = iter(lines)

    async def readline(self) -> bytes:
        return next(self.lines, b"")


class BlockingLines(Lines):
    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""


async def _proc(*args: object, **kwargs: object) -> Proc:
    return Proc()


@pytest.mark.asyncio
async def test_probe_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", _proc)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(b"{}", b"bad probe"))
    proc = Proc(7)
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", lambda *_args, **_kw: _ready(proc))
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", None)
    assert result.error is not None and "bad probe" in result.error


async def _result(stdout: bytes, stderr: bytes) -> tuple[bytes, bytes]:
    return stdout, stderr


async def _ready(proc: Proc) -> Proc:
    return proc


@pytest.mark.asyncio
async def test_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", _proc)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(b"nope", b""))
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", None)
    assert result.error is not None


@pytest.mark.asyncio
async def test_decode_maps_tracks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    async def create(*args: object, **kwargs: object) -> Proc:
        calls.append(args)
        return Proc()

    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(
        healthcheck,
        "_communicate",
        lambda *_: _result(
            b'{"streams":[{"index":0,"codec_type":"video","codec_name":"h264"},{"index":1,"codec_type":"video","codec_name":"hevc"},{"index":2,"codec_type":"audio"},{"index":3,"codec_type":"audio"},{"index":4,"codec_type":"subtitle"},{"index":5,"codec_type":"attachment"}]}',
            b"",
        ),
    )
    result = await healthcheck._decode_healthcheck(
        "movie", "ffmpeg", "ffprobe", create_healthcheck_log(tmp_path, 1), hwaccel=False
    )
    assert result.healthy
    priority = (shutil.which("nice"), "-n", "10", shutil.which("ionice"), "-c", "2", "-n", "7")
    assert calls[0][:8] == calls[1][:8] == priority
    assert calls[0][-3:] == ("-threads", "1", "movie")
    assert calls[1][8:] == (
        "ffmpeg",
        "-v",
        "error",
        "-stats_period",
        "1",
        "-progress",
        "pipe:1",
        "-filter_threads",
        "1",
        "-threads",
        "1",
        "-i",
        "movie",
        "-map",
        "0:0",
        "-map",
        "0:1",
        "-map",
        "0:a?",
        "-c:v",
        "wrapped_avframe",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
    )


@pytest.mark.asyncio
async def test_log_failure_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", _proc)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(b"{}", b""))

    open_file = os.open
    log = create_healthcheck_log(tmp_path, 1)
    failed = False

    def fail_write(path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal failed
        if path == "1.log":
            failed = True
            raise OSError("log unavailable")
        return open_file(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_write)
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", log)
    assert failed
    assert result.healthy and not result.failed


@pytest.mark.asyncio
async def test_probe_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", _proc)

    async def timeout(*args: object) -> tuple[bytes, bytes]:
        raise TimeoutError

    monkeypatch.setattr(healthcheck, "_communicate", timeout)
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", None)
    assert result.error is not None


@pytest.mark.asyncio
async def test_decode_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    budget = asyncio.timeout(None)

    class ExpiringLines(BlockingLines):
        async def readline(self) -> bytes:
            budget.reschedule(asyncio.get_running_loop().time())
            return await super().readline()

    async def create(*args: object, **kwargs: object) -> Proc:
        nonlocal calls
        calls += 1
        proc = Proc()
        if calls == 2:
            proc.stdout = ExpiringLines([])
        return proc

    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(healthcheck.asyncio, "timeout", lambda _: budget)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(b"{}", b""))
    result = await healthcheck._decode_healthcheck(
        "movie", "ffmpeg", "ffprobe", create_healthcheck_log(tmp_path, 1), timeout_s=2
    )
    assert result.error is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "decode_error"), [(0, b""), (0, b"Decoder error.\n"), (1, b"")])
async def test_libva_decode_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, decode_error: bytes
) -> None:
    created = 0
    info = b"libva info: VA-API version 1.22.0\nlibva info: va_openDriver() returns 0\n"

    async def create(*args: object, **kwargs: object) -> Proc:
        nonlocal created
        created += 1
        return Proc() if created == 1 else Proc(code, stderr=[info + decode_error])

    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(b"{}", b""))
    log = create_healthcheck_log(tmp_path, 1)
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", log)

    assert result.healthy == (code == 0 and not decode_error)
    assert result.failed == (code != 0)
    if code == 0:
        assert result.error_count == bool(decode_error)
        assert result.samples == ([decode_error.decode().strip()] if decode_error else [])
    else:
        assert result.error
    assert (info + decode_error).decode() in log.path.read_text()


@pytest.mark.asyncio
async def test_streams_decode_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = 0
    updates: list[dict[str, object]] = []
    release = asyncio.Event()
    stderr_seen = asyncio.Event()

    class LiveStderr(Lines):
        sent = False

        def __init__(self) -> None:
            super().__init__([])

        async def readline(self) -> bytes:
            if not self.sent:
                self.sent = True
                return b"decode warning\n"
            await release.wait()
            return b""

    async def on_progress(update: dict[str, object]) -> None:
        updates.append(update)

    original_append = healthcheck._append_log
    loop = asyncio.get_running_loop()

    def append_log(path: DiagnosticLog | None, text: str, *, sync: bool = False) -> None:
        original_append(path, text, sync=sync)
        if "decode warning" in text:
            loop.call_soon_threadsafe(stderr_seen.set)

    monkeypatch.setattr(healthcheck, "_append_log", append_log)

    async def create(*args: object, **kwargs: object) -> Proc:
        nonlocal created
        created += 1
        process = Proc(
            stdout=[b"out_time_us=500000\n", b"progress=end\n"] if created == 2 else None,
            stderr=[] if created == 2 else None,
        )
        if created == 2:
            process.stderr = LiveStderr()
        return process

    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(b'{"format":{"duration":"1"}}', b""))
    task = asyncio.create_task(
        healthcheck._decode_healthcheck(
            "movie", "ffmpeg", "ffprobe", create_healthcheck_log(tmp_path, 1), on_progress=on_progress
        )
    )
    try:
        await asyncio.wait_for(stderr_seen.wait(), 1)
        assert not task.done()
        assert "decode warning" in (tmp_path / "1.log").read_text()
        assert updates[:2] == [{"stage": "probe"}, {"stage": "decode"}]
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.error_count == 1 and result.samples == ["decode warning"]
        assert updates[2]["percent"] == 50.0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)


@pytest.fixture
def hardware_caps() -> Capabilities:
    return Capabilities(
        ffmpeg_version="fake",
        encoders=frozenset({"libx264", "libx265", "mpeg2video", "mjpeg", "av1_nvenc"}),
        video_encoders=frozenset({"libx264", "libx265", "mpeg2video", "mjpeg", "av1_nvenc"}),
        audio_encoders=frozenset(),
        decoders=frozenset(
            {
                "h264_cuvid",
                "hevc_cuvid",
                "mpeg2_cuvid",
                "h264_qsv",
                "hevc_qsv",
                "mpeg2_qsv",
                "mjpeg_qsv",
                "h264",
                "hevc",
                "mpeg2video",
                "av1",
            }
        ),
        audio_decoders=frozenset({"aac"}),
        hwaccels=frozenset({"cuda", "qsv", "vaapi"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("backend", ["cuda", "qsv", "vaapi", "mixed"])
async def test_hardware_mixed_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hardware_caps: Capabilities, code: int, backend: str
) -> None:
    calls: list[tuple[object, ...]] = []
    metadata = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {"attached_pic": 1}},
            {
                "index": 1,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"timed_thumbnails": 1},
            },
            {"index": 2, "codec_type": "video", "codec_name": "h264"},
            {"index": 3, "codec_type": "video", "codec_name": "mpeg2video"},
            {"index": 4, "codec_type": "video", "codec_name": "hevc"},
            {"index": 5, "codec_type": "video", "codec_name": "av1"},
            {"index": 6, "codec_type": "audio", "codec_name": "aac"},
            {"index": 7, "codec_type": "audio", "codec_name": "aac"},
            {
                "index": 8,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "avg_frame_rate": "30000/1",
                "disposition": {"attached_pic": 0},
                "tags": {"FILENAME": "cover.jpg", "MIMETYPE": "image/jpeg"},
            },
        ]
    }

    async def capabilities(binary: str, *, argv_prefix: tuple[str, ...]) -> Capabilities:
        assert binary == "ffmpeg"
        assert argv_prefix == calls[0][:8]
        return hardware_caps

    async def sample(binary: str, encoder: str, argv_prefix: tuple[str, ...]) -> tuple[bytes, str | None]:
        assert encoder in {"libx264", "libx265", "mpeg2video"}
        assert argv_prefix == calls[0][:8]
        return b"known-good sample", None

    async def decode(
        binary: str, candidate: str, decoder: str, sample: bytes, argv_prefix: tuple[str, ...]
    ) -> str | None:
        assert sample == b"known-good sample"
        assert argv_prefix == calls[0][:8]
        selected = backend
        if backend == "mixed":
            selected = "cuda" if decoder.startswith("h264") else "qsv" if decoder.startswith("mpeg2") else "vaapi"
        return None if candidate == selected else "No runtime device."

    async def create(*args: object, **kwargs: object) -> Proc:
        calls.append(args)
        if len(calls) == 1:
            return Proc()
        return Proc(code, stderr=[f"{backend} decoder initialization failed\n".encode()] if code else [])

    monkeypatch.setattr(healthcheck, "probe_capabilities", capabilities)
    monkeypatch.setattr(capability_checks, "_decode_sample", sample)
    monkeypatch.setattr(capability_checks, "_test_decode", decode)
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(json.dumps(metadata).encode(), b""))
    log = create_healthcheck_log(tmp_path, 1)
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", log)

    assert result.healthy == (code == 0)
    assert result.failed == (code != 0)
    assert len(calls) == (3 if code else 2)
    command = calls[1]
    before_input = command[: command.index("-i")]
    backend_decoders = {
        "cuda": ("h264_cuvid", "mpeg2_cuvid", "hevc_cuvid"),
        "qsv": ("h264_qsv", "mpeg2_qsv", "hevc_qsv"),
        "vaapi": ("h264", "mpeg2video", "hevc"),
    }
    selected = ("cuda", "qsv", "vaapi") if backend == "mixed" else (backend, backend, backend)
    decoder_names = tuple(backend_decoders[value][index] for index, value in enumerate(selected))
    devices = (
        (
            "-init_hw_device",
            "cuda=healthcheck_cuda",
            "-init_hw_device",
            "qsv=healthcheck_qsv",
            "-init_hw_device",
            "vaapi=healthcheck_vaapi",
        )
        if backend == "mixed"
        else ("-init_hw_device", f"{backend}=healthcheck_{backend}")
    )
    assert before_input[-(26 + len(devices)) :] == (
        "-threads",
        "1",
        *devices,
        "-hwaccel:2",
        selected[0],
        "-hwaccel_device:2",
        f"healthcheck_{selected[0]}",
        "-hwaccel_output_format:2",
        selected[0],
        "-c:2",
        decoder_names[0],
        "-hwaccel:3",
        selected[1],
        "-hwaccel_device:3",
        f"healthcheck_{selected[1]}",
        "-hwaccel_output_format:3",
        selected[1],
        "-c:3",
        decoder_names[1],
        "-hwaccel:4",
        selected[2],
        "-hwaccel_device:4",
        f"healthcheck_{selected[2]}",
        "-hwaccel_output_format:4",
        selected[2],
        "-c:4",
        decoder_names[2],
    )
    assert command[command.index("-map") :] == (
        "-map",
        "0:2",
        "-map",
        "0:3",
        "-map",
        "0:4",
        "-map",
        "0:5",
        "-map",
        "0:a?",
        "-filter:v:0",
        f"format={selected[0]}",
        "-filter:v:1",
        f"format={selected[1]}",
        "-filter:v:2",
        f"format={selected[2]}",
        "-c:v",
        "wrapped_avframe",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
    )
    if code:
        assert result.error and f"{backend} decoder initialization failed" in result.error
        assert f"{backend} decoder initialization failed" in log.path.read_text()
        assert calls[2][:8] == calls[1][:8]
        assert calls[2][calls[2].index("-threads") : calls[2].index("-i")] == ("-threads", "1")
        maps = command[command.index("-map") : command.index("-filter:v:0")]
        output = command[command.index("-c:v") :]
        assert calls[2][calls[2].index("-map") :] == (*maps, *output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hardware_code", "software_code", "software_error"),
    [(69, 0, b""), (0, 0, b"bad frame\n"), (69, 1, b"software rejected input\n")],
)
async def test_hardware_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hardware_caps: Capabilities,
    hardware_code: int,
    software_code: int,
    software_error: bytes,
) -> None:
    calls: list[tuple[object, ...]] = []
    updates: list[dict[str, object]] = []

    async def capabilities(binary: str, *, argv_prefix: tuple[str, ...]) -> Capabilities:
        return hardware_caps

    async def sample(*args: object) -> tuple[bytes, str | None]:
        return b"known-good sample", None

    async def decode(*args: object) -> None:
        return None

    async def create(*args: object, **kwargs: object) -> Proc:
        calls.append(args)
        if len(calls) == 1:
            return Proc()
        if len(calls) == 2:
            return Proc(
                hardware_code,
                stdout=[b"out_time_us=5000000\n", b"progress=end\n"],
                stderr=[b"unsupported IO surface\n"],
            )
        return Proc(
            software_code,
            stdout=[b"out_time_us=10000000\n", b"progress=end\n"],
            stderr=[software_error],
        )

    async def progress(update: dict[str, object]) -> None:
        updates.append(update)

    probe = {
        "format": {"duration": "10"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p10le", "profile": "High 10"}
        ],
    }
    monkeypatch.setattr(healthcheck, "probe_capabilities", capabilities)
    monkeypatch.setattr(capability_checks, "_decode_sample", sample)
    monkeypatch.setattr(capability_checks, "_test_decode", decode)
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(healthcheck, "_communicate", lambda *_: _result(json.dumps(probe).encode(), b""))
    log = create_healthcheck_log(tmp_path, 1)
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", log, on_progress=progress)

    assert result.healthy == (software_code == 0 and not software_error)
    assert result.failed == (software_code != 0)
    assert result.probe == probe
    assert len(calls) == 3
    assert "-hwaccel:0" in calls[1]
    assert [update["percent"] for update in updates if "percent" in update] == [50.0, 0.0, 100.0]
    assert "unsupported IO surface" in log.path.read_text()
    if software_code:
        assert result.error and software_error.decode().strip() in result.error
    else:
        assert result.error_count == bool(software_error)
        assert result.samples == ([software_error.decode().strip()] if software_error else [])
    if software_error:
        assert software_error.decode() in log.path.read_text()
    command = calls[2]
    assert command[command.index("-threads") : command.index("-i")] == ("-threads", "1")
    assert command[command.index("-map") :] == (
        "-map",
        "0:0",
        "-map",
        "0:a?",
        "-c:v",
        "wrapped_avframe",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
    )


@pytest.mark.asyncio
async def test_unavailable_gpu_software(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hardware_caps: Capabilities
) -> None:
    calls: list[tuple[object, ...]] = []

    async def capabilities(binary: str, *, argv_prefix: tuple[str, ...]) -> Capabilities:
        return hardware_caps

    async def sample(*args: object) -> tuple[bytes, str | None]:
        return b"known-good sample", None

    async def decode(*args: object) -> str:
        return "Driver is unavailable."

    async def create(*args: object, **kwargs: object) -> Proc:
        calls.append(args)
        return Proc()

    monkeypatch.setattr(healthcheck, "probe_capabilities", capabilities)
    monkeypatch.setattr(capability_checks, "_decode_sample", sample)
    monkeypatch.setattr(capability_checks, "_test_decode", decode)
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", create)
    monkeypatch.setattr(
        healthcheck,
        "_communicate",
        lambda *_: _result(b'{"streams":[{"index":0,"codec_type":"video","codec_name":"h264"}]}', b""),
    )
    log = create_healthcheck_log(tmp_path, 1)
    result = await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", log)
    assert result.healthy
    assert calls[1][calls[1].index("-threads") : calls[1].index("-i")] == ("-threads", "1")
    assert "Driver is unavailable." in log.path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["cuda", "qsv", "vaapi"])
@pytest.mark.parametrize("codec", ["h264", "hevc"])
@pytest.mark.parametrize(
    ("code", "frames", "stderr"),
    [
        (0, b"frame=1\n", b""),
        (1, b"frame=0\n", b"Unavailable GPU."),
        (0, b"frame=0\n", b""),
        (0, b"frame=1\n", b"libva info: va_openDriver() returns 0\n"),
        (0, b"frame=1\n", b"libva info: Driver initialized.\nDecoder initialization failed.\n"),
        (1, b"frame=1\n", b"libva info: Driver initialized.\n"),
    ],
)
async def test_decoder_runtime_check(
    monkeypatch: pytest.MonkeyPatch,
    hardware_caps: Capabilities,
    backend: str,
    codec: str,
    code: int,
    frames: bytes,
    stderr: bytes,
) -> None:
    calls: list[tuple[object, ...]] = []
    inputs: list[bytes | None] = []
    priority = ("/tools/nice", "-n", "10", "/tools/ionice", "-c", "2", "-n", "7")
    hardware_caps.hwaccels = frozenset({backend})

    class Capture:
        def __init__(self, *, sample: bool) -> None:
            self.sample = sample
            self.returncode = 0 if sample else code

        async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
            inputs.append(data)
            return (b"known-good sample", b"") if self.sample else (frames, stderr)

    async def create(*args: object, **kwargs: object) -> Capture:
        calls.append(args)
        assert args[:8] == priority
        assert "-threads" in args and args[args.index("-threads") + 1] == "1"
        return Capture(sample="lavfi" in args)

    monkeypatch.setattr(capability_checks, "spawn_child", create)
    verified, diagnostics = await capability_checks.verify_decoders(
        "ffmpeg", hardware_caps, {codec}, argv_prefix=priority
    )
    decoder = {"cuda": f"{codec}_cuvid", "qsv": f"{codec}_qsv", "vaapi": codec}[backend]
    assert inputs == [None, b"known-good sample"]
    assert len(calls) == 2
    sample_command = calls[0]
    if codec == "h264":
        assert sample_command[sample_command.index("-x264-params") + 1] == "repeat-headers=1"
    else:
        assert sample_command[sample_command.index("-x265-params") + 1] == (
            "pools=none:frame-threads=1:wpp=0:repeat-headers=1"
        )
    test_decode = calls[1]
    assert test_decode[test_decode.index("-init_hw_device") + 1] == f"{backend}=healthcheck"
    assert test_decode[test_decode.index("-hwaccel_output_format") + 1] == backend
    assert test_decode[test_decode.index("-vf") + 1] == "hwdownload,format=nv12"
    assert test_decode[test_decode.index("-c:v") + 1] == decoder
    if code == 0 and frames == b"frame=1\n" and b"Decoder initialization failed." not in stderr:
        assert verified == {codec: (backend, decoder)}
        assert diagnostics == []
    else:
        assert verified == {}
        assert len(diagnostics) == 1
        if stderr:
            assert stderr.decode().strip() in diagnostics[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["sample", "decoder"])
@pytest.mark.parametrize("repeat", [False, True])
async def test_discovery_cancel_drains(monkeypatch: pytest.MonkeyPatch, stage: str, repeat: bool) -> None:
    from tests.test_healthcheck_safety import HeldProcess

    class HeldCapture(HeldProcess):
        async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
            return await super().communicate()

    proc = HeldCapture()

    async def create(*args: object, **kwargs: object) -> HeldCapture:
        return proc

    monkeypatch.setattr(capability_checks, "spawn_child", create)
    operation = (
        capability_checks._decode_sample("ffmpeg", "libx264", ())
        if stage == "sample"
        else capability_checks._test_decode("ffmpeg", "qsv", "h264_qsv", b"sample", ())
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(proc.communicating.wait(), 2)
        task.cancel()
        await asyncio.wait_for(proc.reaping.wait(), 2)
        if repeat:
            task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done()
        proc.stopped.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert proc.killed and proc.returncode == -9
    finally:
        proc.stopped.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["sample", "decoder"])
async def test_discovery_timeout_reaps(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    class TimedCapture:
        returncode: int | None = None
        killed = False

        async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
            raise TimeoutError

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    proc = TimedCapture()

    async def create(*args: object, **kwargs: object) -> TimedCapture:
        return proc

    monkeypatch.setattr(capability_checks, "spawn_child", create)
    if stage == "sample":
        await capability_checks._decode_sample("ffmpeg", "libx264", ())
    else:
        await capability_checks._test_decode("ffmpeg", "qsv", "h264_qsv", b"sample", ())
    assert proc.killed and proc.returncode == -9


@pytest.mark.asyncio
async def test_discovery_exit_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def capabilities(binary: str, *, argv_prefix: tuple[str, ...]) -> Capabilities:
        raise healthcheck.ChildNotReapedError("Hardware discovery exit is unconfirmed.")

    monkeypatch.setattr(healthcheck, "probe_capabilities", capabilities)
    monkeypatch.setattr(healthcheck, "create_subprocess_exec", _proc)
    monkeypatch.setattr(
        healthcheck,
        "_communicate",
        lambda *_: _result(b'{"streams":[{"index":0,"codec_type":"video","codec_name":"h264"}]}', b""),
    )
    with pytest.raises(healthcheck.HealthcheckExitError):
        await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["nice", "ionice"])
async def test_priority_tools_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setattr("common.capabilities.shutil.which", lambda name: None if name == missing else f"/tools/{name}")
    with pytest.raises(CapabilitiesError):
        await healthcheck._decode_healthcheck("movie", "ffmpeg", "ffprobe", None)


@pytest.fixture
def media_tools(tmp_path: Path) -> tuple[Path, Path]:
    script = f"""#!{sys.executable}
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

tool = Path(__file__)
io = subprocess.check_output([{shutil.which("ionice")!r}, '-p', str(os.getpid())], text=True).strip()
tool.with_suffix('.json').write_text(json.dumps({{
    'args': sys.argv[1:], 'pid': os.getpid(), 'nice': os.getpriority(os.PRIO_PROCESS, 0), 'io': io,
}}))
if tool.name == 'ffprobe':
    print('{{}}')
else:
    print('progress=continue', flush=True)
    if 'hold' in sys.argv:
        signal.pause()
"""
    ffmpeg, ffprobe = tmp_path / "ffmpeg", tmp_path / "ffprobe"
    for binary in (ffmpeg, ffprobe):
        binary.write_text(script)
        binary.chmod(0o755)
    return ffmpeg, ffprobe


@pytest.mark.asyncio
async def test_priority_executes_tools(media_tools: tuple[Path, Path], tmp_path: Path) -> None:
    ffmpeg, ffprobe = media_tools
    result = await asyncio.wait_for(
        healthcheck._decode_healthcheck(
            "movie name.mkv", str(ffmpeg), str(ffprobe), create_healthcheck_log(tmp_path, 1)
        ),
        3,
    )
    assert result.healthy
    for binary in media_tools:
        execution = json.loads(binary.with_suffix(".json").read_text())
        assert execution["nice"] == min(19, os.getpriority(os.PRIO_PROCESS, 0) + 10)
        assert execution["io"] == "best-effort: prio 7"
        assert "movie name.mkv" in execution["args"]


@pytest.mark.asyncio
async def test_priority_cancel_reaps(media_tools: tuple[Path, Path]) -> None:
    ffmpeg, ffprobe = media_tools
    entered = asyncio.Event()

    async def progress(update: dict[str, object]) -> None:
        if update == {"stage": "decode"} and ffmpeg.with_suffix(".json").exists():
            entered.set()

    task = asyncio.create_task(
        healthcheck._decode_healthcheck("hold", str(ffmpeg), str(ffprobe), None, on_progress=progress)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        pid = json.loads(ffmpeg.with_suffix(".json").read_text())["pid"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
