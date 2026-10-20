from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from common.capabilities import CapabilitiesError, _names, derive_worker_tags, probe_capabilities, verify_encoders
from node.capabilities import _cpu_facts, _memory_gb, resource_sample, system_facts

FAKE_FFMPEG = Path(__file__).parent / "fakes" / "ffmpeg.py"


def test_derives_cpu_only() -> None:
    assert derive_worker_tags(["libx264", "libx265", "aac"]) == {"cpu"}


def test_family_suffix_matches() -> None:
    assert derive_worker_tags(["h264_nvenc"]) == {"cpu", "gpu"}
    assert derive_worker_tags(["nvenc"]) == {"cpu", "gpu"}


def test_gpu_requires_suffix() -> None:
    # "nvenc" inside the name without being the suffix is not a hardware encoder.
    assert derive_worker_tags(["libnvenc_wrapper"]) == {"cpu"}


@pytest.mark.asyncio
async def test_intel_stream_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "card0" / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x8086\n", encoding="ascii")

    class StreamProcess:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b'[\n{"engines": {"Render/3D": {"busy": 42.5}}},\n')
            self.returncode: int | None = None

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    process = StreamProcess()

    async def start(*args: object, **kwargs: object) -> StreamProcess:
        return process

    monkeypatch.setattr("node.capabilities.shutil.which", lambda name: name if name == "intel_gpu_top" else None)
    monkeypatch.setattr("common.process.create_subprocess_exec", start)
    _, _, gpus, _ = await resource_sample(drm_root=tmp_path)
    assert [gpu["percent"] for gpu in gpus] == [42.5]
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_gpu_devices_ordered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for card, vendor, busy in (("card1", "0x8086", None), ("card0", "0x1002", "19")):
        device = tmp_path / card / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor, encoding="ascii")
        if busy is not None:
            (device / "gpu_busy_percent").write_text(busy, encoding="ascii")
    monkeypatch.setattr("node.capabilities.shutil.which", lambda _: None)

    _, _, gpus, _ = await resource_sample(drm_root=tmp_path)

    assert [gpu["percent"] for gpu in gpus] == [19.0, None]


@pytest.mark.asyncio
async def test_nvidia_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "card2" / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x10de", encoding="ascii")
    monkeypatch.setattr("node.capabilities.shutil.which", lambda _: None)

    _, _, gpus, _ = await resource_sample(drm_root=tmp_path)

    assert [gpu["percent"] for gpu in gpus] == [None]


@pytest.mark.asyncio
async def test_mixed_gpu_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for card, vendor in (("card0", "0x8086"), ("card1", "0x10de")):
        device = tmp_path / card / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor, encoding="ascii")
        if vendor == "0x8086":
            (device / "device").write_text("0x1234", encoding="ascii")

    class Process:
        returncode = 0

        def __init__(self, output: bytes) -> None:
            self.output = output
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(output)
            self.stdout.feed_eof()

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.output, b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode

    async def start(*args: str, **kwargs: object) -> Process:
        output = {
            "intel_gpu_top": b'[{"engines": {"Render/3D": {"busy": 25}}}]\n',
            "nvidia-smi": b"0, RTX, 80\n",
        }
        return Process(output[args[0]])

    monkeypatch.setattr("node.capabilities.shutil.which", lambda name: name)
    monkeypatch.setattr("common.process.create_subprocess_exec", start)

    _, _, gpus, _ = await resource_sample(drm_root=tmp_path)

    assert [gpu["percent"] for gpu in gpus] == [25.0, 80.0]


CAPABILITY_LISTING = """
Encoders:
 V....D libx264 H.264 encoder
 V..E.. h264_nvenc (NVIDIA hardware)
 A...E. aac AAC encoder
 S...E. ass subtitle encoder
 V....D hevc-vaapi hardware encoder
 V....D bad/name malformed
 V....D (alias) prose
 V....D = legend
------
 V....D
not a capability row
 V....D libx264, punctuation

Legend:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
"""

DECODER_LISTING = """
Decoders:
 V..... h264 H.264 decoder
 VFS..D hevc software decoder
 A....D aac AAC decoder
 S....D subrip subtitle decoder
 V....D av1_cuvid hardware decoder
 V....D (description) alias
 V....D = legend
--------
 V....D invalid/name
 prose words here

 V..... = Video
 A..... = Audio
 S..... = Subtitle
"""


@pytest.fixture(autouse=True)
def _executable_fake() -> None:
    FAKE_FFMPEG.chmod(0o755)


@pytest.mark.asyncio
async def test_probe_parses_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")

    caps = await probe_capabilities(FAKE_FFMPEG)

    assert caps.ffmpeg_version.startswith("ffmpeg version n7.1-fake")
    assert caps.encoders == frozenset({"libx264", "libx265", "aac", "h264_nvenc", "hevc_vaapi"})
    assert caps.video_encoders == frozenset({"libx264", "libx265", "h264_nvenc", "hevc_vaapi"})
    assert caps.audio_encoders == frozenset({"aac"})
    assert caps.decoders == frozenset({"h264", "hevc", "aac"})
    assert caps.audio_decoders == frozenset({"aac"})
    assert caps.hwaccels == frozenset({"vdpau", "cuda", "vaapi"})


def test_names_parse_rows() -> None:
    assert _names(CAPABILITY_LISTING, "Encoders:") == frozenset({"libx264", "h264_nvenc", "aac", "ass", "hevc-vaapi"})
    assert _names(CAPABILITY_LISTING, "Encoders:", "V") == frozenset({"libx264", "h264_nvenc", "hevc-vaapi"})
    assert _names(CAPABILITY_LISTING, "Encoders:", "A") == frozenset({"aac"})
    assert _names(DECODER_LISTING, "Decoders:") == frozenset({"h264", "hevc", "aac", "subrip", "av1_cuvid"})
    assert _names(DECODER_LISTING, "Decoders:", "A") == frozenset({"aac"})


@pytest.mark.asyncio
async def test_probe_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    monkeypatch.setenv("FAKE_FFMPEG_ERR", "capability listing denied")

    with pytest.raises(CapabilitiesError, match="capability listing denied"):
        await probe_capabilities(FAKE_FFMPEG)


def test_cpu_facts_parsed(tmp_path: Path) -> None:
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "processor\t: 0\n"
        "model name\t: AMD Ryzen 9 7950X 16-Core Processor\n"
        "processor\t: 1\n"
        "model name\t: AMD Ryzen 9 7950X 16-Core Processor\n",
        encoding="utf-8",
    )

    assert _cpu_facts(cpuinfo) == ("AMD Ryzen 9 7950X 16-Core Processor", 2)


def test_memory_gb_parsed(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       132012456 kB\nMemFree:         123456 kB\n", encoding="utf-8")

    assert _memory_gb(meminfo) == 125.9


@pytest.mark.asyncio
async def test_missing_system_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("node.capabilities.shutil.which", lambda _: None)
    facts = await system_facts(
        cpuinfo=tmp_path / "absent-cpuinfo", meminfo=tmp_path / "absent-meminfo", drm_root=tmp_path
    )

    assert facts.cpu_model == ""
    assert facts.cpu_cores == 0
    assert facts.memory_gb == 0.0
    assert facts.gpus == []


@pytest.mark.asyncio
async def test_gpu_binary_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fail_exec(*args: object, **kwargs: object) -> None:
        raise OSError("nvidia-smi not found")

    monkeypatch.setattr("common.process.create_subprocess_exec", fail_exec)
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nmodel name\t: Fake CPU\n", encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        1048576 kB\n", encoding="utf-8")

    facts = await system_facts(cpuinfo=cpuinfo, meminfo=meminfo, drm_root=tmp_path / "no-drm")

    assert facts.cpu_model == "Fake CPU"
    assert facts.cpu_cores == 1
    assert facts.memory_gb == 1.0
    assert facts.gpus == []


@pytest.mark.asyncio
async def test_verify_dead_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    monkeypatch.setenv("FAKE_FFMPEG_FAIL_ENCODERS", "hevc_vaapi")

    verified = await verify_encoders(FAKE_FFMPEG, {"libx264", "h264_nvenc", "hevc_vaapi"})

    assert verified == {"libx264", "h264_nvenc"}


@pytest.mark.asyncio
async def test_verify_working_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    monkeypatch.setenv("FAKE_FFMPEG_FAIL_ENCODERS", "libx264")

    verified = await verify_encoders(FAKE_FFMPEG, {"libx264", "h264_nvenc", "hevc_vaapi"})

    assert verified == {"libx264", "h264_nvenc", "hevc_vaapi"}


@pytest.mark.asyncio
async def test_verify_qsv(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def start(*args: str, **kwargs: object) -> Process:
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert "testsrc2=duration=1:size=256x256:rate=10" in args
        assert args[args.index("-c:v") + 1] == "hevc_qsv"
        return Process()

    monkeypatch.setattr("common.process.create_subprocess_exec", start)

    assert await verify_encoders("ffmpeg", {"hevc_qsv"}) == {"hevc_qsv"}


@pytest.mark.asyncio
async def test_verify_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "hang")
    monkeypatch.setattr("common.capabilities._HW_TEST_TIMEOUT_S", 0.2)

    verified = await asyncio.wait_for(verify_encoders(FAKE_FFMPEG, {"libx264", "h264_nvenc"}), timeout=10)

    assert verified == {"libx264"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["listing", "encoder", "gpu_names", "nvidia", "intel"])
async def test_cancel_discovery_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    import node.capabilities as capabilities

    started, finishing, release, interrupted = (asyncio.Event() for _ in range(4))

    class Output:
        async def readline(self) -> bytes:
            started.set()
            await asyncio.Event().wait()
            return b""

    class Process:
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = Output()
            self.killed = False
            self.communications = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communications += 1
            if self.communications > 1:
                await self.wait()
                return b"", b""
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            finishing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            self.returncode = -9
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr("common.process.create_subprocess_exec", spawn)
    monkeypatch.setattr(capabilities.shutil, "which", lambda value: value)
    operations = {
        "listing": lambda: probe_capabilities("ffmpeg"),
        "encoder": lambda: verify_encoders("ffmpeg", ["hevc_nvenc"]),
        "gpu_names": lambda: capabilities.system_facts(
            cpuinfo=tmp_path / "cpuinfo", meminfo=tmp_path / "meminfo", drm_root=tmp_path
        ),
        "nvidia": capabilities._nvidia_gpu_usage,
        "intel": capabilities._intel_gpu_usage,
    }
    task = asyncio.create_task(operations[operation]())
    task.add_done_callback(lambda _task: finishing.set())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(finishing.wait(), 2)
        assert not task.done(), f"{operation} cancellation released a live discovery child"
        assert proc.killed and proc.returncode is None
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not interrupted.is_set(), f"repeated cancellation interrupted {operation} child reaping"
        assert not task.done() and proc.returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert proc.returncode == -9
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["listing", "encoder", "gpu_names", "nvidia", "intel"])
async def test_unconfirmed_discovery_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    import node.capabilities as capabilities
    from common.process import ChildNotReapedError

    class Output:
        async def readline(self) -> bytes:
            raise TimeoutError

    class Process:
        returncode = None
        stdout = Output()

        async def communicate(self) -> tuple[bytes, bytes]:
            raise TimeoutError

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            raise TimeoutError

    async def spawn(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("common.process.create_subprocess_exec", spawn)
    monkeypatch.setattr(capabilities.shutil, "which", lambda value: value)
    operations = {
        "listing": lambda: probe_capabilities("ffmpeg"),
        "encoder": lambda: verify_encoders("ffmpeg", ["hevc_nvenc"]),
        "gpu_names": lambda: capabilities.system_facts(
            cpuinfo=tmp_path / "cpuinfo", meminfo=tmp_path / "meminfo", drm_root=tmp_path
        ),
        "nvidia": capabilities._nvidia_gpu_usage,
        "intel": capabilities._intel_gpu_usage,
    }
    outcome = (await asyncio.wait_for(asyncio.gather(operations[operation](), return_exceptions=True), 2))[0]
    assert isinstance(outcome, ChildNotReapedError), f"{operation} lost its unconfirmed child exit: {outcome!r}"
