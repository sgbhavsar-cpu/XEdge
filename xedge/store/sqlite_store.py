"""SD-card / eMMC store-and-forward cold tier (FR-SF-002, FR-SF-003, ADR-003).

One SQLite database (WAL mode) per stream key, holding UnifiedTag samples
that overflowed the RAM ring buffer. WAL + synchronous=NORMAL trades a
small window of possible data loss on power failure (up to the last
un-checkpointed write) for meaningfully better write throughput than
synchronous=FULL — acceptable for telemetry; system-architecture.md §3.4
notes the alarm tier should use synchronous=FULL instead (not yet
differentiated in this MVP — all streams use NORMAL).
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
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_CREATE_TABLE_SQL)
            self._connections[stream_key] = conn
        return conn

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
) -> None:
    """Periodically purge samples older than `retention_duration_seconds`
    from every known stream (FR-SF-003)."""
    while True:
        await asyncio.sleep(interval_seconds)
        cutoff = datetime.now(UTC) - timedelta(seconds=retention_duration_seconds)
        for stream_key in stream_keys():
            purged = cold_store.purge_older_than(stream_key, cutoff)
            if purged:
                logger.info("store.purged", stream_key=stream_key, count=purged)
