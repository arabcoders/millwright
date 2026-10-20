#!/usr/bin/env python3
"""
Updates the probe sidecar because fake ffprobe doesn't read Matroska tags.

Appending a byte models the real tool's size and mtime changes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_TAG_BITRATES = {"video": "5280000", "audio": "640000"}


def main() -> int:
    args = sys.argv[1:]
    if args[:3] != ["--ui-language", "en_US", "--add-track-statistics-tags"] or len(args) != 4:
        sys.stderr.write("usage: mkvpropedit --ui-language en_US --add-track-statistics-tags <file>\n")
        return 2
    media = Path(args[3])
    original = media.resolve()

    log = os.environ.get("FAKE_MKVPROPEDIT_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(str(original) + "\n")

    sidecar = Path(f"{original}.probe.json")
    if sidecar.is_file():
        probe = json.loads(sidecar.read_text(encoding="utf-8"))
        for stream in probe.get("streams", []):
            rate = _TAG_BITRATES.get(stream.get("codec_type"))
            if rate is not None and not stream.get("bit_rate"):
                stream.setdefault("tags", {})["BPS"] = rate
        sidecar.write_text(json.dumps(probe), encoding="utf-8")

    with open(media, "ab") as fh:
        fh.write(b"x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
