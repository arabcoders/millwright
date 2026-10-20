from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query, WebSocket
from sqlalchemy import select

from server.events import EventBus, Subscription
from server.models import SessionRow, utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from server.api.auth import AuthStack

LOG = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

# Browsers cannot set upgrade headers. Missing, expired, or replayed one-time
# tickets close the connection with a policy violation.
TICKET_REJECTED = 1008


@router.websocket("/api/events")
async def events_socket(ws: WebSocket, ticket: Annotated[str | None, Query()] = None) -> None:
    stack: AuthStack = ws.app.state.auth
    bus: EventBus = ws.app.state.events
    principal = stack.ws_tickets.consume(ticket or "")
    if principal is None:
        with contextlib.suppress(Exception):
            await ws.accept()
            await ws.close(code=TICKET_REJECTED)
        return

    session_hash = principal.session_hash
    expires_at = principal.session_expires_at
    revoked = stack.ws_tickets.watch_session(session_hash) if session_hash is not None else None
    sub: Subscription | None = None
    tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = []
    try:
        # Register before the database read so a concurrent committed logout cannot
        # fall between initial validation and revocation monitoring.
        if session_hash is not None:
            async with stack.sessionmaker() as db:
                active = await db.scalar(
                    select(SessionRow.id).where(SessionRow.token_hash == session_hash, SessionRow.expires_at > utcnow())
                )
            if active is None or revoked is None or revoked.is_set():
                await ws.accept()
                await ws.close(code=TICKET_REJECTED)
                return
        await ws.accept()
        sub = bus.subscribe()
        producer = asyncio.create_task(_send_events(ws, sub, revoked, expires_at))
        tasks.append(producer)
        tasks.append(asyncio.create_task(_watch_disconnect(ws, sub)))
        tasks.append(asyncio.create_task(sub.closed.wait()))
        if revoked is not None and expires_at is not None:
            tasks.append(asyncio.create_task(_watch_session(revoked, expires_at, sub)))
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if producer.done():
            await producer
    except Exception:
        LOG.debug("events client disconnected", exc_info=True)
    finally:
        for task in tasks:
            task.cancel()
        if sub is not None:
            bus.unsubscribe(sub)
        if session_hash is not None and revoked is not None:
            stack.ws_tickets.unwatch_session(session_hash, revoked)

        # Drain the send before closing the transport. Shield cleanup so repeated
        # cancellation cannot leave a producer or watcher using the connection.
        async def cleanup() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            if revoked is not None and (revoked.is_set() or expires_at is None or utcnow() >= expires_at):
                with contextlib.suppress(Exception):
                    await ws.close(code=TICKET_REJECTED)

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


async def _send_events(
    ws: WebSocket, sub: Subscription, revoked: asyncio.Event | None, expires_at: datetime | None
) -> None:
    while True:
        event = await sub.next()
        # Subscription closure drains its queue. Session revocation must not.
        if revoked is not None and (revoked.is_set() or expires_at is None or utcnow() >= expires_at):
            return
        if event is None:
            return
        await ws.send_json({"type": event.type, "payload": event.payload})


async def _watch_session(revoked: asyncio.Event, expires_at: datetime, sub: Subscription) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(revoked.wait(), timeout=max(0, (expires_at - utcnow()).total_seconds()))
    revoked.set()
    sub.close()


async def _watch_disconnect(ws: WebSocket, sub: Subscription) -> None:
    # The send loop can block on sub.next() for a long time. Only the receive
    # side notices a vanished client promptly.
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            sub.close()
            return
