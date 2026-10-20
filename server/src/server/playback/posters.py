from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.fs.acquire import directory_fd, regular_fd
from common.fs.identity import source_identity

from server.playback.media import stream_range

if TYPE_CHECKING:
    from pathlib import Path

POSTER_MAX_BYTES = 16 * 1024 * 1024


@dataclass(kw_only=True)
class Poster:
    name: str
    mimetype: str
    identity: dict[str, int]

    @property
    def version(self) -> str:
        return hashlib.sha256(json.dumps([self.name, self.identity], sort_keys=True).encode()).hexdigest()[:16]


def discover_poster(media: Path) -> Poster | None:
    stems = [f"{media.stem}-poster", media.stem, "poster", "cover", "folder"]
    try:
        entries = sorted(path.name for path in media.parent.iterdir())
    except OSError:
        return None
    for stem in stems:
        for name in entries:
            path = media.parent / name
            if path.stem.casefold() != stem.casefold():
                continue
            mimetype, _encoding = mimetypes.guess_file_type(path)
            if mimetype is None or not mimetype.startswith("image/"):
                continue
            try:
                with directory_fd(media.parent) as directory, regular_fd(name, directory) as fd:
                    info = os.fstat(fd)
                    if 0 < info.st_size <= POSTER_MAX_BYTES:
                        return Poster(name=name, mimetype=mimetype, identity=source_identity(info))
            except OSError, ValueError:
                continue
    return None


def read_poster(parent: Path, poster: Poster) -> bytes:
    with directory_fd(parent) as directory, regular_fd(poster.name, directory) as fd:
        info = os.fstat(fd)
        if not 0 < info.st_size <= POSTER_MAX_BYTES:
            raise ValueError("poster size is outside the allowed range")
        return b"".join(stream_range(fd, 0, info.st_size - 1))


def poster_argv(src: str, dst: str, timestamp_s: float) -> list[str]:
    return [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp_s:.6f}",
        "-i",
        src,
        "-frames:v",
        "1",
        "-q:v",
        "4",
        "-fs",
        str(POSTER_MAX_BYTES),
        "-f",
        "image2pipe",
        dst,
    ]
