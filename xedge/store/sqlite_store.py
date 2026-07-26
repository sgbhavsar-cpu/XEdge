"""SD-card / eMMC store-and-forward cold tier (FR-SF-002, FR-SF-003, ADR-003).

One SQLite database (WAL mode) per stream key, holding UnifiedTag samples
that overflowed the RAM ring buffer. WAL + synchronous=NORMAL trades a
small window of possible data loss on power failure (up to the last
un-checkpointed write) for meaningfully better write throughput than
synchronous=FULL — acceptable for telemetry. system-architecture.md §3.4's
alarm-tier synchronous=FULL is now applied (Sprint 31, XEDGE-225), keyed
off the same `ALARM_STREAM_KEY_SUFFIX` stream-naming convention the alarm
engine's independent retention (`purge_loop`, below) also uses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from xedge.core.alarms import ALARM_STREAM_KEY_SUFFIX
from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id TEXT NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    value_json TEXT NOT NULL,
    data_type TEXT NOT NULL,
    quality TEXT NOT NULL,
    source_driver TEXT NOT NULL,
    source_address TEXT NOT NULL,
    engineering_unit TEXT,
    is_alarm INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
)
"""

# Records the stream key this database belongs to (Sprint 0, XEDGE-404).
# The filename can't serve as the key: `_safe_filename` is lossy, so both
# `a/b` and `a_b` — and every alarm stream, whose `::alarm` suffix becomes
# `__alarm` — map onto the same file. `stream_keys()` needs the real key to
# hand back to `peek`/`delete_ids`, so it is stored explicitly.
_CREATE_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STREAM_KEY_META = "stream_key"

_COLUMNS = (
    "tag_id",
    "timestamp_ns",
    "value_json",
    "data_type",
    "quality",
    "source_driver",
    "source_address",
    "engineering_unit",
    "is_alarm",
    "metadata_json",
)


def _safe_filename(stream_key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in stream_key)


def _encode_value(value: Any, data_type: str) -> str:
    if data_type == "BYTES":
        return base64.b64encode(value).decode("ascii")
    return json.dumps(value)


def _decode_value(value_json: str, data_type: str) -> Any:
    if data_type == "BYTES":
        return base64.b64decode(value_json)
    return json.loads(value_json)


def _tag_to_row(tag: UnifiedTag) -> tuple[Any, ...]:
    return (
        tag.tag_id,
        int(tag.timestamp.timestamp() * 1_000_000_000),
        _encode_value(tag.value, tag.data_type),
        tag.data_type,
        tag.quality.value,
        tag.source_driver,
        tag.source_address,
        tag.engineering_unit,
        int(tag.is_alarm),
        json.dumps(tag.metadata),
    )


def _row_to_tag(row: tuple[Any, ...]) -> UnifiedTag:
    (
        _id,
        tag_id,
        timestamp_ns,
        value_json,
        data_type,
        quality,
        source_driver,
        source_address,
        engineering_unit,
        is_alarm,
        metadata_json,
    ) = row
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=UTC),
        value=_decode_value(value_json, data_type),
        data_type=data_type,
        quality=Quality(quality),
        source_driver=source_driver,
        source_address=source_address,
        engineering_unit=engineering_unit,
        is_alarm=bool(is_alarm),
        metadata=json.loads(metadata_json),
    )


class SqliteColdStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._connections: dict[str, sqlite3.Connection] = {}

    def _connection(self, stream_key: str) -> sqlite3.Connection:
        conn = self._connections.get(stream_key)
        if conn is None:
            db_path = self._directory / f"{_safe_filename(stream_key)}.db"
            conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            # Sprint 31 (XEDGE-225): system-architecture.md §3.4 calls for
            # the alarm tier to trade write throughput for durability
            # (FULL fsyncs before returning; NORMAL can lose the last
            # un-checkpointed write on power failure) — differentiated by
            # stream key since that's already the alarm-vs-telemetry
            # boundary (xedge.core.alarms.ALARM_STREAM_KEY_SUFFIX).
            synchronous = "FULL" if stream_key.endswith(ALARM_STREAM_KEY_SUFFIX) else "NORMAL"
            conn.execute(f"PRAGMA synchronous={synchronous}")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_META_TABLE_SQL)
            # Written on every open, not just on create, so a database
            # predating XEDGE-404 becomes self-describing as soon as any
            # code path touches it under its real key.
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                (_STREAM_KEY_META, stream_key),
            )
            self._connections[stream_key] = conn
        return conn

    def stream_keys(self) -> list[str]:
        """Every stream key with a database in this store, including streams
        this process has not touched since starting.

        The northbound dispatcher replays cold-store backlog after a
        reconnect. Before Sprint 0 (XEDGE-404) it enumerated the *ring
        buffer's* stream keys — which are empty immediately after a restart,
        so a backlog persisted across a restart went unreplayed until each
        stream happened to push again, and a backlog belonging to a driver
        since removed from config was never replayed at all.

        Keys come from each database's `meta` table rather than from its
        filename, since `_safe_filename` is lossy (see `_CREATE_META_TABLE_SQL`).
        A database written before XEDGE-404 has no `meta` row and cannot have
        its key recovered safely — guessing would send `peek`/`delete_ids` at
        a different file and silently strand the real backlog — so it is
        skipped with a warning instead.
        """
        keys = set(self._connections)
        for db_path in sorted(self._directory.glob("*.db")):
            key = self._read_stream_key(db_path)
            if key is None:
                logger.warning(
                    "cold_store.stream_key_unrecoverable",
                    path=str(db_path),
                    reason="no meta row; database predates XEDGE-404",
                )
                continue
            keys.add(key)
        return sorted(keys)

    @staticmethod
    def _read_stream_key(db_path: Path) -> str | None:
        """Read a database's recorded stream key without registering a
        long-lived connection for it."""
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_STREAM_KEY_META,)
            ).fetchone()
        except sqlite3.Error:
            # No `meta` table at all — a pre-XEDGE-404 database.
            return None
        finally:
            conn.close()
        return str(row[0]) if row is not None else None

    def append(self, stream_key: str, tag: UnifiedTag) -> None:
        conn = self._connection(stream_key)
        placeholders = ",".join("?" * len(_COLUMNS))
        # The interpolated parts below are our own hardcoded literal column
        # tuple and a repeated placeholder character — never untrusted
        # input. Actual values are always bound via execute()'s
        # parameterized second argument, never interpolated into the SQL.
        query = f"INSERT INTO samples ({','.join(_COLUMNS)}) VALUES ({placeholders})"  # nosec B608
        conn.execute(query, _tag_to_row(tag))

    def count(self, stream_key: str) -> int:
        conn = self._connection(stream_key)
        row = conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        return int(row[0])

    def peek(self, stream_key: str, max_items: int) -> list[tuple[int, UnifiedTag]]:
        """Read (without deleting) up to `max_items` oldest samples, oldest
        first (time_order replay per FR-SF-005 default), as (row_id, tag)
        pairs. Callers that must not lose data on a downstream failure
        (e.g. a northbound publish) should call `delete_ids` only after
        confirming successful delivery — see `drain` for the simpler
        read-and-delete-unconditionally alternative."""
        conn = self._connection(stream_key)
        # _COLUMNS is our own hardcoded literal tuple, not untrusted input;
        # `max_items` is bound via the parameterized `?` below.
        query = f"SELECT id,{','.join(_COLUMNS)} FROM samples ORDER BY id ASC LIMIT ?"  # nosec B608
        rows = conn.execute(query, (max_items,)).fetchall()
        return [(row[0], _row_to_tag(row)) for row in rows]

    def delete_ids(self, stream_key: str, ids: list[int]) -> None:
        if not ids:
            return
        conn = self._connection(stream_key)
        # The f-string only repeats the literal `?` placeholder by
        # len(ids); the actual id values are bound via the parameterized
        # second argument below, never interpolated into the query text.
        query = f"DELETE FROM samples WHERE id IN ({','.join('?' * len(ids))})"  # nosec B608
        conn.execute(query, ids)

    def drain(self, stream_key: str, max_items: int) -> list[UnifiedTag]:
        """Read and unconditionally delete up to `max_items` oldest samples.
        Unsafe for a "publish, then confirm" flow (see `peek`) — data is
        gone as soon as this returns, whether or not the caller successfully
        delivers it anywhere."""
        rows = self.peek(stream_key, max_items)
        if not rows:
            return []
        self.delete_ids(stream_key, [row_id for row_id, _ in rows])
        return [tag for _, tag in rows]

    def purge_older_than(self, stream_key: str, cutoff: datetime) -> int:
        """FR-SF-003: purge samples past their retention window. Returns the
        number of rows purged."""
        conn = self._connection(stream_key)
        cutoff_ns = int(cutoff.timestamp() * 1_000_000_000)
        cursor = conn.execute("DELETE FROM samples WHERE timestamp_ns < ?", (cutoff_ns,))
        return cursor.rowcount

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


async def purge_loop(
    cold_store: SqliteColdStore,
    stream_keys: Callable[[], list[str]],
    retention_duration_seconds: float,
    interval_seconds: float,
    alarm_retention_duration_seconds: float | None = None,
) -> None:
    """Periodically purge samples older than `retention_duration_seconds`
    from every known stream (FR-SF-003) — except an alarm-tier stream
    (`xedge.core.alarms.ALARM_STREAM_KEY_SUFFIX`-suffixed, Sprint 31,
    XEDGE-225/228), which uses `alarm_retention_duration_seconds` instead
    (normally longer; falls back to `retention_duration_seconds` if not
    given, e.g. the alarm engine is disabled and no such stream exists
    anyway)."""
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(UTC)
        normal_cutoff = now - timedelta(seconds=retention_duration_seconds)
        alarm_cutoff = now - timedelta(
            seconds=(
                alarm_retention_duration_seconds
                if alarm_retention_duration_seconds is not None
                else retention_duration_seconds
            )
        )
        for stream_key in stream_keys():
            cutoff = alarm_cutoff if stream_key.endswith(ALARM_STREAM_KEY_SUFFIX) else normal_cutoff
            purged = cold_store.purge_older_than(stream_key, cutoff)
            if purged:
                logger.info("store.purged", stream_key=stream_key, count=purged)
