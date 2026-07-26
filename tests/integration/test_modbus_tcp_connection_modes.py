"""Sprint C3 `connection_mode`/keepalive/connection-retry tests (XEDGE-436):
`persistent` (default, one connection reused for every transaction) vs
`on_demand` (a fresh connection dialed and closed per transaction), TCP
keepalive tuning, and retrying a failed connection *attempt* — distinct
from `retry_count`, which retries a read on an already-open connection
(see test_modbus_batching.py's TestRetries).
"""

from __future__ import annotations

import asyncio
import socket
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


def _unused_tcp_port() -> int:
    """A real, currently-free port that will refuse connections — used to
    exercise connection failure/retry without mocking anything."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


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
    scan_rate_ms: int = 20,
    **config_extra: Any,
) -> DriverConfig:
    return DriverConfig(
        instance_id="modbus_conn_mode_test",
        driver_type="modbus_tcp",
        config={"host": server.host, "port": server.port, "unit_id": 1, **config_extra},
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
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


class TestConnectionMode:
    async def test_default_is_persistent_one_connection_for_many_reads(
        self, fake_server: FakeModbusServer
    ) -> None:
        """No `connection_mode` key at all — the exact shape every config
        written before XEDGE-436 has — must behave exactly as before: one
        connection, reused."""
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)])

        async with _running_driver(config) as (_driver, queue):
            for _ in range(3):
                await asyncio.wait_for(queue.get(), timeout=3.0)

        assert fake_server.connection_count == 1

    async def test_persistent_mode_holds_one_connection_across_reads(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)], connection_mode="persistent")

        async with _running_driver(config) as (_driver, queue):
            for _ in range(3):
                await asyncio.wait_for(queue.get(), timeout=3.0)

        assert fake_server.connection_count == 1

    async def test_on_demand_mode_opens_a_new_connection_per_transaction(
        self, fake_server: FakeModbusServer
    ) -> None:
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)], connection_mode="on_demand")

        async with _running_driver(config) as (_driver, queue):
            for _ in range(3):
                await asyncio.wait_for(queue.get(), timeout=3.0)

        assert fake_server.connection_count >= 3

    async def test_on_demand_mode_holds_no_connection_between_reads(
        self, fake_server: FakeModbusServer
    ) -> None:
        """The defining difference from `persistent`: nothing is left open
        once a transaction finishes, not just "a new one gets used next
        time." """
        fake_server.holding_registers[0] = 1
        config = _config(fake_server, [_tag("t", 0)], connection_mode="on_demand")

        async with _running_driver(config) as (driver, queue):
            await asyncio.wait_for(queue.get(), timeout=3.0)
            assert driver._reader is None  # noqa: SLF001
            assert driver._writer is None  # noqa: SLF001

    async def test_on_demand_writes_also_use_a_fresh_connection(
        self, fake_server: FakeModbusServer
    ) -> None:
        config = _config(fake_server, [_tag("t", 0)], connection_mode="on_demand")
        driver = ModbusTcpDriver()
        await driver.configure(config)
        await driver.connect()

        result = await driver.write("t", 7)

        assert result.success is True
        assert fake_server.holding_registers[0] == 7
        assert fake_server.connection_count == 1
        await driver.disconnect()


class TestConnectionRetryCount:
    async def test_default_zero_retries_fails_immediately(self) -> None:
        driver = ModbusTcpDriver()
        config = DriverConfig(
            instance_id="x",
            driver_type="modbus_tcp",
            config={
                "host": "127.0.0.1",
                "port": _unused_tcp_port(),
                "connect_timeout_seconds": 1,
            },
            tag_groups=[],
        )
        await driver.configure(config)

        with pytest.raises(OSError):
            await driver.connect()

    async def test_configured_retries_delay_the_eventual_failure(self) -> None:
        """A closed port refuses instantly, so measurable elapsed time can
        only come from the retry backoff actually running `connection_retry_
        count` times — proof the loop iterates the configured number of
        times rather than merely accepting the option."""
        driver = ModbusTcpDriver()
        config = DriverConfig(
            instance_id="x",
            driver_type="modbus_tcp",
            config={
                "host": "127.0.0.1",
                "port": _unused_tcp_port(),
                "connect_timeout_seconds": 1,
                "connection_retry_count": 3,
                "connection_retry_backoff_seconds": 0.05,
            },
            tag_groups=[],
        )
        await driver.configure(config)

        started = asyncio.get_running_loop().time()
        with pytest.raises(OSError):
            await driver.connect()
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 3 * 0.05

    async def test_recovers_once_a_transient_failure_stops_happening(
        self, monkeypatch: pytest.MonkeyPatch, fake_server: FakeModbusServer
    ) -> None:
        real_open_connection = asyncio.open_connection
        attempts = 0

        async def flaky_open_connection(host: str, port: int) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionRefusedError("simulated transient failure")
            return await real_open_connection(host, port)

        monkeypatch.setattr(asyncio, "open_connection", flaky_open_connection)
        driver = ModbusTcpDriver()
        config = _config(
            fake_server,
            [_tag("t", 0)],
            connection_retry_count=5,
            connection_retry_backoff_seconds=0,
        )
        await driver.configure(config)

        await driver.connect()

        assert attempts == 3
        await driver.disconnect()


class TestKeepalive:
    async def test_keepalive_interval_enables_so_keepalive_on_the_socket(
        self, fake_server: FakeModbusServer
    ) -> None:
        config = _config(fake_server, [_tag("t", 0)], keepalive_interval_seconds=30)
        driver = ModbusTcpDriver()
        await driver.configure(config)

        await driver.connect()

        try:
            assert driver._writer is not None  # noqa: SLF001
            sock = driver._writer.get_extra_info("socket")  # noqa: SLF001
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        finally:
            await driver.disconnect()

    async def test_omitted_keepalive_leaves_it_off(self, fake_server: FakeModbusServer) -> None:
        """Matches behavior before this field existed."""
        config = _config(fake_server, [_tag("t", 0)])
        driver = ModbusTcpDriver()
        await driver.configure(config)

        await driver.connect()

        try:
            assert driver._writer is not None  # noqa: SLF001
            sock = driver._writer.get_extra_info("socket")  # noqa: SLF001
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0
        finally:
            await driver.disconnect()
