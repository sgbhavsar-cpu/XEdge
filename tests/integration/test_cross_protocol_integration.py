"""XEDGE-490 (Sprint H1): full cross-protocol integration test -- Modbus,
BACnet/IP, OPC UA, SNMP, and EtherNet/IP driver instances running
*concurrently* under one DriverSupervisor, one tag queue, and one
RingBufferManager, with an Asset (XEDGE-460, ADR-010) whose parameters span
all five.

Every previous sprint's integration suite tested its own protocol in
isolation. This is the first test to run all five together, which is
exactly the situation that would expose a shared-state bug or a stream-key
collision between concurrently-polled drivers -- each protocol below reports
one distinctive, known value so any cross-talk would surface as a wrong
value rather than passing silently.

EtherNet/IP's `pycomm3.LogixDriver` boundary is mocked here too, for the
same reason documented in xedge/drivers/ethernet_ip/client.py and
tests/unit/test_ethernet_ip_client.py: no compatible real CIP simulator
exists. The other four run against the same real per-protocol test doubles
their own sprint's integration suite uses (fake_modbus_server.py,
bacnet_device.py, opcua_server.py, snmp_agent.py).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from asyncua import Server
from pycomm3.tag import Tag as PycommTag

from tests.fixtures.fake_modbus_server import FakeModbusServer
from tests.fixtures.snmp_agent import OID_COUNTER, FakeSnmpAgent
from xedge.core.assets import Asset, AssetParameter, all_tag_refs, derive_asset_connection_state
from xedge.core.connectivity import ConnectivityState
from xedge.core.main import _pipeline_to_buffer
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.drivers.bacnet.client import BacnetIpDriver
from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.ethernet_ip.client import EtherNetIpDriver
from xedge.drivers.modbus.tcp import ModbusTcpDriver
from xedge.drivers.opcua.client import OpcUaClientDriver
from xedge.drivers.snmp.client import SnmpClientDriver
from xedge.store.ring_buffer import RingBufferManager

MODBUS_VALUE = 8531
BACNET_VALUE = 85.3  # tests/fixtures/bacnet_device.py's fixed preset
OPCUA_VALUE = 42.5
SNMP_VALUE = 777
ENIP_VALUE = 314.0


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakeLogixDriver:
    """Minimal stand-in for pycomm3.LogixDriver's read boundary -- see
    tests/unit/test_ethernet_ip_client.py for the full-featured version and
    the reasoning for why this protocol has no real-server test path."""

    def __init__(self, result: PycommTag) -> None:
        self._result = result

    def bind(self, path: str) -> _FakeLogixDriver:
        return self

    def open(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def read(self, *tag_names: str) -> PycommTag:
        return self._result


@dataclass
class _Cluster:
    supervisor: DriverSupervisor
    ring_buffers: RingBufferManager
    drivers_config: list[dict[str, Any]]


async def _wait_for_stream(
    ring_buffers: RingBufferManager, stream_key: str, timeout_seconds: float = 5.0
) -> list[UnifiedTag]:
    async with asyncio.timeout(timeout_seconds):
        while stream_key not in ring_buffers.stream_keys():  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    return ring_buffers.drain(stream_key)


@pytest.fixture
async def cross_protocol_cluster(
    bacnet_test_device: tuple[str, str, str],
    opcua_test_server: tuple[Server, str, int],
    snmp_test_agent: FakeSnmpAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Cluster]:
    device_address, analog_object_id, _binary_object_id = bacnet_test_device

    opcua_server, endpoint_url, idx = opcua_test_server
    objects = opcua_server.get_objects_node()
    node = await objects.add_variable(idx, "PumpSpeed", OPCUA_VALUE)
    opcua_node_id = node.nodeid.to_string()

    snmp_test_agent.set_counter(SNMP_VALUE)

    modbus_server = FakeModbusServer()
    await modbus_server.start()
    modbus_server.holding_registers[0] = MODBUS_VALUE

    fake_logix = _FakeLogixDriver(PycommTag("Setpoint_REAL", ENIP_VALUE, "REAL", None))
    monkeypatch.setattr(
        "xedge.drivers.ethernet_ip.client.LogixDriver", lambda path: fake_logix.bind(path)
    )

    drivers_config: list[dict[str, Any]] = [
        {
            "id": "modbus_01",
            "type": "modbus_tcp",
            "config": {"host": modbus_server.host, "port": modbus_server.port},
            "tag_groups": [
                {
                    "id": "group1",
                    "scan_rate_ms": 50,
                    "tags": [
                        {
                            "id": "temperature_01",
                            "function_code": "read_holding_registers",
                            "address": 0,
                        }
                    ],
                }
            ],
        },
        {
            "id": "bacnet_01",
            "type": "bacnet_ip",
            "config": {
                "local_address": f"127.0.0.1:{_free_udp_port()}",
                "device_instance": 3100001,
                "request_timeout_seconds": 1.0,
            },
            "tag_groups": [
                {
                    "id": "group1",
                    "scan_rate_ms": 100,
                    "tags": [
                        {
                            "id": "room_temp",
                            "device_address": device_address,
                            "object_identifier": analog_object_id,
                        }
                    ],
                }
            ],
        },
        {
            "id": "opcua_01",
            "type": "opcua_client",
            "config": {"endpoint_url": endpoint_url},
            "tag_groups": [
                {
                    "id": "group1",
                    "scan_rate_ms": 100,
                    "tags": [{"id": "pump_speed", "node_id": opcua_node_id}],
                }
            ],
        },
        {
            "id": "snmp_01",
            "type": "snmp_client",
            "config": {
                "host": "127.0.0.1",
                "port": snmp_test_agent.port,
                "version": "v2c",
                "community": "public",
                "timeout_seconds": 0.5,
                "retries": 0,
            },
            "tag_groups": [
                {
                    "id": "group1",
                    "scan_rate_ms": 100,
                    "tags": [{"id": "counter", "oid": OID_COUNTER}],
                }
            ],
        },
        {
            "id": "enip_01",
            "type": "ethernet_ip",
            "config": {"host": "10.0.0.5", "port": 44818, "slot": 0},
            "tag_groups": [
                {
                    "id": "group1",
                    "scan_rate_ms": 50,
                    "tags": [{"id": "setpoint", "tag_name": "Setpoint_REAL"}],
                }
            ],
        },
    ]

    tag_queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=1000)
    registry = DriverRegistry()
    registry.register("modbus_tcp", ModbusTcpDriver)
    registry.register("bacnet_ip", BacnetIpDriver)
    registry.register("opcua_client", OpcUaClientDriver)
    registry.register("snmp_client", SnmpClientDriver)
    registry.register("ethernet_ip", EtherNetIpDriver)
    supervisor = DriverSupervisor(registry, tag_queue)

    ring_buffers = RingBufferManager()
    pipeline_task = asyncio.create_task(_pipeline_to_buffer(tag_queue, ring_buffers))

    for driver_entry in drivers_config:
        supervisor.start(
            DriverConfig(
                instance_id=driver_entry["id"],
                driver_type=driver_entry["type"],
                config=driver_entry["config"],
                tag_groups=driver_entry["tag_groups"],
            )
        )

    try:
        yield _Cluster(
            supervisor=supervisor, ring_buffers=ring_buffers, drivers_config=drivers_config
        )
    finally:
        await supervisor.stop_all()
        pipeline_task.cancel()
        await asyncio.gather(pipeline_task, return_exceptions=True)
        await modbus_server.stop()


_CROSS_PROTOCOL_ASSET = Asset(
    id="line1_pump_skid",
    name="Line 1 Pump Skid",
    asset_type="pump_skid",
    parameters=(
        AssetParameter(tag_ref="modbus_01/temperature_01"),
        AssetParameter(tag_ref="bacnet_01/room_temp"),
        AssetParameter(tag_ref="opcua_01/pump_speed"),
        AssetParameter(tag_ref="snmp_01/counter"),
        AssetParameter(tag_ref="enip_01/setpoint"),
    ),
)

_EXPECTED_VALUES: dict[str, float | int] = {
    "modbus_01/temperature_01": MODBUS_VALUE,
    "bacnet_01/room_temp": BACNET_VALUE,
    "opcua_01/pump_speed": OPCUA_VALUE,
    "snmp_01/counter": SNMP_VALUE,
    "enip_01/setpoint": ENIP_VALUE,
}


async def test_five_protocols_produce_correct_data_concurrently_without_cross_talk(
    cross_protocol_cluster: _Cluster,
) -> None:
    """Each of the five concurrently-polled instances must report its own,
    distinct value -- a shared-state or stream-key-collision bug between
    drivers of different protocols would show up here as a wrong value
    silently landing on the wrong tag, not as an exception."""
    ring_buffers = cross_protocol_cluster.ring_buffers

    for tag_ref, expected_value in _EXPECTED_VALUES.items():
        instance_id = tag_ref.split("/", 1)[0]
        updates = await _wait_for_stream(ring_buffers, instance_id)
        matching = [update for update in updates if update.tag_id == tag_ref]
        assert matching, f"no update for {tag_ref} in stream {instance_id!r}: got {updates}"
        assert matching[0].quality == Quality.GOOD
        # abs=1e-3: BACnet's analog-value REAL is a 32-bit float on the
        # wire (tests/fixtures/bacnet_device.py's preset 85.3 round-trips as
        # 85.30000305175781 in Python's float64) -- matches
        # test_bacnet_client_driver.py's own established comparison.
        assert matching[0].value == pytest.approx(expected_value, abs=1e-3)


async def test_asset_parameters_spanning_five_protocols_resolve_to_live_data(
    cross_protocol_cluster: _Cluster,
) -> None:
    """An Asset (ADR-010) is only a metadata/grouping layer over existing
    tags -- this proves that layer works when its parameters reference
    tags from five different protocol drivers at once, both for referential
    integrity (all_tag_refs, the same check ConfigValidator runs on every
    config apply) and for live data (the ring buffer each parameter's
    tag_ref should resolve to)."""
    valid_refs = all_tag_refs(cross_protocol_cluster.drivers_config)
    for parameter in _CROSS_PROTOCOL_ASSET.parameters:
        assert parameter.tag_ref in valid_refs

    ring_buffers = cross_protocol_cluster.ring_buffers
    for parameter in _CROSS_PROTOCOL_ASSET.parameters:
        instance_id = parameter.tag_ref.split("/", 1)[0]
        updates = await _wait_for_stream(ring_buffers, instance_id)
        matching = [update for update in updates if update.tag_id == parameter.tag_ref]
        assert matching, f"asset parameter {parameter.tag_ref!r} produced no live data"
        assert matching[0].value == pytest.approx(_EXPECTED_VALUES[parameter.tag_ref], abs=1e-3)


async def test_asset_connection_state_across_five_protocols_is_degraded_not_connected(
    cross_protocol_cluster: _Cluster,
) -> None:
    """Only ModbusTcpDriver and SnmpClientDriver implement
    get_connectivity_state() today (grep confirms no other driver module
    does); BacnetIpDriver, OpcUaClientDriver, and EtherNetIpDriver all
    report UNKNOWN regardless of how healthy they actually are. assets.py's
    own compute_asset_connection_state already documents and tests the
    abstract two-state-literal case (test_mixed_connected_and_unknown_is_
    degraded); this is the first time it is exercised against a *real*
    multi-protocol DriverSupervisor rather than hand-built ConnectivityState
    literals.

    Customer-relevant consequence, recorded for the handover package: any
    asset whose parameters span a connectivity-aware protocol (Modbus,
    SNMP) and a tag from a protocol that isn't (BACnet, OPC UA,
    EtherNet/IP) will show Degraded, never Connected, until that protocol's
    driver gains connectivity-state support -- this is documented,
    deliberate behavior (ADR-010 §4), not a bug, but it means "Connected"
    is currently only achievable for an asset whose parameters are backed
    entirely by Modbus and/or SNMP tags.
    """
    ring_buffers = cross_protocol_cluster.ring_buffers
    supervisor = cross_protocol_cluster.supervisor

    # Ensure at least one successful read has landed for every backing
    # instance before reading connectivity state -- UNKNOWN --success-->
    # CONNECTED happens on the very first successful poll.
    for parameter in _CROSS_PROTOCOL_ASSET.parameters:
        await _wait_for_stream(ring_buffers, parameter.tag_ref.split("/", 1)[0])

    status = supervisor.all_status()
    for instance_id in ("modbus_01", "snmp_01"):
        assert status[instance_id].connectivity_state is ConnectivityState.CONNECTED
    for instance_id in ("bacnet_01", "opcua_01", "enip_01"):
        assert status[instance_id].connectivity_state is ConnectivityState.UNKNOWN

    connectivity_map = {
        instance_id: instance_status.connectivity_state
        for instance_id, instance_status in status.items()
    }
    result = derive_asset_connection_state(_CROSS_PROTOCOL_ASSET, connectivity_map)
    assert result is ConnectivityState.DEGRADED
