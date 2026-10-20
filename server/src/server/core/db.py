from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import AsyncConnection
from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

LOG = logging.getLogger("server.db")

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from sqlalchemy.engine import URL

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def normalize_database_url(database_url: str | URL) -> URL:
    try:
        url = make_url(database_url)
    except SQLAlchemyError:
        raise ValueError("Invalid PostgreSQL database URL.") from None
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Database URL must use postgresql or postgresql+psycopg.")
    return url


async def _complete[T](operation: Coroutine[Any, Any, T]) -> T:
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def check_db_revision(database_url: str | URL) -> None:
    url = normalize_database_url(database_url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    except SQLAlchemyError as exc:
        msg = "Cannot read database migration revision. Initialize it with automatic migrations enabled."
        raise RuntimeError(msg) from exc
    finally:
        engine.dispose()

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None or rows != [(head,)]:
        revision = ", ".join(row[0] for row in rows) if rows else "missing"
        msg = (
            f"Database revision is {revision}. Expected current migration {head}. "
            "Run the server with automatic migrations enabled."
        )
        raise RuntimeError(msg)


def create_engine_and_session(database_url: str | URL) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = normalize_database_url(database_url)

    async def connect() -> AsyncConnection[Any]:
        args, options = engine.dialect.create_connect_args(url)
        task = asyncio.create_task(AsyncConnection.connect(*args, **options))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        connection = task.result()
        if cancelled:
            # The pool cannot close a raw connection it has not received yet.
            await _complete(connection.close())
            raise asyncio.CancelledError
        return connection

    engine = create_async_engine(url, async_creator=connect, pool_pre_ping=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _session_settings(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
            # Queue queries cost more to compile than execute with PostgreSQL JIT.
            cursor.execute("SET jit = off")
        finally:
            cursor.close()
        dbapi_connection.commit()

    class CancellationSafeSession(AsyncSession):
        async def commit(self) -> None:
            await _complete(super().commit())

        async def rollback(self) -> None:
            await _complete(super().rollback())

        async def close(self) -> None:
            await _complete(super().close())

    return engine, async_sessionmaker(
        engine,
        class_=CancellationSafeSession,
        expire_on_commit=False,
        info={"library_excluded_roots": ()},
    )


def migration_config(engine: AsyncEngine, *, config_file: Path | None = None) -> Config:
    config = Config(
        file_=str(config_file) if config_file is not None else None,
        attributes={"database_url": engine.url},
    )
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def prepare_db(engine: AsyncEngine) -> None:
    command.upgrade(migration_config(engine), "head")
    LOG.info("Applied database migrations", extra={"operation": "database", "outcome": "ready"})
