"""XEDGE-492 (Sprint H1): performance validation -- batched Modbus
throughput and scan-rate accuracy under concurrent multi-instance load.

Real wall-clock timing against real ModbusTcpDriver instances and a real
FakeModbusServer (ADR-006 precedent), not mocked sleeps -- the whole point
of this story is measuring actual behavior, not re-asserting the design.

Broker footprint on the ARM target is deliberately *not* exercised here:
license-audit.md §4 item 6 already recorded that the actual RAM footprint
against the ADR-007 1 GB target was never measured (no ARM hardware or
emulated target available in any development environment used on this
delivery) and folded that gap into open item Q-6 -- the same open item
covering the XEDGE-491 HIL pass. See docs/planning/XEDGE-CRD-001-handover.md
"Known limitations" for the customer-facing statement.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from tests.fixtures.fake_modbus_server import FakeModbusServer
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.modbus.tcp import ModbusTcpDriver

_BATCH_SIZE = 100
_BATCH_CYCLES = 5

_INSTANCE_COUNT = 5
_SCAN_RATE_MS = 50
_CYCLES_TO_OBSERVE = 30


@pytest.fixture
async def fake_server() -> AsyncIterator[FakeModbusServer]:
    server = FakeModbusServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_large_batch_stays_one_request_per_cycle(fake_server: FakeModbusServer) -> None:
    """100 contiguous holding registers (still under Modbus's own 125-register
    read cap) must cost exactly one wire round trip per scan cycle, the same
    as a single tag would -- this is what makes throughput scale with tag
    count rather than degrading linearly per tag. test_modbus_batching.py
    proves the batching *logic*; this proves it holds at realistic scale and
    stays cheap enough to fit inside the configured scan period."""
    tags = [
        {"id": f"reg_{i}", "function_code": "read_holding_registers", "address": i}
        for i in range(_BATCH_SIZE)
    ]
    for i in range(_BATCH_SIZE):
        fake_server.holding_registers[i] = i

    config = DriverConfig(
        instance_id="perf_batch",
        driver_type="modbus_tcp",
        config={"host": fake_server.host, "port": fake_server.port},
        tag_groups=[{"id": "g1", "scan_rate_ms": _SCAN_RATE_MS, "tags": tags}],
    )
    driver = ModbusTcpDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        for _ in range(_BATCH_CYCLES):
            batch = [
                await asyncio.wait_for(queue.get(), timeout=2.0) for _ in range(_BATCH_SIZE)
            ]
            assert len(batch) == _BATCH_SIZE
            assert all(update.quality == Quality.GOOD for update in batch)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()

    assert len(fake_server.request_log) == _BATCH_CYCLES, (
        f"expected exactly {_BATCH_CYCLES} requests (one per cycle) for "
        f"{_BATCH_SIZE} batched tags, got {len(fake_server.request_log)}"
    )


async def test_scan_rate_holds_steady_under_concurrent_multi_instance_load() -> None:
    """Five real ModbusTcpDriver instances, each against its own real
    FakeModbusServer, polling concurrently at the 50ms floor (FR-SA-009) for
    thirty cycles -- proving XEDGE-410's fixed-deadline scheduler ("Fixes
    scan-rate drift," F-6) holds its period under real concurrent multi-
    instance load, not just for one driver in isolation.

    Compares early-window vs. late-window average period rather than
    asserting tight per-sample precision: the old sleep-after-work
    scheduler's failure mode (F-6) was *accumulating* drift over a long
    run, so that comparison is what a regression would actually show,
    without the flakiness a tight absolute tolerance would invite under
    real OS scheduling jitter.
    """
    servers = [FakeModbusServer() for _ in range(_INSTANCE_COUNT)]
    for server in servers:
        await server.start()
        server.holding_registers[0] = 1

    tag_queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=10_000)
    registry = DriverRegistry()
    registry.register("modbus_tcp", ModbusTcpDriver)
    supervisor = DriverSupervisor(registry, tag_queue)

    target_tag = "perf_0/counter"
    timestamps: list[float] = []
    try:
        for index, server in enumerate(servers):
            supervisor.start(
                DriverConfig(
                    instance_id=f"perf_{index}",
                    driver_type="modbus_tcp",
                    config={"host": server.host, "port": server.port},
                    tag_groups=[
                        {
                            "id": "g1",
                            "scan_rate_ms": _SCAN_RATE_MS,
                            "tags": [
                                {
                                    "id": "counter",
                                    "function_code": "read_holding_registers",
                                    "address": 0,
                                }
                            ],
                        }
                    ],
                )
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while len(timestamps) < _CYCLES_TO_OBSERVE and loop.time() < deadline:
            update = await asyncio.wait_for(tag_queue.get(), timeout=2.0)
            if update.tag_id == target_tag:
                timestamps.append(update.timestamp.timestamp())
    finally:
        await supervisor.stop_all()
        for server in servers:
            await server.stop()

    assert len(timestamps) >= _CYCLES_TO_OBSERVE, (
        f"only observed {len(timestamps)} cycles for {target_tag} in 10s "
        f"while 5 instances shared the event loop"
    )

    deltas_ms = [
        (later - earlier) * 1000
        for earlier, later in zip(timestamps, timestamps[1:], strict=False)
    ]
    mean_delta = sum(deltas_ms) / len(deltas_ms)
    assert 30.0 <= mean_delta <= 80.0, (
        f"mean inter-sample period {mean_delta:.1f}ms is far from the "
        f"configured {_SCAN_RATE_MS}ms under 5-instance concurrent load"
    )

    first_window = deltas_ms[:10]
    last_window = deltas_ms[-10:]
    drift_ms = abs(sum(last_window) / len(last_window) - sum(first_window) / len(first_window))
    assert drift_ms < 25.0, (
        f"scan period drifted {drift_ms:.1f}ms between the start and end of "
        f"a {len(timestamps)}-cycle run -- exactly the F-6 failure mode "
        "XEDGE-410's fixed-deadline scheduler was meant to fix"
    )
