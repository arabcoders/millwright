from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.protocol import NodeCapabilities
from server.nodehub.store import (
    authenticate,
    get_node,
    hash_token,
    list_nodes,
    register_node,
    set_node_state,
    touch_node,
)
from tests.helpers import database_context

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 12, 1, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("nodes", tables=("nodes",)) as factory:
        yield factory


@pytest.mark.asyncio
async def test_register_keeps_id(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        first = await register_node(
            session,
            "worker-1",
            "tok-1",
            capabilities=NodeCapabilities(ffmpeg_version="7.1", encoders=["libx264"]),
        )
        first.transcode_limit = 3
        first.healthcheck_limit = 0
        first.mkv_stats_limit = 2
        await session.flush()
        second = await register_node(
            session,
            "worker-1",
            "tok-2",
            capabilities=NodeCapabilities(
                encoders=["hevc_nvenc", "aac"],
                video_encoders=["hevc_nvenc"],
                audio_encoders=["aac"],
                decoders=["aac"],
                audio_decoders=["aac"],
            ),
        )
        assert second.id == first.id
        await session.commit()

    async with db() as session:
        node = await get_node(session, "worker-1")
        assert node is not None
        assert node.token_hash == hash_token("tok-2")
        assert node.capabilities == {
            "hostname": "",
            "ffmpeg_version": "",
            "encoders": ["hevc_nvenc", "aac"],
            "video_encoders": ["hevc_nvenc"],
            "audio_encoders": ["aac"],
            "decoders": ["aac"],
            "audio_decoders": ["aac"],
            "hwaccels": [],
            "worker_tags": [],
            "cpu_model": "",
            "cpu_cores": 0,
            "memory_gb": 0.0,
            "gpus": [],
            "max_transcodes": 1,
            "max_healthchecks": 1,
            "max_mkv_stats": 1,
            "mkvpropedit": False,
        }
        assert (node.transcode_limit, node.healthcheck_limit, node.mkv_stats_limit) == (3, 0, 2)
        assert await authenticate(session, "worker-1", "tok-2") is not None
        assert await authenticate(session, "worker-1", "tok-1") is None
        assert len(await list_nodes(session)) == 1


@pytest.mark.asyncio
async def test_authenticate_wrong_token(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await register_node(session, "worker-1", "right")
        assert await authenticate(session, "worker-1", "right") is not None
        assert await authenticate(session, "worker-1", "wrong") is None
        assert await authenticate(session, "missing", "right") is None


@pytest.mark.asyncio
async def test_state_touch(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        node = await register_node(session, "worker-1", "tok")
        await set_node_state(session, "worker-1", "online", now=T0)
        await session.commit()

    async with db() as session:
        stored = await get_node(session, "worker-1")
        assert stored is not None
        assert stored.state == "online"
        assert stored.last_seen_at == T0
        await touch_node(session, node.id, now=T1)
        await session.commit()

    async with db() as session:
        stored = await get_node(session, "worker-1")
        assert stored is not None
        assert stored.last_seen_at == T1
        assert stored.state == "online"
