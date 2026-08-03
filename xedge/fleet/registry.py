"""Fleet Manager device registry (Sprint 29, XEDGE-211; extended Sprint
C4, XEDGE-442/444; migrated to Postgres Sprint P1, XEDGE-500): every
registered device's identity, last-known health snapshot, certificate
status, and any config push queued for it — now with every table scoped
by `tenant_id` (ADR-013 §5, row-level tenant scoping, enforced here at
the query layer rather than by the database).

This supersedes the SQLite implementation (ADR-009 §4's single-file
reasoning no longer holds once concurrent operator writes, certificate
issuance, and dashboard queries land against the same store — see ADR-013
§5). Schema is owned by Alembic (`xedge.fleet.migrations`,
`xedge.fleet.db_models`), not this module — there is no more
`CREATE TABLE IF NOT EXISTS` here.

Auth model (Sprint C4, XEDGE-442; ADR-013 §3 — supersedes ADR-009's
manager-wide shared `join_token`): an operator provisions a single-use,
time-limited join token bound to one `device_id`. The device redeems it
exactly once, submitting a CSR alongside it, and receives back a
CA-signed certificate plus a per-device `device_token` (opaque,
`secrets.token_urlsafe`) it presents as a bearer token on every
subsequent call. Only hashes are ever persisted — never the join token,
never the device token — the same "don't persist the secret you can
instead verify a hash of" posture as `xedge.api.auth.UserStore`'s bcrypt
hashes.

Tenant scoping, method by method: every method an *operator* calls
(`create_join_token`, `get`, `list_devices`, `update_metadata`,
`queue_config`) takes `tenant_id` and verifies it, because that's where
"whose fleet is this?" first becomes a question. Every method a *device*
calls on its own behalf (`verify_token`, `heartbeat`,
`take_pending_config`, `record_certificate_issued`) stays keyed on
`device_id` alone: `device_id` is globally unique (see
`xedge.fleet.db_models`'s module docstring for why it isn't part of a
composite key with `tenant_id`), and a device proves its own identity
via `device_token` + mTLS, never by asserting a tenant. `register` and
`create_join_token` are the seam between the two worlds — both take
`tenant_id` and guard against a second tenant claiming a `device_id`
that already belongs to a first one (XEDGE-504's "a device cannot enroll
across tenants," landing here because the guard is inseparable from the
schema/upsert logic it protects).
"""

from __future__ import annotations

import enum
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from xedge.fleet._device_auth import hash_token
from xedge.fleet.db_models import Device, JoinToken, Tenant

# A device is considered "offline" once this many missed heartbeat
# intervals have elapsed with no contact — matches the same "N x interval"
# staleness heuristic xedge.core.system_tags uses for a driver instance.
OFFLINE_AFTER_MISSED_HEARTBEATS = 3

# The tenant Sprint P1 auto-bootstraps on first startup (see
# `DeviceRegistry.ensure_default_tenant`) — real tenant provisioning is
# XEDGE-502 scope; see `xedge.fleet.db_models.Tenant`'s docstring.
DEFAULT_TENANT_NAME = "default"

# Metadata columns `update_metadata` is allowed to touch (Sprint C4,
# XEDGE-444) — a fixed whitelist, not whatever keys a caller happens to
# pass, so a typo in a future call site fails loudly instead of silently
# no-op-ing or (worse) building a SQL column list from unchecked input.
_METADATA_COLUMNS = frozenset(
    {"display_name", "serial_number", "make", "protocol", "hardware_firmware_version"}
)


class GatewayConnectionState(enum.Enum):
    """The CRD's own four-state vocabulary (§4.9), via the shared
    connectivity concept ADR-011 Part 3 establishes
    (`xedge.core.connectivity`) — not a literal reuse of
    `ConnectivityTracker`'s consecutive-failure/recovery hysteresis,
    which is built for a *poll* model (a driver actively attempts reads
    and feeds each outcome in). A gateway's heartbeat is *pull*: the
    manager never attempts anything, it only ever notices time passing
    since the last contact. So this adapts the same four-state shape to a
    time-since-last-heartbeat computation instead — see
    `DeviceRecord.connection_state`.

    INACTIVE: never enrolled/heartbeated at all.
    ACTIVE: heartbeating within its own configured interval.
    CONNECTED: heartbeat is late but not yet past the same
        `OFFLINE_AFTER_MISSED_HEARTBEATS` threshold `status` already uses —
        "still connected," just not perfectly punctual.
    DISCONNECTED: past that threshold.

    The CRD names these states but doesn't further define the boundary
    between "Connected" and "Active," or "Disconnected" and "Inactive" —
    this mapping is this project's own interpretation, not a customer
    confirmation; revisit if the customer's actual expectation differs.
    """

    INACTIVE = "inactive"
    ACTIVE = "active"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


def classify_connection_state(
    last_contact_at: datetime | None,
    heartbeat_interval_seconds: float,
    *,
    now: datetime | None = None,
) -> GatewayConnectionState:
    """The pure time-since-last-contact computation `DeviceRecord.
    connection_state` uses — factored out (Sprint C4, XEDGE-447) so the
    fleet *agent* can classify its own connectivity the same way for the
    device-local Web UI's fleet status card
    (`xedge.fleet.agent.FleetAgentStatus.connection_state`), without every
    caller needing a `DeviceRecord`/registry row to do it. `last_contact_at`
    is deliberately a bare parameter name, not `last_seen_at` — the
    manager's "last time I saw this device" and a device's own "last time
    my heartbeat succeeded" are the same computation over different data,
    not the same field."""
    now = now if now is not None else datetime.now(UTC)
    if last_contact_at is None:
        return GatewayConnectionState.INACTIVE
    age = now - last_contact_at
    if age <= timedelta(seconds=heartbeat_interval_seconds):
        return GatewayConnectionState.ACTIVE
    if age <= timedelta(seconds=heartbeat_interval_seconds * OFFLINE_AFTER_MISSED_HEARTBEATS):
        return GatewayConnectionState.CONNECTED
    return GatewayConnectionState.DISCONNECTED


@dataclass(slots=True)
class DeviceRecord:
    device_id: str
    display_name: str | None
    registered_at: datetime
    agent_version: str | None
    heartbeat_interval_seconds: float
    last_seen_at: datetime | None
    driver_count: int | None
    uptime_seconds: float | None
    last_config_apply: dict[str, Any] | None
    has_pending_config: bool
    pending_config_version: int
    cert_serial_number: str | None
    cert_not_after: datetime | None
    serial_number: str | None
    make: str | None
    protocol: str | None
    hardware_firmware_version: str | None

    @property
    def status(self) -> str:
        """`unknown` (never heartbeated), `online`, or `offline` — computed
        from `last_seen_at` rather than stored, since "now" changes on
        every read (same reasoning as xedge.core.supervisor's live-computed
        `last_read_age_seconds`). Kept alongside `connection_state`
        (XEDGE-445) rather than replaced by it — existing consumers of this
        3-value field are unaffected by the CRD's 4-value ask."""
        if self.last_seen_at is None:
            return "unknown"
        threshold = timedelta(
            seconds=self.heartbeat_interval_seconds * OFFLINE_AFTER_MISSED_HEARTBEATS
        )
        if datetime.now(UTC) - self.last_seen_at > threshold:
            return "offline"
        return "online"

    @property
    def connection_state(self) -> GatewayConnectionState:
        """XEDGE-445 (ADR-013 §2, "four-state gateway connection model via
        the XEDGE-420 adapter"; see `GatewayConnectionState`'s docstring
        for what "adapter" means here). Live-computed, same reasoning as
        `status` above."""
        return classify_connection_state(self.last_seen_at, self.heartbeat_interval_seconds)


def _row_to_record(device: Device) -> DeviceRecord:
    return DeviceRecord(
        device_id=device.device_id,
        display_name=device.display_name,
        registered_at=device.registered_at,
        agent_version=device.agent_version,
        heartbeat_interval_seconds=device.heartbeat_interval_seconds,
        last_seen_at=device.last_seen_at,
        driver_count=device.driver_count,
        uptime_seconds=device.uptime_seconds,
        last_config_apply=device.last_config_apply_json,
        has_pending_config=device.pending_config_json is not None,
        pending_config_version=device.pending_config_version,
        cert_serial_number=device.cert_serial_number,
        cert_not_after=device.cert_not_after,
        serial_number=device.serial_number,
        make=device.make,
        protocol=device.protocol,
        hardware_firmware_version=device.hardware_firmware_version,
    )


class DeviceAlreadyRegisteredError(Exception):
    """Raised when `register` is called for a device_id that already has a
    token and the caller didn't ask to reuse it."""


class DeviceTenantConflictError(Exception):
    """Raised by `register`/`create_join_token` when `device_id` already
    belongs to a *different* tenant than the one requesting it (XEDGE-504:
    "a device cannot enroll across tenants") — a `device_id` is globally
    unique (see `xedge.fleet.db_models` module docstring), so this is the
    guard against one tenant's admin silently taking over another
    tenant's device by reusing its device_id."""


class DeviceRegistry:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def ensure_default_tenant(self) -> str:
        """Idempotent get-or-create for the one tenant Sprint P1
        auto-bootstraps (see `xedge.fleet.db_models.Tenant`'s docstring
        for why this project isn't yet self-service multi-tenant).
        Called once by `xedge.fleet.manager_cli` on every startup —
        matches the "safe to call unconditionally" posture already
        established by `load_or_create_ca`/`_load_or_create_token`."""
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(select(Tenant).where(Tenant.name == DEFAULT_TENANT_NAME))
            tenant = result.scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(name=DEFAULT_TENANT_NAME, created_at=datetime.now(UTC))
                session.add(tenant)
                await session.flush()
            return str(tenant.id)

    async def register(
        self,
        tenant_id: str,
        device_id: str,
        display_name: str | None,
        agent_version: str | None,
        heartbeat_interval_seconds: float,
    ) -> str:
        """Register (or re-register) a device, returning its device_token.

        Idempotent by design: a device that lost its persisted token (e.g.
        a fresh /data volume) can re-register with the same join_token and
        device_id and gets a *new* token back — the old one stops working.
        This is a deliberate simplicity trade-off for v1 (no separate
        "rotate token" endpoint yet)."""
        tenant_uuid = uuid.UUID(tenant_id)
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            existing = await session.get(Device, device_id)
            if existing is not None and existing.tenant_id != tenant_uuid:
                raise DeviceTenantConflictError(
                    f"Device {device_id!r} is already registered under a different tenant"
                )
            registered_at = existing.registered_at if existing is not None else now
            await session.execute(
                pg_insert(Device)
                .values(
                    device_id=device_id,
                    tenant_id=tenant_uuid,
                    display_name=display_name,
                    token_hash=token_hash,
                    registered_at=registered_at,
                    agent_version=agent_version,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                )
                .on_conflict_do_update(
                    index_elements=[Device.device_id],
                    set_={
                        "display_name": display_name,
                        "token_hash": token_hash,
                        "agent_version": agent_version,
                        "heartbeat_interval_seconds": heartbeat_interval_seconds,
                    },
                )
            )
        return token

    async def verify_token(self, device_id: str, token: str) -> bool:
        async with self._sessionmaker() as session:
            device = await session.get(Device, device_id)
            if device is None:
                return False
            return hmac.compare_digest(device.token_hash, hash_token(token))

    async def heartbeat(
        self,
        device_id: str,
        agent_version: str | None,
        driver_count: int | None,
        uptime_seconds: float | None,
        last_config_apply: dict[str, Any] | None,
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None:
                return
            device.last_seen_at = datetime.now(UTC)
            if agent_version is not None:
                device.agent_version = agent_version
            device.driver_count = driver_count
            device.uptime_seconds = uptime_seconds
            if last_config_apply is not None:
                device.last_config_apply_json = last_config_apply

    async def queue_config(self, tenant_id: str, device_id: str, config: dict[str, Any]) -> int:
        """Queue `config` for delivery on this device's next heartbeat.
        Returns the new pending_config_version the device is expected to
        report back once applied."""
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None or device.tenant_id != tenant_uuid:
                raise KeyError(f"No such device: {device_id!r}")
            device.pending_config_json = config
            device.pending_config_version += 1
            return device.pending_config_version

    async def take_pending_config(self, device_id: str) -> tuple[dict[str, Any], int] | None:
        """Fetch (and clear) this device's pending config, if any — called
        once per heartbeat response; clearing immediately means a config is
        delivered at most once even if the device never reports back
        (matches xedge.core.hot_reload's "best-effort, not guaranteed
        exactly-once" posture elsewhere in this codebase)."""
        async with self._sessionmaker() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None or device.pending_config_json is None:
                return None
            config = device.pending_config_json
            version = device.pending_config_version
            device.pending_config_json = None
            return config, version

    async def create_join_token(self, tenant_id: str, device_id: str, ttl_seconds: float) -> str:
        """Operator-initiated (Sprint C4, XEDGE-442): provision a one-time
        enrollment credential for a specific device, ahead of that device
        ever having contacted the manager. Supersedes the manager-wide
        shared `join_token` this replaced (ADR-013 §3) — a leaked token
        here only ever admits the one `device_id` it was minted for, and
        only until `consume_join_token` first succeeds or it expires."""
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session, session.begin():
            existing_device = await session.get(Device, device_id)
            if existing_device is not None and existing_device.tenant_id != tenant_uuid:
                raise DeviceTenantConflictError(
                    f"Device {device_id!r} is already registered under a different tenant"
                )
            token = secrets.token_urlsafe(32)
            now = datetime.now(UTC)
            session.add(
                JoinToken(
                    token_hash=hash_token(token),
                    tenant_id=tenant_uuid,
                    device_id=device_id,
                    created_at=now,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
            )
        return token

    async def consume_join_token(self, device_id: str, token: str) -> str | None:
        """Redeem a join token for `device_id`, atomically marking it
        consumed on success. Returns `None` — never raises — for every
        failure reason (unknown token, wrong device_id, expired, already
        consumed): none of those should be distinguishable to whoever is
        presenting the token, or the response itself becomes an oracle for
        guessing valid device_ids. On success, returns the `tenant_id` the
        token was provisioned under (`xedge.fleet.manager_app.enroll`
        registers the new device into that tenant — the device never
        asserts its own tenant)."""
        async with self._sessionmaker() as session, session.begin():
            join_token = await session.get(JoinToken, hash_token(token))
            if join_token is None or join_token.consumed_at is not None:
                return None
            if not hmac.compare_digest(join_token.device_id, device_id):
                return None
            if join_token.expires_at < datetime.now(UTC):
                return None
            join_token.consumed_at = datetime.now(UTC)
            return str(join_token.tenant_id)

    async def record_certificate_issued(
        self, device_id: str, serial_number: int, not_after: datetime
    ) -> None:
        """Called after `xedge.security.ca.CertificateAuthority.sign_csr`
        succeeds, for both initial enrollment (XEDGE-442) and rotation
        (XEDGE-443) — the registry only ever stores the serial number and
        expiry it needs to show cert status (XEDGE-447) and decide rotation
        is due, never the certificate or key material itself."""
        async with self._sessionmaker() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None:
                return
            device.cert_serial_number = str(serial_number)
            device.cert_not_after = not_after

    async def update_metadata(
        self, tenant_id: str, device_id: str, fields: dict[str, str | None]
    ) -> bool:
        """Update any subset of a device's descriptive metadata (Sprint C4,
        XEDGE-444: display name, serial number, make, protocol, hardware
        firmware version — distinct from `agent_version`, which is the
        xEdge *software* version and is only ever set by the device itself,
        via `heartbeat`). `fields` keys must be a subset of
        `_METADATA_COLUMNS` — the caller (the admin endpoint, via
        Pydantic's `exclude_unset`) decides which fields were actually
        provided in a request; this method doesn't guess at that from a
        bare `None`, since every one of these columns is independently
        nullable. Returns `False` if no such device exists in this
        tenant."""
        unknown = set(fields) - _METADATA_COLUMNS
        if unknown:
            raise ValueError(f"Not a metadata field: {sorted(unknown)}")
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None or device.tenant_id != tenant_uuid:
                return False
            for field, value in fields.items():
                setattr(device, field, value)
            return True

    async def get(self, tenant_id: str, device_id: str) -> DeviceRecord | None:
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session:
            device = await session.get(Device, device_id)
            if device is None or device.tenant_id != tenant_uuid:
                return None
            return _row_to_record(device)

    async def list_devices(self, tenant_id: str) -> list[DeviceRecord]:
        tenant_uuid = uuid.UUID(tenant_id)
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(Device).where(Device.tenant_id == tenant_uuid).order_by(Device.device_id)
            )
            return [_row_to_record(d) for d in result.scalars().all()]

    async def close(self) -> None:
        await self._engine.dispose()
