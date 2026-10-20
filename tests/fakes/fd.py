#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path


def main() -> int:
    log = os.environ.get("FAKE_FD_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as output:
            output.write(json.dumps({"argv": sys.argv[1:], "cwd": str(Path.cwd())}) + "\n")
    result = json.loads(os.environ["FAKE_FD_RESULTS"])[str(Path.cwd())]
    if result.get("stall"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stderr.buffer.write(result.get("stderr", "").encode())
    sys.stderr.buffer.flush()
    for name in result.get("paths", []):
        sys.stdout.buffer.write(os.fsencode(name) + b"\0")
    sys.stdout.buffer.flush()
    if result.get("stall"):
        os.read(int(os.environ["FAKE_FD_GATE"]), 1)
    return result.get("code", 0)


if __name__ == "__main__":
    raise SystemExit(main())
