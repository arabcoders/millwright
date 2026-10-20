from __future__ import annotations

import fcntl
import os
import stat
import struct
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from common.fs.acquire import directory_fd

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_MIN_FREE_GB = 10.0
DEFAULT_HARD_SIZE_CEILING = 1.25

# Linux F_OFD_SETLK. Some CPython builds omit the name despite kernel support.
_OFD_SETLK = 37


def open_workspace_lock(path: Path, *, exclusive: bool = False) -> int:
    with directory_fd(path) as directory:
        # NFS remote locking requires a regular file, not a directory descriptor.
        # Opening a lock created over NFS can return EAGAIN on mergerfs with
        # O_NONBLOCK, even after the remote writer closes it.
        fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Workspace lock must be a regular file.")  # noqa: TRY301 -- Close the acquired descriptor below.
        # NFS translates flock to record locks, which do not conflict with native
        # mergerfs flock. OFD record locks coordinate both paths and keep an outer
        # lock alive when a nested open of the same lock file closes.
        lock = struct.pack("hhqqi4x", fcntl.F_WRLCK if exclusive else fcntl.F_RDLCK, os.SEEK_SET, 0, 0, 0)
        fcntl.fcntl(fd, _OFD_SETLK, lock)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def workspace_lock(path: Path, *, exclusive: bool = False) -> Iterator[int]:
    fd = open_workspace_lock(path, exclusive=exclusive)
    try:
        # Cleanup can take the exclusive lock only after all writers release theirs.
        yield fd
    finally:
        os.close(fd)


def resolve_workspace_path(root: Path, relative_path: str) -> Path:
    if not relative_path or relative_path == "." or "\\" in relative_path:
        raise ValueError("workspace path must be a non-empty POSIX path")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("workspace path must remain beneath the root")
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError("workspace path must not contain symlinks")
    root_resolved = root.resolve()
    if root_resolved != root.absolute():
        raise ValueError("workspace root must not be redirected")
    current = root_resolved
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("workspace path must not contain symlinks")
    resolved = current.resolve()
    if resolved != current:
        raise ValueError("workspace path must not be redirected")
    return current
