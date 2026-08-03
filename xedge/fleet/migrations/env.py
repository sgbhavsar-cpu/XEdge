"""Alembic environment (Sprint P1, XEDGE-501). Adapted from Alembic's own
`--template async` scaffold — async because the rest of this codebase's
Postgres access is asyncpg-only (no sync driver dependency; see
`xedge.fleet.db_models`'s module docstring on why a second driver was
avoided). Not used directly in production: `xedge.fleet.migrate.
run_migrations` builds an equivalent `Config` in Python and never reads
`alembic.ini` — this file only matters when Alembic loads it, which
happens either way (`alembic.command.upgrade` invokes this module
regardless of how the `Config` was constructed). The repo-root
`alembic.ini` exists so a developer can also run `alembic revision
--autogenerate` directly from a checkout.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from xedge.fleet.db_models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
