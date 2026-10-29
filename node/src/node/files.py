from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.fs.effects import publish

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path
    from typing import BinaryIO


def open_log(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("ab")


@dataclass(kw_only=True, slots=True)
class _Output:
    path: Path
    retained: bool = False

    def discard(self) -> None:
        if not self.retained:
            self.path.unlink(missing_ok=True)


def create_output(path: Path) -> _Output:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each attempt has one writer. Keep existing output for recovery without using O_EXCL on NFS.
    if path.exists():
        msg = f"Workspace output already exists: {path}"
        raise FileExistsError(msg)
    with path.open("wb"):
        pass
    return _Output(path=path)


def check_free_space(path: Path, min_free_gb: float) -> None:
    free = shutil.disk_usage(path).free
    if free < min_free_gb * 1e9:
        message = f"Not enough free space in workspace directory {path}. Available {free / 1e9:.2f} GB. Required {min_free_gb:g} GB."
        raise ValueError(message)


@contextmanager
def partial_output(path: Path) -> Generator[_Output]:
    if path.exists():
        raise ValueError("subtitle artifact already exists")
    output = create_output(path.with_name(path.name + ".partial"))
    try:
        yield output
    finally:
        output.discard()


def publish_output(output: _Output, path: Path) -> None:
    publish(output.path, path)
    output.path.unlink()
    output.path = path
    output.retained = True
