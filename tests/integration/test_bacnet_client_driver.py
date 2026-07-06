"""BacnetIpDriver against a real bacpypes3-backed BACnet/IP device (test
double — see tests/fixtures/bacnet_device.py)."""

from __future__ import annotations

import asyncio
import socket

import pytest

from xedge.drivers.bacnet.client import BacnetIpDriver
from xedge.drivers.base import DriverConfig, DriverConnectionError, Quality, TagUpdate


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _driver_config(tags: list[dict], scan_rate_ms: int = 100, **config_overrides: object) -> DriverConfig:
    return DriverConfig(
        instance_id="bacnet_01",
        driver_type="bacnet_ip",
        config={
            "local_address": f"127.0.0.1:{_free_udp_port()}",
            "device_instance": 3100001,
            "request_timeout_seconds": 1.0,
            **config_overrides,
        },
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _run_one_cycle(driver: BacnetIpDriver, config: DriverConfig) -> list[TagUpdate]:
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        updates = [
            await asyncio.wait_for(queue.get(), timeout=3.0) for _ in config.tag_groups[0]["tags"]
        ]
        return updates
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_reads_analog_value(bacnet_test_device: tuple[str, str, str]) -> None:
    device_address, analog_object_id, _binary_object_id = bacnet_test_device
    config = _driver_config(
        [{"id": "temperature_01", "device_address": device_address, "object_identifier": analog_object_id}]
    )
    driver = BacnetIpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == pytest.approx(85.3, abs=1e-3)
    assert updates[0].quality == Quality.GOOD
    assert updates[0].tag_id == "bacnet_01/temperature_01"


async def test_reads_binary_value_as_bool(bacnet_test_device: tuple[str, str, str]) -> None:
    device_address, _analog_object_id, binary_object_id = bacnet_test_device
    config = _driver_config(
        [{"id": "pump_running", "device_address": device_address, "object_identifier": binary_object_id}]
    )
    driver = BacnetIpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value is True


async def test_unknown_object_maps_to_bad_quality(bacnet_test_device: tuple[str, str, str]) -> None:
    device_address, _analog_object_id, _binary_object_id = bacnet_test_device
    config = _driver_config(
        [{"id": "missing", "device_address": device_address, "object_identifier": "analog-value,99"}]
    )
    driver = BacnetIpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD
    assert updates[0].value == 0


async def test_unreachable_device_maps_to_bad_quality(bacnet_test_device: tuple[str, str, str]) -> None:
    # Unlike OPC UA (a persistent TCP session), BACnet/IP has no connect-time
    # handshake with a remote device — an unreachable device only ever
    # surfaces per-tag, at read time, never as a connect() failure.
    _device_address, analog_object_id, _binary_object_id = bacnet_test_device
    nothing_listening = f"127.0.0.1:{_free_udp_port()}"
    config = _driver_config(
        [{"id": "unreachable", "device_address": nothing_listening, "object_identifier": analog_object_id}]
    )
    driver = BacnetIpDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD


async def test_connect_raises_on_local_bind_conflict(bacnet_test_device: tuple[str, str, str]) -> None:
    device_address, _analog_object_id, _binary_object_id = bacnet_test_device
    # device_address is already bound by the fixture's own Application —
    # binding a second Application to the same local address must fail.
    driver = BacnetIpDriver()
    config = DriverConfig(
        instance_id="conflict",
        driver_type="bacnet_ip",
        config={"local_address": device_address, "device_instance": 3100002},
        tag_groups=[],
    )
    await driver.configure(config)
    with pytest.raises(DriverConnectionError):
        await driver.connect()


async def test_write_returns_not_supported(bacnet_test_device: tuple[str, str, str]) -> None:
    device_address, analog_object_id, _binary_object_id = bacnet_test_device
    driver = BacnetIpDriver()
    config = _driver_config(
        [{"id": "t1", "device_address": device_address, "object_identifier": analog_object_id}]
    )
    await driver.configure(config)
    await driver.connect()
    try:
        result = await driver.write("some_tag", 1)
        assert result.success is False
    finally:
        await driver.disconnect()


async def test_get_metrics_tracks_reads_and_errors(bacnet_test_device: tuple[str, str, str]) -> None:
    device_address, analog_object_id, _binary_object_id = bacnet_test_device
    config = _driver_config(
        [
            {"id": "good", "device_address": device_address, "object_identifier": analog_object_id},
            {"id": "bad", "device_address": device_address, "object_identifier": "analog-value,99"},
        ]
    )
    driver = BacnetIpDriver()
    await _run_one_cycle(driver, config)

    metrics = driver.get_metrics()
    assert metrics.tag_read_count >= 1
    assert metrics.error_count >= 1


async def test_read_produces_driver_read_span(
    bacnet_test_device: tuple[str, str, str], otel_test_tracer_provider
) -> None:
    device_address, analog_object_id, _binary_object_id = bacnet_test_device
    config = _driver_config(
        [{"id": "span_tag", "device_address": device_address, "object_identifier": analog_object_id}]
    )
    driver = BacnetIpDriver()
    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["driver.instance_id"] == "bacnet_01"
    assert spans[0].attributes["tag.id"] == "span_tag"
    assert spans[0].attributes["quality"] == Quality.GOOD.value


async def test_unknown_object_produces_error_span(
    bacnet_test_device: tuple[str, str, str], otel_test_tracer_provider
) -> None:
    device_address, _analog_object_id, _binary_object_id = bacnet_test_device
    config = _driver_config(
        [{"id": "missing", "device_address": device_address, "object_identifier": "analog-value,99"}]
    )
    driver = BacnetIpDriver()
    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["quality"] == Quality.BAD.value
    assert spans[0].status.status_code.name == "ERROR"
