"""Real Postgres for Fleet Manager registry tests (Sprint P1, XEDGE-500/
503) — same "test against a real, independent implementation" posture as
mqtt_broker.py/smtp_server.py, but session-scoped rather than per-test:
a testcontainers Postgres container takes a few seconds to start, against
amqtt's/aiosmtpd's near-instant in-process startup, so one container for
the whole session plus a per-test `TRUNCATE` keeps the suite fast without
sacrificing test isolation. Needs a Docker daemon reachable from wherever
pytest runs (see pyproject.toml's `test` extra for why this doesn't need
any ci.yml changes).

Migrations run exactly once per session, through the same
`xedge.fleet.migrate.run_migrations` entrypoint `xedge.fleet.manager_cli`
uses in production — this is deliberately not `Base.metadata.create_all()`
against a fresh engine, which would validate the ORM models are internally
consistent but say nothing about whether the actual Alembic migration
file (`xedge/fleet/migrations/versions/`) is correct.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from xedge.fleet.db_models import Base, Tenant, create_engine
from xedge.fleet.migrate import run_migrations
from xedge.fleet.registry import DeviceRegistry


@pytest.fixture(scope="session")
def _fleet_postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        database_url = container.get_connection_url()
        run_migrations(database_url)
        yield database_url


@pytest.fixture
async def fleet_registry(_fleet_postgres_url: str) -> AsyncIterator[DeviceRegistry]:
    """A `DeviceRegistry` against the session's real Postgres container,
    with every table truncated first — each test starts from an empty
    database despite sharing the container (and its already-applied
    migrations) with every other test in the session. All three tables
    are truncated together in one statement rather than per-table: the
    `tenant_id` foreign keys on `devices`/`join_tokens` mean Postgres
    would otherwise refuse to truncate `tenants` alone."""
    engine = create_engine(_fleet_postgres_url)
    table_list = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {table_list} CASCADE"))
    registry = DeviceRegistry(engine)
    try:
        yield registry
    finally:
        await registry.close()


@pytest.fixture
async def fleet_default_tenant_id(fleet_registry: DeviceRegistry) -> str:
    """The bootstrapped default tenant's id — mirrors the exact call
    `xedge.fleet.manager_cli.run` makes on real startup."""
    return await fleet_registry.ensure_default_tenant()


@pytest.fixture
async def other_tenant_id(_fleet_postgres_url: str, fleet_registry: DeviceRegistry) -> str:
    """A second tenant, inserted directly — there's no public API for
    tenant creation yet (Sprint P1 auto-bootstraps exactly one; see
    `xedge.fleet.db_models.Tenant`'s docstring). Exists purely so tests
    can prove tenant isolation (XEDGE-503): that this tenant can never
    see or affect a device registered under `fleet_default_tenant_id`.
    Depends on `fleet_registry` (not just `_fleet_postgres_url`) so it
    always runs after that fixture's per-test `TRUNCATE` — otherwise the
    truncate could wipe the tenant row this inserts."""
    engine = create_engine(_fleet_postgres_url)
    sessionmaker = async_sessionmaker(engine)
    async with sessionmaker() as session, session.begin():
        tenant = Tenant(name="other-tenant", created_at=datetime.now(UTC))
        session.add(tenant)
        await session.flush()
        tenant_id = str(tenant.id)
    await engine.dispose()
    return tenant_id
