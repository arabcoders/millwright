from __future__ import annotations

import os
from typing import TYPE_CHECKING

from alembic import context
from alembic.operations import ops
from sqlalchemy import ForeignKeyConstraint, create_engine

from server.core.db import normalize_database_url
from server.models import Base

if TYPE_CHECKING:
    from collections.abc import Iterable

    from alembic.runtime.migration import MigrationContext
    from sqlalchemy.engine import URL

target_metadata = Base.metadata


def _database_url() -> URL:
    runtime_url = context.config.attributes.get("database_url")
    if runtime_url:
        return normalize_database_url(runtime_url)
    configured = os.environ.get("MW_DATABASE_URL") or context.config.get_main_option("sqlalchemy.url")
    if not configured:
        msg = "No database URL. Set MW_DATABASE_URL or sqlalchemy.url in alembic.ini."
        raise RuntimeError(msg)
    return normalize_database_url(configured)


def _defer_foreign_keys(
    _context: MigrationContext, _revision: str | Iterable[str | None], directives: list[ops.MigrationScript]
) -> None:
    # Alembic omits use_alter constraints when creating individual tables.
    # Emit them after all tables exist so the media/probe cycle stays enforced.
    for script in directives:
        for upgrade, downgrade in zip(script.upgrade_ops_list, script.downgrade_ops_list, strict=True):
            deferred = []
            for operation in upgrade.ops:
                if not isinstance(operation, ops.CreateTableOp):
                    continue
                for constraint in list(operation.columns):
                    if isinstance(constraint, ForeignKeyConstraint) and constraint.use_alter:
                        operation.columns = [column for column in operation.columns if column is not constraint]
                        deferred.append(ops.CreateForeignKeyOp.from_constraint(constraint))
            upgrade.ops.extend(deferred)
            downgrade.ops[:0] = [operation.reverse() for operation in reversed(deferred)]


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                process_revision_directives=_defer_foreign_keys,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
