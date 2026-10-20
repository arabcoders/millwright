from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from common.actions import ActionOutcome, ExtractSubtitles
from common.media.probe import ProbeData, ProbeStream

import node.actions as node_actions
from node.actions import ActionsSpec, run_actions
from common.process import ChildNotReapedError
from tests.helpers import make_test_temp_dir

FAKE_FFMPEG = Path(__file__).parent / "fakes" / "ffmpeg.py"

_TEST_TIMEOUT_S = 10.0


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    FAKE_FFMPEG.chmod(0o755)
    return make_test_temp_dir("node-actions")


def probe(src: Path, *languages: str) -> ProbeData:
    streams = [ProbeStream(index=0, codec="h264", codec_type="video")]
    streams += [
        ProbeStream(index=n, codec="subrip", codec_type="subtitle", language=lang)
        for n, lang in enumerate(languages, 1)
    ]
    return ProbeData(file_path=str(src), duration_s=10.0, size_bytes=1000, streams=streams)


def spec(src: Path, actions: list, probe: ProbeData | None) -> ActionsSpec:
    return ActionsSpec(
        job_id=7,
        path=src,
        probe=probe,
        actions=actions,
        workspace_dir=src.parent,
        log_path=src.parent / "actions.log",
    )


async def run(spec_spec: ActionsSpec) -> list[ActionOutcome]:
    coro = run_actions(spec_spec, str(FAKE_FFMPEG))
    return await asyncio.wait_for(coro, timeout=_TEST_TIMEOUT_S)


@pytest.mark.asyncio
@pytest.mark.parametrize(("fmt", "muxer"), [("srt", "srt"), ("vtt", "webvtt")])
async def test_explicit_muxer(env: Path, monkeypatch: pytest.MonkeyPatch, fmt: str, muxer: str) -> None:
    src = env / "movie.mkv"
    src.write_bytes(b"source")
    out = env / f"movie.eng.{fmt}"
    partial = out.with_name(out.name + ".partial")
    calls: list[tuple[str, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            argv = calls[-1]
            if "-f" not in argv or argv[argv.index("-f") + 1] not in {"srt", "webvtt"}:
                self.returncode = 1
                return b"", b"unknown output format"
            assert not out.exists()
            Path(argv[-1]).write_bytes(b"subtitle")
            return b"", b""

    async def spawn(*args: str, **kwargs: object) -> Process:
        calls.append(args)
        assert "shell" not in kwargs
        source, output = args[args.index("-i") + 1], args[-1]
        assert source == str(src) and output == str(partial)
        assert Path(source).read_bytes() == b"source"
        assert partial.is_file() and Path(output).read_bytes() == b""
        return Process()

    monkeypatch.setattr("node.process.create_tool", spawn)
    outcomes = await run(spec(src, [ExtractSubtitles(formats=[fmt], languages=["eng"])], probe(src, "eng")))

    assert calls == [
        (
            str(FAKE_FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-map",
            "0:1",
            "-c:s",
            muxer,
            "-f",
            muxer,
            str(partial),
        )
    ]
    assert outcomes == [ActionOutcome(kind="extract_subtitles", ok=True, artifacts=[out.name])]
    assert out.read_bytes() == b"subtitle"
    assert not partial.exists()
    assert src.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_duplicate_languages(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = env / "output.mkv"
    src.write_bytes(b"source")

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def spawn(*args: str, **kwargs: object) -> Process:
        stream = args[args.index("-map") + 1]
        Path(args[-1]).write_bytes(f"subtitle {stream}".encode())
        return Process()

    monkeypatch.setattr("node.process.create_tool", spawn)
    outcomes = await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng", "eng")))

    assert outcomes == [
        ActionOutcome(kind="extract_subtitles", ok=True, artifacts=["output.eng.1.srt"]),
        ActionOutcome(kind="extract_subtitles", ok=True, artifacts=["output.eng.2.srt"]),
    ]
    assert (env / "output.eng.1.srt").read_bytes() == b"subtitle 0:1"
    assert (env / "output.eng.2.srt").read_bytes() == b"subtitle 0:2"
    assert src.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_filters_languages(env: Path) -> None:
    src = env / "movie.mkv"
    src.write_bytes(b"x" * 100)
    outcome = await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=["fre"])], probe(src, "eng", "fra")))

    assert outcome == [ActionOutcome(kind="extract_subtitles", ok=True, error=None, artifacts=["movie.fra.srt"])]
    assert (env / "movie.fra.srt").is_file()
    assert (env / "movie.fra.srt").stat().st_size == 100
    assert not (env / "movie.fra.srt.partial").exists()
    assert (env / "actions.log").is_file()
    assert not (env / "movie.eng.srt").exists()


@pytest.mark.asyncio
async def test_no_probe_fails(env: Path) -> None:
    src = env / "movie.mkv"
    src.write_bytes(b"x" * 100)
    outcome = await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=["eng"])], None))

    assert [(item.kind, item.ok, item.artifacts) for item in outcome] == [("extract_subtitles", False, [])]
    assert not (env / "movie.eng.srt").exists()


@pytest.mark.asyncio
async def test_extraction_failure_reported(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    src = env / "movie.mkv"
    src.write_bytes(b"x" * 100)
    outcome = await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=["eng"])], probe(src, "eng")))

    assert outcome[0].ok is False
    assert not (env / "movie.eng.srt").exists()
    assert (env / "actions.log").is_file()


@pytest.mark.asyncio
async def test_timeout_cleans_partial(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = env / "movie.mkv"
    src.write_bytes(b"x" * 100)
    partial = env / "movie.eng.srt.partial"
    calls: list[str] = []

    class Process:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"incomplete")
            raise TimeoutError

        def send_signal(self, _signal) -> None:
            calls.append("kill")
            raise ProcessLookupError

        async def wait(self) -> int:
            calls.append("wait")
            self.returncode = -9
            return self.returncode

    async def spawn(*args: str, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr("node.process.create_tool", spawn)
    outcome = await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=["eng"])], probe(src, "eng")))

    assert outcome[0].ok is False
    assert calls == ["kill", "wait"]
    assert not partial.exists()
    assert src.read_bytes() == b"x" * 100


@pytest.mark.asyncio
async def test_cancel_reaps_child(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = env / "movie.mkv"
    src.write_bytes(b"source")
    partial = env / "movie.eng.srt.partial"
    calls: list[str] = []

    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"incomplete")
            raise asyncio.CancelledError

        def send_signal(self, _signal) -> None:
            calls.append("kill")

        async def wait(self) -> int:
            assert partial.exists()
            calls.append("wait")
            self.returncode = -9
            return self.returncode

    async def spawn(*args: str, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr("node.process.create_tool", spawn)
    with pytest.raises(asyncio.CancelledError):
        await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=["eng"])], probe(src, "eng")))
    assert calls == ["kill", "wait"]
    assert not partial.exists()
    assert src.read_bytes() == b"source"


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [TimeoutError, asyncio.CancelledError])
async def test_unreaped_child_propagates(
    env: Path, monkeypatch: pytest.MonkeyPatch, stopped: type[BaseException]
) -> None:
    src = env / "movie.mkv"
    src.write_bytes(b"source")
    partial = env / "movie.eng.srt.partial"
    calls: list[str] = []

    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"incomplete")
            raise stopped

        def send_signal(self, _signal) -> None:
            calls.append("kill")

        async def wait(self) -> int:
            calls.append("wait")
            raise TimeoutError

    async def spawn(*args: str, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr("node.process.create_tool", spawn)
    with pytest.raises(ChildNotReapedError):
        await run(spec(src, [ExtractSubtitles(formats=["srt"], languages=["eng"])], probe(src, "eng")))
    assert calls == ["kill", "wait"]
    assert partial.read_bytes() == b"incomplete"
    assert src.read_bytes() == b"source"
