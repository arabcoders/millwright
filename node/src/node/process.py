from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import TYPE_CHECKING, Any

from common.process import ChildNotReapedError, drain

if TYPE_CHECKING:
    from pathlib import Path

_TERM_GRACE_S = 10.0


async def create_tool(*argv: str | Path, workspace: Path, **kwargs: Any) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "node.tool", str(workspace), *argv, start_new_session=True, **kwargs
    )


async def spawn_tool(
    *argv: str | Path, workspace: Path, force_on_cancel: bool = False, **kwargs: Any
) -> asyncio.subprocess.Process:
    starting = asyncio.create_task(create_tool(*argv, workspace=workspace, **kwargs))
    try:
        await asyncio.wait((starting,))
        return starting.result()
    except asyncio.CancelledError:
        # Wait for the supervisor to start so cancellation can stop it and the tool.
        try:
            proc = await drain(starting, propagate_cancel=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChildNotReapedError("could not clean up the starting tool supervisor") from exc
        await stop_tool(proc, force=force_on_cancel)
        raise


async def stop_tool(proc: asyncio.subprocess.Process, *, force: bool = False) -> int:
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
        # SIGUSR1 kills the actual tool, never the supervisor holding its lock.
        with contextlib.suppress(OSError):
            proc.send_signal(signal.SIGUSR1)
        try:
            return await proc.wait()
        except Exception as exc:
            raise ChildNotReapedError("tool supervisor exit could not be confirmed after stopping") from exc

    return await drain(asyncio.create_task(stop()))
