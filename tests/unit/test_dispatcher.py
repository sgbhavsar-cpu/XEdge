from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from tests.fixtures.fake_connector import FakeConnector
from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.store.ring_buffer import RingBufferManager


def _tag(value: int) -> UnifiedTag:
    return UnifiedTag(
        tag_id="t1",
        timestamp=datetime.now(UTC),
        value=value,
        data_type="INT64",
        quality=Quality.GOOD,
        source_driver="d1",
        source_address="0",
    )


async def test_dispatcher_connects_and_publishes_buffered_batch() -> None:
    connector = FakeConnector()
    buffers = RingBufferManager()
    buffers.push("d1", _tag(1))
    dispatcher = NorthboundDispatcher(connector, buffers, publish_interval_seconds=0.01)

    task = asyncio.create_task(dispatcher.run())
    try:
        await asyncio.wait_for(_wait_until(lambda: connector.published_batches), timeout=2.0)
        assert connector.connected
        assert connector.published_batches[0][0].value == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_dispatcher_retries_connect_with_backoff() -> None:
    connector = FakeConnector(fail_connect_count=2)
    buffers = RingBufferManager()
    dispatcher = NorthboundDispatcher(connector, buffers, publish_interval_seconds=0.01)

    import xedge.northbound.dispatcher as dispatcher_module

    dispatcher_module._INITIAL_BACKOFF_SECONDS = 0.001  # noqa: SLF001
    dispatcher_module._MAX_BACKOFF_SECONDS = 0.01  # noqa: SLF001

    task = asyncio.create_task(dispatcher.run())
    try:
        await asyncio.wait_for(_wait_until(lambda: connector.connected), timeout=2.0)
        assert connector.connect_count == 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_dispatcher_reconnects_after_publish_failure() -> None:
    connector = FakeConnector(fail_publish_count=1)
    buffers = RingBufferManager()
    buffers.push("d1", _tag(1))
    dispatcher = NorthboundDispatcher(connector, buffers, publish_interval_seconds=0.01)

    import xedge.northbound.dispatcher as dispatcher_module

    dispatcher_module._INITIAL_BACKOFF_SECONDS = 0.001  # noqa: SLF001

    task = asyncio.create_task(dispatcher.run())
    try:
        # first publish fails -> triggers reconnect; second batch pushed after
        # the failure should eventually get through once reconnected.
        await asyncio.sleep(0.05)
        buffers.push("d1", _tag(2))
        await asyncio.wait_for(_wait_until(lambda: connector.published_batches), timeout=2.0)
        assert connector.connect_count >= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_dispatcher_stop_disconnects_when_connected() -> None:
    connector = FakeConnector()
    buffers = RingBufferManager()
    dispatcher = NorthboundDispatcher(connector, buffers, publish_interval_seconds=0.01)

    task = asyncio.create_task(dispatcher.run())
    await asyncio.wait_for(_wait_until(lambda: connector.connected), timeout=2.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    await dispatcher.stop()
    assert connector.disconnected_count == 1


async def test_dispatcher_stop_is_noop_when_never_connected() -> None:
    connector = FakeConnector()
    buffers = RingBufferManager()
    dispatcher = NorthboundDispatcher(connector, buffers)
    await dispatcher.stop()
    assert connector.disconnected_count == 0


async def _wait_until(predicate: object, poll_interval: float = 0.01) -> None:
    # Polling an arbitrary caller-supplied predicate has no Event to wait on
    # instead — ASYNC110's suggestion doesn't apply to this generic helper.
    while not predicate():  # type: ignore[operator]  # noqa: ASYNC110
        await asyncio.sleep(poll_interval)
