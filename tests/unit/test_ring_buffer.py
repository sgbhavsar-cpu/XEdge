from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.store.ring_buffer import RingBuffer, RingBufferBackpressureError, RingBufferManager


def _tag(value: int, tag_id: str = "t1") -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,
        data_type="INT64",
        quality=Quality.GOOD,
        source_driver="d1",
        source_address="0",
    )


def test_push_and_drain_preserves_fifo_order() -> None:
    buf = RingBuffer(max_depth=10)
    buf.push(_tag(1))
    buf.push(_tag(2))
    buf.push(_tag(3))
    drained = buf.drain()
    assert [t.value for t in drained] == [1, 2, 3]
    assert len(buf) == 0


def test_drain_with_max_items_leaves_remainder() -> None:
    buf = RingBuffer(max_depth=10)
    for i in range(5):
        buf.push(_tag(i))
    first_batch = buf.drain(max_items=2)
    assert [t.value for t in first_batch] == [0, 1]
    assert len(buf) == 3


def test_eviction_drops_oldest_when_full() -> None:
    buf = RingBuffer(max_depth=3)
    for i in range(5):
        buf.push(_tag(i))
    assert len(buf) == 3
    drained = buf.drain()
    assert [t.value for t in drained] == [2, 3, 4]
    assert buf.metrics().evicted_count == 2


def test_alarm_stream_raises_backpressure_instead_of_evicting() -> None:
    buf = RingBuffer(max_depth=2, is_alarm_stream=True)
    buf.push(_tag(1))
    buf.push(_tag(2))
    with pytest.raises(RingBufferBackpressureError):
        buf.push(_tag(3))
    # no data lost
    assert [t.value for t in buf.drain()] == [1, 2]


def test_metrics_report_depth_and_max_depth() -> None:
    buf = RingBuffer(max_depth=100)
    buf.push(_tag(1))
    metrics = buf.metrics()
    assert metrics.depth == 1
    assert metrics.max_depth == 100
    assert metrics.evicted_count == 0


def test_manager_creates_separate_buffers_per_stream_key() -> None:
    manager = RingBufferManager(max_depth=10)
    manager.push("driver_a", _tag(1))
    manager.push("driver_b", _tag(2))
    assert [t.value for t in manager.drain("driver_a")] == [1]
    assert [t.value for t in manager.drain("driver_b")] == [2]


def test_manager_drain_unknown_stream_returns_empty() -> None:
    manager = RingBufferManager()
    assert manager.drain("nonexistent") == []


def test_manager_drain_all_covers_every_stream() -> None:
    manager = RingBufferManager(max_depth=10)
    manager.push("a", _tag(1))
    manager.push("b", _tag(2))
    drained = manager.drain_all()
    assert {t.value for t in drained} == {1, 2}
    assert manager.stream_keys() == ["a", "b"]


def test_manager_metrics_for_unknown_stream_is_none() -> None:
    manager = RingBufferManager()
    assert manager.metrics("nonexistent") is None
