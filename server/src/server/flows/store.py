from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from server.flows.definition import FlowDefinition
from server.models import Flow, utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class FlowNotFoundError(ValueError):
    pass


class FlowNameConflictError(ValueError):
    pass


class FlowVersionError(ValueError):
    pass


async def save_flow(
    session: AsyncSession, name: str, definition: FlowDefinition, *, expected_version: int | None = None
) -> Flow:
    payload = definition.model_dump_json()
    flow = await get_flow(session, name)
    if flow is None:
        if expected_version is not None:
            raise FlowVersionError("Flow changed. Reload before saving.")
        flow = await session.scalar(
            insert(Flow)
            .values(name=name, version=1, definition=payload, updated_at=utcnow())
            .on_conflict_do_nothing(index_elements=[Flow.name])
            .returning(Flow)
        )
        if flow is None:
            raise FlowVersionError("Flow already exists. Reload before replacing.")
    else:
        won = await session.scalar(
            update(Flow)
            .where(Flow.id == flow.id, Flow.version == expected_version)
            .values(definition=payload, version=Flow.version + 1)
            .returning(Flow.id)
        )
        if won is None:
            raise FlowVersionError("Flow changed. Reload before saving.")
        await session.refresh(flow)
    await session.flush()
    return flow


async def rename_flow(
    session: AsyncSession, current_name: str, name: str, definition: FlowDefinition, *, expected_version: int
) -> Flow:
    flow = await get_flow(session, current_name)
    if flow is None:
        raise FlowNotFoundError
    if name != current_name and await get_flow(session, name) is not None:
        raise FlowNameConflictError
    payload = definition.model_dump_json()
    try:
        async with session.begin_nested():
            won = await session.scalar(
                update(Flow)
                .where(Flow.id == flow.id, Flow.version == expected_version)
                .values(name=name, definition=payload, version=Flow.version + 1)
                .returning(Flow.id)
            )
    except IntegrityError as exc:
        raise FlowNameConflictError from exc
    if won is None:
        raise FlowVersionError("Flow changed. Reload before saving.")
    await session.refresh(flow)
    await session.flush()
    return flow


async def list_flows(session: AsyncSession) -> list[Flow]:
    return list((await session.execute(select(Flow).order_by(Flow.name))).scalars())


async def get_flow(session: AsyncSession, name: str) -> Flow | None:
    return (await session.execute(select(Flow).where(Flow.name == name))).scalar_one_or_none()


async def delete_flow(session: AsyncSession, name: str, *, expected_version: int) -> bool:
    flow = await get_flow(session, name)
    if flow is None:
        return False
    won = await session.scalar(
        delete(Flow).where(Flow.id == flow.id, Flow.version == expected_version).returning(Flow.id)
    )
    if won is None:
        raise FlowVersionError("Flow changed. Reload before deleting.")
    await session.flush()
    return True
