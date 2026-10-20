from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from alembic import command

from server import cli
from server.core.db import MIGRATIONS, check_db_revision, create_engine_and_session, migration_config
from server.models import Library
from tests.helpers import database_context, database_url_context


def test_cli_generates_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with database_url_context("revision") as url:
        address = url.render_as_string(hide_password=False)
        check_db_revision(url)
        copied = tmp_path / "migrations"
        shutil.copytree(MIGRATIONS, copied, ignore=shutil.ignore_patterns("__pycache__"))
        config_factory = cli.migration_config

        def copied_migration_config(engine, *, config_file=None):
            config = config_factory(engine, config_file=config_file)
            config.set_main_option("script_location", str(copied))
            return config

        monkeypatch.setattr(cli, "migration_config", copied_migration_config)
        before = set((copied / "versions").glob("*.py"))
        assert cli.main(["db", "--database-url", address, "revision", "-m", "audit empty diff"]) == 0
        generated = set((copied / "versions").glob("*.py")) - before
        assert len(generated) == 1
        engine, _ = create_engine_and_session(url)
        try:
            command.check(migration_config(engine))
        finally:
            engine.sync_engine.dispose()


@pytest.mark.asyncio
async def test_fresh_library_identity(tmp_path: Path) -> None:
    async with database_context("fresh") as factory:
        async with factory() as session:
            original = Library(name="Original", roots=[str(tmp_path / "original")])
            session.add(original)
            await session.commit()
            original_id = original.id
            await session.delete(original)
            await session.commit()
            replacement = Library(name="Unrelated", roots=[str(tmp_path / "new")])
            session.add(replacement)
            await session.commit()
            assert replacement.id > original_id
        check_db_revision(factory.kw["bind"].url)
