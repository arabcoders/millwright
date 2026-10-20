from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import re
import shutil
from functools import cache
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from common.secrets import resolve_secret
from sqlalchemy import URL, create_engine, make_url, select, text
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator

    from server.fs.library import LibraryFS, ProbeResult
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.core.db import create_engine_and_session, migration_config
from server.models import Base, Job, JobEvent
from server.plugins.host import PluginHost
from server.serialize import iso_utc


def create_test_app(sessionmaker, *, config=None, **kwargs):
    from server.api.app import create_app
    from server.core.config import ServerConfig

    config = config or ServerConfig()
    if config.api_key is None:
        config = config.model_copy(update={"api_key": "test-key"})
    return create_app(sessionmaker, config=config, **kwargs)


def api_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
    transport = kwargs["transport"]
    key = transport.app.state.auth.config.api_key
    headers = {"Authorization": f"Bearer {key}", **kwargs.pop("headers", {})}
    return httpx.AsyncClient(*args, headers=headers, **kwargs)


async def create_user_session(db, username, ttl_days, now=None):
    from server.core.auth import create_session, hash_password
    from server.models import User

    user = await db.scalar(select(User).where(User.username == username))
    if user is None:
        user = User(username=username, password_hash=hash_password("test-password"))
        db.add(user)
        await db.flush()
    return await create_session(db, user.id, ttl_days, now=now)


async def probe_media(fs: LibraryFS, media_id: int) -> ProbeResult:
    from server.fs.library import LibraryProbeError
    from server.metadata_jobs import enqueue_metadata_job
    from server.models import Library, MediaFile
    from server.probing import ProbeService
    from server.queue.store import cancel_pending

    service = ProbeService(fs.sessionmaker, fs.ffprobe_bin, fs.mkvpropedit_bin, workspace_dir=fs.workspace_dir)
    async with fs.sessionmaker() as session:
        row = await session.get(MediaFile, media_id)
        assert row is not None
        library = await session.get(Library, row.library_id)
        assert library is not None
        for job_id in await session.scalars(
            select(Job.id).where(
                Job.media_file_id == media_id, Job.kind == "healthcheck", Job.status.in_(["queued", "hold"])
            )
        ):
            await cancel_pending(session, job_id)
        await enqueue_metadata_job(session, row, library, kind="probe", priority=1)
        await session.commit()
    result = None
    for kind in ("probe", "mkv_stats"):
        claimed = await service._claim(kind)
        if claimed is None:
            break
        job_id, attempt_id, _ = claimed
        try:
            result = await fs.run_job(job_id, attempt_id)
        except LibraryProbeError as exc:
            await service._finish_attempt(job_id, attempt_id, failure=exc)
            raise
        if result.media.probe_id is not None:
            break
    assert result is not None
    return result


async def request_retry(session: AsyncSession, job_id: int) -> int:
    job = await session.scalar(
        select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True)
    )
    assert job is not None
    assert job.status in {"succeeded", "failed", "cancelled"}
    previous = {key: getattr(job, key) for key in ("status", "error", "warning", "stderr_tail", "progress", "node_id")}
    previous.update(
        {key: iso_utc(getattr(job, key)) for key in ("started_at", "finished_at", "lease_expires_at", "hold_until")}
    )
    event = JobEvent(job_id=job_id, type="requested", payload={"operation": "retry", "previous": previous})
    session.add(event)
    job.status = "pending"
    job.error = job.warning = job.stderr_tail = job.progress = None
    job.node_id = job.started_at = job.finished_at = job.lease_expires_at = job.hold_until = None
    await session.commit()
    return event.id


_TMP_BASE = Path(gettempdir()) / "tests-millwright"
_TMP_PER_RUN = _TMP_BASE / uuid4().hex
_TMP_RUNTIME = _TMP_PER_RUN / "tmp"


def path_size(path: Path) -> dict[str, str | int]:
    return {"path": str(path), "size": path.stat().st_size}


def _slugify(name: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in name).strip("-") or "tmp"


def get_test_run_root() -> Path:
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    return _TMP_BASE


def get_test_per_run_root() -> Path:
    _TMP_PER_RUN.mkdir(parents=True, exist_ok=True)
    return _TMP_PER_RUN


def get_test_system_temp_root() -> Path:
    _TMP_RUNTIME.mkdir(parents=True, exist_ok=True)
    return _TMP_RUNTIME


def cleanup_test_run_root() -> None:
    if _TMP_PER_RUN.exists():
        shutil.rmtree(_TMP_PER_RUN)


def _cleanup_at_exit() -> None:
    try:
        cleanup_test_run_root()
    except Exception:
        pass


def graph_flow(*plugins: tuple[str, dict] | str) -> dict:
    names = list(plugins)
    nodes = [
        {"type": "plugin", "id": f"n{index}", "plugin": item[0], "inputs": item[1]}
        if isinstance(item, tuple)
        else {"type": "plugin", "id": f"n{index}", "plugin": item, "inputs": {}}
        for index, item in enumerate(names)
    ]
    ports = {"filter": "match", "inspector": "match", "transcode": "plan", "action": "next"}
    edges = []
    for index, item in enumerate(names[:-1]):
        plugin = item[0] if isinstance(item, tuple) else item
        kind = plugin.split(".", 1)[0]
        edges.append({"src": f"n{index}", "port": ports[kind], "dst": f"n{index + 1}"})
    return {"entry": "n0", "nodes": nodes, "edges": edges}


def make_test_disk_path(*parts: str) -> Path:
    _TMP_PER_RUN.mkdir(parents=True, exist_ok=True)
    error = "test path escapes the per-run root"
    if ".." in parts:
        raise ValueError(error)
    root = _TMP_PER_RUN.resolve()
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError(error)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.asynccontextmanager
async def database_context(
    name: str, *, tables: tuple[str, ...] | None = None, fresh: bool = False
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    with _database_factory(name, tables=tables, fresh=fresh) as (engine, factory):
        try:
            yield factory
        finally:
            await asyncio.wait_for(engine.dispose(), timeout=5)


def get_test_database_url() -> URL:
    configured = os.environ.get("MW_TEST_DATABASE_URL")
    if not configured:
        raise RuntimeError(
            "Database tests require MW_TEST_DATABASE_URL pointing to a pre-created PostgreSQL test database "
            "with CONNECT and CREATE permissions."
        )
    url = make_url(resolve_secret(configured))
    if url.get_backend_name() != "postgresql":
        raise ValueError("MW_TEST_DATABASE_URL must use PostgreSQL.")
    return url.set(drivername="postgresql+psycopg")


def _schema_name(name: str) -> str:
    worker = re.sub("[^a-z0-9_]", "_", os.environ.get("PYTEST_XDIST_WORKER", "main").lower())[:8] or "main"
    label = re.sub("[^a-z0-9_]", "_", name.lower())[:12] or "test"
    return f"mwtest_{worker}_{label}_{uuid4().hex}"


def _schema_url(url: URL, name: str) -> URL:
    # The final setting overrides inherited search paths without exposing public tables.
    # Disposable test rows need committed visibility, not a WAL fsync before each reply.
    # This does not exercise PostgreSQL crash durability or change server-wide settings.
    options = url.query.get("options", "")
    return url.update_query_dict({"options": f"{options} -c synchronous_commit=off -c search_path={name}".strip()})


def _admin_engine() -> Engine:
    return create_engine(
        _schema_url(get_test_database_url(), "pg_catalog"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )


def _drop_schema(name: str) -> None:
    engine = _admin_engine()
    try:
        with engine.connect() as connection:
            connection.execute(DropSchema(name, cascade=True, if_exists=True))
    finally:
        engine.dispose()


@contextlib.contextmanager
def database_url_context(name: str, *, migrated: bool = True, tables: tuple[str, ...] | None = None) -> Generator[URL]:
    schema = _schema_name(name)
    url = _schema_url(get_test_database_url(), schema)
    migrations = _test_migrations() if migrated and tables is None else None
    engine = _admin_engine()
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            _SCHEMAS.add(schema)
    finally:
        engine.dispose()
    try:
        if migrated and tables != ():
            engine = create_engine(url, poolclass=NullPool)
            try:
                with engine.begin() as connection:
                    # The schema is new. Selected tables must include their FK targets.
                    selected = [Base.metadata.tables[name] for name in tables] if tables is not None else None
                    Base.metadata.create_all(connection, tables=selected, checkfirst=False)
                    if migrations is not None:
                        MigrationContext.configure(connection).stamp(migrations, "head")
            finally:
                engine.dispose()
        yield url
    finally:
        _drop_schema(schema)
        _SCHEMAS.remove(schema)


@contextlib.contextmanager
def database_engine(name: str, *, migrated: bool = True) -> Generator[Engine]:
    with database_url_context(name, migrated=migrated) as url:
        engine = create_engine(url, poolclass=NullPool)
        try:
            yield engine
        finally:
            engine.dispose()


@cache
def _test_migrations() -> ScriptDirectory:
    # Full-schema fixtures check migration/model drift once per worker. Table-scoped
    # fixtures don't claim an Alembic revision for their incomplete schemas.
    with database_url_context("migrations", migrated=False) as url:
        engine, _ = create_engine_and_session(url)
        try:
            config = migration_config(engine)
            command.upgrade(config, "head")
            command.check(config)
            return ScriptDirectory.from_config(config)
        finally:
            # Migrations use their own synchronous engine. The async pool is unopened.
            engine.sync_engine.dispose()


_SCHEMAS: set[str] = set()
_DATABASE_CONTEXTS = contextlib.ExitStack()
_AVAILABLE_DATABASES: dict[
    tuple[str, ...] | None, list[tuple[Engine, AsyncEngine, async_sessionmaker[AsyncSession]]]
] = {}


@contextlib.contextmanager
def _database_factory(
    name: str, *, tables: tuple[str, ...] | None, fresh: bool
) -> Generator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    if fresh:
        with database_url_context(name, tables=tables) as url:
            yield create_engine_and_session(url)
        return
    available = _AVAILABLE_DATABASES.setdefault(tables, [])
    if available:
        cleanup, engine, factory = available.pop()
        selected = [table for table in Base.metadata.sorted_tables if tables is None or table.name in tables]
        if selected:
            with cleanup.begin() as connection:
                quote = connection.dialect.identifier_preparer.quote
                if "media_files" in {table.name for table in selected}:
                    # Clear the media/probe cycle before deleting children in FK order.
                    media = Base.metadata.tables["media_files"]
                    connection.execute(media.update().values(probe_id=None).where(media.c.probe_id.is_not(None)))
                for table in reversed(selected):
                    connection.execute(table.delete())
                    column = table.autoincrement_column
                    if column is not None:
                        connection.execute(
                            text("SELECT setval(pg_get_serial_sequence(:table, :column), 1, false)"),
                            {"table": quote(table.name), "column": column.name},
                        )
    else:
        # Nested contexts must not share live state. Keep each schema until worker teardown.
        url = _DATABASE_CONTEXTS.enter_context(database_url_context(name, tables=tables))
        cleanup = create_engine(url, pool_size=1, max_overflow=0)
        _DATABASE_CONTEXTS.callback(cleanup.dispose)
        engine, factory = create_engine_and_session(url)
    # Keep SQLAlchemy's dialect initialization and bounded compiled-SQL cache. The
    # async context disposes connections between event loops, not the engine object.
    yield engine, factory
    # The async context disposes its connections before releasing this schema. A failed
    # test or disposal leaves it out of the reuse pool until final cleanup.
    available.append((cleanup, engine, factory))


def cleanup_test_schemas() -> None:
    _AVAILABLE_DATABASES.clear()
    _DATABASE_CONTEXTS.close()
    for name in list(_SCHEMAS):
        _drop_schema(name)
        _SCHEMAS.remove(name)
    _test_migrations.cache_clear()


@contextlib.asynccontextmanager
async def plugin_host(roots: list[Path]) -> AsyncIterator[PluginHost]:
    host = PluginHost(roots)
    try:
        yield host
    finally:
        await host.aclose()


def make_test_temp_dir(name: str) -> Path:
    path = make_test_disk_path(f"{_slugify(name)}-{uuid4().hex}")
    path.mkdir(parents=True, exist_ok=False)
    return path


def fd_path(path: str | Path, directory: int | None = None) -> Path:
    # Capture a syscall's pinned parent before a fault hook replaces or redirects it.
    return Path(path) if directory is None else Path(os.readlink(f"/proc/self/fd/{directory}")) / path


@contextlib.contextmanager
def temporary_test_dir(name: str) -> Generator[Path]:
    path = make_test_temp_dir(name)
    try:
        yield path
    finally:
        shutil.rmtree(path)


atexit.register(_cleanup_at_exit)
atexit.register(cleanup_test_schemas)
