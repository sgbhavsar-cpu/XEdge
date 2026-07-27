"""Test-only MQTT brokers for integration tests, backed by amqtt (MIT,
pure-Python asyncio broker) — see ADR-006 / pyproject.toml test extras: not
shipped, used only to exercise our connector against a real, independent
MQTT implementation rather than mocking the network layer.

`mqtt_broker_tls`/`mqtt_broker_mtls` (Sprint C4, XEDGE-441) exercise the
connector's TLS support the same way: a real TLS/mTLS handshake against a
real broker, not a mocked `paho.mqtt.Client`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from amqtt.broker import Broker
from amqtt.contexts import BrokerConfig, ListenerConfig

from xedge.api.tls import load_or_create_server_certificate
from xedge.security.ca import load_or_create_ca
from xedge.security.csr import generate_key_and_csr

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


@dataclass(frozen=True, slots=True)
class TlsBrokerAddress:
    host: str
    port: int
    ca_cert_path: Path
    """Trust anchor a connecting client should verify the broker against.
    A self-signed leaf cert acts as its own trust anchor here (no
    separate CA involved) -- passing it as `tls_ca_certs_path` works the
    same way it would for a real CA certificate."""


@pytest.fixture
async def mqtt_broker_tls(tmp_path: Path) -> AsyncIterator[TlsBrokerAddress]:
    """Server-TLS only, no client certificate requirement -- the common
    "connect to a broker with a real certificate" case."""
    host, port = "127.0.0.1", free_port()
    cert_path, key_path = tmp_path / "broker-cert.pem", tmp_path / "broker-key.pem"
    load_or_create_server_certificate(cert_path, key_path, "127.0.0.1", 90)

    config = BrokerConfig(
        listeners={
            "default": ListenerConfig(
                bind=f"{host}:{port}", ssl=True, certfile=str(cert_path), keyfile=str(key_path)
            )
        }
    )
    broker = Broker(config)
    await asyncio.wait_for(broker.start(), timeout=_START_TIMEOUT_SECONDS)
    try:
        yield TlsBrokerAddress(host, port, cert_path)
    finally:
        await broker.shutdown()


@dataclass(frozen=True, slots=True)
class MtlsBrokerAddress:
    host: str
    port: int
    ca_cert_path: Path
    client_cert_path: Path
    client_key_path: Path


@pytest.fixture
async def mqtt_broker_mtls(tmp_path: Path) -> AsyncIterator[MtlsBrokerAddress]:
    """TLS with a client certificate *required* -- the broker's listener
    is configured with `cafile` pointed at a CA, which amqtt (like any
    TLS server) treats as "verify the client against this," not merely
    "have this available."""
    host, port = "127.0.0.1", free_port()
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-broker-ca", 3650)

    broker_key_pem, broker_csr_pem = generate_key_and_csr("127.0.0.1")
    broker_cert_pem = ca.sign_csr(
        broker_csr_pem,
        common_name="127.0.0.1",
        validity_days=90,
        san_ip_addresses=(ipaddress.ip_address("127.0.0.1"),),
    )
    broker_cert_path, broker_key_path = tmp_path / "broker-cert.pem", tmp_path / "broker-key.pem"
    broker_cert_path.write_bytes(broker_cert_pem)
    broker_key_path.write_bytes(broker_key_pem)

    client_key_pem, client_csr_pem = generate_key_and_csr("test-client")
    client_cert_pem = ca.sign_csr(client_csr_pem, common_name="test-client", validity_days=90)
    client_cert_path, client_key_path = tmp_path / "client-cert.pem", tmp_path / "client-key.pem"
    client_cert_path.write_bytes(client_cert_pem)
    client_key_path.write_bytes(client_key_pem)

    config = BrokerConfig(
        listeners={
            "default": ListenerConfig(
                bind=f"{host}:{port}",
                ssl=True,
                certfile=str(broker_cert_path),
                keyfile=str(broker_key_path),
                cafile=str(tmp_path / "ca.pem"),
            )
        }
    )
    broker = Broker(config)
    await asyncio.wait_for(broker.start(), timeout=_START_TIMEOUT_SECONDS)
    try:
        yield MtlsBrokerAddress(host, port, tmp_path / "ca.pem", client_cert_path, client_key_path)
    finally:
        await broker.shutdown()
