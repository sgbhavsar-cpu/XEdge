"""A real bacpypes3-backed BACnet/IP device used as a test double for the
BacnetIpDriver. Since the driver itself is built directly on bacpypes3
(ADR-006 "buy" decision — a direct library integration, not an in-house
clean-room protocol stack), this exercises the exact same library the
driver uses over real loopback BACnet/IP UDP traffic — not a black-box
oracle for an independently-built codec, but the most direct way to test
the driver without physical/simulated PLC hardware (bacpypes3 has no
dedicated "test server" convenience class the way `asyncua.Server` does;
the same `Application` class serves both client and device/server roles).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from bacpypes3.app import Application
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject

_DEVICE_INSTANCE = 3000001
_ANALOG_VALUE = 85.3
_BINARY_VALUE = "active"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def bacnet_test_device() -> AsyncIterator[tuple[str, str, str]]:
    """Yields (device_address, analog_object_identifier, binary_object_identifier).
    `device_address` is this fake device's own address (`host:port`) — point
    a driver-under-test's tag `device_address` field at it."""
    port = _free_udp_port()
    device_address = f"127.0.0.1:{port}"

    device_object = DeviceObject(
        objectIdentifier=("device", _DEVICE_INSTANCE), objectName="xedge-test-device"
    )
    network_port_object = NetworkPortObject(
        device_address, objectIdentifier=("network-port", 1), objectName="NetworkPort-1"
    )
    app = Application.from_object_list([device_object, network_port_object])
    analog_value = AnalogValueObject(
        objectIdentifier=("analog-value", 1), objectName="av1", presentValue=_ANALOG_VALUE
    )
    binary_value = BinaryValueObject(
        objectIdentifier=("binary-value", 1), objectName="bv1", presentValue=_BINARY_VALUE
    )
    app.add_object(analog_value)
    app.add_object(binary_value)
    try:
        # bacpypes3 binds its UDP socket asynchronously in a background task
        # (confirmed by exercising it directly) rather than blocking inside
        # Application.from_object_list() — give it a moment to settle before
        # any test starts sending it requests.
        await asyncio.sleep(0.2)
        yield device_address, "analog-value,1", "binary-value,1"
    finally:
        app.close()
