from __future__ import annotations

import asyncio
import contextlib

import pytest
from common.protocol import Welcome
from node.agent import Agent, AgentConfig
from tests.helpers import make_test_temp_dir


class WaitingConnection:
    def __init__(self, welcome: bool) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[dict | None] = asyncio.Queue()
        if welcome:
            self.incoming.put_nowait(Welcome(timezone="UTC", active_attempts=[]).model_dump(mode="json"))
        self.closed = False
        self.sent_event = asyncio.Event()
        self.welcome_event = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        self.sent_event.set()

    async def recv_json(self) -> dict | None:
        data = await asyncio.wait_for(self.incoming.get(), timeout=2)
        if data is not None and data.get("type") == "welcome":
            self.welcome_event.set()
        return data

    async def close(self) -> None:
        self.closed = True


def config() -> AgentConfig:
    return AgentConfig(token="token", server_url="ws://hub", workspace_dir=str(make_test_temp_dir("node-shutdown")))


@pytest.mark.asyncio
async def test_shutdown_before_welcome() -> None:
    connection = WaitingConnection(welcome=False)

    async def connect(_: str) -> WaitingConnection:
        return connection

    agent = Agent(config(), connect=connect)
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(connection.sent_event.wait(), timeout=1)
        await asyncio.wait_for(agent.aclose(), timeout=1)
    finally:
        await asyncio.wait_for(agent.aclose(), timeout=1)
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    assert task.cancelled()
    assert connection.closed
    assert agent._welcomed is False


@pytest.mark.asyncio
async def test_shutdown_after_welcome() -> None:
    connection = WaitingConnection(welcome=True)

    async def connect(_: str) -> WaitingConnection:
        return connection

    agent = Agent(config(), connect=connect)
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(connection.welcome_event.wait(), timeout=1)
        await asyncio.wait_for(agent.aclose(), timeout=1)
    finally:
        await asyncio.wait_for(agent.aclose(), timeout=1)
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    assert task.cancelled()
    assert connection.closed
    assert agent._welcomed is True
