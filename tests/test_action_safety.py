from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import node.process as process
from common.actions import ExtractSubtitles
from node.actions import run_actions
from common.process import ChildNotReapedError
from tests.test_node_actions import FAKE_FFMPEG, probe, spec


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", ["partial", "artifact"])
async def test_occupied_artifacts_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupied: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    src = root / "movie.mkv"
    src.write_bytes(b"source")
    target = root / ("movie.eng.srt.partial" if occupied == "partial" else "movie.eng.srt")
    target.write_bytes(b"existing data")
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    outcomes = await asyncio.wait_for(
        run_actions(spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng")), str(FAKE_FFMPEG)),
        2,
    )
    assert target.exists(), "extraction removed an unexpected partial"
    assert target.read_bytes() == b"existing data", "extraction overwrote an occupied artifact"
    assert not any(outcome.ok for outcome in outcomes)
    assert src.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_labels_preserve_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def spawn(*args, **_kwargs):
        Path(args[-1]).write_bytes(args[args.index("-map") + 1].encode())
        return Process()

    monkeypatch.setattr(process, "create_tool", spawn)
    outcomes = await asyncio.wait_for(
        run_actions(
            spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng", "eng", "eng.1")), "ffmpeg"
        ),
        2,
    )
    if all(outcome.ok for outcome in outcomes):
        artifacts = [artifact for outcome in outcomes for artifact in outcome.artifacts]
        assert len(set(artifacts)) == 3, "two retained subtitle streams were reported as the same overwritten artifact"
        assert {(tmp_path / artifact).read_bytes() for artifact in artifacts} == {b"0:1", b"0:2", b"0:3"}
    else:
        assert any(outcome.error for outcome in outcomes)
    assert src.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_repeated_cancel_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, reaping, release = (asyncio.Event() for _ in range(3))
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")
    partial = tmp_path / "movie.eng.srt.partial"

    class Process:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"child output")
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        def send_signal(self, _signal) -> None:
            pass

        async def wait(self) -> int:
            reaping.set()
            await release.wait()
            self.returncode = -9
            return self.returncode

    proc = Process()

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(process, "create_tool", spawn)
    task = asyncio.create_task(
        run_actions(spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng")), "ffmpeg")
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(reaping.wait(), 2)
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done(), "repeated cancellation completed the action before child exit was confirmed"
        assert partial.read_bytes() == b"child output" and proc.returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert proc.returncode == -9 and not partial.exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["signal", "communicate"])
async def test_unconfirmed_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")
    partial = tmp_path / "movie.eng.srt.partial"

    class Process:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"child output")
            if failure == "communicate":
                raise OSError("pipe failed")
            raise TimeoutError

        def send_signal(self, _signal) -> None:
            raise PermissionError("kill denied")

        async def wait(self) -> int:
            raise TimeoutError

    async def spawn(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(process, "create_tool", spawn)
    outcome = (
        await asyncio.wait_for(
            asyncio.gather(
                run_actions(spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng")), "ffmpeg"),
                return_exceptions=True,
            ),
            2,
        )
    )[0]
    assert partial.read_bytes() == b"child output"
    assert isinstance(outcome, ChildNotReapedError), (
        f"live extraction child was reported as completed failure: {outcome!r}"
    )


@pytest.mark.asyncio
async def test_log_write_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import node.actions as actions

    writing, written = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = actions._append_results
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")

    def blocked_write(path, outcomes):
        loop.call_soon_threadsafe(writing.set)
        try:
            assert release.wait(5)
            original(path, outcomes)
        finally:
            loop.call_soon_threadsafe(written.set)

    monkeypatch.setattr(actions, "_append_results", blocked_write)
    task = asyncio.create_task(run_actions(spec(src, [], None), "ffmpeg"))
    try:
        await asyncio.wait_for(writing.wait(), 2)
        task.cancel()
        checkpoint = asyncio.Event()
        loop.call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 2)
        assert not task.done(), "action cancellation released workspace locks while its log writer remained active"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert written.is_set()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(written.wait(), 2)


@pytest.mark.asyncio
async def test_publication_collision_retains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")
    partial = tmp_path / "movie.eng.srt.partial"
    artifact = tmp_path / "movie.eng.srt"

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            partial.write_bytes(b"owned subtitle")
            artifact.write_bytes(b"unexpected subtitle")
            return b"", b""

    async def spawn(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(process, "create_tool", spawn)
    outcomes = await asyncio.wait_for(
        run_actions(spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng")), "ffmpeg"), 2
    )
    assert len(outcomes) == 1 and not outcomes[0].ok and outcomes[0].error
    assert outcomes[0].artifacts == []
    assert artifact.read_bytes() == b"unexpected subtitle" and not partial.exists()
    assert src.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_repeated_artifacts_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")
    artifact = tmp_path / "movie.eng.srt"
    calls: list[str] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def spawn(*args, **_kwargs):
        fmt = args[args.index("-f") + 1]
        calls.append(fmt)
        Path(args[-1]).write_bytes(f"subtitle {len(calls)}".encode())
        return Process()

    monkeypatch.setattr(process, "create_tool", spawn)
    outcomes = await asyncio.wait_for(
        run_actions(
            spec(
                src,
                [
                    ExtractSubtitles(formats=["srt", "vtt"], languages=[]),
                    ExtractSubtitles(formats=["srt"], languages=[]),
                ],
                probe(src, "eng"),
            ),
            "ffmpeg",
        ),
        2,
    )
    assert calls == ["srt", "webvtt"] and len(outcomes) == 3
    assert src.read_bytes() == b"source" and artifact.read_bytes() == b"subtitle 1"
    assert (tmp_path / "movie.eng.vtt").read_bytes() == b"subtitle 2"
    assert outcomes[0].ok and outcomes[1].ok
    assert not outcomes[2].ok and outcomes[2].error and not outcomes[2].artifacts


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_handoff_failure_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import node.actions as actions

    writing, written = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"source")
    original_write = actions._append_results

    def blocked_write(path, outcomes):
        loop.call_soon_threadsafe(writing.set)
        try:
            assert release.wait(5)
            original_write(path, outcomes)
            if failure == "error":
                raise OSError("action log failed")
        finally:
            loop.call_soon_threadsafe(written.set)

    monkeypatch.setenv("FAKE_FFMPEG_MODE", "ok")
    monkeypatch.setattr(actions, "_append_results", blocked_write)
    task = asyncio.create_task(
        run_actions(spec(src, [ExtractSubtitles(formats=["srt"], languages=[])], probe(src, "eng")), str(FAKE_FFMPEG))
    )
    try:
        await asyncio.wait_for(writing.wait(), 2)
        if failure == "cancel":
            for _ in range(2):
                task.cancel()
                checkpoint = asyncio.Event()
                loop.call_soon(checkpoint.set)
                await asyncio.wait_for(checkpoint.wait(), 2)
                assert not task.done() and not written.is_set()
        release.set()
        with pytest.raises(OSError if failure == "error" else asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert written.is_set()
        assert src.read_bytes() == b"source" and (tmp_path / "movie.eng.srt").read_bytes() == b"\0" * 100
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(written.wait(), 2)
