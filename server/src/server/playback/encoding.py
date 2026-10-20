from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
from collections import OrderedDict
from contextlib import suppress
from typing import TYPE_CHECKING

from common.diagnostics import error_message
from common.media.tools import ProbeError
from common.plan.renderer import render_playback
from common.process import ChildNotReapedError, drain, spawn_child, stop_child
from fastapi import HTTPException

from server.models import utcnow
from server.playback import segments
from server.playback.service import WORK_CLOSED

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from common.plan.renderer import PlaybackPlan

    from server.playback.artifacts import Artifact, ArtifactLease
    from server.playback.service import PlaybackService, PlaybackSession

CACHE_SEGMENTS = 8
IDLE_TIMEOUT_S = 30.0
_ENCODER_WAIT_S = 120.0
_READ_BYTES = 65536
LOG = logging.getLogger("server.playback.encoding")


async def read_ranges(reader: asyncio.StreamReader, ranges: asyncio.Queue[tuple[int, float]], tail: bytearray) -> None:
    """Read HLS boundaries and keep a bounded tail of FFmpeg diagnostics."""
    duration = None
    extent = None
    emitted = 0
    while line := await reader.readline():
        text = line.decode("utf-8", errors="replace").strip()
        if text == "#EXTM3U":
            duration = extent = None
        elif text.startswith("#EXTINF:"):
            duration = float(text.partition(":")[2].rstrip(","))
        elif text.startswith("#EXT-X-BYTERANGE:"):
            length, offset = text.partition(":")[2].split("@")
            extent = (int(offset), int(length))
        elif text == "pipe:1" and extent is not None and duration is not None:
            offset, length = extent
            if offset + length <= emitted:
                continue
            if offset != emitted or length <= 0 or not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid playback muxer range.")
            await ranges.put((length, duration))
            emitted += length
        elif text and not text.startswith("#EXT") and text != "pipe:1":
            tail.extend(line)
            del tail[:-2000]


class PlaybackEncoding:
    """One on-demand encoder with a small cache of open segment files."""

    def __init__(self, service: PlaybackService, session: PlaybackSession) -> None:
        self.service = service
        self.session = session
        self.selection: tuple[int | None, int | None] | None = None
        self.cache: OrderedDict[int, Artifact] = OrderedDict()
        self.durations: dict[int, float] = {}
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.demanded = asyncio.Event()
        self.demand = 0
        self.next_index = 0
        self.closed = False
        self.exit_code: int | None = None
        self.stderr_tail = bytearray()

    async def stop(self) -> None:
        task = self.task
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await drain(task, propagate_cancel=False)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            if self.task is task:
                self.task = None
        if isinstance(self.error, ChildNotReapedError):
            raise self.error

    async def aclose(self) -> None:
        self.closed = True
        self.changed.set()
        await self.stop()
        async with self.lock:
            self.clear()

    def check_open(self) -> None:
        if self.session.work.closed.is_set():
            raise asyncio.CancelledError(WORK_CLOSED)
        if self.closed:
            raise HTTPException(409, "Playback session closed.")

    def clear(self) -> None:
        for artifact in self.cache.values():
            artifact.discard()
        self.cache.clear()
        self.durations.clear()

    async def get(
        self,
        index: int,
        audio: int | None,
        subtitle: int | None,
        validate: Callable[[], Awaitable[None]],
    ) -> ArtifactLease:
        async with self.lock:
            try:
                return await self.prepare(index, audio, subtitle, validate)
            except BaseException:
                await self.stop()
                raise

    async def prepare(
        self, index: int, audio: int | None, subtitle: int | None, validate: Callable[[], Awaitable[None]]
    ) -> ArtifactLease:
        self.check_open()
        if isinstance(self.error, ChildNotReapedError):
            raise self.error
        selection = (audio, subtitle)
        if self.selection != selection:
            await self.stop()
            self.check_open()
            self.clear()
            self.selection = selection
        if index in self.cache:
            if index > self.demand:
                self.demand = index
                self.demanded.set()
            return self.cache[index].lease()
        running = self.task is not None and not self.task.done()
        if not running or not self.next_index <= index <= self.demand + segments.ENCODE_AHEAD + 1:
            await self.stop()
            self.check_open()
            self.error = None
            self.demand = self.next_index = index
            self.task = asyncio.create_task(self.run(index, audio, subtitle, validate))
        self.demand = max(index, self.demand)
        self.demanded.set()
        while index not in self.cache:
            self.changed.clear()
            self.check_open()
            if self.error is not None:
                raise self.error
            if self.task is None or self.task.done():
                raise HTTPException(502, f"FFmpeg stopped before producing segment {index}.")
            await self.changed.wait()
        return self.cache[index].lease()

    async def run(
        self, index: int, audio: int | None, subtitle: int | None, validate: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            self.check_open()
            self.session.work.register_current()
            async with asyncio.timeout(_ENCODER_WAIT_S):
                await self.session.segment_semaphore.acquire()
            try:
                failures = []
                for encoder in await self.service.catalog.chain():
                    plan = segments.plan_playback(
                        index=index,
                        duration_s=self.session.summary.duration_s,
                        segment_s=self.session.config.segment_duration_s,
                        encoder=encoder,
                        summary=self.session.summary,
                        audio_stream_index=audio,
                        subtitle_stream_index=subtitle,
                    )
                    published = self.next_index
                    try:
                        await self.encode(plan, self.session.grant.path, validate)
                    except ChildNotReapedError:
                        raise
                    except (OSError, ValueError, TimeoutError) as exc:
                        reason = f"Encoder {encoder}: {error_message(exc)}. Exit code: {self.exit_code}."
                        failures.append(reason)
                        LOG.warning(
                            "Playback conversion for %s failed with encoder %s. Exit code: %s.",
                            self.session.grant.path,
                            encoder,
                            self.exit_code,
                            extra={"error": error_message(exc)},
                        )
                        self.service.catalog.record_failure(encoder)
                        if self.next_index != published:
                            raise HTTPException(502, reason) from exc
                        continue
                    self.service.catalog.record_success(encoder)
                    return
                raise HTTPException(502, " ".join(failures) or "No playback encoder is available.")
            finally:
                self.session.segment_semaphore.release()
        except asyncio.CancelledError:
            pass
        except ProbeError as exc:
            detail = f"Playback source could not be read: {error_message(exc)}"
            LOG.warning(
                "Playback source could not be read. Source: %s",
                self.session.grant.path,
                extra={"error": error_message(exc)},
            )
            self.error = HTTPException(410, detail)
        except TimeoutError:
            detail = f"Playback conversion waited {_ENCODER_WAIT_S:g} seconds for an encoder slot. Retry shortly."
            LOG.warning("%s Source: %s", detail, self.session.grant.path)
            self.error = HTTPException(503, detail)
        except Exception as exc:
            if not isinstance(exc, HTTPException):
                LOG.error(
                    "Playback conversion for %s failed", self.session.grant.path, extra={"error": error_message(exc)}
                )
            self.error = exc
        finally:
            self.changed.set()

    async def encode(self, plan: PlaybackPlan, source: Path, validate: Callable[[], Awaitable[None]]) -> None:
        self.exit_code = None
        self.stderr_tail.clear()
        proc = await spawn_child(
            self.service.ffmpeg_bin,
            *render_playback(plan, str(source)),
            force_on_cancel=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_READ_BYTES,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout, stderr = proc.stdout, proc.stderr
        ranges: asyncio.Queue[tuple[int, float]] = asyncio.Queue(maxsize=4)
        metadata = asyncio.create_task(read_ranges(proc.stderr, ranges, self.stderr_tail))
        reading = asyncio.create_task(proc.stdout.read(_READ_BYTES))
        boundary = asyncio.create_task(ranges.get())
        buffer = bytearray()
        extent: tuple[int, float] | None = None
        eof = False
        artifact = None
        unreaped = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + segments.encode_timeout_s(plan.segment_s)
        try:
            while True:
                remaining = (self.session.grant.expires_at - utcnow()).total_seconds()
                if remaining <= 0:
                    return
                if extent is not None and len(buffer) >= extent[0]:
                    length, duration = extent
                    async with asyncio.timeout(max(0, min(deadline - loop.time(), remaining))):
                        await validate()
                    previous = self.cache.pop(self.next_index, None)
                    if previous is not None:
                        previous.discard()
                    while len(self.cache) >= CACHE_SEGMENTS:
                        old, cached = self.cache.popitem(last=False)
                        self.durations.pop(old, None)
                        cached.discard()
                    artifact = self.session.files.new_artifact("seg")
                    data = bytes(buffer[:length])
                    del buffer[:length]
                    await drain(asyncio.create_task(asyncio.to_thread(self.write, artifact.fd, data)))
                    self.cache[self.next_index] = artifact
                    self.durations[self.next_index] = duration
                    artifact = None
                    self.next_index += 1
                    self.changed.set()
                    deadline = loop.time() + segments.encode_timeout_s(plan.segment_s)
                    extent = None
                    boundary = asyncio.create_task(ranges.get())
                    if self.next_index > self.demand + segments.ENCODE_AHEAD:
                        self.demanded.clear()
                        if proc.returncode is None:
                            with suppress(ProcessLookupError):
                                proc.send_signal(signal.SIGSTOP)
                        try:
                            async with asyncio.timeout(min(IDLE_TIMEOUT_S, remaining)):
                                await self.demanded.wait()
                        except TimeoutError:
                            return
                        finally:
                            if proc.returncode is None:
                                with suppress(ProcessLookupError):
                                    proc.send_signal(signal.SIGCONT)
                        deadline = loop.time() + segments.encode_timeout_s(plan.segment_s)
                    continue
                if boundary.done() and extent is None:
                    extent = boundary.result()
                    if extent[0] > segments.SEGMENT_MAX_BYTES:
                        raise ValueError("Playback output exceeded its size limit.")
                    continue
                if len(buffer) > segments.SEGMENT_MAX_BYTES + _READ_BYTES:
                    raise ValueError("Playback output exceeded its size limit.")
                if metadata.done():
                    metadata.result()
                    if eof and ranges.empty() and extent is None:
                        async with asyncio.timeout(max(0, min(deadline - loop.time(), remaining))):
                            returncode = await proc.wait()
                        if returncode != 0:
                            detail = self.stderr_tail.decode("utf-8", errors="replace").strip()
                            raise ValueError(f"FFmpeg exited with code {returncode}. {detail}".rstrip())
                        if buffer:
                            message = f"FFmpeg left {len(buffer)} bytes without a segment boundary."
                            raise ValueError(message)
                        return
                if reading.done() and not eof:
                    chunk = reading.result()
                    if chunk:
                        buffer.extend(chunk)
                        reading = asyncio.create_task(proc.stdout.read(_READ_BYTES))
                    else:
                        eof = True
                    continue
                waiting = [task for task in (reading, boundary, metadata) if not task.done()]
                done, _ = await asyncio.wait(
                    waiting,
                    timeout=max(0, min(deadline - loop.time(), remaining)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    message = (
                        f"FFmpeg produced no segment within {segments.encode_timeout_s(plan.segment_s):g} seconds."
                    )
                    raise TimeoutError(message)
        except ChildNotReapedError:
            unreaped = True
            raise
        finally:

            async def cleanup() -> None:
                for task in (reading, boundary, metadata):
                    task.cancel()
                await asyncio.gather(reading, boundary, metadata, return_exceptions=True)

                async def discard(reader: asyncio.StreamReader) -> None:
                    while await reader.read(_READ_BYTES):
                        pass

                # asyncio's process wait can depend on pipe EOF. Keep draining
                # both pipes while stopping, including a backpressured child.
                readers = [asyncio.create_task(discard(stdout)), asyncio.create_task(discard(stderr))]
                try:
                    await stop_child(proc, force=True)
                finally:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)

            try:
                await drain(asyncio.create_task(cleanup()))
            except ChildNotReapedError:
                unreaped = True
                raise
            finally:
                self.exit_code = proc.returncode
                if artifact is not None:
                    if unreaped:
                        artifact.close()
                    else:
                        artifact.discard()

    @staticmethod
    def write(fd: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = os.pwrite(fd, data[offset:], offset)
            if written <= 0:
                raise OSError("Playback output write failed.")
            offset += written
