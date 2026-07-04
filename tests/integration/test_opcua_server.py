"""OpcUaTagServer, verified by reading back values with a plain asyncua
Client — the same library the server is built on, used here purely as a
verification reader (not testing our own client driver)."""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import pytest
from asyncua import Client

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.northbound.opcua_server import OpcUaServerConfig, OpcUaServerStateError, OpcUaTagServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _tag(tag_id: str, value: object, quality: Quality = Quality.GOOD) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        data_type="FLOAT64",
        quality=quality,
        source_driver=tag_id.split("/")[0],
        source_address="0",
    )


@pytest.fixture
def endpoint_url() -> str:
    return f"opc.tcp://127.0.0.1:{_free_port()}/xedge/"


async def _get_tag_node(client: Client, idx: int, driver_id: str, tag_name: str):  # type: ignore[no-untyped-def]
    return await client.get_objects_node().get_child(
        [f"{idx}:xEdge", f"{idx}:{driver_id}", f"{idx}:{tag_name}"]
    )


async def test_server_exposes_prebuilt_tags_with_initial_value(endpoint_url: str) -> None:
    server = OpcUaTagServer(
        OpcUaServerConfig(
            endpoint_url=endpoint_url, initial_values={"modbus_tcp_01/temperature_01": 0.0}
        )
    )
    await server.start()
    try:
        async with Client(url=endpoint_url) as client:
            node = await _get_tag_node(
                client, server.namespace_index, "modbus_tcp_01", "temperature_01"
            )
            assert await node.read_value() == 0.0
    finally:
        await server.stop()


async def test_update_tag_writes_value_quality_and_timestamp(endpoint_url: str) -> None:
    tag_id = "modbus_tcp_01/temperature_01"
    server = OpcUaTagServer(
        OpcUaServerConfig(endpoint_url=endpoint_url, initial_values={tag_id: 0.0})
    )
    await server.start()
    try:
        await server.update_tag(_tag(tag_id, 85.3))

        async with Client(url=endpoint_url) as client:
            node = await _get_tag_node(
                client, server.namespace_index, "modbus_tcp_01", "temperature_01"
            )
            data_value = await node.read_data_value()
            assert data_value.Value.Value == pytest.approx(85.3)
            assert data_value.StatusCode.is_good()
    finally:
        await server.stop()


async def test_update_tag_bad_quality_reflected_in_status_code(endpoint_url: str) -> None:
    tag_id = "modbus_tcp_01/temperature_01"
    server = OpcUaTagServer(
        OpcUaServerConfig(endpoint_url=endpoint_url, initial_values={tag_id: 0.0})
    )
    await server.start()
    try:
        await server.update_tag(_tag(tag_id, 0.0, quality=Quality.BAD))

        async with Client(url=endpoint_url) as client:
            node = await _get_tag_node(
                client, server.namespace_index, "modbus_tcp_01", "temperature_01"
            )
            data_value = await node.read_data_value(raise_on_bad_status=False)
            assert data_value.StatusCode.is_bad()
    finally:
        await server.stop()


async def test_update_tag_for_unconfigured_tag_is_ignored(endpoint_url: str) -> None:
    server = OpcUaTagServer(OpcUaServerConfig(endpoint_url=endpoint_url, initial_values={"a/b": 0}))
    await server.start()
    try:
        # should not raise, even though "other/tag" was never pre-built
        await server.update_tag(_tag("other/tag", 1))
    finally:
        await server.stop()


async def test_update_tag_before_start_raises() -> None:
    server = OpcUaTagServer(
        OpcUaServerConfig(endpoint_url="opc.tcp://127.0.0.1:0/x/", initial_values={})
    )
    with pytest.raises(OpcUaServerStateError):
        await server.update_tag(_tag("a/b", 1))


async def test_multiple_drivers_get_separate_folders(endpoint_url: str) -> None:
    server = OpcUaTagServer(
        OpcUaServerConfig(
            endpoint_url=endpoint_url,
            initial_values={"driver_a/tag1": 0, "driver_b/tag1": 0},
        )
    )
    await server.start()
    try:
        async with Client(url=endpoint_url) as client:
            objects = client.get_objects_node()
            xedge = await objects.get_child([f"{server.namespace_index}:xEdge"])
            children = await xedge.get_children()
            names = {(await c.read_browse_name()).Name for c in children}
            assert names == {"driver_a", "driver_b"}
    finally:
        await server.stop()


async def test_boolean_tag_roundtrip(endpoint_url: str) -> None:
    tag_id = "modbus_tcp_01/pump_running"
    server = OpcUaTagServer(
        OpcUaServerConfig(endpoint_url=endpoint_url, initial_values={tag_id: False})
    )
    await server.start()
    try:
        await server.update_tag(_tag(tag_id, True))
        async with Client(url=endpoint_url) as client:
            node = await _get_tag_node(
                client, server.namespace_index, "modbus_tcp_01", "pump_running"
            )
            assert await node.read_value() is True
    finally:
        await server.stop()
