"""OpcUaClientDriver against a real asyncua-backed OPC UA server (test
double — see tests/fixtures/opcua_server.py)."""

from __future__ import annotations

import asyncio

import pytest
from asyncua import Server, ua

from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.opcua.client import OpcUaClientDriver


def _driver_config(endpoint_url: str, tags: list[dict], scan_rate_ms: int = 100) -> DriverConfig:
    return DriverConfig(
        instance_id="opcua_01",
        driver_type="opcua_client",
        config={"endpoint_url": endpoint_url},
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _run_one_cycle(driver: OpcUaClientDriver, config: DriverConfig) -> list[TagUpdate]:
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


async def test_reads_float_node(opcua_test_server: tuple[Server, str, int]) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "Temperature", 85.3)

    config = _driver_config(
        endpoint_url, [{"id": "temperature_01", "node_id": node.nodeid.to_string()}]
    )
    driver = OpcUaClientDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == pytest.approx(85.3)
    assert updates[0].quality == Quality.GOOD
    assert updates[0].tag_id == "opcua_01/temperature_01"


async def test_reads_bool_node(opcua_test_server: tuple[Server, str, int]) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "PumpRunning", True)

    config = _driver_config(
        endpoint_url, [{"id": "pump_running", "node_id": node.nodeid.to_string()}]
    )
    driver = OpcUaClientDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value is True


async def test_reads_int_node(opcua_test_server: tuple[Server, str, int]) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "Counter", 42)

    config = _driver_config(endpoint_url, [{"id": "counter", "node_id": node.nodeid.to_string()}])
    driver = OpcUaClientDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == 42


async def test_source_timestamp_propagates(opcua_test_server: tuple[Server, str, int]) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "Value", 1.0)

    config = _driver_config(endpoint_url, [{"id": "value", "node_id": node.nodeid.to_string()}])
    driver = OpcUaClientDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].source_timestamp is not None


async def test_bad_status_code_maps_to_bad_quality(
    opcua_test_server: tuple[Server, str, int],
) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "FaultyValue", 0)
    await node.write_value(
        ua.DataValue(
            ua.Variant(0, ua.VariantType.Int64),
            StatusCode=ua.StatusCode(ua.StatusCodes.BadDeviceFailure),
        )
    )

    config = _driver_config(endpoint_url, [{"id": "faulty", "node_id": node.nodeid.to_string()}])
    driver = OpcUaClientDriver()
    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD


async def test_connect_to_unreachable_endpoint_raises() -> None:
    driver = OpcUaClientDriver()
    config = DriverConfig(
        instance_id="unreachable",
        driver_type="opcua_client",
        config={"endpoint_url": "opc.tcp://127.0.0.1:1/nonexistent/", "connect_timeout_seconds": 1},
        tag_groups=[],
    )
    await driver.configure(config)
    with pytest.raises((ConnectionError, OSError, asyncio.TimeoutError)):
        await driver.connect()


async def test_write_returns_not_supported(opcua_test_server: tuple[Server, str, int]) -> None:
    _server, endpoint_url, _idx = opcua_test_server
    driver = OpcUaClientDriver()
    config = _driver_config(endpoint_url, [])
    await driver.configure(config)
    await driver.connect()
    try:
        result = await driver.write("some_tag", 1)
        assert result.success is False
    finally:
        await driver.disconnect()


async def test_read_produces_driver_read_span(
    opcua_test_server: tuple[Server, str, int], otel_test_tracer_provider
) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "SpanTest", 1.0)

    config = _driver_config(endpoint_url, [{"id": "span_tag", "node_id": node.nodeid.to_string()}])
    driver = OpcUaClientDriver()
    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["driver.instance_id"] == "opcua_01"
    assert spans[0].attributes["tag.id"] == "span_tag"
    assert spans[0].attributes["quality"] == Quality.GOOD.value


async def test_bad_status_code_produces_error_span(
    opcua_test_server: tuple[Server, str, int], otel_test_tracer_provider
) -> None:
    server, endpoint_url, idx = opcua_test_server
    objects = server.get_objects_node()
    node = await objects.add_variable(idx, "SpanFaultyValue", 0)
    await node.write_value(
        ua.DataValue(
            ua.Variant(0, ua.VariantType.Int64),
            StatusCode=ua.StatusCode(ua.StatusCodes.BadDeviceFailure),
        )
    )

    config = _driver_config(endpoint_url, [{"id": "faulty", "node_id": node.nodeid.to_string()}])
    driver = OpcUaClientDriver()
    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["quality"] == Quality.BAD.value
