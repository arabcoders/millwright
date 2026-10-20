from __future__ import annotations

import asyncio
import contextlib
import os
from asyncio import create_subprocess_exec as _create_subprocess_exec
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

_TERM_GRACE_S = 10.0

# Node tools must keep workspace locks if the node exits. Apply the same
# launcher and stop function to nested capability checks.
_POLICY: ContextVar[
    tuple[Callable[..., Awaitable[asyncio.subprocess.Process]], Callable[..., Awaitable[int]]] | None
] = ContextVar("subprocess_policy", default=None)


@contextmanager
def process_policy(
    *, create: Callable[..., Awaitable[asyncio.subprocess.Process]], stop: Callable[..., Awaitable[int]]
) -> Iterator[None]:
    token = _POLICY.set((create, stop))
    try:
        yield
    finally:
        _POLICY.reset(token)


async def create_subprocess_exec(*argv: str | Path, **kwargs: Any) -> asyncio.subprocess.Process:
    policy = _POLICY.get()
    return await (policy[0](*argv, **kwargs) if policy is not None else _create_subprocess_exec(*argv, **kwargs))


class ChildNotReapedError(RuntimeError):
    pass


async def drain[T](future: asyncio.Future[T], *, propagate_cancel: bool = True) -> T:
    # Cleanup failures take priority over cancellation, especially an unconfirmed exit.
    cancelled = False
    while not future.done():
        try:
            await asyncio.wait((future,))
        except asyncio.CancelledError:
            cancelled = True
    result = future.result()
    if cancelled and propagate_cancel:
        raise asyncio.CancelledError
    return result


async def spawn_child(*argv: str | Path, force_on_cancel: bool = False, **kwargs: Any) -> asyncio.subprocess.Process:
    starting = asyncio.create_task(create_subprocess_exec(*argv, **kwargs))
    try:
        await asyncio.wait((starting,))
        return starting.result()
    except asyncio.CancelledError:
        # Wait for the process to start so cancellation cannot leave it running.
        try:
            proc = await drain(starting, propagate_cancel=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChildNotReapedError("could not clean up the starting subprocess") from exc
        await stop_child(proc, force=force_on_cancel)
        raise


async def stop_child(proc: asyncio.subprocess.Process, *, force: bool = False) -> int:
    policy = _POLICY.get()
    if policy is not None:
        return await policy[1](proc, force=force)

    async def stop() -> int:
        if proc.returncode is not None:
            return proc.returncode
        if not force:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                return await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_S)
            except Exception:
                pass
        with contextlib.suppress(OSError):
            proc.kill()
        try:
            # A killed process can still be stuck in storage I/O. Do not release
            # workspace locks until the OS confirms it has exited.
            return await proc.wait()
        except Exception as exc:
            raise ChildNotReapedError("subprocess exit could not be confirmed after stopping") from exc

    return await drain(asyncio.create_task(stop()))


async def run_capture(
    argv: list[str],
    *,
    timeout_s: float,
    stderr_tail: int = 2000,
    stdout: int | None = asyncio.subprocess.DEVNULL,
    output_fd: int | None = None,
    max_bytes: int = 0,
) -> tuple[int, bytes]:
    """Bound stderr memory. Confirm child exit before propagating timeout or cancellation."""
    proc = await spawn_child(
        *argv,
        force_on_cancel=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if output_fd is not None else stdout,
        stderr=asyncio.subprocess.PIPE,
    )
    tail = bytearray()

    async def collect() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            tail.extend(chunk)
            if len(tail) > stderr_tail:
                del tail[: len(tail) - stderr_tail]

    collecting = asyncio.create_task(collect())

    async def write_output() -> None:
        assert proc.stdout is not None
        assert output_fd is not None
        size = 0
        while chunk := await proc.stdout.read(65536):
            if size + len(chunk) > max_bytes:
                raise ValueError("Playback output exceeded its size limit.")
            offset = 0
            while offset < len(chunk):
                written = await drain(
                    asyncio.create_task(asyncio.to_thread(os.pwrite, output_fd, chunk[offset:], size + offset))
                )
                if written <= 0:
                    raise OSError("Playback output write failed.")
                offset += written
            size += len(chunk)

    waiting = asyncio.create_task(proc.wait())
    writing = asyncio.create_task(write_output()) if output_fd is not None else None
    try:
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(waiting, *([writing] if writing is not None else []))
    except TimeoutError:
        await stop_child(proc, force=True)
        raise
    except Exception, asyncio.CancelledError:
        await stop_child(proc, force=True)
        raise
    finally:
        if proc.returncode is None:
            collecting.cancel()
            waiting.cancel()
            if writing is not None:
                writing.cancel()

        async def cleanup() -> None:
            await asyncio.gather(
                waiting, *([writing] if writing is not None else []), collecting, return_exceptions=True
            )

        await drain(asyncio.create_task(cleanup()))
    return proc.returncode if proc.returncode is not None else -1, bytes(tail)
