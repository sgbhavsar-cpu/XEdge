"""A real asyncua-backed OPC UA server used as a test double for the
OPC UA client driver. Since the MVP driver itself is built directly on
asyncua (ADR-006 §7 amendment), this exercises the exact same library the
driver uses — not a black-box oracle for an independently-built codec, but
the most direct way to test the driver without physical/simulated PLC
hardware.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator

import pytest
from asyncua import Server


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def opcua_test_server() -> AsyncIterator[tuple[Server, str, int]]:
    """Yields (server, endpoint_url, namespace_index). Use
    `server.get_objects_node()` and `.add_variable(idx, name, value)` to
    populate test nodes before or after `connect()`-ing a driver to it."""
    server = Server()
    await server.init()
    port = free_port()
    endpoint_url = f"opc.tcp://127.0.0.1:{port}/xedge/test/"
    server.set_endpoint(endpoint_url)
    idx = await server.register_namespace("http://xedge.test/")

    await server.start()
    try:
        yield server, endpoint_url, idx
    finally:
        await server.stop()
