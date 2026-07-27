"""Unit test for xedge.core.main._pipeline_to_buffer's interaction with
AssetStorageFilter (Sprint C6, XEDGE-462; ADR-010 §3): proves the
per-parameter storage toggle actually suppresses cold-store spill at the
real boundary this pipeline function feeds into, not just in
AssetStorageFilter's own isolated unit tests (tests/unit/test_assets.py).
Same "call the real pipeline function directly with a synthetic queue, no
real driver/network I/O" pattern as test_pipeline_to_buffer_alarms.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from xedge.core.assets import AssetStorageFilter
from xedge.core.main import _pipeline_to_buffer
from xedge.core.pipeline import DeadbandFilter, UnifiedTag
from xedge.drivers.base import Quality, TagUpdate
from xedge.store.ring_buffer import RingBufferManager


def _update(tag_id: str, value: object) -> TagUpdate:
    return TagUpdate(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        quality=Quality.GOOD,
        source_driver=tag_id.split("/")[0],
        source_address="0",
    )


async def _push_through_pipeline(
    updates: list[TagUpdate], on_evict: Callable[[str, UnifiedTag], None]
) -> None:
    """Ring buffer sized to 1 so every push past the first forces an
    eviction -- on_evict fires synchronously inside RingBuffer.push, so
    once every update has been drained from the queue, every eviction it
    was going to cause has already happened."""
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    ring_buffers = RingBufferManager(max_depth=1, on_evict=on_evict)
    for update in updates:
        await queue.put(update)
    task = asyncio.create_task(
        _pipeline_to_buffer(queue, ring_buffers, deadband_filter=DeadbandFilter())
    )
    try:
        for _ in range(200):
            if queue.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # let the last dequeued item finish processing
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_tag_with_store_false_never_reaches_cold_store() -> None:
    spilled: list[UnifiedTag] = []
    filt = AssetStorageFilter(
        [{"id": "a1", "name": "A1", "parameters": [{"tag_ref": "d1/temp", "store": False}]}]
    )

    await _push_through_pipeline(
        [_update("d1/temp", 1.0), _update("d1/temp", 2.0), _update("d1/temp", 3.0)],
        filt.wrap(lambda stream_key, tag: spilled.append(tag)),
    )

    assert spilled == []


async def test_a_tag_with_no_asset_reference_still_spills_normally() -> None:
    """The common case -- most tags belong to no asset at all -- must be
    completely unaffected by this filter existing."""
    spilled: list[UnifiedTag] = []
    filt = AssetStorageFilter([])  # no assets configured

    await _push_through_pipeline(
        [_update("d1/temp", 1.0), _update("d1/temp", 2.0), _update("d1/temp", 3.0)],
        filt.wrap(lambda stream_key, tag: spilled.append(tag)),
    )

    assert len(spilled) >= 1
    assert all(tag.tag_id == "d1/temp" for tag in spilled)
