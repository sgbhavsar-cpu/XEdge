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
    # Sprint P3, XEDGE-513: operator-initiated revocation, kept distinct
    # from `consumed_at` -- reusing that column for revocation would
    # conflate "the device redeemed it" with "an operator killed it
    # first," losing the distinction for anyone reading the join-token
    # list later. `revoked_by` is plain text (a username), not a foreign
    # key to `fleet_users` -- same "must still read correctly after the
    # account is renamed/deleted" reasoning as `FleetAuditLogEntry.actor`.
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[str | None]


class FleetUser(Base):
    """Named operator accounts (Sprint P2, XEDGE-502) — supersedes the
    single shared `admin_token` (ADR-009), the same kind of transition
    `xedge.api.auth.UserStore` made for the device-local Web UI in
    Sprint 14. `username` is unique per tenant, not globally: two
    tenants each choosing "admin" doesn't collide, since a username only
    ever needs to be unambiguous within the one tenant a session is
    scoped to."""

    __tablename__ = "fleet_users"
    __table_args__ = (
        Index("ix_fleet_users_tenant_id", "tenant_id"),
        Index("ix_fleet_users_tenant_id_username", "tenant_id", "username", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    username: Mapped[str]
    password_hash: Mapped[str]
    role: Mapped[str]
    created_at: Mapped[datetime]


class FleetSession(Base):
    """A logged-in user's bearer session (Sprint P2, XEDGE-502) —
    deliberately *not* `xedge.api.auth.SessionManager`'s stateless,
    HMAC-signed cookie: this token is instead opaque and hash-verified
    against this table, the same shape as this manager's own
    `join_tokens`/`devices.token_hash` (Sprint C4). A compromised admin
    session is higher blast-radius than a compromised device token (it
    carries `user:manage` over a whole tenant), so it gets the mechanism
    that supports real, immediate revocation — deleting the row ends the
    session outright, which a global HMAC secret can't do without
    invalidating every other session at once. `ON DELETE CASCADE`:
    deleting a user (or, transitively, its tenant) must not leave orphaned
    sessions someone could still be presenting."""

    __tablename__ = "fleet_sessions"
    __table_args__ = (Index("ix_fleet_sessions_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleet_users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class FleetAuditLogEntry(Base):
    """Manager-side admin action log (Sprint P2, XEDGE-505) — the
    device-local Web UI has had this since ADR-007 (`xedge.observability.
    audit_log.AuditLog`); the manager didn't because there was no
    per-user identity to attribute an action to before XEDGE-502. Field
    names (`actor`/`event`/`details`) match that module's vocabulary, but
    the storage mechanism deliberately doesn't: the device-side log is a
    hash-chained append-only JSONL *file*, the right choice for a single-
    process gateway with no database of its own, but exactly the
    single-file-concurrent-write problem ADR-013 §5 migrated the rest of
    this manager away from (XEDGE-500) — a real Postgres table gets
    row-level tenant scoping and durable, access-controlled storage for
    free instead. Hash-chain tamper-evidence is not reproduced here; it
    was never XEDGE-505's own ask (a per-manager audit trail), just a
    property of the device-side's specific file-based mechanism.
    `actor` is plain text, not a foreign key to `fleet_users`: an entry
    must read the same after the account that made it is later renamed
    or deleted. Append-only by convention — nothing in this codebase
    ever issues an UPDATE or DELETE against this table."""

    __tablename__ = "fleet_audit_log"
    __table_args__ = (Index("ix_fleet_audit_log_tenant_id_created_at", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    created_at: Mapped[datetime]
    actor: Mapped[str]
    event: Mapped[str]
    details: Mapped[dict[str, Any]]


class DeviceConfigHistory(Base):
    """Every config push queued for a device (Sprint P3, XEDGE-512) —
    `Device.pending_config_json`/`last_config_apply_json` only ever hold
    the *current* queue/latest-report, overwritten on the next push or
    heartbeat; this table is the append-only record a dashboard's
    "config history" view actually reads. `applied_at`/`apply_success`/
    `apply_error` start `NULL` and are filled in by
    `DeviceRegistry.heartbeat` once the device's heartbeat body reports
    back which version it applied and whether that succeeded
    (`xedge.fleet.agent._apply_pending_config`'s `{"version", "success",
    "error"}` shape) -- a push a device never reports back on (e.g. it's
    replaced by a newer push before applying, or the device is
    decommissioned) simply stays `NULL` forever, which is itself
    meaningful information, not a bug."""

    __tablename__ = "device_config_history"
    __table_args__ = (
        Index("ix_device_config_history_tenant_id_device_id", "tenant_id", "device_id"),
        Index(
            "ix_device_config_history_device_id_version",
            "device_id",
            "config_version",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), nullable=False)
    config_version: Mapped[int]
    config_json: Mapped[dict[str, Any]]
    pushed_at: Mapped[datetime]
    pushed_by: Mapped[str]
    applied_at: Mapped[datetime | None]
    apply_success: Mapped[bool | None]
    apply_error: Mapped[str | None]


class DeviceCertificateHistory(Base):
    """Every certificate issued to a device -- initial enrollment and
    every later rotation (Sprint P3, XEDGE-512). `Device.
    cert_serial_number`/`cert_not_after` only ever hold the *current*
    certificate's identity, overwritten in place by
    `DeviceRegistry.record_certificate_issued` on every issuance; this
    table is the append-only history underneath that. `reason`
    distinguishes the one-time enrollment issuance (XEDGE-442) from a
    later proactive rotation (XEDGE-443) -- not a foreign key or enum
    type, a plain constrained string, the same "keep it simple until a
    second consumer needs more" posture already used for
    `DeviceRecord.status`'s three string values."""

    __tablename__ = "device_certificate_history"
    __table_args__ = (
        Index("ix_device_certificate_history_tenant_id_device_id", "tenant_id", "device_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), nullable=False)
    serial_number: Mapped[str]
    not_before: Mapped[datetime]
    not_after: Mapped[datetime]
    issued_at: Mapped[datetime]
    reason: Mapped[str]


def create_engine(database_url: str) -> AsyncEngine:
    """One place for the engine options every caller (`manager_cli`, the
    Postgres test fixture, the SQLite→Postgres import script) needs
    identically, rather than each repeating `create_async_engine(...)`."""
    return create_async_engine(database_url, pool_pre_ping=True)
