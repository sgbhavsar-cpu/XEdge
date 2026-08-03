"""SnmpClientDriver against a real pysnmp-backed SNMP agent (test double —
see tests/fixtures/snmp_agent.py). Unlike xedge.drivers.ethernet_ip (no
compatible simulator was found for that protocol), pysnmp's own agent-side
API makes a real local oracle straightforward — this driver is tested the
same way Modbus/OPC UA/BACnet are, against real wire traffic, not a mock.
"""

from __future__ import annotations

import asyncio

import pytest
from pysnmp.error import PySnmpError

from tests.fixtures.snmp_agent import (
    OID_COUNTER,
    OID_LIVE,
    OID_MISSING,
    OID_TEXT,
    TEST_ENTERPRISE_OID,
    FakeSnmpAgent,
)
from xedge.core.connectivity import ConnectivityState
from xedge.drivers.base import DriverConfig, DriverConnectionError, Quality, TagUpdate
from xedge.drivers.snmp.client import SnmpClientDriver, SnmpConfigError


def _driver_config(
    tags: list[dict], scan_rate_ms: int = 100, port: int = 0, **config_overrides: object
) -> DriverConfig:
    return DriverConfig(
        instance_id="snmp_01",
        driver_type="snmp_client",
        config={
            "host": "127.0.0.1",
            "port": port,
            "version": "v2c",
            "community": "public",
            "timeout_seconds": 0.5,
            "retries": 0,
            **config_overrides,
        },
        tag_groups=[{"id": "group1", "scan_rate_ms": scan_rate_ms, "tags": tags}],
    )


async def _run_one_cycle(driver: SnmpClientDriver, config: DriverConfig) -> list[TagUpdate]:
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


async def test_reads_integer_scalar(snmp_test_agent: FakeSnmpAgent) -> None:
    snmp_test_agent.set_counter(12345)
    config = _driver_config([{"id": "counter", "oid": OID_COUNTER}], port=snmp_test_agent.port)
    driver = SnmpClientDriver()

    updates = await _run_one_cycle(driver, config)

    assert updates[0].tag_id == "snmp_01/counter"
    assert updates[0].value == 12345
    assert updates[0].quality == Quality.GOOD
    assert updates[0].source_address == OID_COUNTER


async def test_reads_octet_string_scalar(snmp_test_agent: FakeSnmpAgent) -> None:
    snmp_test_agent.set_text("hello-device")
    config = _driver_config([{"id": "text", "oid": OID_TEXT}], port=snmp_test_agent.port)
    driver = SnmpClientDriver()

    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == "hello-device"
    assert updates[0].quality == Quality.GOOD


async def test_missing_oid_maps_to_bad_quality(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config([{"id": "missing", "oid": OID_MISSING}], port=snmp_test_agent.port)
    driver = SnmpClientDriver()

    updates = await _run_one_cycle(driver, config)

    assert updates[0].quality == Quality.BAD
    assert updates[0].value == 0
    assert "snmp_error" in updates[0].metadata


async def test_read_batches_multiple_tags_into_one_request(snmp_test_agent: FakeSnmpAgent) -> None:
    snmp_test_agent.set_counter(1)
    snmp_test_agent.set_text("batched")
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}, {"id": "text", "oid": OID_TEXT}],
        port=snmp_test_agent.port,
    )
    driver = SnmpClientDriver()

    updates = await _run_one_cycle(driver, config)

    by_id = {u.tag_id: u for u in updates}
    assert by_id["snmp_01/counter"].value == 1
    assert by_id["snmp_01/text"].value == "batched"


async def test_getbulk_reads_a_group(snmp_test_agent: FakeSnmpAgent) -> None:
    snmp_test_agent.set_counter(99)
    snmp_test_agent.set_text("bulk-read")
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}, {"id": "text", "oid": OID_TEXT}],
        port=snmp_test_agent.port,
    )
    config.tag_groups[0]["use_bulk"] = True
    driver = SnmpClientDriver()

    updates = await _run_one_cycle(driver, config)

    by_id = {u.tag_id: u for u in updates}
    assert by_id["snmp_01/counter"].value == 99
    assert by_id["snmp_01/text"].value == "bulk-read"


async def test_get_next_walks_into_the_scalar_instance(snmp_test_agent: FakeSnmpAgent) -> None:
    snmp_test_agent.set_counter(55)
    # The bare object OID (no trailing .0 instance index) -- GETNEXT from
    # here must land on the scalar's one instance, the same as any SNMP
    # walk tool starting from a column/object OID.
    object_oid = ".".join(map(str, (*TEST_ENTERPRISE_OID, 1)))
    config = _driver_config(
        [{"id": "counter", "oid": object_oid, "operation": "get_next"}],
        port=snmp_test_agent.port,
    )
    driver = SnmpClientDriver()

    updates = await _run_one_cycle(driver, config)

    assert updates[0].value == 55
    assert updates[0].quality == Quality.GOOD


async def test_write_success_round_trips_to_the_real_agent(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER, "data_type": "integer"}], port=snmp_test_agent.port
    )
    driver = SnmpClientDriver()
    await driver.configure(config)
    await driver.connect()

    result = await driver.write("counter", 4242)

    assert result.success is True
    # Confirm against the agent's own instance state, not just the SET
    # response -- a real round trip, not an echoed request.
    readback = await _run_one_cycle(SnmpClientDriver(), config)
    assert readback[0].value == 4242
    await driver.disconnect()


async def test_write_applies_inverse_scaling(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config(
        [
            {
                "id": "counter",
                "oid": OID_COUNTER,
                "data_type": "integer",
                "scaling": {"scale": 0.1, "offset": 5.0},
            }
        ],
        port=snmp_test_agent.port,
    )
    driver = SnmpClientDriver()
    await driver.configure(config)
    await driver.connect()

    # engineering_value = raw * scale + offset => raw = (engineering - offset) / scale
    await driver.write("counter", 15.0)

    readback = await _run_one_cycle(SnmpClientDriver(), config)
    assert readback[0].value == 100
    await driver.disconnect()


async def test_write_rejected_for_read_only_tag(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER, "data_type": "integer", "access": "read_only"}],
        port=snmp_test_agent.port,
    )
    driver = SnmpClientDriver()
    await driver.configure(config)
    await driver.connect()

    result = await driver.write("counter", 1)

    assert result.success is False
    assert "read_only" in (result.error_message or "")
    await driver.disconnect()


async def test_write_without_data_type_fails_without_sending(
    snmp_test_agent: FakeSnmpAgent,
) -> None:
    config = _driver_config([{"id": "counter", "oid": OID_COUNTER}], port=snmp_test_agent.port)
    driver = SnmpClientDriver()
    await driver.configure(config)
    await driver.connect()

    result = await driver.write("counter", 1)

    assert result.success is False
    assert "data_type" in (result.error_message or "")
    await driver.disconnect()


async def test_unreachable_device_marks_connectivity_not_connected(
    snmp_test_agent: FakeSnmpAgent,
) -> None:
    snmp_test_agent.close()  # nothing is listening on this port anymore
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}],
        port=snmp_test_agent.port,
        scan_rate_ms=50,
        consecutive_failure_threshold=2,
    )
    driver = SnmpClientDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        for _ in range(2):
            await asyncio.wait_for(queue.get(), timeout=3.0)
        assert driver.get_connectivity_state() == ConnectivityState.NOT_CONNECTED
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


async def test_use_bulk_rejected_under_v1_at_connect(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}], port=snmp_test_agent.port, version="v1"
    )
    config.tag_groups[0]["use_bulk"] = True
    driver = SnmpClientDriver()
    await driver.configure(config)

    with pytest.raises(SnmpConfigError):
        await driver.connect()


async def test_v3_privacy_without_auth_rejected_at_connect(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}],
        port=snmp_test_agent.port,
        version="v3",
        v3_username="operator",
        v3_priv_protocol="aes128",
        v3_priv_password="privpassword",
    )
    del config.config["community"]
    driver = SnmpClientDriver()
    await driver.configure(config)

    with pytest.raises(SnmpConfigError):
        await driver.connect()


async def test_dns_resolution_failure_raises_driver_connection_error() -> None:
    # getaddrinfo() for a nonexistent hostname has no timeout control
    # exposed through pysnmp's API at all -- it's bounded only by the OS
    # resolver's own (sometimes very slow) default behavior, so this test
    # bounds it itself rather than risk hanging the suite.
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}], host="this-host-does-not-resolve.invalid"
    )
    driver = SnmpClientDriver()
    await driver.configure(config)

    with pytest.raises((DriverConnectionError, PySnmpError, TimeoutError)):
        await asyncio.wait_for(driver.connect(), timeout=15.0)


async def test_get_metrics_tracks_reads_and_errors(snmp_test_agent: FakeSnmpAgent) -> None:
    config = _driver_config(
        [{"id": "counter", "oid": OID_COUNTER}, {"id": "missing", "oid": OID_MISSING}],
        port=snmp_test_agent.port,
    )
    driver = SnmpClientDriver()

    await _run_one_cycle(driver, config)

    metrics = driver.get_metrics()
    assert metrics.tag_read_count >= 1
    assert metrics.error_count >= 1


async def test_read_produces_driver_read_span(
    snmp_test_agent: FakeSnmpAgent, otel_test_tracer_provider
) -> None:
    snmp_test_agent.set_counter(1)
    config = _driver_config([{"id": "counter", "oid": OID_COUNTER}], port=snmp_test_agent.port)
    driver = SnmpClientDriver()

    await _run_one_cycle(driver, config)

    spans = [s for s in otel_test_tracer_provider.get_finished_spans() if s.name == "driver.read"]
    assert len(spans) >= 1
    assert spans[0].attributes["driver.instance_id"] == "snmp_01"
    assert spans[0].attributes["quality"] == Quality.GOOD.value


async def test_live_value_changes_are_reflected_across_polls(
    snmp_test_agent: FakeSnmpAgent,
) -> None:
    snmp_test_agent.set_live(1)
    config = _driver_config(
        [{"id": "live", "oid": OID_LIVE}], scan_rate_ms=50, port=snmp_test_agent.port
    )
    driver = SnmpClientDriver()
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        first = await asyncio.wait_for(queue.get(), timeout=3.0)
        snmp_test_agent.set_live(2)
        second = await asyncio.wait_for(queue.get(), timeout=3.0)
        assert first.value == 1
        assert second.value == 2
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()
