from __future__ import annotations

import asyncio

import pytest
from pydantic import JsonValue
from sqlalchemy import event, func, select

from server.kv import KeyValueStore
from server.models import KeyValue
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_store_crud() -> None:
    async with database_context("kv-crud", tables=("key_values",)) as factory:
        store = KeyValueStore(factory)
        assert await store.get("activity_paused", default=False) is False
        await store.put("activity_paused", True)
        assert await store.get("activity_paused") is True
        await store.put("activity_paused", False)
        assert await KeyValueStore(factory).get("activity_paused") is False
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(KeyValue)) == 1
        assert await store.delete("activity_paused")
        assert not await store.delete("activity_paused")
        assert await store.get("activity_paused", default=True) is True

        value: JsonValue = {"enabled": True, "counts": [1, 2], "label": "example"}
        await store.put("example", value)
        assert await store.get("example") == value
        await store.put("empty", None)
        assert await store.get("empty", default="missing") is None
        assert await store.get("absent", default="missing") == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [True, False], ids=["same-key", "independent-keys"])
async def test_store_concurrent_writes(monkeypatch: pytest.MonkeyPatch, same_key: bool) -> None:
    async with database_context("kv-concurrent", tables=("key_values",)) as factory:
        store = KeyValueStore(factory)
        inserted, release, second_insert = asyncio.Event(), asyncio.Event(), asyncio.Event()
        commit = factory.class_.commit
        first = None

        async def paused_commit(session) -> None:
            if asyncio.current_task() is first:
                inserted.set()
                await release.wait()
            await commit(session)

        def before_sql(_connection, _cursor, statement, _parameters, _context, _many) -> None:
            if inserted.is_set() and statement.startswith("INSERT INTO key_values"):
                second_insert.set()

        monkeypatch.setattr(factory.class_, "commit", paused_commit)
        engine = factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", before_sql)
        first = asyncio.create_task(store.put("first", {"writer": 1}))
        second = None
        try:
            await asyncio.wait_for(inserted.wait(), 2)
            second = asyncio.create_task(store.put("first" if same_key else "second", {"writer": 2}))
            await asyncio.wait_for(second_insert.wait(), 2)
            if same_key:
                assert not second.done()
            else:
                await asyncio.wait_for(second, 2)
                assert await store.get("second") == {"writer": 2}
                assert await store.get("first") is None
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert await store.get("first") == {"writer": 2 if same_key else 1}
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(KeyValue)) == (1 if same_key else 2)
        finally:
            release.set()
            tasks = [first] + ([second] if second is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            event.remove(engine, "before_cursor_execute", before_sql)
