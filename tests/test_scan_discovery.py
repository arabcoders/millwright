from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from common.process import ChildNotReapedError
from server.settings import ScannerConfig
from server.models import Library, MediaFile, utcnow
from server.scanner import scan
from server.staging import StagingService
from server.workspace import wait_workspace_io
from tests.helpers import database_context

pytestmark = pytest.mark.usefixtures("without_fd")
_NATIVE_FD = shutil.which("fd") or shutil.which("fdfind")


@pytest.fixture
def fake_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fd"
    shutil.copyfile(Path(__file__).parent / "fakes" / "fd.py", binary)
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_FD_LOG", str(tmp_path / "fd-log.jsonl"))
    return binary


@pytest.mark.parametrize("tool", ["fd", "fdfind", None])
def test_discovery_matches_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_fd: Path, tool: str | None):
    root = tmp_path / "library"
    names = [
        "z.MKV",
        "a.mkv",
        "b/z.mp4",
        "b/a/movie.mkv",
        "b.mkv/movie.mkv",
        "a/z.mkv",
        "a/z/a.mkv",
        "a/z/space\n\t雪.mkv",
        "a/z/bytes\udcff.mkv",
        "glob[1]*/hidden.mkv",
        "glob1x/kept.mkv",
        "movie.mkv.partial/nested.mkv",
        "dir.TdarrCacheFile-1/nested.mkv",
        ".hidden/movie.mkv",
        ".hidden.mkv",
        "Extras/movie.mkv",
        "workspace/movie.mkv",
        "trash/movie.mkv",
        "other/workspace/kept.mkv",
        "notes.txt",
        "movie.TdarrCacheFile-1.mkv",
        "other.mkv.partial",
        "literal.mkv",  # An ignored directory name is not an ignored filename.
        "name/child/movie.mkv",
    ]
    ignored = {"Extras", "glob[1]*", "name/child", ""}
    ignored.add("literal.mkv")
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
    (root / ".gitignore").write_text("*.mkv\n*.mp4\n")
    (root / ".fdignore").write_text("*\n")
    (root / "linked.mkv").symlink_to(root / "a.mkv")
    (root / "linked").symlink_to(root / "a", target_is_directory=True)
    hidden_root = tmp_path / ".library"
    hidden_root.mkdir()
    (hidden_root / "movie.mkv").write_bytes(b"media")
    excluded = (root / "workspace", root / "trash")
    roots = [root, hidden_root, root / "a", root / "workspace"]
    expected = scan._wanted_files(roots, ignored, {"mkv", "mp4", "partial"}, [], set(), excluded)
    errors, unavailable, progress = [], set(), []
    lookups = []

    def which(name):
        lookups.append(name)
        return str(fake_fd) if name == tool else None

    monkeypatch.setattr(scan.shutil, "which", which)
    # fd's parallel output need not follow tree order.
    outputs = {
        str(root): {
            "paths": ["./" + name for name in reversed(names) if not name.startswith(("workspace/", "trash/"))]
        },
        str(root / "a"): {
            "paths": ["./" + str(path.relative_to(root / "a")) for path in (root / "a").rglob("*") if path.is_file()]
        },
    }
    monkeypatch.setenv("FAKE_FD_RESULTS", json.dumps(outputs))
    clock = iter(range(1000))
    monkeypatch.setattr(scan.time, "monotonic", lambda: next(clock))
    actual = scan._wanted_files(
        roots, ignored, {"mkv", "mp4", "partial"}, errors, unavailable, excluded, on_found=progress.append
    )
    assert actual == expected
    assert actual[:2] == [root / "a.mkv", root / "literal.mkv"]
    assert root / "movie.mkv.partial/nested.mkv" in actual
    assert root / "dir.TdarrCacheFile-1/nested.mkv" in actual
    assert root / "movie.TdarrCacheFile-1.mkv" in actual
    assert not errors and not unavailable
    assert progress == list(range(1, len(actual) + 1))
    assert lookups == (["fd"] if tool == "fd" else ["fd", "fdfind"])
    log = tmp_path / "fd-log.jsonl"
    if tool is None:
        assert not log.exists()
    else:
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert [call["cwd"] for call in calls] == [str(root), str(root / "a")]
        argv = calls[0]["argv"]
        assert "--no-ignore" in argv and "--show-errors" in argv and "--no-follow" in argv
        assert argv[argv.index("--type") + 1] == "file" and "--print0" in argv
        assert "**/glob\\[1\\]\\*/" in argv and "**/literal.mkv/" in argv
        assert "/workspace/" in argv and "/trash/" in argv
        assert argv[-3:] == ["--", r"(?-u)\.(?:mkv|mp4|partial)$", "."]


@pytest.mark.skipif(_NATIVE_FD is None, reason="fd is not installed")
def test_native_discovery_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert _NATIVE_FD is not None
    paths = [tmp_path / name for name in ("upper.MKV", "bytes\udcff.mkv", "line\n雪.mp4")]
    for path in paths:
        path.write_bytes(b"media")
    (tmp_path / "notes.txt").write_text("not media")
    monkeypatch.setattr(scan.shutil, "which", lambda name: _NATIVE_FD if name == "fd" else None)
    errors, unavailable = [], set()
    assert scan._wanted_files([tmp_path], set(), {"mkv", "mp4"}, errors, unavailable, ()) == sorted(paths)
    assert not errors and not unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "stderr", "spawn"])
@pytest.mark.parametrize("scoped", [False, True])
async def test_failed_discovery_preserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_fd: Path, failure: str, scoped: bool
):
    root = tmp_path / "library"
    root.mkdir()
    visible = root / "visible.mkv"
    visible.write_bytes(b"media")
    missing = root / "missing.mkv"

    def walk(*_args, **_kwargs):
        pytest.fail("Installed fd failures must not fall back to os.walk.")

    monkeypatch.setattr(scan.os, "walk", walk)
    monkeypatch.setattr(scan.shutil, "which", lambda name: str(fake_fd) if name == "fd" else None)
    monkeypatch.setenv(
        "FAKE_FD_RESULTS",
        json.dumps(
            {
                str(root): {
                    "paths": ["./visible.mkv"],
                    "stderr": "Storage I/O failure: /library/offline" if failure == "stderr" else "",
                    "code": 1 if failure == "exit" else 0,
                }
            }
        ),
    )
    if failure == "spawn":

        def unavailable(*_args, **_kwargs):
            raise OSError(5, "Cannot spawn installed fd", str(fake_fd))

        monkeypatch.setattr(scan.subprocess, "Popen", unavailable)
    async with database_context("fd-failure") as factory:
        async with factory() as session:
            library = Library(name="library", roots=[str(root)])
            session.add(library)
            await session.flush()
            retained = MediaFile(
                library_id=library.id,
                path=str(missing),
                container="mkv",
                size=1,
                mtime_ns=1,
                medium="other",
                last_scanned_at=utcnow(),
            )
            session.add(retained)
            await session.commit()
            report = await scan.scan_library(
                session,
                library,
                config=ScannerConfig(),
                staging=StagingService(factory, tmp_path / "workspace", min_free_gb=1),
                target_paths={root} if scoped else None,
            )
            assert report.pruned == 0 and report.found == report.added == int(failure != "spawn")
            assert len(report.errors) == 1 and str(root) in report.errors[0]
            if failure == "spawn":
                assert "Cannot spawn installed fd" in report.errors[0]
            elif failure == "stderr":
                assert "Storage I/O failure: /library/offline" in report.errors[0]
            assert await session.get(MediaFile, retained.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("drain_failure", [False, True])
async def test_cancel_reaps_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_fd: Path, cancel: bool, drain_failure: bool
):
    root = tmp_path / "library"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"media")
    entered = asyncio.Event()
    stopped = threading.Event()
    loop = asyncio.get_running_loop()
    children = []
    drains = []
    spawn = subprocess.Popen
    gate_read, gate_write = os.pipe()

    def popen(*args, **kwargs):
        proc = spawn(*args, **kwargs, pass_fds=(gate_read,))
        children.append(proc)
        communicate = proc.communicate

        def drain(*, timeout):
            drains.append(timeout)
            if drain_failure:
                raise subprocess.TimeoutExpired(args[0], timeout, stderr=b"Storage drain failed.")
            return communicate(timeout=timeout)

        monkeypatch.setattr(proc, "communicate", drain)
        return proc

    def found(_count: int) -> None:
        loop.call_soon_threadsafe(entered.set)

    monkeypatch.setattr(scan.shutil, "which", lambda name: str(fake_fd) if name == "fd" else None)
    monkeypatch.setattr(scan.subprocess, "Popen", popen)
    monkeypatch.setenv("FAKE_FD_GATE", str(gate_read))
    monkeypatch.setenv(
        "FAKE_FD_RESULTS",
        json.dumps({str(root): {"paths": ["./movie.mkv"], "stderr": "diagnostic\n" * 10000, "stall": True}}),
    )
    errors, unavailable = [], set()
    task = asyncio.create_task(
        wait_workspace_io(
            lambda: scan._wanted_files(
                [root],
                set(),
                {"mkv"},
                errors,
                unavailable,
                (),
                stopped=stopped,
                on_found=found,
            )
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert len(children) == 1 and children[0].poll() is None
        if cancel:
            task.cancel()
        stopped.set()
        if drain_failure:
            with pytest.raises(ChildNotReapedError, match="Storage drain failed") as raised:
                await asyncio.wait_for(task, 3)
            assert str(root) in str(raised.value) and "diagnostic" in str(raised.value)
        elif cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            assert await asyncio.wait_for(task, 3) == [root / "movie.mkv"]
        proc = children[0]
        if not drain_failure:
            assert proc.returncode < 0
        assert drains == [5]
        assert proc.stdout.closed and proc.stderr.closed
        assert not errors and not unavailable
    finally:
        stopped.set()
        os.close(gate_write)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        os.close(gate_read)
        for proc in children:
            proc.wait(timeout=3)
