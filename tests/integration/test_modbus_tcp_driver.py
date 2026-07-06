"""Integration tests: ModbusTcpDriver against the in-house fake server
(tests/fixtures/fake_modbus_server.py) — fast, deterministic wire-level
coverage. See test_modbus_tcp_oracle.py for cross-validation against a real,
independent Modbus TCP server implementation (ADR-006 black-box oracle)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from tests.fixtures.fake_modbus_server import FakeModbusServer
from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.modbus import codec
from xedge.drivers.modbus.tcp import ModbusTcpDriver


@pytest.fixture
async def fake_server() -> AsyncIterator[FakeModbusServer]:
    server = FakeModbusServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _driver_config(
    server: FakeModbusServer, tags: list[dict], scan_rate_ms: int = 50
) -> DriverConfig:
    return DriverConfig(
        instance_id="modbus_tcp_test",
        driver_type="modbus_tcp",
        config={"host": server.host, "port": server.port, "unit_id": 1},
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _run_one_cycle(driver: ModbusTcpDriver, config: DriverConfig) -> list[TagUpdate]:
    """Connect, run the driver just long enough to collect one TagUpdate per
    configured tag, then disconnect."""
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


async def test_reads_holding_register(fake_server: FakeModbusServer) -> None:
    fake_server.holding_registers[0] = 12345
    config = _driver_config(
        fake_server,
        [{"id": "temperature_01", "function_code": "read_holding_registers", "address": 0}],
    )
    driver = ModbusTcpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == 12345
    assert updates[0].quality == Quality.GOOD
    assert updates[0].tag_id == "modbus_tcp_test/temperature_01"
    assert updates[0].metadata["modbus_exception"] is None


async def test_reads_input_register(fake_server: FakeModbusServer) -> None:
    fake_server.input_registers[10] = 999
    config = _driver_config(
        fake_server, [{"id": "flow_01", "function_code": "read_input_registers", "address": 10}]
    )
    driver = ModbusTcpDriver()
    updates = await _run_one_cycle(driver, config)
    assert updates[0].value == 999


async def test_reads_coil_true_and_false(fake_server: FakeModbusServer) -> None:
    fake_server.coils[0] = True
    fake_server.coils[1] = False
    config = _driver_config(
        fake_server,
        [
            {"id": "pump_running", "function_code": "read_coils", "address": 0},
            {"id": "valve_open", "function_code": "read_coils", "address": 1},
        ],
    )
    driver = ModbusTcpDriver()
    updates = await _run_one_cycle(driver, config)
    assert updates[0].value is True
    assert updates[1].value is False


async def test_reads_discrete_input(fake_server: FakeModbusServer) -> None:
    fake_server.discrete_inputs[5] = True
    config = _driver_config(
        fake_server, [{"id": "door_open", "function_code": "read_discrete_inputs", "address": 5}]
    )
    driver = ModbusTcpDriver()
    updates = await _run_one_cycle(driver, config)
    assert updates[0].value is True


async def test_modbus_exception_marks_tag_bad_without_killing_driver(
    fake_server: FakeModbusServer,
) -> None:
    fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
        codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
    )
    fake_server.holding_registers[1] = 42
    config = _driver_config(
        fake_server,
        [
            {"id": "bad_tag", "function_code": "read_holding_registers", "address": 0},
            {"id": "good_tag", "function_code": "read_holding_registers", "address": 1},
        ],
    )
    driver = ModbusTcpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD
    assert updates[0].value == 0
    assert updates[0].metadata["modbus_exception"] == codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
    assert updates[1].quality == Quality.GOOD
    assert updates[1].value == 42


async def test_connect_to_unreachable_host_raises() -> None:
    driver = ModbusTcpDriver()
    config = DriverConfig(
        instance_id="unreachable",
        driver_type="modbus_tcp",
        config={"host": "127.0.0.1", "port": 1, "connect_timeout_seconds": 1},
        tag_groups=[],
    )
    await driver.configure(config)
    with pytest.raises((ConnectionRefusedError, OSError, asyncio.TimeoutError)):
        await driver.connect()


async def test_write_returns_not_supported(fake_server: FakeModbusServer) -> None:
    driver = ModbusTcpDriver()
    config = _driver_config(fake_server, [])
    await driver.configure(config)
    await driver.connect()
    try:
        result = await driver.write("some_tag", 1)
        assert result.success is False
    finally:
        await driver.disconnect()


async def test_read_produces_driver_read_span(
    fake_server: FakeModbusServer, otel_test_tracer_provider
) -> None:
    fake_server.holding_registers[0] = 100
    config = _driver_config(
        fake_server,
        [{"id": "tag1", "function_code": "read_holding_registers", "address": 0}],
    )
    driver = ModbusTcpDriver()
    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["driver.instance_id"] == "modbus_tcp_test"
    assert spans[0].attributes["tag.id"] == "tag1"
    assert spans[0].attributes["quality"] == Quality.GOOD.value


async def test_modbus_exception_produces_error_span(
    fake_server: FakeModbusServer, otel_test_tracer_provider
) -> None:
    fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
        codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
    )
    config = _driver_config(
        fake_server,
        [{"id": "tag1", "function_code": "read_holding_registers", "address": 0}],
    )
    driver = ModbusTcpDriver()
    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["quality"] == Quality.BAD.value
    assert spans[0].status.status_code.name == "ERROR"
