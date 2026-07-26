from __future__ import annotations

import asyncio

from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.loopback.driver import LoopbackDriver


def _config(initial_value: object = 42) -> DriverConfig:
    return DriverConfig(
        instance_id="loop_01",
        driver_type="loopback",
        config={},
        tag_groups=[
            {
                "id": "g1",
                "scan_rate_ms": 50,
                "tags": [{"id": "echo", "initial_value": initial_value}],
            }
        ],
    )


async def _run_one_cycle(
    driver: LoopbackDriver, config: DriverConfig, count: int = 1
) -> list[TagUpdate]:
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    task = asyncio.create_task(driver.run(queue))
    try:
        return [await asyncio.wait_for(queue.get(), timeout=2.0) for _ in range(count)]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await driver.disconnect()


async def test_reports_initial_value() -> None:
    driver = LoopbackDriver()
    updates = await _run_one_cycle(driver, _config(initial_value=42))

    assert updates[0].tag_id == "loop_01/echo"
    assert updates[0].value == 42
    assert updates[0].quality == Quality.GOOD


async def test_defaults_to_zero_without_initial_value() -> None:
    driver = LoopbackDriver()
    config = DriverConfig(
        instance_id="loop_01",
        driver_type="loopback",
        config={},
        tag_groups=[{"id": "g1", "scan_rate_ms": 50, "tags": [{"id": "echo"}]}],
    )
    updates = await _run_one_cycle(driver, config)
    assert updates[0].value == 0


async def test_write_round_trips_to_next_read() -> None:
    driver = LoopbackDriver()
    config = _config(initial_value=1)
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    task = asyncio.create_task(driver.run(queue))
    try:
        first = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert first.value == 1

        result = await driver.write("echo", 99)
        assert result.success is True

        second = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert second.value == 99
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await driver.disconnect()


async def test_write_to_unknown_tag_fails() -> None:
    driver = LoopbackDriver()
    await driver.configure(_config())
    result = await driver.write("nonexistent", 1)
    assert result.success is False


async def test_metrics_track_reads() -> None:
    driver = LoopbackDriver()
    await _run_one_cycle(driver, _config(), count=3)
    metrics = driver.get_metrics()
    assert metrics.tag_read_count >= 3
    assert metrics.last_successful_read is not None


async def test_connect_and_disconnect_are_safe_no_ops() -> None:
    driver = LoopbackDriver()
    await driver.configure(_config())
    await driver.connect()
    await driver.disconnect()
    await driver.disconnect()  # safe to call again
