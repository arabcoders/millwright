#!/usr/bin/env python3
"""
Uses the final argv entry as output except for attachments and pipe output.

Hang and silent_ok modes require caller timeouts or cancellation.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BLOCK_LINES = [
    "frame=10",
    "fps=30.0",
    "out_time_us=5000000",
    "total_size=4096",
    "speed=1.0x",
]


def emit_block(pace: float) -> None:
    for line in BLOCK_LINES:
        if pace:
            time.sleep(pace)
        sys.stdout.write(line + "\n")
    if pace:
        time.sleep(pace)
    sys.stdout.write("progress=continue\n")
    sys.stdout.flush()


def main() -> int:
    mode = os.environ.get("FAKE_FFMPEG_MODE", "ok")
    args = sys.argv[1:]

    if mode == "fail":
        sys.stderr.write(os.environ.get("FAKE_FFMPEG_ERR", "fake ffmpeg failure"))
        return 3
    if "-version" in args:
        sys.stdout.write("ffmpeg version n7.1-fake Copyright (c) fake\nbuilt with fake cc\n")
        return 0
    if "-hwaccels" in args:
        sys.stdout.write("Hardware acceleration methods:\nvdpau\ncuda\nvaapi\n")
        return 0
    if "-encoders" in args:
        sys.stdout.write(
            "Encoders:\n"
            " V....D libx264              fake H.264 encoder\n"
            " V....D libx265              fake HEVC (H.265) encoder\n"
            " A....D aac                  fake AAC encoder\n"
            " V....D h264_nvenc           fake NVENC H.264 encoder\n"
            " V....D hevc_vaapi           fake VAAPI HEVC encoder\n"
        )
        return 0
    if "-decoders" in args:
        sys.stdout.write(
            "Decoders:\n"
            " V....D h264                 fake H.264 decoder\n"
            " VFS..D hevc                 fake HEVC decoder\n"
            " A....D aac                  fake AAC decoder\n"
        )
        return 0

    if any(args[i] == "-f" and args[i + 1] == "lavfi" for i in range(len(args) - 1)):
        if mode == "hang":
            time.sleep(3600)
            return 0
        encoder = next((args[i + 1] for i in range(len(args) - 1) if args[i].startswith("-c:v")), "")
        failing = os.environ.get("FAKE_FFMPEG_FAIL_ENCODERS", "").split(",")
        return 1 if encoder in failing else 0

    attachment = next((args[i + 1] for i in range(len(args) - 1) if args[i].startswith("-dump_attachment")), None)
    if attachment is not None:
        import struct

        family = "Fake Font".encode("utf-16-be")
        table = struct.pack(">HHH", 0, 1, 18) + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(family), 0) + family
        data = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0) + struct.pack(">4sIII", b"name", 0, 28, len(table)) + table
        if attachment == "pipe:1":
            sys.stdout.buffer.write(data)
        else:
            Path(attachment).write_bytes(data)
        return 0

    if any(args[i] == "-f" and args[i + 1] == "null" for i in range(len(args) - 1)):
        if mode == "decode_fail":
            sys.stderr.write(os.environ.get("FAKE_FFMPEG_ERR", "fake decode failure") + "\n")
            return 1
        if mode == "decode_errors":
            count = int(os.environ.get("FAKE_FFMPEG_DECODE_ERRORS", "3"))
            for n in range(count):
                sys.stderr.write(f"[mpegts @ 0xfake] fake decode error {n}\n")
            return 0
        if mode == "hang":
            time.sleep(3600)
            return 0
        return 0

    encoder = next((args[i + 1] for i in range(len(args) - 1) if args[i] == "-c:v"), "")
    if encoder and encoder in os.environ.get("FAKE_FFMPEG_FAIL_ENCODERS", "").split(","):
        sys.stderr.write(f"fake encoder failure: {encoder}\n")
        return 1
    # Runtime failures skip the lavfi verification path: an encoder can pass
    # its capability test and still fail a real encode.
    if encoder and encoder in os.environ.get("FAKE_FFMPEG_RUNTIME_FAIL_ENCODERS", "").split(","):
        sys.stderr.write(f"fake runtime encoder failure: {encoder}\n")
        return 1

    if "-hls_time" in args:
        import math

        duration = float(args[args.index("-t") + 1])
        segment = float(args[args.index("-hls_time") + 1])
        size = int(os.environ.get("FAKE_FFMPEG_OUTPUT_BYTES", "100"))
        for index in range(math.ceil(duration / segment)):
            sys.stdout.buffer.write(b"\0" * size)
            sys.stdout.buffer.flush()
            sys.stderr.write("#EXTM3U\n")
            for entry in range(max(0, index - 1), index + 1):
                length = min(segment, duration - entry * segment)
                sys.stderr.write(f"#EXTINF:{length:.6f},\n#EXT-X-BYTERANGE:{size}@{entry * size}\npipe:1\n")
            sys.stderr.flush()
        return 0

    # `-dump_attachment:N <dst>` puts the destination mid-argv, not last.
    output = None
    for i, arg in enumerate(args[:-1]):
        if arg.startswith("-dump_attachment"):
            output = Path(args[i + 1])
            break
    if output is None and args:
        output = Path(args[-1])
    if args and args[-1] == "pipe:1":
        sys.stdout.buffer.write(b"\0" * int(os.environ.get("FAKE_FFMPEG_OUTPUT_BYTES", "100")))
        sys.stdout.buffer.flush()
        return 0
    if output is not None:
        output.write_bytes(b"\0" * int(os.environ.get("FAKE_FFMPEG_OUTPUT_BYTES", "100")))

    if mode in {"silent_ok", "hang"}:
        time.sleep(3600)
        return 0
    if mode == "slow_ok":
        while True:
            emit_block(0.05)
    emit_block(0.0)
    for line in BLOCK_LINES:
        sys.stdout.write(line + "\n")
    sys.stdout.write("progress=end\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
