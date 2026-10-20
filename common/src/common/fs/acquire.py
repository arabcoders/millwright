from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _check_entry(path: Path, parent_fd: int, fd: int) -> None:
    current, opened = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False), os.fstat(fd)
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError("file entry changed")


def _check_parent(path: Path, parent_fd: int) -> None:
    with directory_fd(path.parent) as current_fd:
        pinned, current = os.fstat(parent_fd), os.fstat(current_fd)
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("file parent changed")


def entry_stat(path: Path, *, follow_symlinks: bool = False) -> os.stat_result | None:
    """Return metadata or None for a missing entry. Propagate other filesystem errors."""
    try:
        return path.stat(follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        return None


@contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Generator[int]:
    absolute = path.absolute()
    fd = os.open(absolute.anchor, DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular_fd(name: str, directory: int) -> Generator[int]:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise ValueError("file name must be a single non-empty basename")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe non-regular file")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def private_file(parent_fd: int) -> Generator[tuple[str, int, int]]:
    """Avoid NFS exclusive-create errors by creating the file in a new directory."""
    name = f".{uuid.uuid4().hex}.partial"
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    directory = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    fd: int | None = None
    try:
        fd = os.open("file", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666, dir_fd=directory)
        yield name, directory, fd
    finally:
        try:
            if fd is not None:
                os.close(fd)
            # Close the temporary file before unlinking to avoid NFS sillyrename.
            try:
                os.unlink("file", dir_fd=directory)
            except FileNotFoundError:
                pass
            os.rmdir(name, dir_fd=parent_fd)
        finally:
            os.close(directory)
