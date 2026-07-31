"""One-shot SQLite -> Postgres import for the Fleet Manager device
registry (Sprint P1, XEDGE-501) -- not part of the shipped xedge
package. For any Delivery-1 pilot deployment that ran the pre-P1
SQLite-backed manager and needs its existing devices/join tokens
carried into the new Postgres-backed registry, preserving the exact
token hashes so already-enrolled devices don't need to re-enroll.

Imports everything into the single tenant Sprint P1 auto-bootstraps
(xedge.fleet.registry.DeviceRegistry.ensure_default_tenant) -- a
pre-P1 deployment was single-tenant by definition (multi-tenancy didn't
exist yet), so "all of it becomes the default tenant's fleet" is the
only sensible mapping. Point --database-url at a database that has
already been through `alembic upgrade head` (starting xedge-fleet-manager
once against it does this automatically) before running this.

Usage:
    python tools/migrate_sqlite_to_postgres.py \\
        --sqlite-path /data/fleet-manager/devices.db \\
        --database-url postgresql+asyncpg://xedge:xedge@localhost:5432/xedge_fleet

Refuses to run against a target whose default tenant already has any
devices -- this is a one-shot cutover import, not a sync: stop the old
SQLite-backed manager, run this once against a fresh Postgres database,
then start the new manager.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from xedge.fleet.db_models import Device, JoinToken, create_engine
from xedge.fleet.registry import DeviceRegistry


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path", required=True, help="Path to the pre-P1 devices.db file"
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="Async SQLAlchemy URL for the target Postgres database "
        "(must already be migrated to head)",
    )
    return parser.parse_args(argv)


def _read_source(sqlite_path: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    try:
        device_rows = source.execute("SELECT * FROM devices").fetchall()
        join_token_rows = source.execute("SELECT * FROM join_tokens").fetchall()
    finally:
        source.close()
    return device_rows, join_token_rows


async def _import(sqlite_path: str, database_url: str) -> None:
    device_rows, join_token_rows = _read_source(sqlite_path)

    engine = create_engine(database_url)
    registry = DeviceRegistry(engine)
    tenant_id = await registry.ensure_default_tenant()
    tenant_uuid = uuid.UUID(tenant_id)

    existing = await registry.list_devices(tenant_id)
    if existing:
        raise SystemExit(
            f"Refusing to import: the default tenant already has {len(existing)} device(s). "
            "This is a one-shot import for a fresh Postgres database, not a sync."
        )

    sessionmaker = async_sessionmaker(engine)
    async with sessionmaker() as session, session.begin():
        for row in device_rows:
            session.add(
                Device(
                    device_id=row["device_id"],
                    tenant_id=tenant_uuid,
                    display_name=row["display_name"],
                    token_hash=row["token_hash"],
                    registered_at=datetime.fromisoformat(row["registered_at"]),
                    agent_version=row["agent_version"],
                    heartbeat_interval_seconds=row["heartbeat_interval_seconds"],
                    last_seen_at=(
                        datetime.fromisoformat(row["last_seen_at"])
                        if row["last_seen_at"]
                        else None
                    ),
                    driver_count=row["driver_count"],
                    uptime_seconds=row["uptime_seconds"],
                    last_config_apply_json=(
                        json.loads(row["last_config_apply_json"])
                        if row["last_config_apply_json"]
                        else None
                    ),
                    pending_config_json=(
                        json.loads(row["pending_config_json"])
                        if row["pending_config_json"]
                        else None
                    ),
                    pending_config_version=row["pending_config_version"],
                    cert_serial_number=row["cert_serial_number"],
                    cert_not_after=(
                        datetime.fromisoformat(row["cert_not_after"])
                        if row["cert_not_after"]
                        else None
                    ),
                    serial_number=row["serial_number"],
                    make=row["make"],
                    protocol=row["protocol"],
                    hardware_firmware_version=row["hardware_firmware_version"],
                )
            )
        for row in join_token_rows:
            session.add(
                JoinToken(
                    token_hash=row["token_hash"],
                    tenant_id=tenant_uuid,
                    device_id=row["device_id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    expires_at=datetime.fromisoformat(row["expires_at"]),
                    consumed_at=(
                        datetime.fromisoformat(row["consumed_at"])
                        if row["consumed_at"]
                        else None
                    ),
                )
            )

    print(
        f"Imported {len(device_rows)} device(s) and {len(join_token_rows)} join token(s) "
        f"into tenant {tenant_id!r}."
    )
    await registry.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_import(args.sqlite_path, args.database_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
