"""Integration tests: ModbusRtuOverTcpDriver against the in-house fake RTU
server (tests/fixtures/fake_modbus_server.py:FakeModbusRtuServer)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from tests.fixtures.fake_modbus_server import FakeModbusRtuServer
from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.modbus import codec
from xedge.drivers.modbus.rtu_over_tcp import ModbusRtuOverTcpDriver


@pytest.fixture
async def fake_rtu_server() -> AsyncIterator[FakeModbusRtuServer]:
    server = FakeModbusRtuServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _driver_config(
    server: FakeModbusRtuServer, tags: list[dict], scan_rate_ms: int = 50
) -> DriverConfig:
    return DriverConfig(
        instance_id="modbus_rtu_test",
        driver_type="modbus_rtu_tcp",
        config={"host": server.host, "port": server.port, "unit_id": 1},
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _run_one_cycle(driver: ModbusRtuOverTcpDriver, config: DriverConfig) -> list[TagUpdate]:
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        updates = [
            await asyncio.wait_for(queue.get(), timeout=2.0) for _ in config.tag_groups[0]["tags"]
        ]
        return updates
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_reads_holding_register(fake_rtu_server: FakeModbusRtuServer) -> None:
    fake_rtu_server.holding_registers[0] = 4321
    config = _driver_config(
        fake_rtu_server, [{"id": "reg_01", "function_code": "read_holding_registers", "address": 0}]
    )
    driver = ModbusRtuOverTcpDriver()
    updates = await _run_one_cycle(driver, config)
    assert updates[0].value == 4321
    assert updates[0].quality == Quality.GOOD


async def test_reads_coil(fake_rtu_server: FakeModbusRtuServer) -> None:
    fake_rtu_server.coils[3] = True
    config = _driver_config(
        fake_rtu_server, [{"id": "coil_03", "function_code": "read_coils", "address": 3}]
    )
    driver = ModbusRtuOverTcpDriver()
    updates = await _run_one_cycle(driver, config)
    assert updates[0].value is True


async def test_modbus_exception_marks_tag_bad(fake_rtu_server: FakeModbusRtuServer) -> None:
    fake_rtu_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
        codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
    )
    config = _driver_config(
        fake_rtu_server,
        [{"id": "bad_tag", "function_code": "read_holding_registers", "address": 0}],
    )
    driver = ModbusRtuOverTcpDriver()
    updates = await _run_one_cycle(driver, config)
    assert updates[0].quality == Quality.BAD
    assert updates[0].metadata["modbus_exception"] == codec.ExceptionCode.ILLEGAL_DATA_ADDRESS


async def test_connect_to_unreachable_host_raises() -> None:
    driver = ModbusRtuOverTcpDriver()
    config = DriverConfig(
        instance_id="unreachable",
        driver_type="modbus_rtu_tcp",
        config={"host": "127.0.0.1", "port": 1, "connect_timeout_seconds": 1},
        tag_groups=[],
    )
    await driver.configure(config)
    with pytest.raises((ConnectionRefusedError, OSError, TimeoutError)):
        await driver.connect()
