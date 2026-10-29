from __future__ import annotations

import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import partial
from multiprocessing.connection import wait
from threading import Lock, Thread
from typing import TYPE_CHECKING

from common.process import drain

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

_ACTIVE_POOL: ContextVar[ProcessPoolExecutor | None] = ContextVar("storage_pool", default=None)
_OPERATION = Lock()


def _watch_parent() -> None:
    parent = multiprocessing.parent_process()
    assert parent is not None
    wait([parent.sentinel])
    # An idle executor's task queue does not wake when its parent dies. Keep
    # locks held by the worker until the writer or tool finishes, then exit so recovery can
    # acquire them instead of leaving an idle orphan holding them indefinitely.
    with _OPERATION:
        os._exit(0)


def _start_worker() -> None:
    Thread(target=_watch_parent, name="storage-parent", daemon=True).start()


def _operate[T, **P](operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    with _OPERATION:
        return operation(*args, **kwargs)


@asynccontextmanager
async def storage_scope() -> AsyncGenerator[ProcessPoolExecutor]:
    if (pool := _ACTIVE_POOL.get()) is not None:
        yield pool
        return
    # Copy/fsync descriptors on FUSE blocked posix_spawn while it held the API's
    # GIL. Spawn a clean process before opening media, not a filesystem thread.
    pool = ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn"), initializer=_start_worker
    )
    token = _ACTIVE_POOL.set(pool)
    try:
        yield pool
    finally:
        try:
            await drain(asyncio.create_task(asyncio.to_thread(pool.shutdown, wait=True)))
        finally:
            _ACTIVE_POOL.reset(token)


async def storage_io[T, **P](operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    async with storage_scope() as pool:
        future = asyncio.get_running_loop().run_in_executor(pool, partial(_operate, operation, *args, **kwargs))
        # Cancellation must not release job locks while a writer or its
        # media tool can still mutate files. The scope also reaps the worker.
        return await drain(future)
