from __future__ import annotations

import errno
import fcntl
import os
import shutil
import uuid
from typing import TYPE_CHECKING

from common.fs.acquire import directory_fd, private_file, regular_fd

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_LINK_FALLBACK = {errno.EXDEV, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
_CLONE_FALLBACK = {errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOTTY}
_FICLONE = 0x40049409


def copy_file(
    src: Path,
    dst: Path,
    *,
    overwrite: bool = False,
) -> None:
    with (
        directory_fd(src.parent) as source_parent,
        regular_fd(src.name, source_parent) as opened,
        directory_fd(dst.parent) as parent,
        private_file(parent) as (_, private, fd),
        os.fdopen(os.dup(fd), "w+b") as target,
        os.fdopen(os.dup(opened), "rb") as source,
    ):
        try:
            fcntl.ioctl(target.fileno(), _FICLONE, source.fileno())
        except OSError as exc:
            if exc.errno not in _CLONE_FALLBACK:
                raise
            source.seek(0)
            target.seek(0)
            target.truncate()
            shutil.copyfileobj(source, target)
        target.flush()
        metadata = os.fstat(opened)
        if os.fstat(fd).st_size != metadata.st_size:
            raise OSError(errno.EIO, "Copy size differs from source", str(dst))
        os.fchmod(fd, metadata.st_mode & 0o7777)
        os.utime(fd, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        os.fsync(fd)
        if overwrite:
            os.replace("file", dst.name, src_dir_fd=private, dst_dir_fd=parent)
        else:
            os.link("file", dst.name, src_dir_fd=private, dst_dir_fd=parent)


def claim(source: Path, claim: Path) -> None:
    if source.parent != claim.parent or source.name == claim.name:
        raise ValueError("claim must be a distinct file in the source parent")
    with directory_fd(source.parent) as parent:
        os.link(source.name, claim.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(source.name, dir_fd=parent)


def publish(source: Path, destination: Path, *, overwrite: bool = False) -> None:
    with directory_fd(source.parent) as source_parent, directory_fd(destination.parent) as destination_parent:
        temporary = f".{uuid.uuid4().hex}.part" if overwrite else destination.name
        try:
            os.link(
                source.name,
                temporary,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
                follow_symlinks=False,
            )
        except OSError as exc:
            if exc.errno not in _LINK_FALLBACK:
                raise
            copy_file(source, destination, overwrite=overwrite)
            return
        if overwrite:
            try:
                os.replace(temporary, destination.name, src_dir_fd=destination_parent, dst_dir_fd=destination_parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=destination_parent)
                except FileNotFoundError:
                    pass


def move_file(
    src: Path,
    dst: Path,
    *,
    overwrite: bool = True,
    sync_destination: Callable[[Path], None] | None = None,
) -> None:
    publish(src, dst, overwrite=overwrite)
    if sync_destination is not None:
        sync_destination(dst)
    src.unlink()
