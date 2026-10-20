from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from common.workspace import workspace_lock

if TYPE_CHECKING:
    from types import FrameType


def run(workspace: Path, argv: list[str]) -> int:
    child: subprocess.Popen[bytes] | None = None
    stopping: int | None = None

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = signum
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                if signum == signal.SIGUSR1:
                    child.kill()
                else:
                    child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGUSR1, stop)
    # The worker owns this lock independently so abrupt node death cannot let
    # cleanup remove files while the media tool is still using them. Locking
    # requires an existing attempt directory and never recreates a removed one.
    with workspace_lock(workspace):
        if stopping is not None:
            return -signal.SIGKILL if stopping == signal.SIGUSR1 else -stopping
        child = subprocess.Popen(argv, close_fds=True)
        if stopping is not None:
            stop(stopping, None)
        return child.wait()


def main() -> int:
    workspace = Path(sys.argv[1])
    try:
        code = run(workspace, sys.argv[2:])
    except (OSError, ValueError) as exc:
        print(f"Tool supervisor in {workspace}: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    if code < 0:
        # Preserve the tool's signal exit status after releasing its lock.
        signum = -code
        if signum != signal.SIGKILL:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    return code


if __name__ == "__main__":
    sys.exit(main())
