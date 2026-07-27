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
    cert_not_after TEXT
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

    @property
    def status(self) -> str:
        """`unknown` (never heartbeated), `online`, or `offline` — computed
        from `last_seen_at` rather than stored, since "now" changes on
        every read (same reasoning as xedge.core.supervisor's live-computed
        `last_read_age_seconds`)."""
        if self.last_seen_at is None:
            return "unknown"
        threshold = timedelta(
            seconds=self.heartbeat_interval_seconds * OFFLINE_AFTER_MISSED_HEARTBEATS
        )
        if datetime.now(UTC) - self.last_seen_at > threshold:
            return "offline"
        return "online"


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
