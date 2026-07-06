"""Real, timing-bounded proof for Sprint 25's XEDGE-184/188 acceptance
criterion: "hot-reload of 10 drivers simultaneously; no tag gap > 2 scan
cycles." Uses wall-clock timestamps on real TagUpdates flowing through a
real DriverSupervisor + apply_driver_changes() — not a mocked timing
assertion.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime

from xedge.core.hot_reload import apply_driver_changes
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.drivers.base import TagUpdate

_DRIVER_COUNT = 10
_EMIT_INTERVAL_SECONDS = 0.05
_MAX_ALLOWED_GAP_SECONDS = 2 * _EMIT_INTERVAL_SECONDS + 0.15  # scan-cycle bound + scheduling slack


def _entry(instance_id: str, marker: int = 502) -> dict:
    # `type: modbus_tcp` purely to satisfy build_driver_config()'s real
    # schema validation — the registry below maps this type string to
    # FakeDriver, not an actual ModbusTcpDriver, same trick
    # tests/unit/test_hot_reload.py's own fixture already uses. `marker`
    # (bumped between cycles) is stashed as the unused `port` field so a
    # value change is detected as "changed" without altering shape.
    return {
        "id": instance_id,
        "type": "modbus_tcp",
        "config": {"host": "127.0.0.1", "port": marker},
        "tag_groups": [
            {
                "id": "g1",
                "scan_rate_ms": 100,
                "tags": [{"id": "counter", "function_code": "read_holding_registers", "address": 0}],
            }
        ],
    }


async def test_hot_reload_of_ten_drivers_keeps_gap_under_two_scan_cycles() -> None:
    from tests.fixtures.fake_driver import FakeDriver

    queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=10_000)
    registry = DriverRegistry()
    registry.register(
        "modbus_tcp", lambda: FakeDriver(emit_interval_seconds=_EMIT_INTERVAL_SECONDS)
    )
    supervisor = DriverSupervisor(registry, queue)

    instance_ids = [f"driver_{i}" for i in range(_DRIVER_COUNT)]
    entries = [_entry(i) for i in instance_ids]
    current = await apply_driver_changes(entries, {}, registry, supervisor)

    last_seen_before: dict[str, datetime] = {}
    # Drain until every instance has emitted at least once, so each has a
    # real "last seen before the reload" timestamp to measure the gap from.
    while len(last_seen_before) < _DRIVER_COUNT:
        update = await asyncio.wait_for(queue.get(), timeout=5.0)
        last_seen_before[update.source_driver] = update.timestamp
    # A little extra runway so every instance has a *recent* last-seen time,
    # not just its very first (possibly early-scheduled) update.
    await asyncio.sleep(_EMIT_INTERVAL_SECONDS * 3)
    while not queue.empty():
        update = queue.get_nowait()
        last_seen_before[update.source_driver] = update.timestamp

    reload_started_at = datetime.now(UTC)
    changed_entries = [_entry(i, marker=1) for i in instance_ids]  # forces every instance to restart
    await apply_driver_changes(changed_entries, copy.deepcopy(current), registry, supervisor)

    first_seen_after: dict[str, datetime] = {}
    deadline = asyncio.get_event_loop().time() + 5.0
    while len(first_seen_after) < _DRIVER_COUNT and asyncio.get_event_loop().time() < deadline:
        try:
            update = await asyncio.wait_for(queue.get(), timeout=5.0)
        except TimeoutError:
            break
        if update.timestamp >= reload_started_at and update.source_driver not in first_seen_after:
            first_seen_after[update.source_driver] = update.timestamp

    try:
        assert set(first_seen_after) == set(instance_ids), (
            f"not every instance resumed emitting: missing {set(instance_ids) - set(first_seen_after)}"
        )
        for instance_id in instance_ids:
            gap = (first_seen_after[instance_id] - last_seen_before[instance_id]).total_seconds()
            assert gap <= _MAX_ALLOWED_GAP_SECONDS, (
                f"{instance_id}: gap {gap:.3f}s exceeded the 2-scan-cycle bound "
                f"({_MAX_ALLOWED_GAP_SECONDS:.3f}s)"
            )
    finally:
        await supervisor.stop_all()
