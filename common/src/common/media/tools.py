from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from common.process import _POLICY, create_subprocess_exec

PROBE_TIMEOUT_S = 30.0
_OUTPUT_TAIL_CHARS = 2000
_OUTPUT_TAIL_BYTES = 8192
LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


class ProbeError(Exception):
    def __init__(
        self,
        message: str,
        *,
        tool: str | None = None,
        exit_code: int | None = None,
        stderr: str | None = None,
        stdout: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tool = tool
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout


async def _run(
    argv: list[str], timeout_s: float | None, name: str, *, on_progress: Callable[[int], Awaitable[None]] | None = None
) -> tuple[int, bytes, bytes]:
    spawn = asyncio.create_task(
        create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    )
    cancelled = False
    try:
        while not spawn.done():
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError:
                cancelled = True
        proc = spawn.result()
    except OSError as exc:
        msg = f"{name} failed to start: {argv[0]}: {exc}"
        raise ProbeError(msg, tool=name) from exc

    async def collect(stream: asyncio.StreamReader, *, progress: bool = False) -> bytes:
        tail = bytearray()
        previous = None
        while chunk := await stream.read(4096):
            tail.extend(chunk)
            if progress and on_progress is not None:
                matches = list(re.finditer(rb"Progress:\s*(\d+)%", tail))
                if matches:
                    percent = int(matches[-1][1])
                    if percent != previous:
                        await on_progress(percent)
                        previous = percent
            del tail[:-_OUTPUT_TAIL_BYTES]
        return bytes(tail)

    if name == "mkvpropedit":
        assert proc.stdout is not None
        assert proc.stderr is not None
        communication = asyncio.gather(collect(proc.stdout, progress=True), collect(proc.stderr))
    else:
        communication = proc.communicate()
    drain = asyncio.gather(communication, proc.wait(), return_exceptions=True)
    interrupted: OSError | asyncio.CancelledError | None = asyncio.CancelledError() if cancelled else None
    if interrupted is None:
        try:
            await asyncio.wait_for(asyncio.shield(drain), timeout=timeout_s)
        except (TimeoutError, asyncio.CancelledError) as exc:
            interrupted = exc
    if interrupted is not None:
        try:
            policy = _POLICY.get()
            if policy is None:
                proc.kill()
            else:
                await policy[1](proc, force=True)
        except ProcessLookupError:
            pass
        except OSError as exc:
            interrupted = exc
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                if isinstance(interrupted, (TimeoutError, asyncio.CancelledError)):
                    interrupted = asyncio.CancelledError()
        for outcome in drain.result():
            if isinstance(outcome, BaseException):
                raise outcome
        raise interrupted
    communication, returncode = drain.result()
    if isinstance(communication, BaseException):
        raise communication
    if isinstance(returncode, BaseException):
        raise returncode
    stdout, stderr = communication
    return returncode, stdout, stderr


async def run_ffprobe(
    ffprobe_bin: str | Path,
    path: Path,
    timeout_s: float = PROBE_TIMEOUT_S,
    *,
    on_result: Callable[[dict], Awaitable[None]] | None = None,
    argv_prefix: tuple[str, ...] = (),
) -> dict:
    argv = [
        *argv_prefix,
        str(ffprobe_bin),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        returncode, stdout, stderr = await _run(argv, timeout_s, "ffprobe")
    except TimeoutError:
        msg = f"ffprobe timed out after {timeout_s}s: {path}"
        raise ProbeError(msg, tool="ffprobe") from None
    if on_result is not None:
        await on_result(
            {
                "tool": "ffprobe",
                "argv": argv,
                "exit_code": returncode,
                "stderr": stderr[-_OUTPUT_TAIL_BYTES:].decode(errors="replace"),
                "stage": "finished",
            }
        )
    if returncode != 0:
        tail = stderr.decode(errors="replace")[-_OUTPUT_TAIL_CHARS:].strip()
        output = stdout.decode(errors="replace")[-_OUTPUT_TAIL_CHARS:].strip()
        outcome = f"exited with code {returncode}" if returncode > 0 else f"was terminated by signal {-returncode}"
        msg = f"ffprobe {outcome} for {path}. {tail or output or 'No diagnostic output was returned.'}"
        raise ProbeError(msg, tool="ffprobe", exit_code=returncode, stderr=tail, stdout=output)
    try:
        return json.loads(stdout)
    except ValueError as exc:
        msg = f"ffprobe returned invalid JSON for {path}: {exc}"
        tail = stderr.decode(errors="replace")[-_OUTPUT_TAIL_CHARS:].strip()
        output = stdout.decode(errors="replace")[-_OUTPUT_TAIL_CHARS:].strip()
        raise ProbeError(msg, tool="ffprobe", exit_code=returncode, stderr=tail, stdout=output) from exc


async def run_mkvpropedit(
    mkvpropedit_bin: str | Path,
    path: Path,
    *,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
    on_result: Callable[[dict], Awaitable[None]] | None = None,
    argv_prefix: tuple[str, ...] = (),
) -> None:
    argv = [*argv_prefix, str(mkvpropedit_bin), "--ui-language", "en_US", "--add-track-statistics-tags", str(path)]
    started = time.monotonic()
    LOG.debug("Calculating MKV track statistics for %s", path)

    try:
        # Statistics scan track data. Total runtime depends on file size and storage speed.
        returncode, stdout, stderr = await _run(argv, None, "mkvpropedit", on_progress=on_progress)
    except asyncio.CancelledError:
        LOG.info("MKV track statistics cancelled for %s after %.1fs", path, time.monotonic() - started)
        raise
    if on_result is not None:
        await on_result(
            {
                "tool": "mkvpropedit",
                "argv": argv,
                "exit_code": returncode,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "stage": "finished",
            }
        )
    LOG.debug(
        "MKV track statistics finished for %s after %.1fs with exit code %s",
        path,
        time.monotonic() - started,
        returncode,
    )
    if returncode != 0:
        tail = stderr.decode(errors="replace")[-_OUTPUT_TAIL_CHARS:].strip()
        output = stdout.decode(errors="replace")[-_OUTPUT_TAIL_CHARS:].strip()
        # MKVToolNix uses 1 for completed edits with warnings. Callers re-probe the result.
        if returncode == 1:
            LOG.warning("MKV track statistics completed with warnings for %s. %s", path, tail or output)
            return
        outcome = f"exited with code {returncode}" if returncode > 0 else f"was terminated by signal {-returncode}"
        msg = f"mkvpropedit {outcome} for {path}. {tail or output or 'No diagnostic output was returned.'}"
        raise ProbeError(msg, tool="mkvpropedit", exit_code=returncode, stderr=tail, stdout=output)
