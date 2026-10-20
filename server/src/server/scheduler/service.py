from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

LOG = logging.getLogger(__name__)


@dataclass(kw_only=True)
class _Callback:
    name: str
    run: Callable[[], Awaitable[object]]
    every: float
    due: float = 0
    task: asyncio.Task[None] | None = None


class Scheduler:
    def __init__(
        self,
        *,
        tick_s: float = 60,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not math.isfinite(tick_s) or tick_s <= 0:
            raise ValueError("Scheduler tick must be positive and finite")
        self._tick_s = tick_s
        self._clock = clock
        self._sleep = sleep
        self._callbacks: list[_Callback] = []
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    def add(self, callback: Callable[[], Awaitable[object]], *, every: float = 60, name: str = "callback") -> None:
        if not math.isfinite(every) or every <= 0:
            raise ValueError("Scheduler interval must be positive and finite")
        if any(entry.run == callback for entry in self._callbacks):
            raise ValueError("Scheduler callback is already registered")
        self._callbacks.append(_Callback(name=name, run=callback, every=every, due=self._clock()))

    def start(self) -> None:
        if self._close_task is not None and not self._close_task.done():
            return
        if self._task is not None and not self._task.done():
            return
        self._close_task = None
        for entry in self._callbacks:
            entry.due = self._clock()
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def aclose(self) -> None:
        if self._close_task is None:
            tasks = [entry.task for entry in self._callbacks if entry.task is not None]
            if self._task is not None:
                self._task.cancel()
                tasks.append(self._task)
            self._close_task = asyncio.create_task(self._close(tasks))
        # Finish each running pass instead of cancelling during connection creation
        # or filesystem work. Repeated cancellation must still wait for that work.
        closing = self._close_task
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self, tasks: list[asyncio.Task[None]]) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        for entry in self._callbacks:
            entry.task = None

    async def _run(self) -> None:
        while True:
            now = self._clock()
            for entry in self._callbacks:
                if now < entry.due or (entry.task is not None and not entry.task.done()):
                    continue
                # Skip missed ticks rather than building a backlog of callback runs.
                entry.due = now + entry.every
                entry.task = asyncio.create_task(self._run_callback(entry), name=f"scheduler:{entry.name}")
            await self._sleep(self._tick_s)

    async def _run_callback(self, entry: _Callback) -> None:
        try:
            await entry.run()
        except Exception:
            LOG.exception("Scheduler callback %s failed", entry.name)
