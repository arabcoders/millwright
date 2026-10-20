from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import sys
from pathlib import Path

import pytest

from node.process import spawn_tool, stop_tool
from server.workspace import _remove_attempt


@pytest.mark.asyncio
@pytest.mark.parametrize("launcher", ["tool", "shared"])
async def test_parent_death_lock(tmp_path: Path, launcher: str) -> None:
    root = tmp_path / "workspace"
    attempt = root / "jobs/1/attempts/1"
    attempt.mkdir(parents=True)
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    gate_fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "with Path(sys.argv[1]).open('wb') as output, Path(sys.argv[2]).open('rb', buffering=0) as gate:\n"
        "    output.write(b'before')\n"
        "    output.flush()\n"
        "    print(os.getpid(), flush=True)\n"
        "    gate.read(1)\n"
        "    output.write(b'after')\n",
        encoding="utf-8",
    )
    controller = tmp_path / "controller.py"
    controller.write_text(
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from common.workspace import workspace_lock\n"
        "from node.process import spawn_tool\n"
        "from node.process import create_tool, stop_tool\n"
        "from common.process import process_policy, spawn_child\n"
        "from functools import partial\n"
        "async def main():\n"
        "    attempt, writer, gate = map(Path, sys.argv[1:])\n"
        "    with workspace_lock(attempt):\n"
        + (
            "        with process_policy(create=partial(create_tool, workspace=attempt), stop=stop_tool):\n"
            "            proc = await spawn_child(sys.executable, writer, attempt / 'output.mkv', gate,\n"
            "                                     stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)\n"
            if launcher == "shared"
            else "        proc = await spawn_tool(sys.executable, writer, attempt / 'output.mkv', gate, workspace=attempt,\n"
            "                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)\n"
        )
        + "        print(proc.pid, flush=True)\n"
        "        print(int(await proc.stdout.readline()), flush=True)\n"
        "        await proc.wait()\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    # Adopt and reap the real orphaned supervisor instead of leaving it to the
    # test environment's PID 1. Restore the runner's setting during teardown.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    proc = None
    supervisor: int | None = None
    tool: int | None = None
    reaping: asyncio.Task[tuple[int, int]] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            controller,
            attempt,
            writer,
            gate,
            stdout=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        supervisor = int(await asyncio.wait_for(proc.stdout.readline(), 5))
        tool = int(await asyncio.wait_for(proc.stdout.readline(), 5))
        proc.kill()
        assert await asyncio.wait_for(proc.wait(), 5) == -signal.SIGKILL
        reaping = asyncio.create_task(asyncio.to_thread(os.waitpid, supervisor, 0))
        with pytest.raises(BlockingIOError):
            _remove_attempt(root, 1, 1)
        output = attempt / "output.mkv"
        assert output.read_bytes() == b"before"
        os.write(gate_fd, b"x")
        await asyncio.wait_for(asyncio.wait((reaping,)), 5)
        _, status = reaping.result()
        supervisor = None
        assert os.waitstatus_to_exitcode(status) == 0
        assert output.read_bytes() == b"beforeafter"
        assert _remove_attempt(root, 1, 1)
        assert not attempt.exists()
    finally:
        try:
            os.write(gate_fd, b"x")
            try:
                if proc is not None:
                    if proc.returncode is None:
                        proc.kill()
                    await asyncio.wait_for(proc.communicate(), 5)
            finally:
                if tool is not None and supervisor is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(tool, signal.SIGKILL)
                if supervisor is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(supervisor, signal.SIGUSR1)
                    if reaping is None:
                        reaping = asyncio.create_task(asyncio.to_thread(os.waitpid, supervisor, 0))
                    await asyncio.wait_for(asyncio.wait((reaping,)), 5)
                    reaping.result()
        finally:
            os.close(gate_fd)
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.mark.asyncio
async def test_force_reaps_tool(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    attempt = root / "jobs/1/attempts/1"
    attempt.mkdir(parents=True)
    output = attempt / "output.mkv"
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    proc = None
    tool: int | None = None
    stopping: asyncio.Task[int] | None = None
    try:
        proc = await spawn_tool(
            sys.executable,
            "-c",
            "import os, signal, sys\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "with Path(sys.argv[1]).open('wb') as output:\n"
            "    output.write(b'before')\n"
            "    output.flush()\n"
            "    print(os.getpid(), flush=True)\n"
            "    sys.stdin.buffer.read(1)\n"
            "    output.write(b'after')\n",
            output,
            workspace=attempt,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        tool = int(await asyncio.wait_for(proc.stdout.readline(), 5))
        with pytest.raises(BlockingIOError):
            _remove_attempt(root, 1, 1)
        stopping = asyncio.create_task(stop_tool(proc, force=True))
        await asyncio.wait_for(asyncio.wait((stopping,)), 5)
        assert stopping.result() == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(tool, 0)
        assert output.read_bytes() == b"before"
        assert _remove_attempt(root, 1, 1)
        assert not attempt.exists()
    finally:
        try:
            if tool is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(tool, signal.SIGKILL)
            if proc is not None:
                if proc.returncode is None:
                    proc.send_signal(signal.SIGUSR1)
                await asyncio.wait_for(proc.communicate(), 5)
            if stopping is not None:
                await asyncio.wait_for(asyncio.wait((stopping,)), 5)
                stopping.result()
            if tool is not None:
                with contextlib.suppress(ChildProcessError):
                    await asyncio.wait_for(asyncio.to_thread(os.waitpid, tool, 0), 5)
        finally:
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.mark.asyncio
async def test_missing_attempt_refused(tmp_path: Path) -> None:
    attempt = tmp_path / "missing-attempt"
    marker = tmp_path / "launched"
    proc = await spawn_tool(
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('launched')",
        marker,
        workspace=attempt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), 5)
        assert proc.returncode != 0 and attempt.name.encode() in stderr
        assert not attempt.exists() and not marker.exists()
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGUSR1)
        await asyncio.wait_for(proc.wait(), 5)
