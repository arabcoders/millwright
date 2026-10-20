from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from common.process import drain
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from server.models import utcnow
from server.playback.service import WORK_CLOSED

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

    from server.api.auth import AuthStack
    from server.playback.grants import PlaybackGrant
    from server.playback.service import PlaybackService, PlaybackSession


class PlaybackStreamClosed(asyncio.CancelledError):
    """Abort a revoked stream without presenting normal revocation as a server failure."""


class GuardedPlaybackResponse(StreamingResponse):
    """
    Streams playback content while the grant, its session, or its work can still be revoked.

    Authorization registers the revocation watchers before rechecking the grant,
    so a committed logout or close cannot be missed between those steps. Any trigger cancels the in-flight ASGI send. Bytes already handed to
    the transport cannot be recalled.
    """

    def __init__(
        self,
        request: Request,
        grant: PlaybackGrant,
        session: PlaybackSession,
        content: AsyncGenerator[bytes] | None,
        *,
        media_type: str,
        headers: dict[str, str],
        status_code: int = 200,
        cleanup: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self.request = request
        self.grant = grant
        self.session = session
        self.stack: AuthStack = request.app.state.auth
        self.playback: PlaybackService = request.app.state.playback
        self.revoked: asyncio.Event | None = None
        self.cleanup = cleanup
        stream = content if content is not None else _empty()
        super().__init__(stream, status_code=status_code, media_type=media_type, headers=headers)

    @property
    def session_hash(self) -> str | None:
        return self.grant.session_hash

    @property
    def deadline(self) -> datetime:
        # The shorter lifetime wins: an OIDC session must not extend a grant.
        deadlines = [self.session.grant.expires_at]
        if self.grant.session_expires_at is not None:
            deadlines.append(self.grant.session_expires_at)
        return min(deadlines)

    async def authorize(self) -> None:
        if self.session_hash is not None:
            self.revoked = self.stack.ws_tickets.watch_session(self.session_hash)
        still_valid = self.playback.session(self.grant.playback_id)
        revoked_now = (self.revoked is not None and self.revoked.is_set()) or self.session.work.closed.is_set()
        if still_valid is not self.session or revoked_now or utcnow() >= self.deadline:
            self.unwatch()
            raise HTTPException(401, "invalid or expired playback grant")

    def unwatch(self) -> None:
        if self.session_hash is not None and self.revoked is not None:
            self.stack.ws_tickets.unwatch_session(self.session_hash, self.revoked)
            self.revoked = None

    async def watch_deadline(self) -> None:
        waiters = [asyncio.create_task(self.session.work.closed.wait())]
        if self.revoked is not None:
            waiters.append(asyncio.create_task(self.revoked.wait()))
        try:
            while True:
                remaining = (self.deadline - utcnow()).total_seconds()
                done, _ = await asyncio.wait(waiters, timeout=max(0.0, remaining), return_when=asyncio.FIRST_COMPLETED)
                if done or utcnow() >= self.deadline:
                    if self.session.work.closed.is_set():
                        reason = "Playback session closed."
                    elif self.revoked is not None and self.revoked.is_set():
                        reason = "Login session revoked."
                    else:
                        reason = "Playback grant expired."
                    raise PlaybackStreamClosed(reason)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        tasks: list[asyncio.Task[None]] = []
        try:
            try:
                await self.authorize()
                self.session.work.register_current()
                response = asyncio.create_task(super().__call__(scope, receive, send))
                tasks.append(response)
                deadline = asyncio.create_task(self.watch_deadline())
                tasks.append(deadline)
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if response.done():
                    await response
                else:
                    await deadline
            finally:
                current = asyncio.current_task()
                for task in tasks:
                    task.cancel()
                self.unwatch()

                async def cleanup() -> None:
                    try:
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        try:
                            if isinstance(self.body_iterator, AsyncGenerator):
                                await self.body_iterator.aclose()
                        finally:
                            if self.cleanup is not None:
                                result = self.cleanup()
                                if inspect.isawaitable(result):
                                    await result
                        for result in results:
                            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                                raise result
                    finally:
                        if current is not None:
                            self.session.work.tasks.discard(current)

                # Keep cleanup registered with the session until reads finish and temporary files are removed.
                await drain(asyncio.create_task(cleanup()))
        except asyncio.CancelledError as exc:
            if exc.args == (WORK_CLOSED,) and self.session.work.closed.is_set():
                raise PlaybackStreamClosed("Playback session closed.") from None
            raise


async def _empty() -> AsyncGenerator[bytes]:
    return
    yield b""
