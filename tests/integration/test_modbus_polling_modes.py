"""Sprint C3 tag-group `polling_mode` tests (XEDGE-435; ADR-011 Part 2):
`on_connect` (read once, then idle) and `on_demand` (never auto-read; a read
happens only when `poll_now()` is called — the REST side of that is
`POST /api/v1/drivers/{id}/tag-groups/{group_id}/poll`, covered in
tests/unit/test_api.py's TestPollTagGroupEndpoint). `continuous`, the
pre-existing default, is covered exhaustively in test_modbus_batching.py;
this file adds only a light regression check for it alongside the two new
modes, against the real driver and fake server so the wire traffic (or lack
of it) is an assertion rather than a claim.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.fixtures.fake_modbus_server import FakeModbusServer
from xedge.drivers.base import DriverConfig, TagUpdate
from xedge.drivers.modbus.tcp import ModbusTcpDriver


@pytest.fixture
async def fake_server() -> AsyncIterator[FakeModbusServer]:
    server = FakeModbusServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _tag(tag_id: str, address: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": tag_id,
        "address": address,
        "function_code": extra.pop("function_code", "read_holding_registers"),
        **extra,
    }


def _config(
    server: FakeModbusServer,
    tags: list[dict[str, Any]],
    polling_mode: str,
    **group_extra: Any,
) -> DriverConfig:
    return DriverConfig(
        instance_id="modbus_polling_mode_test",
        driver_type="modbus_tcp",
        config={"host": server.host, "port": server.port, "unit_id": 1},
        tag_groups=[
            {
                "id": "group1",
                "scan_rate_ms": 20,
                "polling_mode": polling_mode,
                "tags": tags,
                **group_extra,
            }
        ],
    )


@asynccontextmanager
async def _running_driver(
    config: DriverConfig,
) -> AsyncIterator[tuple[ModbusTcpDriver, asyncio.Queue[TagUpdate]]]:
    driver = ModbusTcpDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        yield driver, queue
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


class TestOnConnectMode:
    async def test_reads_exactly_once_then_stays_idle(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[0] = 42
        config = _config(fake_server, [_tag("t", 0)], polling_mode="on_connect")

        async with _running_driver(config) as (_driver, queue):
            first = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert first.value == 42
            # Long enough that a continuous 20ms-period group would have
            # produced several more updates by now.
            await asyncio.sleep(0.2)
            assert queue.empty()
            assert fake_server.read_request_count == 1

    async def test_group_task_ends_after_the_one_read(self, fake_server: FakeModbusServer) -> None:
        """An on_connect group's task returns — rather than looping idle
        forever — once its one read is done, so `run()`'s `gather()` over
        every group task completes on its own for a driver configured with
        only on_connect groups, with no external cancellation needed."""
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)], polling_mode="on_connect")
        driver = ModbusTcpDriver()
        await driver.configure(config)
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()

        await asyncio.wait_for(driver.run(queue), timeout=3.0)

        assert queue.qsize() == 1
        await driver.disconnect()


class TestOnDemandMode:
    async def test_never_auto_reads(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)], polling_mode="on_demand")

        async with _running_driver(config) as (_driver, queue):
            await asyncio.sleep(0.15)  # several would-be 20ms cycles
            assert queue.empty()
            assert fake_server.read_request_count == 0

    async def test_poll_now_triggers_exactly_one_read(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[0] = 99
        config = _config(fake_server, [_tag("t", 0)], polling_mode="on_demand")

        async with _running_driver(config) as (driver, queue):
            assert await driver.poll_now("group1") is True
            update = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert update.value == 99
            await asyncio.sleep(0.1)
            assert queue.empty()
            assert fake_server.read_request_count == 1

    async def test_poll_now_can_be_triggered_repeatedly(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)], polling_mode="on_demand")

        async with _running_driver(config) as (driver, queue):
            for expected_count in (1, 2, 3):
                assert await driver.poll_now("group1") is True
                await asyncio.wait_for(queue.get(), timeout=3.0)
                assert fake_server.read_request_count == expected_count

    async def test_poll_now_on_unknown_group_returns_false(
        self, fake_server: FakeModbusServer
    ) -> None:
        config = _config(fake_server, [_tag("t", 0)], polling_mode="on_demand")

        async with _running_driver(config) as (driver, _queue):
            assert await driver.poll_now("no_such_group") is False
            assert fake_server.read_request_count == 0

    async def test_poll_now_on_a_continuous_group_returns_false(
        self, fake_server: FakeModbusServer
    ) -> None:
        """`poll_now` is scoped to on_demand groups specifically — a
        continuous group already reads on its own schedule, so it never
        gets an entry in `_on_demand_triggers` (populated in `configure()`
        only for on_demand groups) for `poll_now` to find."""
        config = _config(fake_server, [_tag("t", 0)], polling_mode="continuous")

        async with _running_driver(config) as (driver, _queue):
            assert await driver.poll_now("group1") is False


class TestContinuousModeRegression:
    async def test_default_mode_is_continuous_and_still_repeats(
        self, fake_server: FakeModbusServer
    ) -> None:
        """No `polling_mode` key at all — the exact shape every config
        written before XEDGE-435 has — must behave exactly as before: read
        repeatedly on the configured period."""
        fake_server.holding_registers[0] = 7
        config = DriverConfig(
            instance_id="modbus_polling_mode_test",
            driver_type="modbus_tcp",
            config={"host": fake_server.host, "port": fake_server.port, "unit_id": 1},
            tag_groups=[{"id": "group1", "scan_rate_ms": 20, "tags": [_tag("t", 0)]}],
        )

        async with _running_driver(config) as (_driver, queue):
            first = await asyncio.wait_for(queue.get(), timeout=3.0)
            second = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert first.value == 7
            assert second.value == 7
            assert fake_server.read_request_count >= 2
