from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from common.enums import ExecutionResult
from common.media.probe import ProbeData, probe_from_raw
from common.plan.models import FFmpegPlan  # noqa: TC002 -- pydantic resolves annotations at runtime
from common.plan.renderer import render
from common.plan.verification import validate_duration, validate_streams
from common.process import ChildNotReapedError, drain
from common.workspace import DEFAULT_HARD_SIZE_CEILING, DEFAULT_MIN_FREE_GB
from pydantic import BaseModel, Field, field_validator

from node.files import check_free_space, create_output, open_log
from node.process import spawn_tool, stop_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from common.plan.renderer import StreamLogEntry

    from node.files import _Output
    from node.repair import PreparedInput

_PROGRESS_INTERVAL_S = 1.0
_PROBE_TIMEOUT_S = 30.0
_STDERR_TAIL_BYTES = 2048
LOG = logging.getLogger("node.executor")


class _AbortError(Exception):
    pass


class _ProbeError(Exception):
    pass


class JobSpec(BaseModel):
    job_id: str
    attempt_id: int = Field(gt=0)
    src: str
    dst: str
    log: str
    subtitle_path: str | None = None
    plan: FFmpegPlan
    probe: ProbeData
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    hard_size_ceiling: float = DEFAULT_HARD_SIZE_CEILING
    stall_timeout_s: float = 600.0

    @field_validator("src", "dst", "log", "subtitle_path")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            msg = "executor paths must be absolute"
            raise ValueError(msg)
        return str(path)


@dataclass(kw_only=True)
class ProgressUpdate:
    frame: int | None = None
    fps: float | None = None
    out_time_us: int | None = None
    total_size: int | None = None
    speed: str | None = None
    percent: float | None = None


@dataclass(kw_only=True)
class JobResult:
    job_id: str
    status: ExecutionResult | Literal["stalled"]
    argv: list[str]
    stream_log: list[StreamLogEntry]
    progress: ProgressUpdate | None = None
    error: str | None = None
    output_path: str | None = None
    source_size: int | None = None
    output_size: int | None = None
    output_probe: ProbeData | None = None


@dataclass(kw_only=True)
class _Run:
    returncode: int
    stalled: bool
    cancelled: bool
    progress: ProgressUpdate | None
    stderr: str = ""


class _Emitter:
    def __init__(
        self, duration_s: float, source_fps: float | None, on_progress: Callable[[ProgressUpdate], None] | None
    ) -> None:
        self.duration_s = duration_s
        self.source_fps = source_fps
        self.on_progress = on_progress
        self.block: dict[str, str] = {}
        self.last_emit = float("-inf")
        self.latest: ProgressUpdate | None = None

    def feed(self, line: str) -> None:
        key, sep, value = line.strip().partition("=")
        if not sep:
            return
        self.block[key] = value
        if key == "progress":
            now = time.monotonic()
            if value == "end" or now - self.last_emit >= _PROGRESS_INTERVAL_S:
                self.last_emit = now
                out_time_us = _int(self.block.get("out_time_us"))
                frame = _int(self.block.get("frame"))
                fps = _float(self.block.get("fps"))
                time_percent = (
                    out_time_us / (self.duration_s * 1_000_000) * 100
                    if self.duration_s > 0 and out_time_us is not None
                    else None
                )
                frame_percent = (
                    frame / (self.duration_s * self.source_fps) * 100
                    if self.duration_s > 0
                    and self.source_fps is not None
                    and self.source_fps > 0
                    and frame is not None
                    and frame > 0
                    else None
                )
                # A short copied subtitle stream can pin the mux timestamp even
                # while video frames advance. Don't let it freeze the display.
                use_frames = frame_percent is not None and (time_percent is None or frame_percent > time_percent)
                percent = frame_percent if use_frames else time_percent
                speed = self.block.get("speed")
                if use_frames and fps is not None and self.source_fps is not None:
                    speed = f"{fps / self.source_fps:.3g}x"
                self.latest = ProgressUpdate(
                    frame=frame,
                    fps=fps,
                    out_time_us=out_time_us,
                    total_size=_int(self.block.get("total_size")),
                    speed=speed,
                    percent=None if percent is None else min(100.0, max(0.0, percent)),
                )
                if self.on_progress:
                    self.on_progress(self.latest)
            self.block = {}


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Pre-actions may already have written outcomes to this stage's log.
        self.file = open_log(path)

    def write(self, text: str) -> None:
        self._write(text.encode("utf-8", errors="replace"))

    def stderr(self, data: bytes) -> None:
        self._write(data)

    def _write(self, data: bytes) -> None:
        self.file.write(data)
        self.file.flush()

    def finish(self) -> None:
        try:
            try:
                self.file.flush()
                os.fsync(self.file.fileno())
            except OSError as exc:
                LOG.error("Execution log finalization failed for %s", self.path, extra={"error": str(exc)})
        finally:
            try:
                self.file.close()
            except OSError as exc:
                LOG.error("Execution log close failed for %s", self.path, extra={"error": str(exc)})


async def execute(
    spec: JobSpec,
    ffmpeg_bin: str | Path,
    ffprobe_bin: str | Path,
    on_progress: Callable[[ProgressUpdate], None] | None = None,
    *,
    prepared: PreparedInput | None = None,
) -> JobResult:
    dst = Path(spec.dst)
    try:
        log = _Log(Path(spec.log))
    except (OSError, ValueError) as exc:
        LOG.error(
            "Could not open execution log %s for job %s attempt %s",
            spec.log,
            spec.job_id,
            spec.attempt_id,
            extra={
                "operation": "executor.log.failed",
                "job_id": spec.job_id,
                "attempt_id": spec.attempt_id,
                "error": str(exc),
            },
        )
        return JobResult(job_id=spec.job_id, status=ExecutionResult.FAILED, argv=[], stream_log=[], error=str(exc))
    output: _Output | None = None
    result: JobResult
    succeeded = False
    unreaped = False
    try:
        log.write("phase: prepare\n")
        LOG.debug(
            "Preparing job %s attempt %s",
            spec.job_id,
            spec.attempt_id,
            extra={
                "operation": "executor.phase",
                "job_id": spec.job_id,
                "attempt_id": spec.attempt_id,
                "stage": "prepare",
            },
        )
        try:
            source_size = Path(spec.src).stat().st_size
            check_free_space(dst.parent, spec.min_free_gb)
        except (OSError, ValueError, _AbortError) as exc:
            LOG.warning(
                "Could not prepare workspace file %s for job %s attempt %s",
                Path(spec.src).name,
                spec.job_id,
                spec.attempt_id,
                extra={
                    "operation": "executor.prepare.failed",
                    "job_id": spec.job_id,
                    "attempt_id": spec.attempt_id,
                    "error": str(exc),
                },
            )
            log.write(f"preparation failed: {exc}\nresult: aborted\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.ABORTED,
                argv=[],
                stream_log=[],
                error=str(exc),
                source_size=None,
            )
            return result
        try:
            output = create_output(dst)
        except (OSError, ValueError) as exc:
            LOG.warning(
                "Could not create workspace output file %s for job %s attempt %s",
                dst.name,
                spec.job_id,
                spec.attempt_id,
                extra={
                    "operation": "executor.output.failed",
                    "job_id": spec.job_id,
                    "attempt_id": spec.attempt_id,
                    "error": str(exc),
                },
            )
            log.write(f"result: failed\nCould not create workspace output file: {exc}\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.FAILED,
                argv=[],
                stream_log=[],
                error=f"Could not create workspace output file: {exc}",
                source_size=source_size,
            )
            return result
        rendered = render(
            spec.plan,
            prepared.probe if prepared is not None else spec.probe,
            str(prepared.path) if prepared is not None else spec.src,
            str(output.path),
            subtitle_path=spec.subtitle_path,
        )
        argv = [str(ffmpeg_bin), *rendered.argv[:-1], "-progress", "pipe:1", "-nostats", rendered.argv[-1]]
        log.write(f"phase: ffmpeg\nargv: {json.dumps(argv)}\n")
        LOG.debug(
            "Running FFmpeg for job %s attempt %s",
            spec.job_id,
            spec.attempt_id,
            extra={
                "operation": "executor.phase",
                "job_id": spec.job_id,
                "attempt_id": spec.attempt_id,
                "stage": "ffmpeg",
            },
        )
        try:
            run = await _run_ffmpeg(spec, argv, on_progress, log)
        except OSError as exc:
            LOG.warning(
                "Could not run FFmpeg for workspace file %s, job %s attempt %s",
                Path(spec.src).name,
                spec.job_id,
                spec.attempt_id,
                extra={
                    "operation": "executor.ffmpeg.failed",
                    "job_id": spec.job_id,
                    "attempt_id": spec.attempt_id,
                    "error": str(exc),
                },
            )
            log.write(f"execution failure: {exc}\n")
            log.write("result: failed\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.FAILED,
                argv=argv,
                stream_log=rendered.stream_log,
                error=f"ffmpeg execution failed: {exc}",
                source_size=source_size,
            )
            return result
        if run.cancelled:
            log.write("result: cancelled\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.CANCELLED,
                argv=argv,
                stream_log=rendered.stream_log,
                progress=run.progress,
                source_size=source_size,
                error="cancelled",
            )
            return result
        if run.stalled:
            error = f"FFmpeg stopped reporting progress for {spec.stall_timeout_s:g} seconds."
            LOG.warning(
                "FFmpeg stalled for workspace file %s, job %s attempt %s",
                Path(spec.src).name,
                spec.job_id,
                spec.attempt_id,
                extra={
                    "operation": "executor.ffmpeg.stalled",
                    "job_id": spec.job_id,
                    "attempt_id": spec.attempt_id,
                    "error": error,
                },
            )
            log.write("result: stalled\n")
            result = JobResult(
                job_id=spec.job_id,
                status="stalled",
                argv=argv,
                stream_log=rendered.stream_log,
                progress=run.progress,
                source_size=source_size,
                error=error,
            )
            return result
        if run.returncode != 0:
            error = f"FFmpeg exited with code {run.returncode}: {run.stderr or 'No stderr output'}"
            LOG.warning(
                "FFmpeg failed for workspace file %s, job %s attempt %s\nExecution log: %s",
                Path(spec.src).name,
                spec.job_id,
                spec.attempt_id,
                spec.log,
                extra={
                    "operation": "executor.ffmpeg.failed",
                    "job_id": spec.job_id,
                    "attempt_id": spec.attempt_id,
                    "outcome": "failed",
                    "returncode": run.returncode,
                    "error": error,
                },
            )
            log.write("result: failed\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.FAILED,
                argv=argv,
                stream_log=rendered.stream_log,
                progress=run.progress,
                source_size=source_size,
                error=error,
            )
            return result
        if output.path.stat().st_size == 0:
            LOG.warning(
                "FFmpeg produced no output for workspace file %s, job %s attempt %s",
                Path(spec.src).name,
                spec.job_id,
                spec.attempt_id,
                extra={"operation": "executor.output.failed", "job_id": spec.job_id, "attempt_id": spec.attempt_id},
            )
            log.write("result: failed\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.FAILED,
                argv=argv,
                stream_log=rendered.stream_log,
                source_size=source_size,
                error="no output",
            )
            return result
        log.write("phase: probe\n")
        LOG.debug(
            "Verifying output for job %s attempt %s",
            spec.job_id,
            spec.attempt_id,
            extra={
                "operation": "executor.phase",
                "job_id": spec.job_id,
                "attempt_id": spec.attempt_id,
                "stage": "probe",
            },
        )
        try:
            payload = await _probe_json(ffprobe_bin, dst, log)
            output_size = output.path.stat().st_size
            output_probe = probe_from_raw(Path(spec.dst), output_size, payload)
            validate_duration(spec.probe, output_probe, spec.plan, rendered.stream_log)
            if (
                output_size > spec.hard_size_ceiling * source_size
                or output_size > spec.plan.expected.size_ceiling_ratio * source_size
            ):
                limit = min(spec.hard_size_ceiling, spec.plan.expected.size_ceiling_ratio) * source_size
                msg = f"Output size {output_size} bytes exceeds the allowed {limit:g} bytes for this stage."
                raise _ProbeError(msg)  # noqa: TRY301
            validate_streams(spec.probe, output_probe, rendered.stream_log)
        except (OSError, TypeError, ValueError, KeyError, _ProbeError) as exc:
            LOG.warning(
                "Could not verify workspace output %s for job %s attempt %s",
                dst.name,
                spec.job_id,
                spec.attempt_id,
                extra={
                    "operation": "executor.verification.failed",
                    "job_id": spec.job_id,
                    "attempt_id": spec.attempt_id,
                    "outcome": "failed",
                    "error": str(exc),
                },
            )
            log.write(f"verification failed: {exc}\n")
            log.write("result: failed\n")
            result = JobResult(
                job_id=spec.job_id,
                status=ExecutionResult.FAILED,
                argv=argv,
                stream_log=rendered.stream_log,
                progress=run.progress,
                source_size=source_size,
                error=str(exc),
            )
            return result
        log.write("result: succeeded\n")
        succeeded = True
        result = JobResult(
            job_id=spec.job_id,
            status=ExecutionResult.SUCCEEDED,
            argv=argv,
            stream_log=rendered.stream_log,
            progress=run.progress,
            output_path=str(Path(spec.dst)),
            source_size=source_size,
            output_size=output_size,
            output_probe=output_probe,
        )
        return result
    except ChildNotReapedError:
        # Do not remove output while an interrupted encoder may still be writing it.
        unreaped = True
        raise
    finally:
        try:
            if not succeeded and not unreaped and output is not None:
                output.discard()
        finally:
            try:
                if "result" not in locals():
                    with contextlib.suppress(OSError):
                        log.write("result: executor failure\n")
            finally:
                finishing = asyncio.create_task(asyncio.to_thread(log.finish))
                try:
                    await drain(finishing, propagate_cancel=not unreaped)
                except OSError as exc:
                    LOG.error("Execution log finalization failed for %s", spec.log, extra={"error": str(exc)})


async def _run_ffmpeg(
    spec: JobSpec,
    argv: list[str],
    on_progress: Callable[[ProgressUpdate], None] | None,
    log: _Log,
) -> _Run:
    source_fps = next((stream.fps for stream in spec.probe.real_video if stream.fps is not None), None)
    emitter = _Emitter(spec.probe.duration_s, source_fps, on_progress)
    stalled = False
    tail = bytearray()

    async def stdout() -> None:
        nonlocal stalled
        assert proc.stdout is not None
        while line := await asyncio.wait_for(proc.stdout.readline(), timeout=spec.stall_timeout_s):
            emitter.feed(line.decode(errors="replace"))
        await asyncio.wait_for(proc.wait(), timeout=spec.stall_timeout_s)

    async def stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(65536):
            tail.extend(chunk)
            del tail[:-_STDERR_TAIL_BYTES]
            writing = asyncio.create_task(asyncio.to_thread(log.stderr, chunk))
            await drain(writing)

    try:
        proc = await spawn_tool(
            *argv,
            workspace=Path(spec.dst).parent,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 1:
            raise
        return _Run(returncode=-1, stalled=False, cancelled=True, progress=emitter.latest)
    readers = (asyncio.create_task(stdout()), asyncio.create_task(stderr()))
    unreaped = False
    try:
        try:
            await asyncio.gather(*readers)
            code = await asyncio.wait_for(proc.wait(), timeout=spec.stall_timeout_s)
        except TimeoutError:
            stalled = True
            code = await stop_tool(proc)
        except asyncio.CancelledError:
            code = await stop_tool(proc)
            return _Run(returncode=code, stalled=False, cancelled=True, progress=emitter.latest)
        except Exception:
            await stop_tool(proc)
            raise
    except ChildNotReapedError:
        unreaped = True
        raise
    finally:
        for reader in readers:
            if not reader.done() and not reader.cancelling():
                reader.cancel()
        await drain(asyncio.gather(*readers, return_exceptions=True), propagate_cancel=not unreaped)
    return _Run(
        returncode=code,
        stalled=stalled,
        cancelled=False,
        progress=emitter.latest,
        stderr=tail.decode(errors="replace").strip(),
    )


async def _probe_json(binary: str | Path, path: Path, log: _Log) -> dict:
    argv = [str(binary), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)]
    proc = await spawn_tool(
        *argv, workspace=path.parent, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
    except asyncio.CancelledError:
        await stop_tool(proc)
        raise
    except TimeoutError:
        await stop_tool(proc, force=True)
        raise _ProbeError("ffprobe timed out") from None
    except Exception:
        await stop_tool(proc, force=True)
        raise
    if stderr:
        await drain(asyncio.create_task(asyncio.to_thread(log.stderr, stderr)))
    if proc.returncode != 0:
        raise _ProbeError(f"ffprobe exited with {proc.returncode}: {stderr.decode(errors='replace')}")  # noqa: EM102
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise _ProbeError(f"ffprobe returned invalid JSON: {exc}") from exc  # noqa: EM102
