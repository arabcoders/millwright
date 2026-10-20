from __future__ import annotations

import errno
import logging
import os
import secrets
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from common.fs.acquire import DIRECTORY_FLAGS, directory_fd, private_file

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

LOG = logging.getLogger("server.playback.artifacts")

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_READ_CHUNK = 256 * 1024


class PlaybackFilesError(Exception):
    pass


def _relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_playback_dir(playback_dir: Path, forbidden: list[Path]) -> None:
    """Playback files must not overlap configured runtime or media directories."""
    resolved = playback_dir.resolve()
    for other in forbidden:
        guard = other.resolve()
        if resolved == guard or _relative_to(resolved, guard) or _relative_to(guard, resolved):
            msg = f"Playback directory {playback_dir} overlaps {other}."
            raise PlaybackFilesError(msg)


class Artifact:
    """A temporary request file accessed through an open descriptor."""

    def __init__(self, fd: int, name: str, session: SessionFiles) -> None:
        self.fd = fd
        self.name = name
        self.session = session

    def stat(self) -> os.stat_result:
        return os.fstat(self.fd)

    def read_bytes(self) -> bytes:
        return b"".join(self.read_chunks())

    def lease(self) -> ArtifactLease:
        return ArtifactLease(os.dup(self.fd), self.name, self.session)

    def read_chunks(self) -> Iterator[bytes]:
        info = os.fstat(self.fd)
        position = 0
        while position < info.st_size:
            chunk = os.pread(self.fd, min(_READ_CHUNK, info.st_size - position), position)
            if not chunk:
                break
            position += len(chunk)
            yield chunk

    def discard(self) -> None:
        if self.fd < 0:
            return
        self.session.release(self)
        os.close(self.fd)
        self.fd = -1
        with _ErrnoSuppressed(errno.ENOENT):
            os.unlink(self.name, dir_fd=self.session.directory)

    def close(self) -> None:
        """Keep the file for inspection and release the descriptor."""
        if self.fd < 0:
            return
        self.session.release(self)
        os.close(self.fd)
        self.fd = -1


class ArtifactLease(Artifact):
    """A delivery descriptor remains readable after its cached name is evicted."""

    on_close: Callable[[], None] | None = None

    def discard(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
            if self.on_close is not None:
                self.on_close()
                self.on_close = None

    def close(self) -> None:
        self.discard()


class SessionFiles:
    """Files in a playback session directory held open through a descriptor."""

    def __init__(self, root_fd: int, root: Path, directory: int, name: str) -> None:
        self.root = root
        self.directory = directory
        self.name = name
        self._root_fd = root_fd
        self._files: dict[str, Artifact] = {}

    def new_artifact(self, kind: str) -> Artifact:
        name = f"{kind}-{secrets.token_hex(8)}"
        with private_file(self.directory) as (_, private, fd):
            os.fchmod(fd, _FILE_MODE)
            os.link("file", name, src_dir_fd=private, dst_dir_fd=self.directory)
        fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=self.directory)
        artifact = Artifact(fd=fd, name=name, session=self)
        self._files[name] = artifact
        return artifact

    def release(self, artifact: Artifact) -> None:
        self._files.pop(artifact.name, None)

    def close(self) -> None:
        """Remove the files created by this session, then its empty directory."""
        if self.directory < 0:
            return
        for artifact in list(self._files.values()):
            artifact.discard()
        self._files.clear()
        # Leave the directory in place if it contains any other files.
        with _ErrnoSuppressed(errno.ENOTEMPTY, errno.ENOENT):
            os.rmdir(self.name, dir_fd=self._root_fd)
        os.close(self.directory)
        self.directory = -1
        os.close(self._root_fd)


class PlaybackFiles:
    """Open playback directories through descriptors without following symlinks."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create_session(self) -> SessionFiles:
        with directory_fd(self.root, create=True) as directory:
            root_fd = os.dup(directory)
        try:
            name = f"p{os.getpid()}-{secrets.token_hex(8)}"
            try:
                os.mkdir(name, _DIR_MODE, dir_fd=root_fd)
            except OSError as exc:
                msg = f"Could not create playback session directory: {exc}"
                raise PlaybackFilesError(msg) from exc
            directory = os.open(name, DIRECTORY_FLAGS, dir_fd=root_fd)
        except Exception:
            os.close(root_fd)
            raise
        return SessionFiles(root_fd=root_fd, root=self.root, directory=directory, name=name)


class _ErrnoSuppressed(AbstractContextManager[None]):
    def __init__(self, *codes: int) -> None:
        self._codes = codes

    def __exit__(self, exc_type, exc, _tb) -> bool:
        return isinstance(exc, OSError) and exc.errno in self._codes
