"""Standalone OPC UA server for manual testing — not part of the shipped
xedge package. Built on the same asyncua library xedge's own OPC UA client
driver and server use (ADR-006 §7 amendment), so it exercises the exact
library the driver talks to. Exposes one changing Double variable and one
static Boolean, so you can point config/examples/opcua-example.yaml's
`drivers[].config.endpoint_url` / `tags[].node_id` at it without a real
OPC UA-speaking PLC.

Usage:
    python tools/mock_opcua_server.py [--port 4841]

Prints the endpoint URL and the two nodes' full NodeId strings on startup —
copy those into your config's `endpoint_url` / `node_id` fields.
"""

from __future__ import annotations

import argparse
import asyncio
import random

from asyncua import Server


async def _drift_temperature(node: object) -> None:
    while True:
        await asyncio.sleep(3)
        await node.write_value(20.0 + random.uniform(-2.0, 2.0))  # type: ignore[attr-defined]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4841)
    args = parser.parse_args()

    server = Server()
    await server.init()
    endpoint_url = f"opc.tcp://{args.host}:{args.port}/xedge/mock/"
    server.set_endpoint(endpoint_url)
    idx = await server.register_namespace("http://xedge.mock/")

    objects = server.get_objects_node()
    temperature = await objects.add_variable(idx, "Temperature", 20.0)
    pump_running = await objects.add_variable(idx, "PumpRunning", True)
    await temperature.set_writable()

    await server.start()
    print(f"OPC UA mock server listening at {endpoint_url}")
    print(f"Temperature node_id: {temperature.nodeid.to_string()}")
    print(f"PumpRunning node_id: {pump_running.nodeid.to_string()}")

    drift_task = asyncio.ensure_future(_drift_temperature(temperature))
    try:
        await asyncio.Event().wait()  # run until Ctrl+C
    finally:
        drift_task.cancel()
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
