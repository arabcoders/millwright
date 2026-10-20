from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from server.models import PluginState, utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def disabled_ids(session: AsyncSession) -> set[str]:
    rows = await session.scalars(select(PluginState.plugin_id).where(PluginState.enabled.is_(False)))
    return set(rows)


async def save_enabled(session: AsyncSession, plugin_id: str, enabled: bool) -> None:
    statement = insert(PluginState).values(plugin_id=plugin_id, enabled=enabled)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[PluginState.plugin_id],
            set_={"enabled": statement.excluded.enabled, "updated_at": utcnow()},
        )
    )
