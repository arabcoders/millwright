from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, make_url, text

import tests.helpers as helpers
from server.core.config import load_config
from tests.helpers import (
    database_context,
    get_test_database_url,
    get_test_per_run_root,
    get_test_system_temp_root,
    make_test_disk_path,
    temporary_test_dir,
)


@pytest.mark.parametrize("parts", [("..", "outside.txt"), ("/outside.txt",)])
def test_rejects_escape(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        make_test_disk_path(*parts)


def test_temp_dir_cleans() -> None:
    with temporary_test_dir("cleanup") as path:
        (path / "nested").mkdir()
        (path / "nested" / "file.txt").write_text("data", encoding="utf-8")

    assert not path.exists()


def test_tmp_path_root(tmp_path: Path) -> None:
    expected_root = get_test_per_run_root() / "pytest"

    assert tmp_path.is_relative_to(expected_root)
    assert get_test_system_temp_root().is_relative_to(get_test_per_run_root())
    assert Path(os.environ["TMPDIR"]).is_relative_to(get_test_per_run_root())


@pytest.mark.asyncio
@pytest.mark.parametrize("tables", [None, ("flows", "libraries")], ids=["full", "selected"])
async def test_database_isolated(tables: tuple[str, ...] | None) -> None:
    async with (
        database_context("helpers", tables=tables, fresh=True) as first,
        database_context("helpers", tables=tables, fresh=True) as second,
    ):
        async with first() as session:
            await session.execute(text("CREATE TABLE marker (value TEXT)"))
            await session.execute(text("INSERT INTO marker VALUES ('first')"))
            from server.models import Library

            session.add(Library(name="first", roots=[]))
            await session.commit()

        async with second() as session:
            assert await session.scalar(text("SELECT to_regclass('marker')").execution_options(read_only=True)) is None
            assert (await session.execute(text("SELECT count(*) FROM libraries"))).scalar_one() == 0
            if tables is not None:
                created = await session.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").execution_options(
                        read_only=True
                    )
                )
                assert set(created) == set(tables)

        async with first() as session:
            assert (await session.execute(text("SELECT name FROM libraries"))).scalar_one() == "first"


@pytest.mark.asyncio
@pytest.mark.parametrize("tables", [("key_values",), None], ids=["selected", "full"])
async def test_reuse_resets_rows(tables: tuple[str, ...] | None) -> None:
    from server.models import KeyValue, Library, MediaFile, ProbeRow, utcnow

    async with database_context("reset", tables=tables) as first:
        async with first() as session:
            schema = await session.scalar(text("SELECT current_schema()"))
            row = KeyValue(key="first", value={"saved": True})
            session.add(row)
            await session.commit()
            assert row.id == 1
            if tables is None:
                library = Library(name="linked", roots=[])
                session.add(library)
                await session.flush()
                media = MediaFile(
                    library_id=library.id,
                    path="/media/movie.mkv",
                    size=1,
                    mtime_ns=1,
                    container="mkv",
                    medium="video",
                    last_scanned_at=utcnow(),
                )
                session.add(media)
                await session.flush()
                probe = ProbeRow(media_file_id=media.id, raw_json="{}")
                session.add(probe)
                await session.flush()
                media.probe_id = probe.id
                await session.commit()
        async with database_context("nested", tables=tables) as second, second() as session:
            assert await session.scalar(text("SELECT current_schema()")) != schema
            session.add(KeyValue(key="nested", value=True))
            await session.commit()
        async with first() as session:
            assert (await session.execute(text("SELECT key FROM key_values"))).scalar_one() == "first"

    async with database_context("reset", tables=tables) as reused, reused() as session:
        assert await session.scalar(text("SELECT current_schema()")) == schema
        assert (await session.execute(text("SELECT count(*) FROM key_values"))).scalar_one() == 0
        if tables is None:
            for table in ("libraries", "media_files", "probes"):
                assert (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
        row = KeyValue(key="first", value=False)
        session.add(row)
        await session.commit()
        assert row.id == 1


def test_database_environment_isolated(tmp_path: Path, database_url: URL, monkeypatch: pytest.MonkeyPatch) -> None:
    url = make_url(os.environ["MW_DATABASE_URL"])
    service = get_test_database_url()
    secret = tmp_path / "database-url"
    secret.write_text(service.render_as_string(hide_password=False) + "\n", encoding="utf-8")
    monkeypatch.setenv("MW_TEST_DATABASE_URL", f"file:{secret}")
    service = get_test_database_url()
    assert url.database == service.database
    assert url.difference_update_query(["options"]) == service.difference_update_query(["options"])
    monkeypatch.setenv("MW_DATABASE_URL", database_url.render_as_string(hide_password=False))
    url = database_url

    config = tmp_path / "config.toml"
    config.write_text('database_url = "postgresql://operator/live"\n', encoding="utf-8")
    assert make_url(load_config(config).database_url) == url

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == url.database
            assert connection.scalar(text("SELECT current_schema()")).startswith("mwtest_")
    finally:
        engine.dispose()


def test_schema_teardown() -> None:
    with helpers.database_url_context("drop-schema", migrated=False) as url:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                name = connection.scalar(text("SELECT current_schema()"))
                assert name in helpers._SCHEMAS
        finally:
            engine.dispose()
    admin = helpers._admin_engine()
    try:
        with admin.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname = :name"), {"name": name}) == 0
            )
            assert name not in helpers._SCHEMAS
    finally:
        admin.dispose()
