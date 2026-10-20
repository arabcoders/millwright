from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from server.models import KeyValue, utcnow

if TYPE_CHECKING:
    from pydantic import JsonValue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class KeyValueStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def get(self, key: str, *, default: JsonValue = None) -> JsonValue:
        async with self.sessionmaker() as session:
            row = await session.scalar(select(KeyValue).where(KeyValue.key == key))
            return default if row is None else row.value

    async def put(self, key: str, value: JsonValue) -> None:
        async with self.sessionmaker() as session:
            statement = insert(KeyValue).values(key=key, value=value)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[KeyValue.key],
                    set_={"value": statement.excluded.value, "updated_at": utcnow()},
                )
            )
            await session.commit()

    async def delete(self, key: str) -> bool:
        async with self.sessionmaker() as session:
            identifier = await session.scalar(delete(KeyValue).where(KeyValue.key == key).returning(KeyValue.id))
            await session.commit()
            return identifier is not None
