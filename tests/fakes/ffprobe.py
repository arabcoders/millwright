#!/usr/bin/env python3
"""Uses sidecar JSON, not media contents."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_PROBE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "profile": "High",
            "pix_fmt": "yuv420p",
            "width": 1920,
            "height": 1080,
            "disposition": {"default": 1},
            "tags": {"language": "und"},
        },
        {
            "index": 1,
            "codec_name": "ac3",
            "codec_type": "audio",
            "channels": 6,
            "bit_rate": "640000",
            "disposition": {},
            "tags": {"language": "eng"},
        },
    ],
    "format": {"duration": "600.0", "size": "1000000000", "bit_rate": "1333333"},
}


def probe_output(media_path: Path) -> str:
    media = str(media_path)
    if (
        len(media_path.parts) == 5
        and media_path.parts[:3] == ("/", "proc", media_path.parts[2])
        and media_path.parts[3] == "fd"
    ):
        media = str(media_path.resolve(strict=True))
    elif media_path.parent == Path("/proc/self/fd"):
        media = str(Path(media).resolve(strict=True))

    log = os.environ.get("FAKE_FFPROBE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(media + "\n")

    failed = os.environ.get("FAKE_FFPROBE_FAIL", "").split(":")
    if Path(media).name in failed:
        return "this is not json {{{"

    sidecar = Path(f"{media}.probe.json")
    return sidecar.read_text(encoding="utf-8") if sidecar.is_file() else json.dumps(DEFAULT_PROBE)


def main() -> int:
    args = sys.argv[1:]
    media = next((arg for arg in reversed(args) if not arg.startswith("-")), "")
    sys.stdout.write(probe_output(Path(media)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
