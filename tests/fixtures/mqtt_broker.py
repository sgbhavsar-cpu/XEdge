"""A test-only MQTT broker for integration tests, backed by amqtt (MIT,
pure-Python asyncio broker) — see ADR-006 / pyproject.toml test extras: not
shipped, used only to exercise our connector against a real, independent
MQTT implementation rather than mocking the network layer."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from amqtt.broker import Broker
from amqtt.contexts import BrokerConfig, ListenerConfig

_START_TIMEOUT_SECONDS = 15.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def mqtt_broker() -> AsyncIterator[tuple[str, int]]:
    host, port = "127.0.0.1", free_port()
    config = BrokerConfig(listeners={"default": ListenerConfig(bind=f"{host}:{port}")})
    broker = Broker(config)
    # Bounded defensively: a broker that never finishes starting should
    # fail this fixture with a clear TimeoutError, not hang whatever test
    # uses it (and, transitively, the entire test run) indefinitely.
    await asyncio.wait_for(broker.start(), timeout=_START_TIMEOUT_SECONDS)
    try:
        yield host, port
    finally:
        await broker.shutdown()
