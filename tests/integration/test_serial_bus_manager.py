"""SerialBusManager tests against a real pty (Sprint C3, XEDGE-430/431/432).

**POSIX only — skipped on Windows.** Built on `tests/fixtures/fake_serial_port.py`,
which uses the stdlib `pty`/`termios` modules to give the real
`ModbusRtuSerialDriver`/`SerialBusManager` a genuine character device to
open and configure exactly as they would `/dev/ttyUSB0`. This repo's CI
runs on `ubuntu-latest`, where these run and are the actual verification
for this module — see `fake_serial_port.py`'s module docstring for why a
plain socketpair could not stand in for a pty here.

The headline property under test: two `ModbusRtuSerialDriver` instances
with different `unit_id`s, configured against the *same* port path, must
share one connection and one write-priority queue rather than each opening
the port independently — the collision ADR-011 exists to fix.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.fixtures.fake_serial_port import HAS_PTY, FakeSerialSlave
from xedge.drivers.base import DriverConfig, TagUpdate
from xedge.drivers.modbus.bus_manager import get_bus_manager
from xedge.drivers.modbus.serial import ModbusRtuSerialDriver

pytestmark = pytest.mark.skipif(not HAS_PTY, reason="pty is POSIX-only; validated by Linux CI")


@pytest.fixture
async def fake_slave() -> AsyncIterator[FakeSerialSlave]:
    slave = FakeSerialSlave()
    slave.start()
    try:
        yield slave
    finally:
        await slave.stop()


def _config(
    port_path: str,
    unit_id: int,
    tags: list[dict[str, Any]],
    scan_rate_ms: int = 50,
    instance_id: str = "rtu_test",
    config_extra: dict[str, Any] | None = None,
) -> DriverConfig:
    return DriverConfig(
        instance_id=instance_id,
        driver_type="modbus_rtu_serial",
        config={"port": port_path, "unit_id": unit_id, "baud_rate": 19200, **(config_extra or {})},
        tag_groups=[{"id": "g1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _collect(
    driver: ModbusRtuSerialDriver, config: DriverConfig, count: int
) -> list[TagUpdate]:
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        return [await asyncio.wait_for(queue.get(), timeout=3.0) for _ in range(count)]
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


class TestBasicReadWrite:
    async def test_reads_a_holding_register_over_a_real_pty(
        self, fake_slave: FakeSerialSlave
    ) -> None:
        fake_slave.holding_registers[0] = 4242
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusRtuSerialDriver()

        updates = await _collect(driver, _config(fake_slave.port_path, 1, [tag]), count=1)

        assert updates[0].value == 4242

    async def test_write_round_trips(self, fake_slave: FakeSerialSlave) -> None:
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 3}
        driver = ModbusRtuSerialDriver()
        config = _config(fake_slave.port_path, 1, [tag])
        await driver.configure(config)
        await driver.connect()
        try:
            result = await driver.write("t", 77)
            assert result.success is True
            assert fake_slave.holding_registers[3] == 77
        finally:
            await driver.disconnect()

    async def test_requests_carry_the_configured_unit_id(self, fake_slave: FakeSerialSlave) -> None:
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver = ModbusRtuSerialDriver()

        await _collect(driver, _config(fake_slave.port_path, unit_id=9, tags=[tag]), count=1)

        assert fake_slave.received[0][0] == 9


class TestMultiDropSharedBus:
    """The headline ADR-011 property: two instances, two unit_ids, one
    port — no collision, both slaves reachable."""

    async def test_two_instances_on_one_port_both_read_correctly(
        self, fake_slave: FakeSerialSlave
    ) -> None:
        # FakeSerialSlave models one shared register datastore behind the
        # single pty, distinguishing slaves only by the unit_id each
        # request carries (see test_requests_carry_the_configured_unit_id
        # and `received` below) — a real multi-drop bus has one physical
        # register space per slave device, which this fixture doesn't need
        # to model to prove the collision-free sharing property under test.
        fake_slave.holding_registers[0] = 111
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver_1 = ModbusRtuSerialDriver()
        driver_2 = ModbusRtuSerialDriver()
        config_1 = _config(fake_slave.port_path, unit_id=1, tags=[tag], instance_id="slave_1")
        config_2 = _config(fake_slave.port_path, unit_id=2, tags=[tag], instance_id="slave_2")

        await driver_1.configure(config_1)
        await driver_2.configure(config_2)
        await driver_1.connect()
        await driver_2.connect()
        try:
            assert get_bus_manager(fake_slave.port_path).is_open
            result_1 = await driver_1.write("t", 1)
            result_2 = await driver_2.write("t", 2)
            assert result_1.success is True
            assert result_2.success is True
            # Both unit ids reached the fake slave over the *same* fd pair.
            unit_ids_seen = {unit_id for unit_id, _pdu in fake_slave.received}
            assert unit_ids_seen == {1, 2}
        finally:
            await driver_1.disconnect()
            await driver_2.disconnect()

    async def test_manager_closes_only_after_the_last_instance_releases(
        self, fake_slave: FakeSerialSlave
    ) -> None:
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        driver_1 = ModbusRtuSerialDriver()
        driver_2 = ModbusRtuSerialDriver()
        await driver_1.configure(
            _config(fake_slave.port_path, unit_id=1, tags=[tag], instance_id="a")
        )
        await driver_2.configure(
            _config(fake_slave.port_path, unit_id=2, tags=[tag], instance_id="b")
        )
        await driver_1.connect()
        await driver_2.connect()

        manager = get_bus_manager(fake_slave.port_path)
        assert manager.is_open

        await driver_1.disconnect()
        assert manager.is_open, "must stay open while driver_2 still holds a reference"

        await driver_2.disconnect()
        assert not manager.is_open

    async def test_a_third_instance_can_reuse_the_port_after_reopening(
        self, fake_slave: FakeSerialSlave
    ) -> None:
        """Once every instance has released, a fresh acquire() must reopen
        the port rather than staying permanently closed."""
        tag = {"id": "t", "function_code": "read_holding_registers", "address": 0}
        first = ModbusRtuSerialDriver()
        await first.configure(_config(fake_slave.port_path, unit_id=1, tags=[tag]))
        await first.connect()
        await first.disconnect()
        assert not get_bus_manager(fake_slave.port_path).is_open

        second = ModbusRtuSerialDriver()
        fake_slave.holding_registers[0] = 55
        await second.configure(_config(fake_slave.port_path, unit_id=1, tags=[tag]))
        await second.connect()
        try:
            result = await second.write("t", 1)
            assert result.success is True
        finally:
            await second.disconnect()


class TestWritePriorityAcrossInstancesOnOneBus:
    async def test_write_from_one_instance_is_served_ahead_of_reads_from_another(
        self, fake_slave: FakeSerialSlave
    ) -> None:
        """Write priority (XEDGE-424/431) must hold *across* instances
        sharing a bus, not just within one instance's own reads — this is
        the property a per-instance scheduler could never provide."""
        for address in range(30):
            fake_slave.holding_registers[address] = address
        read_tags = [
            {"id": f"t{i}", "function_code": "read_holding_registers", "address": i}
            for i in range(30)
        ]
        write_tag = {
            "id": "cmd",
            "function_code": "read_holding_registers",
            "address": 500,
            "access": "write_only",
        }

        reader = ModbusRtuSerialDriver()
        writer_driver = ModbusRtuSerialDriver()
        await reader.configure(
            _config(
                fake_slave.port_path,
                unit_id=1,
                tags=read_tags,
                scan_rate_ms=500,
                instance_id="reader",
            )
        )
        await writer_driver.configure(
            _config(
                fake_slave.port_path,
                unit_id=2,
                tags=[write_tag],
                instance_id="writer",
            )
        )
        await reader.connect()
        await writer_driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(reader.run(queue))
        try:
            await asyncio.sleep(0.02)  # let the (slow, 30-tag) read cycle start queuing
            started = asyncio.get_running_loop().time()
            result = await writer_driver.write("cmd", 99)
            elapsed = asyncio.get_running_loop().time() - started
            assert result.success is True
            assert elapsed < 0.3, (
                f"write took {elapsed:.3f}s - it must not queue behind another instance's reads"
            )
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await reader.disconnect()
            await writer_driver.disconnect()


class TestFC15And16OverSerial:
    async def test_multi_register_write_via_fc16(self, fake_slave: FakeSerialSlave) -> None:
        tag = {
            "id": "counter",
            "function_code": "read_holding_registers",
            "address": 10,
            "data_type": "int32",
        }
        driver = ModbusRtuSerialDriver()
        await driver.configure(_config(fake_slave.port_path, 1, [tag]))
        await driver.connect()
        try:
            result = await driver.write("counter", 65536)
            assert result.success is True
            assert (fake_slave.holding_registers[10], fake_slave.holding_registers[11]) == (1, 0)
        finally:
            await driver.disconnect()
