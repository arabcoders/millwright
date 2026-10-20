from __future__ import annotations

import asyncio
import logging

import pytest
from alembic import command
from psycopg import AsyncConnection
from sqlalchemy import event, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from server.core.db import (
    check_db_revision,
    create_engine_and_session,
    migration_config,
    prepare_db,
)
from server.models import Job, Library, MediaFile, ProbeRow, SessionRow, User, utcnow
from tests.helpers import database_context, database_engine, database_url_context


def test_baseline_migration() -> None:
    with database_engine("baseline", migrated=False) as sync:
        url = sync.url
        engine, _ = create_engine_and_session(url)
        try:
            config = migration_config(engine)
            command.upgrade(config, "head")
            with sync.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO nodes (name, display_name, token_hash, capabilities, state, max_concurrency, transcode_limit) "
                        "VALUES ('worker', 'Worker', 'unused', '{}', 'offline', 1, 3)"
                    )
                )
                original = connection.execute(
                    text(
                        "INSERT INTO jobs (media_file_id, library_id, kind, priority, status, attempts) "
                        "VALUES (1, 1, 'transcode', 0, 'pending', 0) RETURNING id"
                    )
                ).scalar_one()
                assert connection.execute(
                    text("SELECT transcode_limit, healthcheck_limit, mkv_stats_limit FROM nodes")
                ).one() == (3, 1, 1)
                user_ids = list(
                    connection.scalars(
                        insert(User)
                        .values(
                            [
                                {"username": "first", "password_hash": "first-hash"},
                                {"username": "second", "password_hash": "second-hash"},
                            ]
                        )
                        .returning(User.id)
                    )
                )
                connection.execute(
                    insert(SessionRow).values(user_id=user_ids[1], token_hash="new-session", expires_at=utcnow())
                )
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        insert(SessionRow).values(
                            user_id=max(user_ids) + 1, token_hash="missing-user", expires_at=utcnow()
                        )
                    )
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(insert(User).values(username="first", password_hash="duplicate"))
                metadata = insert(Job).values(media_file_id=1, library_id=1, kind="mkv_stats", status="queued")
                with pytest.raises(IntegrityError, match="uq_jobs_active_media_file"), connection.begin_nested():
                    connection.execute(metadata)
                connection.execute(update(Job).where(Job.id == original).values(waiting_metadata=True))
                metadata_id = connection.execute(metadata.returning(Job.id)).scalar_one()
                with pytest.raises(IntegrityError, match="uq_jobs_active_media_file"), connection.begin_nested():
                    connection.execute(metadata)
                with pytest.raises(IntegrityError, match="uq_jobs_active_media_file"), connection.begin_nested():
                    connection.execute(update(Job).where(Job.id == original).values(status="running"))
                connection.execute(update(Job).where(Job.id == metadata_id).values(status="succeeded"))
                connection.execute(
                    update(Job).where(Job.id == original).values(status="staging", waiting_metadata=False)
                )
            check_db_revision(url)
            command.check(config)
            command.downgrade(config, "base")
            with pytest.raises(RuntimeError):
                check_db_revision(url)
            command.upgrade(config, "head")
            check_db_revision(url)
        finally:
            engine.sync_engine.dispose()


@pytest.mark.asyncio
async def test_connection_session_settings() -> None:
    with database_url_context("session-settings", migrated=False) as url:
        url = url.update_query_dict({"options": f"{url.query['options']} -c timezone=Pacific/Honolulu -c jit=on"})
        engine, _ = create_engine_and_session(url)
        try:
            async with engine.connect() as connection:
                settings = await connection.execute(text("SELECT current_setting('TimeZone'), current_setting('jit')"))
                assert settings.one() == ("UTC", "off")
        finally:
            await asyncio.wait_for(engine.dispose(), 5)


@pytest.mark.asyncio
async def test_stale_connection_replaced() -> None:
    with database_url_context("stale-connection", migrated=False) as url:
        engine, _ = create_engine_and_session(url)
        try:
            async with engine.connect() as connection:
                previous = await connection.scalar(text("SELECT pg_backend_pid()"))
                raw = await connection.get_raw_connection()
                driver = raw.driver_connection
                assert isinstance(driver, AsyncConnection)
            await driver.close()
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT pg_backend_pid()")) != previous
                assert await connection.scalar(text("SELECT 1")) == 1
        finally:
            await asyncio.wait_for(engine.dispose(), 5)


@pytest.mark.asyncio
async def test_cancel_connect_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    opened, release, closing, finish_close = (asyncio.Event() for _ in range(4))
    connections = []
    supplied = []
    connect, close = AsyncConnection.connect, AsyncConnection.close

    async def controlled_connect(_cls, *args, **kwargs):
        connection = await connect(*args, **kwargs)
        connections.append(connection)
        expected_args, expected_kwargs = engine.dialect.create_connect_args(url)
        supplied.append((args, kwargs) == (tuple(expected_args), expected_kwargs))
        opened.set()
        await release.wait()
        return connection

    async def controlled_close(connection):
        closing.set()
        await finish_close.wait()
        await close(connection)

    monkeypatch.setattr(AsyncConnection, "connect", classmethod(controlled_connect))
    monkeypatch.setattr(AsyncConnection, "close", controlled_close)
    with database_url_context("cancel-connect", migrated=False) as url:
        url = url.update_query_dict(
            {
                "application_name": "mwtest-connect-owner",
                "options": f"{url.query['options']} -c timezone=Pacific/Honolulu",
                "sslmode": "disable",
            }
        )
        engine, _ = create_engine_and_session(url)

        async def acquire() -> None:
            async with engine.connect():
                pytest.fail("Cancelled acquisition handed off its raw connection.")

        task = asyncio.create_task(acquire())
        try:
            await asyncio.wait_for(opened.wait(), 5)
            raw = connections[0]
            assert supplied == [True]
            assert raw.info.parameter_status("application_name") == "mwtest-connect-owner"
            assert raw.info.parameter_status("TimeZone") == "Pacific/Honolulu"
            task.cancel()
            task.cancel()
            release.set()
            await asyncio.wait_for(closing.wait(), 5)
            assert not raw.closed and not task.done()
            task.cancel()
            finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert raw.closed
        finally:
            release.set()
            finish_close.set()
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            finally:
                try:
                    await asyncio.wait_for(engine.dispose(), 5)
                finally:
                    for connection in connections:
                        await asyncio.wait_for(close(connection), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,kind", [("commit", "typed"), ("rollback", "text"), ("close", "autoflush")])
async def test_same_row_serializes(operation: str, kind: str) -> None:
    async with database_context("writer-queue", tables=("flows", "libraries")) as factory:
        async with factory() as seed:
            library = Library(name="original", roots=["/media"])
            seed.add(library)
            await seed.commit()
            library_id = library.id
        async with factory() as owner, factory() as waiting:
            connection = await waiting.connection()
            reached = asyncio.Event()

            def reached_sql(_connection, _cursor, statement, _parameters, context, _many) -> None:
                if context.isupdate or statement.startswith("UPDATE "):
                    reached.set()

            event.listen(connection.sync_connection, "before_cursor_execute", reached_sql)
            current = await owner.get(Library, library_id)
            assert current is not None
            current.name = "owner"
            await owner.flush()
            entered = asyncio.Event()

            async def write() -> None:
                entered.set()
                if kind == "typed":
                    await waiting.execute(update(Library).where(Library.id == library_id).values(name="waiting"))
                elif kind == "text":
                    await waiting.execute(text("UPDATE libraries SET name = 'waiting'"))
                else:
                    row = await waiting.get(Library, library_id)
                    assert row is not None
                    row.name = "waiting"
                    await waiting.scalar(select(Library.name))
                await waiting.commit()

            task = asyncio.create_task(write())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(reached.wait(), 2)
                async with factory() as reader:
                    assert await asyncio.wait_for(reader.scalar(select(Library.name)), 2) == "original"
                assert not task.done()
                await getattr(owner, operation)()
                await asyncio.wait_for(task, 2)
                assert reached.is_set()
            finally:
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        async with factory() as reader:
            assert await reader.scalar(select(Library.name)) == "waiting"


@pytest.mark.asyncio
async def test_cancel_waiting_writer() -> None:
    async with database_context("cancel-writer", tables=("flows", "libraries")) as factory:
        async with factory() as seed:
            seed.add(Library(name="original", roots=["/media"]))
            await seed.commit()
        async with factory() as owner, factory() as waiting, factory() as observer:
            await owner.execute(update(Library).values(name="owner"))
            pid = await waiting.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(waiting.execute(update(Library).values(name="cancelled")))
            try:
                # The execute hook fires before PostgreSQL sees the query. Cancel
                # only after this backend is actually waiting for the owner's row.
                async with asyncio.timeout(5):
                    while not await observer.scalar(
                        text("SELECT EXISTS (SELECT FROM pg_locks WHERE pid = :pid AND NOT granted)"),
                        {"pid": pid},
                    ):
                        assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                await waiting.rollback()
                await owner.commit()
                await asyncio.wait_for(waiting.execute(update(Library).values(name="next")), 5)
                await waiting.commit()
            finally:
                try:
                    await asyncio.wait_for(owner.rollback(), 5)
                finally:
                    task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        async with factory() as reader:
            assert await reader.scalar(select(Library.name)) == "next"


@pytest.mark.asyncio
async def test_savepoint_retains_lock() -> None:
    async with database_context("savepoint-writer", tables=("flows", "libraries")) as factory:
        async with factory() as seed:
            seed.add(Library(name="original", roots=["/media"]))
            await seed.commit()
        async with factory() as owner:
            await owner.execute(update(Library).values(name="owner"))
            savepoint = await owner.begin_nested()
            assert await owner.scalar(select(Library.name)) == "owner"
            entered = asyncio.Event()

            async def write() -> None:
                async with factory() as waiting:
                    entered.set()
                    await waiting.execute(update(Library).values(name="waiting"))
                    await waiting.commit()

            task = asyncio.create_task(write())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await savepoint.rollback()
                assert not task.done()
                await owner.execute(update(Library).values(name="owner"))
                await owner.commit()
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        async with factory() as reader:
            assert await reader.scalar(select(Library.name)) == "waiting"


@pytest.mark.asyncio
async def test_cancel_owned_writer() -> None:
    async with database_context("cancel-owned", tables=("flows", "libraries")) as factory:
        entered, release = asyncio.Event(), asyncio.Event()

        async def write() -> None:
            async with factory() as owner:
                owner.add(Library(name="cancelled", roots=["/media"]))
                await owner.flush()
                entered.set()
                await release.wait()

        task = asyncio.create_task(write())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            async with factory() as next_writer:
                next_writer.add(Library(name="next", roots=["/media"]))
                await asyncio.wait_for(next_writer.commit(), 2)
        finally:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        async with factory() as reader:
            assert await reader.scalar(select(Library.name)) == "next"


@pytest.mark.asyncio
async def test_savepoint_outer_rollback() -> None:
    async with database_context("outer-rollback", tables=("flows", "libraries")) as factory:
        async with factory() as session:
            async with session.begin_nested():
                session.add(Library(name="released", roots=["/media"]))
                await session.flush()
            await session.rollback()
        async with factory() as reader:
            assert await reader.scalar(select(Library.id)) is None


@pytest.mark.asyncio
async def test_read_only_snapshot() -> None:
    async with database_context("snapshot-upgrade", tables=("flows", "libraries")) as factory:
        async with factory() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execution_options(read_only=True)
            )
            await session.scalar(select(Library.id))
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                await session.execute(update(Library).values(name="invalid"))
            await session.rollback()
            session.add(Library(name="fresh", roots=["/media"]))
            await session.commit()


@pytest.mark.asyncio
async def test_independent_writers_concurrent() -> None:
    async with database_context("independent-writers", tables=("flows", "libraries")) as factory:
        async with factory() as seed:
            seed.add_all([Library(name="first", roots=[]), Library(name="second", roots=[])])
            await seed.commit()
        async with factory() as owner, factory() as other:
            await owner.execute(update(Library).where(Library.name == "first").values(name="owned"))
            await asyncio.wait_for(
                other.execute(update(Library).where(Library.name == "second").values(name="changed")), 2
            )
            await asyncio.wait_for(other.commit(), 2)
            async with factory() as reader:
                assert set(await reader.scalars(select(Library.name))) == {"first", "changed"}
            await owner.rollback()


@pytest.mark.asyncio
async def test_independent_sources_concurrent() -> None:
    from server.fs.source import lock_job_source

    async with database_context("independent-sources") as factory:
        async with factory() as session:
            library = Library(name="shared", roots=["/media"])
            session.add(library)
            await session.flush()
            media = [
                MediaFile(
                    library_id=library.id,
                    path=f"/media/{index}.mkv",
                    size=4,
                    mtime_ns=1,
                    container="mkv",
                    medium="movie",
                    last_scanned_at=utcnow(),
                )
                for index in range(2)
            ]
            session.add_all(media)
            await session.flush()
            jobs = [Job(media_file_id=row.id, library_id=library.id, status="queued") for row in media]
            session.add_all(jobs)
            await session.commit()
        async with factory() as owner, factory() as other:
            assert await lock_job_source(owner, jobs[0].id) is not None
            changed = await asyncio.wait_for(lock_job_source(other, jobs[1].id), 2)
            assert changed is not None
            changed.status = "claimed"
            await asyncio.wait_for(other.commit(), 2)
            async with factory() as reader:
                assert list(await reader.scalars(select(Job.status).order_by(Job.id))) == ["queued", "claimed"]
            await owner.rollback()


@pytest.mark.asyncio
async def test_cross_engine_row_lock() -> None:
    async with database_context("cross-engine", tables=("flows", "libraries")) as factory:
        engine, other = create_engine_and_session(factory.kw["bind"].url)
        try:
            async with factory() as seed:
                library = Library(name="original", roots=[])
                seed.add(library)
                await seed.commit()
                library_id = library.id
            async with factory() as owner, other() as waiting:
                await owner.scalar(select(Library).where(Library.id == library_id).with_for_update())
                started = asyncio.Event()

                def before_sql(_connection, _cursor, statement, _parameters, _context, _many) -> None:
                    if statement.startswith("UPDATE"):
                        started.set()

                connection = await waiting.connection()
                event.listen(connection.sync_connection, "before_cursor_execute", before_sql)
                task = asyncio.create_task(
                    waiting.execute(update(Library).where(Library.id == library_id).values(name="next"))
                )
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    assert not task.done()
                    await owner.rollback()
                    await asyncio.wait_for(task, 2)
                    await waiting.commit()
                finally:
                    task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            async with factory() as reader:
                assert await reader.scalar(select(Library.name)) == "next"
        finally:
            await asyncio.wait_for(engine.dispose(), 5)


@pytest.mark.asyncio
async def test_failed_flush_releases() -> None:
    async with database_context("failed-writer", tables=("flows", "libraries")) as factory:
        with pytest.raises(IntegrityError):
            async with factory() as failed:
                failed.add(Library(name="duplicate", roots=["/media"]))
                await failed.flush()
                failed.add(Library(name="duplicate", roots=["/media"]))
                await failed.flush()
        async with factory() as next_writer:
            next_writer.add(Library(name="next", roots=["/media"]))
            await asyncio.wait_for(next_writer.commit(), 2)
        async with factory() as reader:
            assert await reader.scalar(select(Library.name)) == "next"


def test_prepares_database() -> None:
    with database_engine("prepare", migrated=False) as connection_engine:
        engine, _ = create_engine_and_session(connection_engine.url)
        root = logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        handler = logging.NullHandler()
        root.handlers = [handler]
        root.setLevel(logging.ERROR)
        try:
            prepare_db(engine)
            with connection_engine.begin() as connection:
                connection.execute(text("INSERT INTO flows (name, version, definition) VALUES ('saved', 1, '{}')"))
            prepare_db(engine)
            check_db_revision(engine.url)
            with connection_engine.connect() as connection:
                assert connection.execute(text("SELECT name FROM flows")).all() == [("saved",)]
                assert [column["name"] for column in inspect(connection).get_columns("probes")] == list(
                    ProbeRow.__table__.columns.keys()
                )
            assert root.handlers == [handler]
            assert root.level == logging.ERROR
        finally:
            root.handlers = old_handlers
            root.setLevel(old_level)
            engine.sync_engine.dispose()


def test_revision_read_only() -> None:
    with database_engine("revision", migrated=False) as engine:
        with pytest.raises(RuntimeError):
            check_db_revision(engine.url)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('unknown-revision')"))
        with pytest.raises(RuntimeError):
            check_db_revision(engine.url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).all() == [("unknown-revision",)]
            assert inspect(connection).get_table_names() == ["alembic_version"]
