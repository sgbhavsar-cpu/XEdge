"""SQLAlchemy ORM models for the Fleet Manager's Postgres-backed registry
(Sprint P1, XEDGE-500; ADR-013 §5 — row-level tenant scoping, not
schema-per-tenant). These classes are the single source of truth for the
schema: Alembic's `xedge.fleet.migrations.env` autogenerates against
`Base.metadata`, and nothing else creates or alters tables. This
supersedes `xedge.fleet.registry`'s previous SQLite `CREATE TABLE IF NOT
EXISTS` strings, which owned the schema directly since there was no
migration tooling before now (XEDGE-501).

Every tenant-scoped table carries a `tenant_id` column and an index on
it — the mechanism ADR-013 §5 requires ("every table gets a tenant_id
column enforced at the query layer"). `devices.device_id` stays the
primary key (globally unique) rather than becoming part of a composite
`(tenant_id, device_id)` key: the device-facing wire protocol
(`xedge.fleet.manager_device_app`, and `xedge.fleet.agent` on the other
end of it) identifies a device by `device_id` alone and predates
multi-tenancy, and changing that would mean every already-enrolled
device in the field needs to start sending a `tenant_id` it was never
issued. Global uniqueness is enforced instead (a device_id can't move
between tenants — see `xedge.fleet.registry.DeviceRegistry.register`'s
takeover guard, XEDGE-504) — the same trade-off usernames/emails make in
many multi-tenant systems.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        str: Text,
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
    }


class Tenant(Base):
    """A tenant is bootstrapped, not self-service-provisioned, in Sprint
    P1 — see `DeviceRegistry.ensure_default_tenant`. Real tenant
    management (creating additional tenants, associating named user
    accounts with one) is XEDGE-502 scope: a tenant with no addressable
    user account is not yet an operator-facing feature, just the schema
    boundary XEDGE-502 will provision against."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime]


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (Index("ix_devices_tenant_id", "tenant_id"),)

    device_id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    display_name: Mapped[str | None]
    token_hash: Mapped[str]
    registered_at: Mapped[datetime]
    agent_version: Mapped[str | None]
    heartbeat_interval_seconds: Mapped[float] = mapped_column(default=60)
    last_seen_at: Mapped[datetime | None]
    driver_count: Mapped[int | None]
    uptime_seconds: Mapped[float | None]
    last_config_apply_json: Mapped[dict[str, Any] | None]
    pending_config_json: Mapped[dict[str, Any] | None]
    pending_config_version: Mapped[int] = mapped_column(default=0)
    cert_serial_number: Mapped[str | None]
    cert_not_after: Mapped[datetime | None]
    serial_number: Mapped[str | None]
    make: Mapped[str | None]
    protocol: Mapped[str | None]
    hardware_firmware_version: Mapped[str | None]


class JoinToken(Base):
    __tablename__ = "join_tokens"
    __table_args__ = (Index("ix_join_tokens_tenant_id", "tenant_id"),)

    token_hash: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    device_id: Mapped[str]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]


def create_engine(database_url: str) -> AsyncEngine:
    """One place for the engine options every caller (`manager_cli`, the
    Postgres test fixture, the SQLite→Postgres import script) needs
    identically, rather than each repeating `create_async_engine(...)`."""
    return create_async_engine(database_url, pool_pre_ping=True)
