"""Fleet Manager device registry (Sprint 29, XEDGE-211; extended Sprint C4,
XEDGE-442/444): one SQLite database (WAL mode, same posture as
xedge.store.sqlite_store's cold tier) holding every registered device's
identity, last-known health snapshot, certificate status, and any config
push queued for it.

Auth model (Sprint C4, XEDGE-442; ADR-013 §3 — supersedes ADR-009's
manager-wide shared `join_token`): an operator provisions a single-use,
time-limited join token bound to one `device_id`. The device redeems it
exactly once, submitting a CSR alongside it, and receives back a
CA-signed certificate plus a per-device `device_token` (opaque,
`secrets.token_urlsafe`) it presents as a bearer token on every subsequent
call. Only hashes are ever persisted — never the join token, never the
device token — the same "don't persist the secret you can instead verify a
hash of" posture as `xedge.api.auth.UserStore`'s bcrypt hashes.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# A device is considered "offline" once this many missed heartbeat
# intervals have elapsed with no contact — matches the same "N x interval"
# staleness heuristic xedge.core.system_tags uses for a driver instance.
OFFLINE_AFTER_MISSED_HEARTBEATS = 3

# Metadata columns `update_metadata` is allowed to touch (Sprint C4,
# XEDGE-444) — a fixed whitelist, not whatever keys a caller happens to
# pass, so a typo in a future call site fails loudly instead of silently
# no-op-ing or (worse) building a SQL column list from unchecked input.
_METADATA_COLUMNS = frozenset(
    {"display_name", "serial_number", "make", "protocol", "hardware_firmware_version"}
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    display_name TEXT,
    token_hash TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    agent_version TEXT,
    heartbeat_interval_seconds REAL NOT NULL DEFAULT 60,
    last_seen_at TEXT,
    driver_count INTEGER,
    uptime_seconds REAL,
    last_config_apply_json TEXT,
    pending_config_json TEXT,
    pending_config_version INTEGER NOT NULL DEFAULT 0,
    cert_serial_number TEXT,
    cert_not_after TEXT,
    serial_number TEXT,
    make TEXT,
    protocol TEXT,
    hardware_firmware_version TEXT
)
"""

# One row per issued join token (Sprint C4, XEDGE-442; ADR-013 §3):
# single-use, bound to a specific device_id, time-limited. Kept in its own
# table rather than a column on `devices` because the device row may not
# exist yet the first time a token is issued (a device enrolls, it doesn't
# pre-register), and because a re-provisioned token for the same device
# needs its own audit trail rather than overwriting the last one.
_CREATE_JOIN_TOKENS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS join_tokens (
    token_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
)
"""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


def _row_to_record(row: sqlite3.Row) -> DeviceRecord:
    return DeviceRecord(
        device_id=row["device_id"],
        display_name=row["display_name"],
        registered_at=datetime.fromisoformat(row["registered_at"]),
        agent_version=row["agent_version"],
        heartbeat_interval_seconds=row["heartbeat_interval_seconds"],
        last_seen_at=(datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None),
        driver_count=row["driver_count"],
        uptime_seconds=row["uptime_seconds"],
        last_config_apply=(
            json.loads(row["last_config_apply_json"]) if row["last_config_apply_json"] else None
        ),
        has_pending_config=row["pending_config_json"] is not None,
        pending_config_version=row["pending_config_version"],
        cert_serial_number=row["cert_serial_number"],
        cert_not_after=(
            datetime.fromisoformat(row["cert_not_after"]) if row["cert_not_after"] else None
        ),
        serial_number=row["serial_number"],
        make=row["make"],
        protocol=row["protocol"],
        hardware_firmware_version=row["hardware_firmware_version"],
    )


class DeviceAlreadyRegisteredError(Exception):
    """Raised when `register` is called for a device_id that already has a
    token and the caller didn't ask to reuse it."""


class DeviceRegistry:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.execute(_CREATE_JOIN_TOKENS_TABLE_SQL)

    def register(
        self,
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
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC).isoformat()
        existing = self._conn.execute(
            "SELECT registered_at FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        registered_at = existing["registered_at"] if existing else now
        self._conn.execute(
            """
            INSERT INTO devices
                (device_id, display_name, token_hash, registered_at, agent_version,
                 heartbeat_interval_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                display_name=excluded.display_name,
                token_hash=excluded.token_hash,
                agent_version=excluded.agent_version,
                heartbeat_interval_seconds=excluded.heartbeat_interval_seconds
            """,
            (
                device_id,
                display_name,
                _hash_token(token),
                registered_at,
                agent_version,
                heartbeat_interval_seconds,
            ),
        )
        return token

    def verify_token(self, device_id: str, token: str) -> bool:
        row = self._conn.execute(
            "SELECT token_hash FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            return False
        return hmac.compare_digest(row["token_hash"], _hash_token(token))

    def heartbeat(
        self,
        device_id: str,
        agent_version: str | None,
        driver_count: int | None,
        uptime_seconds: float | None,
        last_config_apply: dict[str, Any] | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE devices SET
                last_seen_at = ?,
                agent_version = COALESCE(?, agent_version),
                driver_count = ?,
                uptime_seconds = ?,
                last_config_apply_json = COALESCE(?, last_config_apply_json)
            WHERE device_id = ?
            """,
            (
                datetime.now(UTC).isoformat(),
                agent_version,
                driver_count,
                uptime_seconds,
                json.dumps(last_config_apply) if last_config_apply is not None else None,
                device_id,
            ),
        )

    def queue_config(self, device_id: str, config: dict[str, Any]) -> int:
        """Queue `config` for delivery on this device's next heartbeat.
        Returns the new pending_config_version the device is expected to
        report back once applied."""
        row = self._conn.execute(
            "SELECT pending_config_version FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No such device: {device_id!r}")
        new_version: int = row["pending_config_version"] + 1
        self._conn.execute(
            "UPDATE devices SET pending_config_json = ?, pending_config_version = ? "
            "WHERE device_id = ?",
            (json.dumps(config), new_version, device_id),
        )
        return new_version

    def take_pending_config(self, device_id: str) -> tuple[dict[str, Any], int] | None:
        """Fetch (and clear) this device's pending config, if any — called
        once per heartbeat response; clearing immediately means a config is
        delivered at most once even if the device never reports back
        (matches xedge.core.hot_reload's "best-effort, not guaranteed
        exactly-once" posture elsewhere in this codebase)."""
        row = self._conn.execute(
            "SELECT pending_config_json, pending_config_version FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None or row["pending_config_json"] is None:
            return None
        config: dict[str, Any] = json.loads(row["pending_config_json"])
        version = row["pending_config_version"]
        self._conn.execute(
            "UPDATE devices SET pending_config_json = NULL WHERE device_id = ?", (device_id,)
        )
        return config, version

    def create_join_token(self, device_id: str, ttl_seconds: float) -> str:
        """Operator-initiated (Sprint C4, XEDGE-442): provision a one-time
        enrollment credential for a specific device, ahead of that device
        ever having contacted the manager. Supersedes the manager-wide
        shared `join_token` this replaced (ADR-013 §3) — a leaked token
        here only ever admits the one `device_id` it was minted for, and
        only until `consume_join_token` first succeeds or it expires."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        self._conn.execute(
            "INSERT INTO join_tokens (token_hash, device_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), device_id, now.isoformat(), expires_at.isoformat()),
        )
        return token

    def consume_join_token(self, device_id: str, token: str) -> bool:
        """Redeem a join token for `device_id`, atomically marking it
        consumed on success. Returns `False` — never raises — for every
        failure reason (unknown token, wrong device_id, expired, already
        consumed): none of those should be distinguishable to whoever is
        presenting the token, or the response itself becomes an oracle for
        guessing valid device_ids."""
        row = self._conn.execute(
            "SELECT device_id, expires_at, consumed_at FROM join_tokens WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return False
        if not hmac.compare_digest(row["device_id"], device_id):
            return False
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return False
        self._conn.execute(
            "UPDATE join_tokens SET consumed_at = ? WHERE token_hash = ?",
            (datetime.now(UTC).isoformat(), _hash_token(token)),
        )
        return True

    def record_certificate_issued(
        self, device_id: str, serial_number: int, not_after: datetime
    ) -> None:
        """Called after `xedge.security.ca.CertificateAuthority.sign_csr`
        succeeds, for both initial enrollment (XEDGE-442) and rotation
        (XEDGE-443) — the registry only ever stores the serial number and
        expiry it needs to show cert status (XEDGE-447) and decide rotation
        is due, never the certificate or key material itself."""
        self._conn.execute(
            "UPDATE devices SET cert_serial_number = ?, cert_not_after = ? WHERE device_id = ?",
            (str(serial_number), not_after.isoformat(), device_id),
        )

    def update_metadata(self, device_id: str, fields: dict[str, str | None]) -> bool:
        """Update any subset of a device's descriptive metadata (Sprint C4,
        XEDGE-444: display name, serial number, make, protocol, hardware
        firmware version — distinct from `agent_version`, which is the
        xEdge *software* version and is only ever set by the device itself,
        via `heartbeat`). `fields` keys must be a subset of
        `_METADATA_COLUMNS` — the caller (the admin endpoint, via
        Pydantic's `exclude_unset`) decides which fields were actually
        provided in a request; this method doesn't guess at that from a
        bare `None`, since every one of these columns is independently
        nullable. Returns `False` if no such device exists."""
        unknown = set(fields) - _METADATA_COLUMNS
        if unknown:
            raise ValueError(f"Not a metadata field: {sorted(unknown)}")
        if not fields:
            return self.get(device_id) is not None
        set_clause = ", ".join(f"{column} = ?" for column in fields)
        # The interpolated column names above are checked against the
        # hardcoded _METADATA_COLUMNS whitelist just above, never
        # unchecked input; every value is still bound via execute()'s
        # parameterized second argument, never interpolated into the SQL.
        query = f"UPDATE devices SET {set_clause} WHERE device_id = ?"  # nosec B608
        cursor = self._conn.execute(
            query,
            (*fields.values(), device_id),
        )
        return cursor.rowcount > 0

    def get(self, device_id: str) -> DeviceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_devices(self) -> list[DeviceRecord]:
        rows = self._conn.execute("SELECT * FROM devices ORDER BY device_id ASC").fetchall()
        return [_row_to_record(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
