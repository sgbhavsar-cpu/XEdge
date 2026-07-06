from __future__ import annotations

from xedge.observability.logging import LogRingBuffer


def _event(event: str, **fields: object) -> dict[str, object]:
    return {"event": event, "level": "info", **fields}


def test_tail_returns_entries_in_append_order_with_seq_assigned() -> None:
    buf = LogRingBuffer()
    buf.append(_event("driver.started", instance_id="d1"))
    buf.append(_event("driver.started", instance_id="d2"))

    entries = buf.tail()
    assert [e["event"] for e in entries] == ["driver.started", "driver.started"]
    assert [e["seq"] for e in entries] == [1, 2]


def test_tail_since_seq_only_returns_newer_entries() -> None:
    buf = LogRingBuffer()
    buf.append(_event("a"))
    buf.append(_event("b"))
    buf.append(_event("c"))

    entries = buf.tail(since_seq=1)
    assert [e["event"] for e in entries] == ["b", "c"]


def test_tail_filters_by_instance_id() -> None:
    buf = LogRingBuffer()
    buf.append(_event("driver.started", instance_id="d1"))
    buf.append(_event("driver.started", instance_id="d2"))
    buf.append(_event("driver.failed", instance_id="d1"))

    entries = buf.tail(instance_id="d1")
    assert [e["event"] for e in entries] == ["driver.started", "driver.failed"]


def test_tail_filters_by_source_event_prefix() -> None:
    buf = LogRingBuffer()
    buf.append(_event("northbound.connected"))
    buf.append(_event("driver.started", instance_id="d1"))
    buf.append(_event("northbound.publish_failed"))

    entries = buf.tail(source="northbound")
    assert [e["event"] for e in entries] == ["northbound.connected", "northbound.publish_failed"]


def test_tail_limit_keeps_most_recent_matches() -> None:
    buf = LogRingBuffer()
    for i in range(5):
        buf.append(_event(f"e{i}"))

    entries = buf.tail(limit=2)
    assert [e["event"] for e in entries] == ["e3", "e4"]


def test_max_size_evicts_oldest_entries() -> None:
    buf = LogRingBuffer(max_size=3)
    for i in range(5):
        buf.append(_event(f"e{i}"))

    entries = buf.tail()
    assert [e["event"] for e in entries] == ["e2", "e3", "e4"]
