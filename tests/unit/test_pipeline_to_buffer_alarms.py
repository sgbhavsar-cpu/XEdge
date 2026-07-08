"""Unit tests for `xedge.core.main._pipeline_to_buffer`'s alarm-tier
routing (Sprint 31, XEDGE-224/225): a tag with a configured alarm rule
must buffer under a distinct `{source_driver}::alarm` stream key with
backpressure (`is_alarm_stream=True`), regardless of whether it's
currently alarming — see xedge.core.alarms module docstring.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from xedge.core.alarms import ALARM_STREAM_KEY_SUFFIX, AlarmEngine, AlarmRule
from xedge.core.main import _pipeline_to_buffer
from xedge.core.pipeline import DeadbandFilter
from xedge.drivers.base import Quality, TagUpdate
from xedge.store.ring_buffer import RingBufferManager


async def _run_one_update(update: TagUpdate, alarm_engine: AlarmEngine | None) -> RingBufferManager:
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    ring_buffers = RingBufferManager(max_depth=10)
    await queue.put(update)
    task = asyncio.create_task(
        _pipeline_to_buffer(
            queue,
            ring_buffers,
            deadband_filter=DeadbandFilter(),
            alarm_engine=alarm_engine,
        )
    )
    try:
        for _ in range(100):
            if ring_buffers.stream_keys():
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return ring_buffers


def _update(tag_id: str, value: object) -> TagUpdate:
    return TagUpdate(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        quality=Quality.GOOD,
        source_driver=tag_id.split("/")[0],
        source_address="0",
    )


async def test_alarm_ruled_tag_buffers_under_a_distinct_alarm_stream_key() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    ring_buffers = await _run_one_update(_update("d1/temp", 50.0), engine)

    assert ring_buffers.stream_keys() == [f"d1{ALARM_STREAM_KEY_SUFFIX}"]
    drained = ring_buffers.drain(f"d1{ALARM_STREAM_KEY_SUFFIX}")
    assert len(drained) == 1
    assert drained[0].tag_id == "d1/temp"
    assert drained[0].is_alarm is False  # 50.0 doesn't trip high=90


async def test_alarm_ruled_tag_is_alarm_tier_even_while_not_currently_alarming() -> None:
    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    ring_buffers = await _run_one_update(_update("d1/temp", 10.0), engine)
    # Still routed to the alarm-tier stream key, not the normal one, per
    # "alarm-tier is static (has a rule), not dynamic (currently tripped)".
    assert ring_buffers.stream_keys() == [f"d1{ALARM_STREAM_KEY_SUFFIX}"]


async def test_non_alarm_ruled_tag_buffers_under_the_normal_stream_key() -> None:
    engine = AlarmEngine({"d1/other_tag": AlarmRule(tag_id="d1/other_tag", high=90)})
    ring_buffers = await _run_one_update(_update("d1/temp", 50.0), engine)

    assert ring_buffers.stream_keys() == ["d1"]


async def test_no_alarm_engine_configured_uses_normal_stream_key() -> None:
    ring_buffers = await _run_one_update(_update("d1/temp", 50.0), None)
    assert ring_buffers.stream_keys() == ["d1"]


async def test_alarm_ruled_tag_buffer_retries_instead_of_dropping_when_full(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """A full alarm-tier buffer must retry (per RingBuffer's own docstring:
    "caller must retry, not drop") rather than crash the whole pipeline
    consumer task or silently evict — proven here by shrinking the retry
    interval and confirming a second sample, blocked behind a full
    1-deep buffer, lands only after the first is drained."""
    import xedge.core.main as main_module

    monkeypatch.setattr(main_module, "_BACKPRESSURE_RETRY_INTERVAL_SECONDS", 0.05)

    engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    ring_buffers = RingBufferManager(max_depth=1)
    stream_key = f"d1{ALARM_STREAM_KEY_SUFFIX}"
    task = asyncio.create_task(
        _pipeline_to_buffer(
            queue, ring_buffers, deadband_filter=DeadbandFilter(), alarm_engine=engine
        )
    )
    try:
        await queue.put(_update("d1/temp", 1.0))
        for _ in range(100):
            if ring_buffers.metrics(stream_key) is not None:
                break
            await asyncio.sleep(0.01)
        assert ring_buffers.metrics(stream_key).depth == 1  # type: ignore[union-attr]

        await queue.put(_update("d1/temp", 2.0))
        await asyncio.sleep(0.2)  # second push retries against the still-full buffer
        assert ring_buffers.metrics(stream_key).depth == 1  # type: ignore[union-attr]
        assert ring_buffers.metrics(stream_key).evicted_count == 0  # type: ignore[union-attr]

        ring_buffers.drain(stream_key)  # frees a slot
        for _ in range(100):
            if ring_buffers.metrics(stream_key).depth == 1:  # type: ignore[union-attr]
                break
            await asyncio.sleep(0.02)
        assert ring_buffers.drain(stream_key)[0].value == 2.0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
