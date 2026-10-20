from __future__ import annotations

import asyncio
import contextlib
import json
import os
from functools import partial
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

    from server.api.auth import AuthStack

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from server.api.auth import current_principal
from server.core.log_history import open_log, query_logs
from server.models import utcnow
from server.workspace import wait_workspace_io

router = APIRouter(prefix="/api", tags=["logs"], dependencies=[Depends(current_principal)])


@router.get("/logs")
async def read_logs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: Annotated[int | None, Query(ge=1)] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    levels: str | None = None,
    logger: str | None = None,
    search: str | None = None,
) -> dict:
    if before is not None and after is not None:
        raise HTTPException(400, "before and after cannot be combined")
    state_dir: Path = request.app.state.log_state_dir
    parsed = {part.strip().lower() for part in levels.split(",")} if levels else None
    return await asyncio.wait_for(
        wait_workspace_io(
            lambda: query_logs(
                state_dir, limit=limit, before=before, after=after, levels=parsed, logger=logger, search=search
            )
        ),
        timeout=10,
    )


@router.get("/logs/stream")
async def stream_logs(request: Request, after: Annotated[int | None, Query(ge=1)] = None) -> StreamingResponse:
    header = request.headers.get("last-event-id")
    if header is not None:
        try:
            header_id = int(header)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be a positive integer") from exc
        if header_id < 1:
            raise HTTPException(400, "Last-Event-ID must be a positive integer")
        after = header_id
    return _LogStreamResponse(request, after)


def _read_tail(
    state_dir: Path, identity: tuple[int, int] | None, offset: int | None
) -> tuple[os.stat_result, bytes, int]:
    with open_log(state_dir) as handle:
        info = os.fstat(handle.fileno())
        if offset is None:
            return info, b"", info.st_size
        if (info.st_dev, info.st_ino) != identity or info.st_size < offset:
            return info, b"", 0
        handle.seek(offset)
        data = handle.read(min(65536, info.st_size - offset))
        return info, data, handle.tell()


class _LogStreamResponse(StreamingResponse):
    def __init__(self, request: Request, after: int | None) -> None:
        self.request = request
        self.after = after
        self.stack: AuthStack = request.app.state.auth
        self.shutdown_requested: asyncio.Event = request.app.state.shutdown_requested
        self.session_hash: str | None = None
        self.expires_at: datetime | None = None
        self.revoked: asyncio.Event | None = None
        self.authorized = False
        self.serving = False
        self.stream = self.events()
        super().__init__(self.stream, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def authorize(self) -> None:
        principal = await current_principal(self.request)
        self.session_hash = principal.session_hash
        self.expires_at = principal.session_expires_at
        if self.session_hash is not None:
            self.revoked = self.stack.ws_tickets.watch_session(self.session_hash)
            # The first query identifies the session. Revalidate after registering
            # so a committed logout cannot fall into the gap between validation and monitoring.
            await current_principal(self.request)
            if self.revoked.is_set() or self.expires_at is None or utcnow() >= self.expires_at:
                raise HTTPException(401, "invalid or expired session")
        self.authorized = True

    def unwatch(self) -> None:
        if self.session_hash is not None and self.revoked is not None:
            self.stack.ws_tickets.unwatch_session(self.session_hash, self.revoked)
            self.revoked = None

    async def watch_session(self) -> None:
        if self.revoked is None or self.expires_at is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.revoked.wait(), timeout=max(0, (self.expires_at - utcnow()).total_seconds()))
        self.revoked.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.serving = True
        tasks: list[asyncio.Task[None]] = []
        try:
            await self.authorize()
            response = asyncio.create_task(super().__call__(scope, receive, send))
            tasks.append(response)
            if self.revoked is not None:
                tasks.append(asyncio.create_task(self.watch_session()))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if response.done():
                await response
        finally:
            for task in tasks:
                task.cancel()
            self.unwatch()

            async def cleanup() -> None:
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.stream.aclose()

            cleaning = asyncio.create_task(cleanup())
            cancelled = False
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    cancelled = True
            cleaning.result()
            if cancelled:
                raise asyncio.CancelledError

    async def events(self) -> AsyncGenerator[str]:
        try:
            if not self.authorized:
                await self.authorize()
            async with contextlib.aclosing(self.log_events()) as events:
                async for event in events:
                    yield event
        finally:
            if not self.serving:
                self.unwatch()

    async def log_events(self) -> AsyncGenerator[str]:
        request = self.request
        state_dir: Path = request.app.state.log_state_dir

        async def session_is_valid() -> bool:
            if self.shutdown_requested.is_set():
                return False
            if self.revoked is not None and self.revoked.is_set():
                return False
            try:
                await current_principal(request)
            except HTTPException:
                return False
            return True

        cursor = self.after or 0
        if self.shutdown_requested.is_set():
            return
        try:
            initial, _, offset = await wait_workspace_io(lambda: _read_tail(state_dir, None, None))
            identity = (initial.st_dev, initial.st_ino)
        except FileNotFoundError:
            offset, identity = 0, None
        backlog = await wait_workspace_io(partial(query_logs, state_dir, after=cursor, limit=500))
        while True:
            for entry in backlog["entries"]:
                if int(entry["id"]) > cursor:
                    if not await session_is_valid():
                        return
                    cursor = int(entry["id"])
                    yield f"id: {entry['id']}\nevent: log_lines\ndata: {json.dumps(entry, separators=(',', ':'))}\n\n"
            if len(backlog["entries"]) < 500:
                break
            backlog = await wait_workspace_io(partial(query_logs, state_dir, after=cursor, limit=500))
        pending = b""
        while not await request.is_disconnected():
            if not await session_is_valid():
                return
            try:
                current, data, new_offset = await wait_workspace_io(partial(_read_tail, state_dir, identity, offset))
                if (current.st_dev, current.st_ino) != identity or current.st_size < offset:
                    identity = (current.st_dev, current.st_ino)
                    offset = new_offset
                    pending = b""
                    backlog = await wait_workspace_io(partial(query_logs, state_dir, after=cursor, limit=500))
                    while True:
                        for entry in backlog["entries"]:
                            if int(entry["id"]) > cursor:
                                if not await session_is_valid():
                                    return
                                cursor = int(entry["id"])
                                yield f"id: {entry['id']}\nevent: log_lines\ndata: {json.dumps(entry, separators=(',', ':'))}\n\n"
                        if len(backlog["entries"]) < 500:
                            break
                        backlog = await wait_workspace_io(partial(query_logs, state_dir, after=cursor, limit=500))
                elif data:
                    pending += data
                    offset = new_offset
                    lines = pending.splitlines(keepends=True)
                    pending = lines.pop() if lines and not lines[-1].endswith(b"\n") else b""
                    for line in lines:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError, UnicodeDecodeError:
                            continue
                        if int(entry["id"]) > cursor:
                            if not await session_is_valid():
                                return
                            cursor = int(entry["id"])
                            yield f"id: {entry['id']}\nevent: log_lines\ndata: {json.dumps(entry, separators=(',', ':'))}\n\n"
            except FileNotFoundError:
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.shutdown_requested.wait(), timeout=0.25)
