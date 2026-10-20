from __future__ import annotations

import errno
import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from common.workspace import workspace_lock


def test_shared_blocks_cleanup(tmp_path: Path) -> None:
    with workspace_lock(tmp_path):
        with workspace_lock(tmp_path):
            pass
        with pytest.raises(BlockingIOError), workspace_lock(tmp_path, exclusive=True):
            pytest.fail("cleanup acquired an active workspace")
    with workspace_lock(tmp_path, exclusive=True):
        with pytest.raises(BlockingIOError), workspace_lock(tmp_path):
            pytest.fail("writer acquired a workspace during cleanup")


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_unsafe_lock_refused(tmp_path: Path, kind: str) -> None:
    lock = tmp_path / ".lock"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"unrelated data")
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir()
    else:
        os.mkfifo(lock)
    with pytest.raises((OSError, ValueError)), workspace_lock(tmp_path):
        pytest.fail("acquisition accepted a non-regular lock file")
    if kind == "symlink":
        assert target.read_bytes() == b"unrelated data"


def test_lock_failure_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(fd: int, operation: int, lock: bytes) -> None:
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        raise OSError("Remote locking unavailable.")

    monkeypatch.setattr(fcntl, "fcntl", fail)
    with pytest.raises(OSError, match="Remote locking unavailable"), workspace_lock(tmp_path):
        pytest.fail("acquisition ignored a locking failure")


@pytest.mark.parametrize(
    ("owner_exclusive", "contender_exclusive", "blocked"),
    [(False, False, False), (False, True, True), (True, False, True)],
)
def test_record_lock_interop(tmp_path: Path, owner_exclusive: bool, contender_exclusive: bool, blocked: bool) -> None:
    # NFS flock reaches the server as a POSIX record lock, not a native flock.
    script = """
import errno
import fcntl
import sys

with open(sys.argv[1], 'r+b') as handle:
    try:
        mode = fcntl.LOCK_EX if sys.argv[2] == 'exclusive' else fcntl.LOCK_SH
        fcntl.lockf(handle, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(errno.EAGAIN)
"""
    with workspace_lock(tmp_path, exclusive=owner_exclusive):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / ".lock"), "exclusive" if contender_exclusive else "shared"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert result.returncode == (errno.EAGAIN if blocked else 0), result.stderr


def test_missing_lock_refused(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    with pytest.raises(FileNotFoundError), workspace_lock(attempt):
        pytest.fail("acquisition created a directory")
    assert not attempt.exists()


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_lock_refused(tmp_path: Path, ancestor: bool) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    nested = actual / "nested"
    nested.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError), workspace_lock(link / "nested" if ancestor else link):
        pytest.fail("acquisition followed a symlink")
