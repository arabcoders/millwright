from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.flows.definition import FlowDefinition
from server.flows.store import FlowVersionError, delete_flow, get_flow, list_flows, rename_flow, save_flow
from tests.helpers import database_context, graph_flow


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Competing saves need independent connections and transactions.
    async with database_context("flows", tables=("flows",)) as factory:
        yield factory


def definition(quality: int = 21) -> FlowDefinition:
    return FlowDefinition.model_validate(graph_flow(("transcode.video", {"quality": quality})))


@pytest.mark.asyncio
async def test_upsert_bumps_version(db: async_sessionmaker[AsyncSession]) -> None:
    defn = definition()
    async with db() as session:
        saved = await save_flow(session, "movies", defn)
        await session.commit()
        assert saved.version == 1

    async with db() as session:
        loaded = await get_flow(session, "movies")
        assert loaded is not None
        assert loaded.name == "movies"
        assert loaded.updated_at is not None
        assert FlowDefinition.model_validate_json(loaded.definition) == defn
        assert await get_flow(session, "nope") is None
        unchanged = await save_flow(session, "movies", definition(quality=21), expected_version=1)
        assert unchanged.version == 2
        changed = await save_flow(session, "movies", definition(quality=19), expected_version=2)
        assert changed.version == 3
        await session.commit()

    async with db() as session:
        loaded = await get_flow(session, "movies")
        assert loaded is not None and loaded.version == 3
        assert '"quality":19' in loaded.definition


@pytest.mark.asyncio
async def test_list_and_delete(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await save_flow(session, "movies", definition())
        await save_flow(session, "music", FlowDefinition.model_validate(graph_flow("transcode.video")))
        await session.commit()

    async with db() as session:
        assert [flow.name for flow in await list_flows(session)] == ["movies", "music"]
        assert await delete_flow(session, "music", expected_version=1) is True
        await session.commit()

    async with db() as session:
        assert await get_flow(session, "music") is None
        assert [flow.name for flow in await list_flows(session)] == ["movies"]
        assert await delete_flow(session, "music", expected_version=1) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["save", "rename", "delete"])
async def test_stale_version_rejected(db: async_sessionmaker[AsyncSession], mutation: str) -> None:
    async with db() as session:
        await save_flow(session, "movies", definition())
        await session.commit()
    async with db() as stale:
        old = await get_flow(stale, "movies")
        assert old is not None and old.version == 1
        async with db() as fresh:
            await save_flow(fresh, "movies", definition(19), expected_version=1)
            await fresh.commit()
        with pytest.raises(FlowVersionError):
            if mutation == "save":
                await save_flow(stale, "movies", definition(27), expected_version=1)
            elif mutation == "rename":
                await rename_flow(stale, "movies", "renamed", definition(27), expected_version=1)
            else:
                await delete_flow(stale, "movies", expected_version=1)
        await stale.rollback()
    async with db() as session:
        saved = await get_flow(session, "movies")
        assert saved is not None and saved.version == 2 and saved.definition == definition(19).model_dump_json()
        with pytest.raises(FlowVersionError):
            await save_flow(session, "movies", definition(27))


@pytest.mark.asyncio
async def test_competing_versions(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await save_flow(session, "movies", definition())
        await session.commit()

    async def save(quality: int) -> bool:
        async with db() as session:
            try:
                await save_flow(session, "movies", definition(quality), expected_version=1)
                await session.commit()
            except FlowVersionError:
                await session.rollback()
                return False
            return True

    assert sorted(await asyncio.wait_for(asyncio.gather(save(19), save(27)), 3)) == [False, True]
    async with db() as session:
        saved = await get_flow(session, "movies")
        assert saved is not None and saved.version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["create", "rename"])
async def test_flow_outer_rollback(db: async_sessionmaker[AsyncSession], mutation: str) -> None:
    if mutation == "rename":
        async with db() as session:
            await save_flow(session, "original", definition())
            await session.commit()
    async with db() as session:
        if mutation == "create":
            await save_flow(session, "new", definition())
        else:
            await rename_flow(session, "original", "new", definition(19), expected_version=1)
        await session.rollback()
    async with db() as session:
        assert await get_flow(session, "new") is None
        original = await get_flow(session, "original")
        if mutation == "rename":
            assert original is not None and original.version == 1
            assert original.definition == definition().model_dump_json()
        else:
            assert original is None
