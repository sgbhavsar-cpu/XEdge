from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.store.sqlite_store import SqliteColdStore


def _tag(
    value: object,
    data_type: str,
    tag_id: str = "t1",
    timestamp: datetime | None = None,
    quality: Quality = Quality.GOOD,
    is_alarm: bool = False,
) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=timestamp or datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        data_type=data_type,
        quality=quality,
        source_driver="d1",
        source_address="0",
        engineering_unit="°C" if data_type == "FLOAT64" else None,
        is_alarm=is_alarm,
        metadata={"k": "v", "n": None},
    )


def test_append_and_drain_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    original = _tag(85.3, "FLOAT64")
    store.append("stream1", original)

    drained = store.drain("stream1", max_items=10)
    assert len(drained) == 1
    tag = drained[0]
    assert tag.tag_id == original.tag_id
    assert tag.value == original.value
    assert tag.data_type == original.data_type
    assert tag.quality == original.quality
    assert tag.source_driver == original.source_driver
    assert tag.source_address == original.source_address
    assert tag.engineering_unit == original.engineering_unit
    assert tag.is_alarm == original.is_alarm
    assert tag.metadata == original.metadata
    # nanosecond precision may be truncated to microseconds by datetime, but
    # should agree to within a microsecond
    assert abs((tag.timestamp - original.timestamp).total_seconds()) < 1e-6


def test_roundtrip_preserves_types_across_all_tagvalue_variants(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    cases = [
        (True, "BOOL"),
        (42, "INT64"),
        (3.14, "FLOAT64"),
        ("hello", "STRING"),
        (b"\x00\x01\xff", "BYTES"),
    ]
    for value, data_type in cases:
        store.append("stream1", _tag(value, data_type))

    drained = store.drain("stream1", max_items=10)
    assert [(t.value, t.data_type) for t in drained] == cases


def test_drain_returns_oldest_first_fifo_order(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    for i in range(5):
        store.append("stream1", _tag(i, "INT64"))
    drained = store.drain("stream1", max_items=10)
    assert [t.value for t in drained] == [0, 1, 2, 3, 4]


def test_drain_with_max_items_leaves_remainder(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    for i in range(5):
        store.append("stream1", _tag(i, "INT64"))
    first_batch = store.drain("stream1", max_items=2)
    assert [t.value for t in first_batch] == [0, 1]
    assert store.count("stream1") == 3
    remainder = store.drain("stream1", max_items=10)
    assert [t.value for t in remainder] == [2, 3, 4]


def test_separate_streams_are_independent(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    store.append("stream_a", _tag(1, "INT64"))
    store.append("stream_b", _tag(2, "INT64"))
    assert [t.value for t in store.drain("stream_a", 10)] == [1]
    assert [t.value for t in store.drain("stream_b", 10)] == [2]


def test_count_reflects_pending_samples(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    assert store.count("stream1") == 0
    store.append("stream1", _tag(1, "INT64"))
    store.append("stream1", _tag(2, "INT64"))
    assert store.count("stream1") == 2


def test_purge_older_than_removes_only_expired_samples(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    now = datetime.now(UTC)
    old_tag = _tag(1, "INT64", timestamp=now - timedelta(days=10))
    recent_tag = _tag(2, "INT64", timestamp=now)
    store.append("stream1", old_tag)
    store.append("stream1", recent_tag)

    purged_count = store.purge_older_than("stream1", now - timedelta(days=1))
    assert purged_count == 1
    remaining = store.drain("stream1", 10)
    assert [t.value for t in remaining] == [2]


def test_data_persists_across_store_instances(tmp_path: Path) -> None:
    store1 = SqliteColdStore(tmp_path)
    store1.append("stream1", _tag(1, "INT64"))
    store1.close()

    store2 = SqliteColdStore(tmp_path)
    assert store2.count("stream1") == 1
    store2.close()


def test_database_file_uses_wal_mode(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    store.append("stream1", _tag(1, "INT64"))
    db_path = tmp_path / "stream1.db"
    assert db_path.is_file()

    conn = sqlite3.connect(str(db_path))
    try:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_stream_key_with_special_characters_is_sanitized_for_filename(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    store.append("modbus_tcp_01/../etc", _tag(1, "INT64"))
    # must not have escaped the store directory
    assert list(tmp_path.iterdir())
    for entry in tmp_path.iterdir():
        assert entry.is_relative_to(tmp_path)


def test_peek_does_not_delete(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    store.append("stream1", _tag(1, "INT64"))
    peeked = store.peek("stream1", max_items=10)
    assert [t.value for _id, t in peeked] == [1]
    assert store.count("stream1") == 1  # still there


def test_delete_ids_removes_only_specified_rows(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    for i in range(3):
        store.append("stream1", _tag(i, "INT64"))
    peeked = store.peek("stream1", max_items=10)
    first_id = peeked[0][0]
    store.delete_ids("stream1", [first_id])
    remaining = store.drain("stream1", max_items=10)
    assert [t.value for t in remaining] == [1, 2]


def test_delete_ids_empty_list_is_noop(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    store.append("stream1", _tag(1, "INT64"))
    store.delete_ids("stream1", [])
    assert store.count("stream1") == 1


async def test_purge_loop_purges_expired_samples_periodically(tmp_path: Path) -> None:
    import asyncio

    from xedge.store.sqlite_store import purge_loop

    store = SqliteColdStore(tmp_path)
    old_tag = _tag(1, "INT64", timestamp=datetime.now(UTC) - timedelta(days=10))
    store.append("stream1", old_tag)

    task = asyncio.create_task(
        purge_loop(
            store, lambda: ["stream1"], retention_duration_seconds=86400, interval_seconds=0.01
        )
    )
    try:
        for _ in range(200):
            if store.count("stream1") == 0:
                break
            await asyncio.sleep(0.01)
        assert store.count("stream1") == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_stream_keys_lists_streams_written_by_this_store(tmp_path: Path) -> None:
    store = SqliteColdStore(tmp_path)
    store.append("modbus_01", _tag(1, "INT64"))
    store.append("opcua_01", _tag(2, "INT64"))

    assert store.stream_keys() == ["modbus_01", "opcua_01"]


def test_stream_keys_finds_backlog_written_before_this_process_started(tmp_path: Path) -> None:
    """XEDGE-404 regression: the dispatcher replays from stream_keys() after a
    reconnect. A backlog persisted by a previous process must be discoverable
    by a freshly-constructed store, or it is never replayed."""
    previous_process = SqliteColdStore(tmp_path)
    previous_process.append("modbus_01", _tag(1, "INT64"))
    previous_process.close()

    after_restart = SqliteColdStore(tmp_path)
    assert after_restart.stream_keys() == ["modbus_01"]
    assert after_restart.count("modbus_01") == 1


def test_stream_keys_roundtrips_keys_that_the_filename_cannot_represent(tmp_path: Path) -> None:
    """`_safe_filename` maps every non-alphanumeric character to `_`, so
    `modbus_01::alarm` and `modbus_01__alarm` share a filename. The key has to
    come from inside the database, not from its name — otherwise peek/delete_ids
    would be handed a key that opens a different file."""
    previous_process = SqliteColdStore(tmp_path)
    previous_process.append("modbus_01::alarm", _tag(1, "INT64", is_alarm=True))
    previous_process.close()

    keys = SqliteColdStore(tmp_path).stream_keys()
    assert keys == ["modbus_01::alarm"]


def test_stream_keys_skips_databases_predating_the_meta_table(tmp_path: Path) -> None:
    """A database with no `meta` row cannot have its key recovered safely, so
    it is skipped rather than guessed at — a wrong guess would silently open a
    different file and strand the real backlog."""
    legacy = tmp_path / "legacy_stream.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.commit()
    conn.close()

    store = SqliteColdStore(tmp_path)
    assert store.stream_keys() == []


def test_touching_a_legacy_database_by_its_real_key_makes_it_discoverable(tmp_path: Path) -> None:
    """The meta row is written on every open, not only on create, so a
    pre-XEDGE-404 database becomes self-describing as soon as any code path
    uses it under its real key."""
    legacy = tmp_path / "legacy_stream.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.commit()
    conn.close()

    store = SqliteColdStore(tmp_path)
    assert store.stream_keys() == []

    store.count("legacy_stream")
    store.close()

    assert SqliteColdStore(tmp_path).stream_keys() == ["legacy_stream"]
