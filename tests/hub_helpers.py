from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from collections.abc import Sequence

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeConn:
    def __init__(self, incoming: Sequence[dict[str, Any] | None] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.receiving = asyncio.Event()
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.closed: int | None = None
        for item in incoming or []:
            self.incoming.put_nowait(item)

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)
        self.changed.set()

    async def recv_json(self) -> dict[str, Any] | None:
        if self.messages("welcome"):
            self.ready.set()
        self.receiving.set()
        return await self.incoming.get()

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    def push(self, data: dict[str, Any] | None) -> None:
        self.incoming.put_nowait(data)
        self.changed.set()

    def disconnect(self) -> None:
        self.incoming.put_nowait(None)

    def messages(self, kind: str) -> list[dict[str, Any]]:
        return [message for message in self.sent if message.get("type") == kind]


class NoDatabase(async_sessionmaker[AsyncSession]):
    def __call__(self, **_local_kw: Any) -> AsyncSession:
        pytest.fail("Hub unit tests must not open a database session.")
