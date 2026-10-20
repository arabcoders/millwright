from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from common.capabilities import low_priority_argv, probe_capabilities, verify_decoders
from common.media.probe import probe_stream
from common.process import _POLICY, ChildNotReapedError, create_subprocess_exec, drain

LOG = logging.getLogger("common.healthcheck")
ProgressCallback = Callable[[dict[str, object]], Awaitable[None]]
_PROBE_TIMEOUT = 30.0
_MAX_SAMPLES = 20


class DiagnosticLog(Protocol):
    def append(self, text: str, *, sync: bool = False) -> None: ...


async def wait_workspace_io[T](function: Callable[[], T]) -> T:
    return await drain(asyncio.create_task(asyncio.to_thread(function)))


def _append_log(log: DiagnosticLog | None, text: str, *, sync: bool = False) -> None:
    if log is None:
        return
    try:
        log.append(text, sync=sync)
    except OSError, ValueError:
        LOG.warning("healthcheck diagnostic log write failed", exc_info=True)


@dataclass(kw_only=True, slots=True)
class HealthcheckResult:
    healthy: bool
    error_count: int = 0
    samples: list[str] | None = None
    error: str | None = None
    failed: bool = False
    probe: dict | None = None


class HealthcheckExitError(RuntimeError):
    pass


async def _drain[T](task: asyncio.Future[T]) -> T:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _wait_exit(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except Exception as exc:
        raise HealthcheckExitError("healthcheck process exit is unconfirmed") from exc


async def _reap(proc: asyncio.subprocess.Process) -> None:
    policy = _POLICY.get()
    if policy is not None:
        try:
            await policy[1](proc, force=True)
        except Exception as exc:
            raise HealthcheckExitError("Healthcheck supervisor exit is unconfirmed.") from exc
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:
        raise HealthcheckExitError("Healthcheck process signal failed. Exit is unconfirmed.") from exc
    await _drain(asyncio.create_task(_wait_exit(proc)))


async def _acquire(task: asyncio.Task[asyncio.subprocess.Process]) -> asyncio.subprocess.Process:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        async def stop_acquired() -> None:
            process = await task
            await _reap(process)

        await _drain(asyncio.create_task(stop_acquired()))
        raise


async def _communicate(proc: asyncio.subprocess.Process, timeout_s: float) -> tuple[bytes, bytes]:
    try:
        async with asyncio.timeout(timeout_s):
            return await proc.communicate()
    except BaseException:
        await _reap(proc)
        raise


async def _decode_healthcheck(
    source: str,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    log_path: DiagnosticLog | None,
    *,
    hwaccel: bool = True,
    timeout_s: float = 7200.0,
    on_progress: ProgressCallback | None = None,
) -> HealthcheckResult:
    await wait_workspace_io(lambda: _append_log(log_path, "healthcheck: start\n"))
    priority = low_priority_argv()
    await wait_workspace_io(
        lambda: _append_log(log_path, "healthcheck: CPU nice +10. I/O best-effort priority 7. Threads 1 per stream.\n")
    )
    if on_progress is not None:
        await on_progress({"stage": "probe"})
    probe = await _acquire(
        asyncio.create_task(
            create_subprocess_exec(
                *priority,
                ffprobe_bin,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                "-threads",
                "1",
                source,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
    )
    try:
        stdout, stderr = await _communicate(probe, _PROBE_TIMEOUT)
    except TimeoutError:
        reason = f"ffprobe timed out after {_PROBE_TIMEOUT:g}s"
        await wait_workspace_io(
            lambda: _append_log(log_path, f"healthcheck: {reason}\nhealthcheck: result failed\n", sync=True)
        )
        return HealthcheckResult(healthy=False, error=reason, failed=True)
    if probe.returncode != 0:
        tail = stderr.decode(errors="replace")[-2000:]
        detail = tail or "no stderr output"
        reason = f"ffprobe exited {probe.returncode}: {detail}"
        await wait_workspace_io(
            lambda: _append_log(log_path, f"{tail}\nhealthcheck: {reason}\nhealthcheck: result failed\n", sync=True)
        )
        return HealthcheckResult(healthy=False, error=reason, failed=True)
    try:
        probe_data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        reason = f"ffprobe returned invalid JSON: {exc}"
        await wait_workspace_io(
            lambda: _append_log(log_path, f"healthcheck: {reason}\nhealthcheck: result failed\n", sync=True)
        )
        return HealthcheckResult(healthy=False, error=reason, failed=True)
    decode_options = ["-threads", "1"]
    output_options: list[str] = []
    decisions: list[str] = []
    # Remuxed JPEG artwork can lose attached_pic and report extreme frame rates.
    # Use the MIME-aware classifier and explicit maps to keep it out of decoding.
    videos = [
        stream
        for stream in probe_data.get("streams", [])
        if stream.get("codec_type") == "video"
        and not probe_stream(stream).disposition_attached_pic
        and not (stream.get("disposition") or {}).get("timed_thumbnails")
    ]
    video_maps = [argument for stream in videos for argument in ("-map", f"0:{stream['index']}")]
    decoders: dict[str, tuple[str, str]] = {}
    if hwaccel and videos:
        await wait_workspace_io(lambda: _append_log(log_path, "healthcheck: discovering hardware decoders\n"))
        try:
            capabilities = await probe_capabilities(ffmpeg_bin, argv_prefix=priority)
            decoders, diagnostics = await verify_decoders(
                ffmpeg_bin,
                capabilities,
                {stream.get("codec_name", "unknown") for stream in videos},
                argv_prefix=priority,
            )
        except ChildNotReapedError as exc:
            raise HealthcheckExitError(str(exc)) from exc
        decisions.extend(f"healthcheck: {line}\n" for line in diagnostics)
    for backend in sorted({backend for backend, _ in decoders.values()}):
        decode_options.extend(["-init_hw_device", f"{backend}=healthcheck_{backend}"])
    for output_index, stream in enumerate(videos):
        index, codec = stream["index"], stream.get("codec_name", "unknown")
        if codec in decoders:
            backend, decoder = decoders[codec]
            decode_options.extend(
                [
                    f"-hwaccel:{index}",
                    backend,
                    f"-hwaccel_device:{index}",
                    f"healthcheck_{backend}",
                    f"-hwaccel_output_format:{index}",
                    backend,
                    f"-c:{index}",
                    decoder,
                ]
            )
            # Require hardware frames so native decoder fallback cannot silently pass as GPU decoding.
            output_options.extend([f"-filter:v:{output_index}", f"format={backend}"])
            decisions.append(f"healthcheck: video stream {index} ({codec}) uses {backend} decoder {decoder}.\n")
        else:
            reason = "No verified hardware decoder." if hwaccel else "Hardware decoding disabled."
            decisions.append(f"healthcheck: video stream {index} ({codec}) uses software. {reason}\n")
    decisions.append("healthcheck: audio decoding uses software with one thread per stream.\n")
    await wait_workspace_io(lambda: _append_log(log_path, "".join(decisions)))
    if on_progress is not None:
        await on_progress({"stage": "decode"})

    async def run_decode() -> HealthcheckResult:
        await wait_workspace_io(lambda: _append_log(log_path, "healthcheck: decode\n"))
        proc = await _acquire(
            asyncio.create_task(
                create_subprocess_exec(
                    *priority,
                    ffmpeg_bin,
                    "-v",
                    "error",
                    "-stats_period",
                    "1",
                    "-progress",
                    "pipe:1",
                    "-filter_threads",
                    "1",
                    *decode_options,
                    "-i",
                    source,
                    *video_maps,
                    "-map",
                    "0:a?",
                    *output_options,
                    # The null sink accepts decoded GPU frames without a transfer or a GPU encoder.
                    "-c:v",
                    "wrapped_avframe",
                    "-threads",
                    "1",
                    "-f",
                    "null",
                    "-",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        )
        duration = None
        stdout = proc.stdout
        stderr = proc.stderr
        if stdout is None or stderr is None:
            await _reap(proc)
            raise RuntimeError("ffmpeg pipes unavailable")
        try:
            duration = float(probe_data.get("format", {}).get("duration", 0))
        except AttributeError, TypeError, ValueError:
            pass
        try:
            async with asyncio.timeout(timeout_s):
                block: dict[str, str] = {}
                last_emit = float("-inf")
                diagnostics: list[str] = []
                diagnostic_count = 0

                async def read_stdout() -> None:
                    nonlocal block, last_emit
                    while chunk := await stdout.readline():
                        key, sep, value = chunk.decode(errors="replace").strip().partition("=")
                        if not sep:
                            continue
                        block[key] = value
                        if key == "progress":
                            now = time.monotonic()
                            if on_progress is not None and (value == "end" or now - last_emit >= 1):
                                last_emit = now
                                update: dict[str, object] = {"stage": "decode"}
                                if (
                                    duration is not None
                                    and math.isfinite(duration)
                                    and duration > 0
                                    and block.get("out_time_us") is not None
                                ):
                                    try:
                                        out_time_us = float(block["out_time_us"])
                                        if math.isfinite(out_time_us):
                                            update["percent"] = max(0.0, min(100.0, out_time_us / (duration * 10_000)))
                                    except ValueError:
                                        pass
                                await on_progress(update)
                            block = {}

                async def read_stderr() -> None:
                    nonlocal diagnostic_count
                    while chunk := await stderr.readline():
                        text = chunk.decode(errors="replace")
                        # libva initialization info is retained in the log, not counted as media errors.
                        lines = [
                            line for line in text.splitlines() if line.strip() and not line.startswith("libva info:")
                        ]
                        diagnostic_count += len(lines)
                        diagnostics.extend(lines)
                        del diagnostics[:-_MAX_SAMPLES]
                        await wait_workspace_io(lambda text=text: _append_log(log_path, text))

                readers = (asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr()))
                try:
                    await asyncio.gather(*readers)
                    await proc.wait()
                finally:
                    for reader in readers:
                        if not reader.done():
                            reader.cancel()
                    await _drain(asyncio.gather(*readers, return_exceptions=True))
        except asyncio.CancelledError:
            await _reap(proc)
            raise
        except TimeoutError:
            await _reap(proc)
            reason = f"ffmpeg timed out after {timeout_s:g}s"
            await wait_workspace_io(
                lambda: _append_log(log_path, f"healthcheck: {reason}\nhealthcheck: result failed\n", sync=True)
            )
            return HealthcheckResult(healthy=False, error=reason, failed=True, probe=probe_data)
        except Exception:
            await _reap(proc)
            raise
        lines = diagnostics
        await wait_workspace_io(
            lambda: _append_log(
                log_path, f"healthcheck: result {'failed' if proc.returncode else 'succeeded'}\n", sync=True
            )
        )
        if proc.returncode:
            return HealthcheckResult(
                healthy=False,
                error=f"ffmpeg exited {proc.returncode}: {' '.join(lines[-3:]) or 'no decode error output'}",
                failed=True,
                probe=probe_data,
            )
        return HealthcheckResult(
            healthy=not lines, error_count=diagnostic_count, samples=lines[-_MAX_SAMPLES:], probe=probe_data
        )

    result = await run_decode()
    # A codec-level sample cannot prove support for every profile or pixel format.
    # Confirm hardware failures in software before classifying the library media.
    if decoders and not result.healthy:
        await wait_workspace_io(
            lambda: _append_log(
                log_path, "healthcheck: hardware decode did not pass. Retrying in software.\n", sync=True
            )
        )
        if on_progress is not None:
            await on_progress({"stage": "decode", "percent": 0.0})
        decode_options = ["-threads", "1"]
        output_options = []
        return await run_decode()
    return result
