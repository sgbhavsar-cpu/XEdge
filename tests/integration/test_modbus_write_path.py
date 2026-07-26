"""Sprint C2 write-path tests: dedicated write-tag access (XEDGE-422),
FC16 multi-register write (XEDGE-423), write priority (XEDGE-424), and
device connectivity end-to-end (XEDGE-420/421).

Same fake-server pattern as test_modbus_tcp_driver.py — these exercise the
real driver against real wire traffic, not the scheduler/state-machine
units in isolation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.fixtures.fake_modbus_server import FakeModbusServer
from xedge.core.connectivity import ConnectivityState
from xedge.drivers.base import DriverConfig, TagUpdate
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


def _config(
    server: FakeModbusServer,
    tags: list[dict[str, Any]],
    scan_rate_ms: int = 50,
    config_extra: dict[str, Any] | None = None,
) -> DriverConfig:
    return DriverConfig(
        instance_id="write_path_test",
        driver_type="modbus_tcp",
        config={"host": server.host, "port": server.port, "unit_id": 1, **(config_extra or {})},
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


class TestDedicatedWriteTags:
    """XEDGE-422: a tag's `access` decides whether it's polled and whether
    write() accepts it."""

    async def test_write_only_tag_is_never_polled(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[5] = 999
        tag = {
            "id": "command",
            "function_code": "read_holding_registers",
            "address": 5,
            "access": "write_only",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            await asyncio.sleep(0.15)  # several scan cycles' worth
            assert queue.empty(), "a write_only tag must never produce a read TagUpdate"
            assert fake_server.read_request_count == 0
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

    async def test_write_only_tag_is_still_writable(self, fake_server: FakeModbusServer) -> None:
        tag = {
            "id": "command",
            "function_code": "read_holding_registers",
            "address": 5,
            "access": "write_only",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            result = await driver.write("command", 42)
            assert result.success is True
            assert fake_server.holding_registers[5] == 42
        finally:
            await driver.disconnect()

    async def test_read_only_tag_rejects_writes(self, fake_server: FakeModbusServer) -> None:
        tag = {
            "id": "status_word",
            "function_code": "read_holding_registers",
            "address": 2,
            "access": "read_only",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            result = await driver.write("status_word", 1)
            assert result.success is False
            assert "read-only" in result.error_message
            assert "read_only" in result.error_message
        finally:
            await driver.disconnect()

    async def test_read_only_tag_is_still_polled(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[2] = 7
        tag = {
            "id": "status_word",
            "function_code": "read_holding_registers",
            "address": 2,
            "access": "read_only",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag], scan_rate_ms=10))
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            update = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert update.value == 7
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

    async def test_default_access_matches_pre_xedge_422_behavior(
        self, fake_server: FakeModbusServer
    ) -> None:
        """No `access` field at all — every config written before this
        sprint — must still be pollable and writable, exactly as before."""
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            result = await driver.write("t", 123)
            assert result.success is True
            assert fake_server.holding_registers[0] == 123
        finally:
            await driver.disconnect()


class TestMultiRegisterWrite:
    """XEDGE-423: FC16, replacing the one-sprint-long rejection from C1."""

    async def test_int32_tag_writes_via_fc16(self, fake_server: FakeModbusServer) -> None:
        tag = {
            "id": "counter",
            "function_code": "read_holding_registers",
            "address": 10,
            "data_type": "int32",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            result = await driver.write("counter", 65536)
            assert result.success is True
            assert fake_server.request_log[-1][0] == codec.FunctionCode.WRITE_MULTIPLE_REGISTERS
            assert (fake_server.holding_registers[10], fake_server.holding_registers[11]) == (1, 0)
        finally:
            await driver.disconnect()

    async def test_float32_tag_writes_via_fc16(self, fake_server: FakeModbusServer) -> None:
        tag = {
            "id": "flow",
            "function_code": "read_holding_registers",
            "address": 20,
            "data_type": "float32",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            result = await driver.write("flow", 1.0)
            assert result.success is True
            # 1.0f is 0x3F80_0000.
            assert (fake_server.holding_registers[20], fake_server.holding_registers[21]) == (
                0x3F80,
                0x0000,
            )
        finally:
            await driver.disconnect()

    async def test_word_order_is_honoured_on_write(self, fake_server: FakeModbusServer) -> None:
        tag = {
            "id": "counter",
            "function_code": "read_holding_registers",
            "address": 30,
            "data_type": "uint32",
            "word_order": "little",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            await driver.write("counter", 65536)  # 0x0001_0000
            # Little word order: low word first.
            assert (fake_server.holding_registers[30], fake_server.holding_registers[31]) == (0, 1)
        finally:
            await driver.disconnect()

    async def test_single_register_tag_still_uses_fc06_not_fc16(
        self, fake_server: FakeModbusServer
    ) -> None:
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            await driver.write("t", 5)
            assert fake_server.request_log[-1][0] == codec.FunctionCode.WRITE_SINGLE_REGISTER
        finally:
            await driver.disconnect()

    async def test_multi_register_write_applies_scaling(
        self, fake_server: FakeModbusServer
    ) -> None:
        tag = {
            "id": "energy",
            "function_code": "read_holding_registers",
            "address": 40,
            "data_type": "int32",
            "scaling": {"scale": 0.01, "offset": 0.0},
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            # engineering value 12.34 -> raw 1234 (inverse of raw*scale+offset)
            await driver.write("energy", 12.34)
            assert (fake_server.holding_registers[40], fake_server.holding_registers[41]) == (
                0,
                1234,
            )
        finally:
            await driver.disconnect()

    async def test_a_rejected_multi_register_write_reports_failure(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.exceptions[(codec.FunctionCode.WRITE_MULTIPLE_REGISTERS, 50)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        tag = {
            "id": "counter",
            "function_code": "read_holding_registers",
            "address": 50,
            "data_type": "int32",
        }
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        await driver.connect()
        try:
            result = await driver.write("counter", 1)
            assert result.success is False
        finally:
            await driver.disconnect()


class TestWritePriorityAgainstARealServer:
    """XEDGE-424: end-to-end proof that a write does not queue behind a
    slow poll cycle."""

    async def test_write_is_served_before_a_large_pending_read(
        self, fake_server: FakeModbusServer
    ) -> None:
        for address in range(100):
            fake_server.holding_registers[address] = address
        read_tags = [
            {"id": f"t{i}", "function_code": "read_holding_registers", "address": i}
            for i in range(100)
        ]
        write_tag = {
            "id": "setpoint",
            "function_code": "read_holding_registers",
            "address": 500,
            "access": "write_only",
        }
        config = _config(fake_server, [*read_tags, write_tag], scan_rate_ms=200)

        original_read_block = ModbusTcpDriver._read_block

        async def slow_read_block(
            self: ModbusTcpDriver, function_code: Any, address: int, quantity: int
        ) -> Any:
            await asyncio.sleep(0.03)  # simulate a slow bus per-request
            return await original_read_block(self, function_code, address, quantity)

        driver = ModbusTcpDriver()
        driver._read_block = slow_read_block.__get__(driver, ModbusTcpDriver)  # type: ignore[method-assign]  # noqa: SLF001
        await driver.configure(config)
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            # Let the first (slow) read block start, so subsequent reads
            # queue up behind it before the write is submitted.
            await asyncio.sleep(0.01)
            write_started = asyncio.get_running_loop().time()
            result = await driver.write("setpoint", 99)
            write_elapsed = asyncio.get_running_loop().time() - write_started
            assert result.success is True
            # 100 reads at 0.03s each would be ~3s if the write waited behind
            # them; it must complete close to a single request's latency.
            assert write_elapsed < 0.15, (
                f"write took {write_elapsed:.3f}s - it must not queue behind pending reads"
            )
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()


class TestDeviceConnectivityEndToEnd:
    """XEDGE-420/421: the device-level signal `state` (DriverState) cannot
    represent, observed through the real driver rather than the tracker
    in isolation."""

    async def test_starts_unknown_before_any_read(self, fake_server: FakeModbusServer) -> None:
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusTcpDriver()
        await driver.configure(_config(fake_server, [tag]))
        assert driver.get_connectivity_state() is ConnectivityState.UNKNOWN

    async def test_becomes_connected_after_a_successful_read(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.holding_registers[0] = 1
        driver = ModbusTcpDriver()
        await driver.configure(
            _config(
                fake_server, [{"id": "t", "function_code": "read_holding_registers", "address": 0}]
            )
        )
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            await asyncio.wait_for(queue.get(), timeout=2.0)
            assert driver.get_connectivity_state() is ConnectivityState.CONNECTED
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

    async def test_becomes_not_connected_after_consecutive_exceptions(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusTcpDriver()
        await driver.configure(
            _config(
                fake_server,
                [tag],
                scan_rate_ms=10,
                config_extra={"consecutive_failure_threshold": 2},
            )
        )
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            for _ in range(200):
                if driver.get_connectivity_state() is ConnectivityState.NOT_CONNECTED:
                    break
                await asyncio.sleep(0.01)
            assert driver.get_connectivity_state() is ConnectivityState.NOT_CONNECTED
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

    async def test_configured_thresholds_are_honoured(self, fake_server: FakeModbusServer) -> None:
        """A threshold of 1 should reach Not Connected on the very first
        failure - proves the schema-configured value actually reaches the
        tracker, not just the default."""
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusTcpDriver()
        await driver.configure(
            _config(
                fake_server,
                [tag],
                scan_rate_ms=10,
                config_extra={"consecutive_failure_threshold": 1},
            )
        )
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            await asyncio.wait_for(queue.get(), timeout=2.0)
            assert driver.get_connectivity_state() is ConnectivityState.NOT_CONNECTED
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

    async def test_recovers_after_the_device_starts_answering_again(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusTcpDriver()
        await driver.configure(
            _config(
                fake_server,
                [tag],
                scan_rate_ms=10,
                config_extra={"consecutive_failure_threshold": 1, "recovery_threshold": 2},
            )
        )
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            for _ in range(200):
                if driver.get_connectivity_state() is ConnectivityState.NOT_CONNECTED:
                    break
                await asyncio.sleep(0.01)
            assert driver.get_connectivity_state() is ConnectivityState.NOT_CONNECTED

            del fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)]
            fake_server.holding_registers[0] = 1

            for _ in range(200):
                if driver.get_connectivity_state() is ConnectivityState.CONNECTED:
                    break
                await asyncio.sleep(0.01)
            assert driver.get_connectivity_state() is ConnectivityState.CONNECTED
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()
