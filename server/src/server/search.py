from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import false, func, or_, select

from server.event_rows import FILE_TRANSCODE_STATUS
from server.models import MediaFile

if TYPE_CHECKING:
    from sqlalchemy import Result, Select
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


def _search_clause(query: str) -> ColumnElement[bool]:
    term = query.strip()
    # IDs are PostgreSQL integers. Longer numeric filenames remain text searches.
    if match := re.fullmatch(r"file_id:\s*([0-9]{1,10})", term, flags=re.IGNORECASE):
        return MediaFile.id == int(match[1])
    phrase = re.sub(r"[\W_]+", " ", query).strip()
    if not phrase:
        return false()
    clause = MediaFile.search_vector.bool_op("@@")(func.phraseto_tsquery("simple", phrase))
    if re.fullmatch(r"[0-9]{1,10}", term):
        clause = or_(MediaFile.id == int(term), clause)
    return clause


async def search_files(
    session: AsyncSession,
    query: str,
    limit: int = 50,
    offset: int = 0,
    *,
    library_id: int | None = None,
    transcode_status: str | None = None,
    health_status: str | None = None,
    sort_by: str = "id",
    sort_dir: str = "desc",
) -> list[MediaFile]:
    stmt = select(MediaFile).where(_search_clause(query))
    rows = await _filtered(session, stmt, library_id, transcode_status, health_status, limit, offset, sort_by, sort_dir)
    return list(rows.scalars())


async def count_files(
    session: AsyncSession,
    query: str | None = None,
    *,
    library_id: int | None = None,
    transcode_status: str | None = None,
    health_status: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(MediaFile)
    if query is not None:
        stmt = stmt.where(_search_clause(query))
    stmt = _where_filters(stmt, library_id, transcode_status, health_status)
    return (await session.execute(stmt)).scalar_one()


async def list_files(
    session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    *,
    library_id: int | None = None,
    transcode_status: str | None = None,
    health_status: str | None = None,
    sort_by: str = "id",
    sort_dir: str = "desc",
) -> list[MediaFile]:
    stmt = select(MediaFile)
    rows = await _filtered(session, stmt, library_id, transcode_status, health_status, limit, offset, sort_by, sort_dir)
    return list(rows.scalars())


async def _filtered(
    session: AsyncSession,
    stmt: Select,
    library_id: int | None,
    transcode_status: str | None,
    health_status: str | None,
    limit: int,
    offset: int,
    sort_by: str = "id",
    sort_dir: str = "desc",
) -> Result:
    stmt = _where_filters(stmt, library_id, transcode_status, health_status)
    column = {
        "id": MediaFile.id,
        "path": MediaFile.path,
        "size": MediaFile.size,
        "duration_s": MediaFile.duration_s,
        "bit_rate": MediaFile.bit_rate,
        "transcode_status": FILE_TRANSCODE_STATUS,
        "health_status": MediaFile.health_status,
        "created_at": MediaFile.created_at,
        "updated_at": MediaFile.updated_at,
    }[sort_by]
    ordering = column.asc() if sort_dir == "asc" else column.desc()
    tie = MediaFile.id.asc() if sort_dir == "asc" else MediaFile.id.desc()
    nulls = [column.is_(None)] if sort_by in {"duration_s", "bit_rate"} else []
    stmt = stmt.order_by(ordering) if sort_by == "id" else stmt.order_by(*nulls, ordering, tie)
    stmt = stmt.limit(limit).offset(offset)
    return await session.execute(stmt)


def _where_filters(
    stmt: Select,
    library_id: int | None,
    transcode_status: str | None,
    health_status: str | None,
) -> Select:
    if library_id is not None:
        stmt = stmt.where(MediaFile.library_id == library_id)
    transcode_statuses = [part.strip() for part in (transcode_status or "").split(",") if part.strip()]
    if transcode_statuses:
        stmt = stmt.where(FILE_TRANSCODE_STATUS.in_(transcode_statuses))
    health_statuses = [part.strip() for part in (health_status or "").split(",") if part.strip()]
    if health_statuses:
        stmt = stmt.where(MediaFile.health_status.in_(health_statuses))
    return stmt
