"""Standalone BACnet/IP device for manual testing -- not part of the
shipped xedge package. Built on bacpypes3, the same library xedge's own
BacnetIpDriver uses (ADR-006 "buy" decision), in its device/server role --
the same class serves both roles, there is no separate "test server"
convenience class. Exposes one changing analog-value object and one
static binary-value object, so you can point
config/examples/bacnet-example.yaml's `drivers[].config.device_address` /
`tags[].object_identifier` at it without a real BACnet-speaking device.

Usage:
    python tools/mock_bacnet_device.py [--port 47808]

Prints the device address and both objects' identifiers on startup --
copy those into your config's `device_address` / `object_identifier`
fields. Point your own driver instance's `config.local_address` at a
*different* port on the same host (two local processes cannot share one
UDP port).
"""

from __future__ import annotations

import argparse
import asyncio
import random

from bacpypes3.app import Application
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject

_DEVICE_INSTANCE = 3000001


async def _drift_temperature(analog_value: AnalogValueObject) -> None:
    while True:
        await asyncio.sleep(3)
        analog_value.presentValue = 72.0 + random.uniform(-3.0, 3.0)  # type: ignore[attr-defined]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47808)
    args = parser.parse_args()

    device_address = f"{args.host}:{args.port}"
    device_object = DeviceObject(
        objectIdentifier=("device", _DEVICE_INSTANCE), objectName="xedge-mock-device"
    )
    network_port_object = NetworkPortObject(
        device_address, objectIdentifier=("network-port", 1), objectName="NetworkPort-1"
    )
    app = Application.from_object_list([device_object, network_port_object])
    analog_value = AnalogValueObject(
        objectIdentifier=("analog-value", 1), objectName="room_temp", presentValue=72.0
    )
    binary_value = BinaryValueObject(
        objectIdentifier=("binary-value", 1), objectName="fan_running", presentValue="active"
    )
    app.add_object(analog_value)
    app.add_object(binary_value)

    await asyncio.sleep(0.2)  # bacpypes3 binds its UDP socket asynchronously
    print(f"BACnet/IP mock device listening at {device_address}")
    print(f"  device_instance: {_DEVICE_INSTANCE}")
    print("  analog-value,1 (room_temp, drifts every 3s)")
    print("  binary-value,1 (fan_running, static 'active')")

    drift_task = asyncio.ensure_future(_drift_temperature(analog_value))
    try:
        await asyncio.Event().wait()  # run until Ctrl+C
    finally:
        drift_task.cancel()
        app.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
