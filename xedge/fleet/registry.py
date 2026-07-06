"""Fleet Manager device registry (Sprint 29, XEDGE-211): one SQLite database
(WAL mode, same posture as xedge.store.sqlite_store's cold tier) holding
every registered device's identity, last-known health snapshot, and any
config push queued for it.

Auth model (ADR-009 interim posture, deferring XEDGE-214's mTLS): a device
registers once with a shared `join_token` configured on the manager, and
receives back a per-device `device_token` (opaque, `secrets.token_urlsafe`)
it must present as a bearer token on every subsequent call. Only the
token's SHA-256 hash is stored, never the token itself — the same
"don't persist the secret you can instead verify a hash of" posture as
`xedge.api.auth.UserStore`'s bcrypt hashes.
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
    pending_config_version INTEGER NOT NULL DEFAULT 0
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
        last_seen_at=(
            datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None
        ),
        driver_count=row["driver_count"],
        uptime_seconds=row["uptime_seconds"],
        last_config_apply=(
            json.loads(row["last_config_apply_json"]) if row["last_config_apply_json"] else None
        ),
        has_pending_config=row["pending_config_json"] is not None,
        pending_config_version=row["pending_config_version"],
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
