from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from common.protocol import NodeCapabilities
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from server.models import NodeRow, utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def register_node(
    session: AsyncSession,
    name: str,
    token: str,
    capabilities: NodeCapabilities | None = None,
    *,
    display_name: str | None = None,
) -> NodeRow:
    caps = (capabilities if capabilities is not None else NodeCapabilities()).model_dump(mode="json")
    statement = insert(NodeRow).values(
        name=name,
        display_name=display_name or name,
        token_hash=hash_token(token),
        state="offline",
        capabilities=caps,
        workspace_ready=False,
        workspace_free_bytes=None,
    )
    return (
        await session.scalars(
            statement.on_conflict_do_update(
                index_elements=[NodeRow.name],
                set_={
                    "token_hash": statement.excluded.token_hash,
                    "capabilities": caps,
                    "workspace_ready": False,
                    "workspace_free_bytes": None,
                    "updated_at": utcnow(),
                },
            )
            .returning(NodeRow)
            .execution_options(populate_existing=True)
        )
    ).one()


async def authenticate(session: AsyncSession, name: str, token: str) -> NodeRow | None:
    node = await session.scalar(
        select(NodeRow).where(NodeRow.name == name).with_for_update(read=True).execution_options(populate_existing=True)
    )
    if node is None or not hmac.compare_digest(node.token_hash, hash_token(token)):
        return None
    return node


async def set_node_state(session: AsyncSession, name: str, state: str, now: datetime | None = None) -> None:
    await session.execute(update(NodeRow).where(NodeRow.name == name).values(state=state, last_seen_at=_effective(now)))


async def touch_node(
    session: AsyncSession, node_id: int, now: datetime | None = None, usage: dict | None = None
) -> None:
    await session.execute(
        update(NodeRow).where(NodeRow.id == node_id).values(last_seen_at=_effective(now), usage=usage)
    )


async def get_node(session: AsyncSession, name: str) -> NodeRow | None:
    return (await session.execute(select(NodeRow).where(NodeRow.name == name))).scalar_one_or_none()


async def list_nodes(session: AsyncSession) -> list[NodeRow]:
    return list((await session.execute(select(NodeRow).order_by(NodeRow.name))).scalars())


def _effective(now: datetime | None) -> datetime:
    return now if now is not None else utcnow()
