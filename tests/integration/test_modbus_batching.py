"""Sprint C1 polling-loop tests: block batching, fixed-period scheduling,
per-tag fallback, retries and multi-register decoding (XEDGE-410/411/412/414).

These go through the real driver against the fake server so the wire traffic
is observable — `FakeModbusServer.request_log` records every request, which is
what makes "batched into one round trip" an assertion rather than a claim.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

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
    scan_rate_ms: int = 50,
    group_extra: dict[str, Any] | None = None,
    config_extra: dict[str, Any] | None = None,
) -> DriverConfig:
    return DriverConfig(
        instance_id="modbus_batch_test",
        driver_type="modbus_tcp",
        config={"host": server.host, "port": server.port, "unit_id": 1, **(config_extra or {})},
        tag_groups=[
            {
                "id": "group1",
                "scan_rate_ms": scan_rate_ms,
                "tags": tags,
                **(group_extra or {}),
            }
        ],
    )


async def _collect(driver: ModbusTcpDriver, config: DriverConfig, count: int) -> list[TagUpdate]:
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


class TestBatching:
    async def test_contiguous_tags_cost_one_round_trip(self, fake_server: FakeModbusServer) -> None:
        """The headline XEDGE-411 claim, asserted on actual wire traffic:
        20 contiguous tags used to be 20 requests per cycle."""
        for address in range(20):
            fake_server.holding_registers[address] = address * 10
        tags = [_tag(f"t{i}", i) for i in range(20)]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=20)

        assert fake_server.read_request_count == 1
        assert fake_server.request_log[0] == (codec.FunctionCode.READ_HOLDING_REGISTERS, 0, 20)
        assert {u.tag_id.split("/")[1]: u.value for u in updates} == {
            f"t{i}": i * 10 for i in range(20)
        }

    async def test_every_value_lands_on_the_right_tag(self, fake_server: FakeModbusServer) -> None:
        """A coalescing off-by-one produces plausible-but-wrong values — each
        tag silently reading its neighbour's register. Distinct values per
        address make that visible."""
        for address in range(10):
            fake_server.holding_registers[address] = 1000 + address
        tags = [_tag(f"t{i}", i) for i in range(10)]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=10)

        for update in updates:
            tag_id = update.tag_id.split("/")[1]
            expected_address = int(tag_id[1:])
            assert update.value == 1000 + expected_address, f"{tag_id} read the wrong register"

    async def test_gap_produces_two_requests_by_default(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.holding_registers[0] = 11
        fake_server.holding_registers[100] = 22
        tags = [_tag("near", 0), _tag("far", 100)]

        await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=2)

        assert fake_server.read_request_count == 2

    async def test_gap_tolerance_merges_them(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[0] = 11
        fake_server.holding_registers[4] = 22
        tags = [_tag("a", 0), _tag("b", 4)]

        updates = await _collect(
            ModbusTcpDriver(),
            _config(fake_server, tags, group_extra={"max_block_gap": 8}),
            count=2,
        )

        assert fake_server.read_request_count == 1
        assert fake_server.request_log[0] == (codec.FunctionCode.READ_HOLDING_REGISTERS, 0, 5)
        assert sorted(u.value for u in updates) == [11, 22]

    async def test_max_block_size_splits_requests(self, fake_server: FakeModbusServer) -> None:
        for address in range(20):
            fake_server.holding_registers[address] = address
        tags = [_tag(f"t{i}", i) for i in range(20)]

        await _collect(
            ModbusTcpDriver(),
            _config(fake_server, tags, group_extra={"max_block_size": 8}),
            count=20,
        )

        quantities = [q for fc, _, q in fake_server.request_log if fc == 0x03]
        assert quantities[:3] == [8, 8, 4]

    async def test_batching_is_recorded_in_metadata(self, fake_server: FakeModbusServer) -> None:
        """`batched_with` lets an operator confirm batching is actually
        happening for a given tag, rather than trusting the config."""
        tags = [_tag(f"t{i}", i) for i in range(3)]
        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=3)
        assert all(u.metadata["batched_with"] == 2 for u in updates)

    async def test_unbatched_tag_reports_no_batch_partners(
        self, fake_server: FakeModbusServer
    ) -> None:
        updates = await _collect(ModbusTcpDriver(), _config(fake_server, [_tag("solo", 0)]), 1)
        assert updates[0].metadata["batched_with"] == 0


class TestMultiRegisterDecoding:
    async def test_float32_decoded_from_two_registers(self, fake_server: FakeModbusServer) -> None:
        # 1.0f is 0x3F800000.
        fake_server.holding_registers[0] = 0x3F80
        fake_server.holding_registers[1] = 0x0000
        tags = [_tag("flow", 0, data_type="float32")]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=1)

        assert updates[0].value == 1.0
        assert fake_server.request_log[0][2] == 2, "a float32 must request 2 registers"

    async def test_word_swapped_device(self, fake_server: FakeModbusServer) -> None:
        fake_server.holding_registers[0] = 0x0000
        fake_server.holding_registers[1] = 0x3F80
        tags = [_tag("flow", 0, data_type="float32", word_order="little")]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=1)

        assert updates[0].value == 1.0

    async def test_int32_and_uint16_in_one_batched_read(
        self, fake_server: FakeModbusServer
    ) -> None:
        """Mixed widths in a single request — the case where an offset bug
        would corrupt the narrow tag."""
        fake_server.holding_registers[0] = 0xFFFF
        fake_server.holding_registers[1] = 0xFFFF  # int32 at 0 == -1
        fake_server.holding_registers[2] = 4242
        tags = [_tag("counter", 0, data_type="int32"), _tag("plain", 2)]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=2)

        assert fake_server.read_request_count == 1
        assert fake_server.request_log[0] == (codec.FunctionCode.READ_HOLDING_REGISTERS, 0, 3)
        by_tag = {u.tag_id.split("/")[1]: u.value for u in updates}
        assert by_tag == {"counter": -1, "plain": 4242}

    async def test_bit_functions_still_read_as_bools(self, fake_server: FakeModbusServer) -> None:
        fake_server.coils[0] = True
        fake_server.coils[1] = False
        fake_server.coils[2] = True
        tags = [_tag(f"c{i}", i, function_code="read_coils") for i in range(3)]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=3)

        assert fake_server.read_request_count == 1
        by_tag = {u.tag_id.split("/")[1]: u.value for u in updates}
        assert by_tag == {"c0": True, "c1": False, "c2": True}


class TestExceptionFallback:
    async def test_one_bad_address_does_not_poison_its_neighbours(
        self, fake_server: FakeModbusServer
    ) -> None:
        """The cost of batching is error precision: a device rejects the whole
        block for one unmapped register. The fallback re-reads tag by tag so
        only the genuinely bad tag goes Bad — preserving the behaviour the
        unbatched loop had.
        """
        for address in range(4):
            fake_server.holding_registers[address] = 100 + address
        # The block read starting at 0 is rejected; individual reads succeed
        # except at address 2.
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 2)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        tags = [_tag(f"t{i}", i) for i in range(4)]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=4)

        by_tag = {u.tag_id.split("/")[1]: u for u in updates}
        assert by_tag["t2"].quality is Quality.BAD
        assert by_tag["t1"].quality is Quality.GOOD
        assert by_tag["t3"].quality is Quality.GOOD
        assert by_tag["t3"].value == 103

    async def test_bad_tag_carries_a_readable_exception_name(
        self, fake_server: FakeModbusServer
    ) -> None:
        """XEDGE-425: before Sprint C1 only the raw numeric code reached the
        operator."""
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 7)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        updates = await _collect(ModbusTcpDriver(), _config(fake_server, [_tag("t", 7)]), 1)

        assert updates[0].quality is Quality.BAD
        assert updates[0].metadata["modbus_exception_name"] == "ILLEGAL_DATA_ADDRESS"
        assert updates[0].metadata["modbus_exception"] == codec.ExceptionCode.ILLEGAL_DATA_ADDRESS

    async def test_float_tag_gets_a_float_placeholder_when_bad(
        self, fake_server: FakeModbusServer
    ) -> None:
        """A Bad float tag reporting int 0 would change its OPC UA node type
        and Sparkplug metric type mid-stream."""
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        tags = [_tag("flow", 0, data_type="float32")]

        updates = await _collect(ModbusTcpDriver(), _config(fake_server, tags), count=1)

        assert updates[0].quality is Quality.BAD
        assert isinstance(updates[0].value, float)


class TestRetries:
    async def test_no_retry_on_exception_by_default(self, fake_server: FakeModbusServer) -> None:
        """ILLEGAL_DATA_ADDRESS answers the same way every time; retrying it
        only spends bus time."""
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.ILLEGAL_DATA_ADDRESS
        )
        await _collect(
            ModbusTcpDriver(),
            _config(fake_server, [_tag("t", 0)], config_extra={"retry_count": 3}),
            count=1,
        )
        assert fake_server.read_request_count == 1

    async def test_retry_on_exception_when_enabled(self, fake_server: FakeModbusServer) -> None:
        fake_server.exceptions[(codec.FunctionCode.READ_HOLDING_REGISTERS, 0)] = (
            codec.ExceptionCode.SLAVE_DEVICE_BUSY
        )
        await _collect(
            ModbusTcpDriver(),
            _config(
                fake_server,
                [_tag("t", 0)],
                config_extra={
                    "retry_count": 2,
                    "retry_on_exception": True,
                    "retry_backoff_seconds": 0,
                },
            ),
            count=1,
        )
        assert fake_server.read_request_count == 3, "initial attempt plus 2 retries"


class TestFixedPeriodScheduling:
    async def test_period_does_not_drift_with_read_latency(
        self, fake_server: FakeModbusServer
    ) -> None:
        """XEDGE-410. The old loop slept `scan_rate_ms` *after* the reads, so
        the true period was `read_time + scan_rate`. Here a deliberately slow
        transport would have pushed a 60 ms group out to ~110 ms per cycle;
        the deadline-based loop absorbs the latency instead.
        """
        read_delay = 0.05
        original_read_block = ModbusTcpDriver._read_block

        async def slow_read_block(
            self: ModbusTcpDriver, function_code: Any, address: int, quantity: int
        ) -> Any:
            await asyncio.sleep(read_delay)
            return await original_read_block(self, function_code, address, quantity)

        driver = ModbusTcpDriver()
        driver._read_block = slow_read_block.__get__(driver, ModbusTcpDriver)  # type: ignore[method-assign]  # noqa: SLF001

        config = _config(fake_server, [_tag("t", 0)], scan_rate_ms=60)
        await driver.configure(config)
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            cycles = 4
            timestamps = []
            for _ in range(cycles):
                await asyncio.wait_for(queue.get(), timeout=3.0)
                timestamps.append(asyncio.get_running_loop().time())
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

        intervals = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False)]
        average = sum(intervals) / len(intervals)
        # The old behaviour would average ~0.11s here (0.06 sleep + 0.05 read).
        assert 0.05 <= average <= 0.085, f"period drifted to {average:.3f}s (expected ~0.06s)"

    async def test_overrun_skips_cycles_rather_than_queueing_them(
        self, fake_server: FakeModbusServer
    ) -> None:
        """When a cycle cannot fit its slot, catching up back-to-back would
        saturate the bus exactly when it is already too slow. Reads should
        stay spaced by the read time, not pile up."""
        original_read_block = ModbusTcpDriver._read_block

        async def slow_read_block(
            self: ModbusTcpDriver, function_code: Any, address: int, quantity: int
        ) -> Any:
            await asyncio.sleep(0.05)
            return await original_read_block(self, function_code, address, quantity)

        driver = ModbusTcpDriver()
        driver._read_block = slow_read_block.__get__(driver, ModbusTcpDriver)  # type: ignore[method-assign]  # noqa: SLF001

        # 10ms requested, ~50ms achievable: a permanent overrun.
        config = _config(fake_server, [_tag("t", 0)], scan_rate_ms=10)
        await driver.configure(config)
        await driver.connect()
        queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
        run_task = asyncio.create_task(driver.run(queue))
        try:
            started = asyncio.get_running_loop().time()
            for _ in range(3):
                await asyncio.wait_for(queue.get(), timeout=3.0)
            elapsed = asyncio.get_running_loop().time() - started
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            await driver.disconnect()

        assert elapsed >= 0.1, "cycles must remain spaced by the achievable read time"
        assert fake_server.read_request_count <= 5, "overrun must not queue up extra reads"
