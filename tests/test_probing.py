from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.fs import library as library_fs
from server.fs.library import LibraryFS, LibraryProbeError
from server.fs.source import restore_source_dates
from server.models import Job, MediaFile, ProbeRow
from server.probing import ProbeService
from common.media import tools as probe
from tests.test_metadata_jobs import RAW, db, media


class ControlledProcess:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.killed = asyncio.Event()
        self.exited = asyncio.Event()
        self.reaped = asyncio.Event()
        self.output_closed = asyncio.Event()
        self.returncode: int | None = None
        self.stdout = self.stderr = SimpleNamespace(read=self.read)

    async def read(self, _size: int) -> bytes:
        self.entered.set()
        await self.output_closed.wait()
        return b""

    async def communicate(self) -> tuple[bytes, bytes]:
        self.entered.set()
        await self.output_closed.wait()
        return b"{}", b""

    def kill(self) -> None:
        self.killed.set()

    async def wait(self) -> int:
        await self.exited.wait()
        self.returncode = -9
        self.reaped.set()
        return self.returncode


async def checkpoint() -> None:
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await asyncio.wait_for(reached.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ffprobe", "mkvpropedit"])
@pytest.mark.parametrize("during_spawn", [False, True])
async def test_repeated_cancellation_reaps(monkeypatch, tmp_path: Path, name: str, during_spawn: bool) -> None:
    process = ControlledProcess()
    spawning, spawned = asyncio.Event(), asyncio.Event()
    if not during_spawn:
        spawned.set()

    async def spawn(*_args, **_kwargs):
        spawning.set()
        await spawned.wait()
        return process

    monkeypatch.setattr(probe, "create_subprocess_exec", spawn)
    task = asyncio.create_task(getattr(probe, f"run_{name}")(name, tmp_path / "source.mkv"))
    try:
        await asyncio.wait_for(spawning.wait(), 2)
        if not during_spawn:
            await asyncio.wait_for(process.entered.wait(), 2)
        for _ in range(2):
            task.cancel()
            await checkpoint()
            assert not task.done()
        spawned.set()
        await asyncio.wait_for(process.killed.wait(), 2)
        task.cancel()
        process.exited.set()
        await asyncio.wait_for(process.reaped.wait(), 2)
        assert not task.done()
    finally:
        spawned.set()
        process.exited.set()
        process.output_closed.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_timeout_drains_pipes(monkeypatch, tmp_path: Path) -> None:
    process = ControlledProcess()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(probe, "create_subprocess_exec", spawn)
    task = asyncio.create_task(probe.run_ffprobe("ffprobe", tmp_path / "source.mkv", timeout_s=0))
    try:
        await asyncio.wait_for(process.killed.wait(), 2)
        process.exited.set()
        await asyncio.wait_for(process.reaped.wait(), 2)
        assert not task.done()
    finally:
        process.exited.set()
        process.output_closed.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert isinstance(outcomes[0], probe.ProbeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ffprobe", "mkvpropedit"])
async def test_reaping_failure_surfaces(monkeypatch, tmp_path: Path, name: str) -> None:
    class FailedWait(ControlledProcess):
        async def wait(self) -> int:
            await self.exited.wait()
            raise OSError("Exit status unavailable.")

    process = FailedWait()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(probe, "create_subprocess_exec", spawn)
    task = asyncio.create_task(getattr(probe, f"run_{name}")(name, tmp_path / "source.mkv"))
    try:
        await asyncio.wait_for(process.entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(process.killed.wait(), 2)
    finally:
        process.exited.set()
        process.output_closed.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert isinstance(outcomes[0], OSError)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ffprobe", "mkvpropedit"])
async def test_kill_failure_drains(monkeypatch, tmp_path: Path, name: str) -> None:
    class FailedKill(ControlledProcess):
        def kill(self) -> None:
            super().kill()
            raise OSError("Kill failed.")

    process = FailedKill()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(probe, "create_subprocess_exec", spawn)
    task = asyncio.create_task(getattr(probe, f"run_{name}")(name, tmp_path / "source.mkv"))
    try:
        await asyncio.wait_for(process.entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(process.killed.wait(), 2)
        task.cancel()
        await checkpoint()
        assert not task.done() and process.returncode is None
    finally:
        process.exited.set()
        process.output_closed.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert isinstance(outcomes[0], OSError)
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_editor_streams_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exited, progressed = asyncio.Event(), asyncio.Event()
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    percentages = []

    class Editor:
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout, self.stderr = stdout, stderr

        async def wait(self) -> int:
            await exited.wait()
            self.returncode = 0
            return self.returncode

        def kill(self) -> None:
            exited.set()
            stdout.feed_eof()
            stderr.feed_eof()

    async def spawn(*_args, **_kwargs):
        return Editor()

    async def progress(percent: int) -> None:
        percentages.append(percent)
        progressed.set()

    monkeypatch.setattr(probe, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        probe._run(["editor", str(tmp_path / "movie.mkv")], None, "mkvpropedit", on_progress=progress)
    )
    try:
        stdout.feed_data(b"x" * (4096 * 5 - len(b"\rProgress: 27")) + b"\rProgress: 27")
        stdout.feed_data(b"%\r")
        await asyncio.wait_for(progressed.wait(), 2)
        assert percentages == [27] and not task.done()
        stdout.feed_data(b"y" * 20000 + b"\rProgress: 100%\rDone.")
        stderr.feed_data(b"z" * 20000 + b"Warning: source structure resynchronized.")
        stdout.feed_eof()
        stderr.feed_eof()
        exited.set()
        code, output, error = await asyncio.wait_for(task, 2)
        assert code == 0 and len(output) == len(error) == 8192
        assert output.endswith(b"Done.") and percentages == [27, 100]
    finally:
        if not task.done():
            task.cancel()
        exited.set()
        stdout.feed_eof()
        stderr.feed_eof()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_editor_warns_reprobes(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool) -> None:
    media_id = await media(db, tmp_path, "movie.mkv")
    calls = 0
    async with db() as session:
        row = await session.get(MediaFile, media_id)
        row.transcode_decided = True
        first_seen = row.first_seen
        await session.commit()

    async def run(argv, timeout, name, **_kwargs):
        nonlocal calls
        if name == "ffprobe":
            calls += 1
            raw = json.loads(json.dumps(RAW))
            if calls == 2:
                if failed:
                    return 0, b"invalid probe JSON", b""
                raw["streams"][0]["bit_rate"] = "500"
            return 0, json.dumps(raw).encode(), b""
        assert timeout is None and name == "mkvpropedit"
        assert "-n" in argv and "7" in argv
        with Path(argv[-1]).open("ab") as edited:
            edited.write(b"statistics")
        return 1, b"Warning: resynchronized. Done.", b""

    monkeypatch.setattr(probe, "_run", run)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    try:
        for _ in range(2):
            await service._fill()
            await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        assert calls == 2
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            assert row is not None and row.transcode_decided and row.first_seen == first_seen
            assert row.size == (tmp_path / "movie.mkv").stat().st_size
            if failed:
                assert row.probe_id is None and row.health_status == "probe_failed"
            else:
                assert row.probe_id is not None
                stored = await session.get(ProbeRow, row.probe_id)
                assert stored is not None
                assert json.loads(stored.raw_json)["streams"][0]["bit_rate"] == "500"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_restores_dates(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mkv", stored=True)
    path = tmp_path / "movie.mkv"
    dates = (1_600_000_000_000_000_111, path.stat().st_mtime_ns)
    os.utime(path, ns=dates)
    process = ControlledProcess()
    restoring = asyncio.Event()
    restore_release = threading.Event()
    restore = restore_source_dates
    loop = asyncio.get_running_loop()

    async def spawn(*argv, **_kwargs):
        with Path(argv[-1]).open("ab") as edited:
            edited.write(b"statistics")
        return process

    def restore_dates(*args):
        loop.call_soon_threadsafe(restoring.set)
        if not restore_release.wait(5):
            raise TimeoutError("Test restoration release missing.")
        return restore(*args)

    monkeypatch.setattr(probe, "create_subprocess_exec", spawn)
    monkeypatch.setattr(library_fs, "restore_source_dates", restore_dates)
    service = ProbeService(db, "probe", "editor", workspace_dir=tmp_path)
    closing = None
    try:
        await service._fill()
        await asyncio.wait_for(process.entered.wait(), 3)
        job_id, task = next(iter(service._tasks.items()))
        assert await service.cancel_job(job_id)
        await asyncio.wait_for(process.killed.wait(), 2)
        closing = asyncio.create_task(service.close())
        process.exited.set()
        process.output_closed.set()
        await asyncio.wait_for(restoring.wait(), 2)
        await service._recover_jobs()
        async with db() as session:
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "running"
        for _ in range(2):
            task.cancel()
            closing.cancel()
            await checkpoint()
            assert not task.done() and not closing.done()
        restore_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(closing), 3)
        stat = path.stat()
        assert (stat.st_atime_ns, stat.st_mtime_ns) == dates
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            job = await session.get(Job, job_id)
            assert row.metadata_cancelled and row.probe_id is None
            assert job.status == "cancelled"
    finally:
        restore_release.set()
        process.exited.set()
        process.output_closed.set()
        if closing is not None:
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 3)
        await service.close()


@pytest.mark.asyncio
async def test_cancel_postedit_stat(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mkv")
    path = tmp_path / "movie.mkv"
    dates = (1_600_000_000_000_000_111, path.stat().st_mtime_ns)
    os.utime(path, ns=dates)
    entered, checkpoint = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    stat = Path.stat
    edited = False

    async def metadata(*_args, **_kwargs):
        return RAW

    async def edit(_bin, source, **_kwargs):
        nonlocal edited
        with source.open("ab") as handle:
            handle.write(b"statistics")
        edited = True

    def held_stat(current, *args, **kwargs):
        nonlocal edited
        if current == path and edited:
            edited = False
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("Post-edit stat was not released.")
        return stat(current, *args, **kwargs)

    monkeypatch.setattr(library_fs, "run_ffprobe", metadata)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    monkeypatch.setattr(Path, "stat", held_stat)
    async with db() as session:
        row = await session.get(MediaFile, media_id)
        library_id = row.library_id
    fs = LibraryFS(library_id, sessionmaker=db, ffprobe_bin="probe", mkvpropedit_bin="editor", workspace_dir=tmp_path)
    task = asyncio.create_task(fs.refresh_current(media_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(2):
            task.cancel()
            checkpoint.clear()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        current = path.stat()
        assert (current.st_atime_ns, current.st_mtime_ns) == dates
        async with db() as session:
            row = await session.get(MediaFile, media_id)
            assert row.probe_id is None and row.health_status == "probe_failed"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_failed_dates_invalidate(db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = await media(db, tmp_path, "movie.mkv")

    async def metadata(*_args, **_kwargs):
        return RAW

    async def edit(_bin, source, **_kwargs):
        with source.open("ab") as handle:
            handle.write(b"statistics")

    def fail_restore(*_args):
        raise OSError("Date restoration failed.")

    monkeypatch.setattr(library_fs, "run_ffprobe", metadata)
    monkeypatch.setattr(library_fs, "run_mkvpropedit", edit)
    monkeypatch.setattr(library_fs, "restore_source_dates", fail_restore)
    async with db() as session:
        row = await session.get(MediaFile, media_id)
        library_id = row.library_id
    fs = LibraryFS(library_id, sessionmaker=db, ffprobe_bin="probe", mkvpropedit_bin="editor", workspace_dir=tmp_path)
    with pytest.raises(LibraryProbeError):
        await fs.refresh_current(media_id)
    async with db() as session:
        row = await session.get(MediaFile, media_id)
        assert row.probe_id is None and row.health_status == "probe_failed"
