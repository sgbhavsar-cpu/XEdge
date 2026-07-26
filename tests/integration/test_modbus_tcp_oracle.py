"""Cross-validates the in-house Modbus driver/codec against pymodbus's own
TCP server implementation — an independent implementation of the same
public spec. This is the ADR-006 black-box oracle check: pymodbus is
exercised only as a running server process here, never read as source.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from xedge.drivers.base import DriverConfig, Quality
from xedge.drivers.modbus import codec
from xedge.drivers.modbus.tcp import ModbusTcpDriver

_ILLEGAL_ADDRESS = 50000  # within the 16-bit wire range, but outside any defined SimData block


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _driver_config(host: str, port: int, tag_groups: list[dict] | None = None) -> DriverConfig:
    return DriverConfig(
        instance_id="oracle",
        driver_type="modbus_tcp",
        config={"host": host, "port": port},
        tag_groups=tag_groups or [],
    )


@pytest.fixture
async def oracle_server() -> AsyncIterator[tuple[str, int]]:
    host, port = "127.0.0.1", _free_port()

    holding_registers = [0] * 200
    holding_registers[100] = 12345  # FC03 holding register (wire address) 100
    coil_values = [False] * 200
    coil_values[5] = True  # FC01 coil (wire address) 5

    # Non-shared blocks, per pymodbus.simulator's modern SimData/SimDevice API
    # (the old ModbusDeviceContext/ModbusSequentialDataBlock shim is
    # deprecated and, in this pymodbus release, silently drops all but the
    # first value). Each block is a single SimData spanning the full address
    # range as an explicit values list — deliberately avoiding multiple
    # SimData entries per block, whose overlap-validation double-counts
    # `count` for BITS blocks in this pymodbus version.
    coils = [SimData(address=0, values=coil_values, datatype=DataType.BITS)]
    discrete_inputs = [SimData(address=0, values=[False] * 200, datatype=DataType.BITS)]
    holding = [SimData(address=0, values=holding_registers, datatype=DataType.UINT16)]
    inputs = [SimData(address=0, values=[0] * 200, datatype=DataType.UINT16)]

    device = SimDevice(
        id=1, simdata=(coils, discrete_inputs, holding, inputs), use_bit_addressing=True
    )
    server = ModbusTcpServer(device, address=(host, port))

    await server.serve_forever(background=True)
    try:
        yield host, port
    finally:
        await server.shutdown()


async def test_holding_register_matches_pymodbus_oracle(oracle_server: tuple[str, int]) -> None:
    host, port = oracle_server
    driver = ModbusTcpDriver()
    await driver.configure(_driver_config(host, port))
    await driver.connect()
    try:
        values = await driver._read_block(codec.FunctionCode.READ_HOLDING_REGISTERS, 100, 1)  # noqa: SLF001
        assert values == [12345]
    finally:
        await driver.disconnect()


async def test_coil_matches_pymodbus_oracle(oracle_server: tuple[str, int]) -> None:
    host, port = oracle_server
    driver = ModbusTcpDriver()
    await driver.configure(_driver_config(host, port))
    await driver.connect()
    try:
        values = await driver._read_block(codec.FunctionCode.READ_COILS, 5, 1)  # noqa: SLF001
        assert values == [True]
    finally:
        await driver.disconnect()


async def test_illegal_address_raises_modbus_exception_against_oracle(
    oracle_server: tuple[str, int],
) -> None:
    host, port = oracle_server
    driver = ModbusTcpDriver()
    await driver.configure(_driver_config(host, port))
    await driver.connect()
    try:
        with pytest.raises(codec.ModbusException) as exc_info:
            await driver._read_block(  # noqa: SLF001
                codec.FunctionCode.READ_HOLDING_REGISTERS, _ILLEGAL_ADDRESS, 1
            )
        assert exc_info.value.exception_code == codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
    finally:
        await driver.disconnect()


async def test_full_tag_group_read_against_oracle_via_run_cycle(
    oracle_server: tuple[str, int],
) -> None:
    """End-to-end: driver.run() polling a real tag group against the oracle server."""
    host, port = oracle_server
    driver = ModbusTcpDriver()
    config = _driver_config(
        host,
        port,
        tag_groups=[
            {
                "id": "group1",
                "scan_rate_ms": 50,
                "tags": [
                    {"id": "reg_100", "function_code": "read_holding_registers", "address": 100}
                ],
            }
        ],
    )
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        update = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert update.value == 12345
        assert update.quality == Quality.GOOD
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()
