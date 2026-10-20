from __future__ import annotations

import errno
import json
import os
import stat
from typing import TYPE_CHECKING

from common.fs.acquire import directory_fd, regular_fd
from common.workspace import resolve_workspace_path

if TYPE_CHECKING:
    from pathlib import Path


def append_message(root: Path, log_path: str, message: dict) -> None:
    if message.get("type") not in {"progress", "finished", "action_report", "node_logs"}:
        raise ValueError("Only job messages belong in the job journal.")
    path = resolve_workspace_path(root, log_path).with_name("job.ndjson")
    payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    with directory_fd(path.parent) as directory:
        fd = os.open(
            path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Job journal must be a regular file.")
            if os.write(fd, payload) != len(payload):
                raise OSError(errno.EIO, "Job journal write was incomplete.")
        finally:
            os.close(fd)


def read_messages(root: Path, log_path: str) -> list[dict]:
    path = resolve_workspace_path(root, log_path).with_name("job.ndjson")
    with (
        directory_fd(path.parent) as directory,
        regular_fd(path.name, directory) as fd,
        os.fdopen(os.dup(fd), "rb") as source,
    ):
        messages = []
        for line in source:
            if not line.endswith(b"\n"):
                break
            try:
                message = json.loads(line)
            except UnicodeDecodeError, ValueError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages
