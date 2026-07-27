"""Embedded MQTT broker (Sprint C5, XEDGE-453/454) against real paho-mqtt
clients -- same "test against real infrastructure, not mocks" pattern as
tests/integration/test_mqtt_subscriber_driver.py, but exercising the
broker *service* (MqttBrokerService) itself rather than a driver/
connector that talks to one.

Two amqtt behaviors here were confirmed empirically (a throwaway repro
script against the raw `amqtt.broker.Broker`, before writing these
assertions) rather than assumed from its docs:

- A failed authenticate() closes the raw connection with no CONNACK sent
  at all (`broker.py::_handle_client_session`) -- paho surfaces that as
  `on_disconnect`, never `on_connect`. A rejected client is asserted by
  waiting for disconnect, not for a CONNACK failure reason code.
- `client.connect()` performs the full TLS handshake *synchronously*, on
  whatever thread calls it. Called directly from a test coroutine, that
  starves the event loop the broker itself runs on (same loop) until the
  handshake times out. Every `.connect()` call below is offloaded via
  `asyncio.to_thread` for that reason -- it matters for TLS, and is
  harmless for the plaintext cases.

See xedge.northbound.mqtt_broker's module docstring for a third, related
finding (the publish/subscribe ACL asymmetry) that test_publish_acl_* and
test_subscribe_acl_* below are written around.
"""

from __future__ import annotations

import asyncio
import queue
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.reasoncodes import ReasonCode

from xedge.api.tls import load_or_create_server_certificate
from xedge.northbound.mqtt_broker import (
    BrokerUserCredential,
    MqttBrokerConfig,
    MqttBrokerService,
    MqttBrokerStateError,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_until(
    predicate: Callable[[], bool], *, attempts: int = 300, interval: float = 0.02
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


@asynccontextmanager
async def _running_broker(
    config: MqttBrokerConfig, tmp_path: Path
) -> AsyncIterator[MqttBrokerConfig]:
    service = MqttBrokerService(config, tmp_path / "passwords")
    await service.start()
    try:
        yield config
    finally:
        await service.stop()


@dataclass
class _ConnectOutcome:
    reason_code: ReasonCode | None = None


@dataclass
class _SubscribeOutcome:
    reason_codes: list[ReasonCode] | None = None


@dataclass
class _DisconnectOutcome:
    fired: bool = False


@dataclass
class _ClientHandle:
    client: mqtt.Client
    connected: _ConnectOutcome
    subscribed: _SubscribeOutcome
    disconnected: _DisconnectOutcome


def _client(
    *, username: str | None = None, password: str | None = None, ca_certs: str | None = None
) -> _ClientHandle:
    client = mqtt.Client(CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
    if username is not None:
        client.username_pw_set(username, password)
    if ca_certs is not None:
        client.tls_set(ca_certs=ca_certs)
    connected = _ConnectOutcome()
    subscribed = _SubscribeOutcome()
    disconnected = _DisconnectOutcome()

    def on_connect(
        c: mqtt.Client,
        userdata: object,
        flags: object,
        reason_code: ReasonCode,
        properties: object = None,
    ) -> None:
        connected.reason_code = reason_code

    def on_subscribe(
        c: mqtt.Client,
        userdata: object,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: object = None,
    ) -> None:
        subscribed.reason_codes = list(reason_codes)

    def on_disconnect(
        c: mqtt.Client,
        userdata: object,
        flags: object,
        reason_code: ReasonCode,
        properties: object = None,
    ) -> None:
        disconnected.fired = True

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    return _ClientHandle(client, connected, subscribed, disconnected)


async def _connect(client: mqtt.Client, host: str, port: int) -> None:
    """`Client.connect()` blocks its caller for the full TCP (+ TLS, if
    enabled) handshake -- see module docstring. Never call it directly
    from a test coroutine that shares a loop with the broker under test."""
    await asyncio.to_thread(client.connect, host, port, 10)


def _message_queue(client: mqtt.Client) -> queue.Queue[mqtt.MQTTMessage]:
    q: queue.Queue[mqtt.MQTTMessage] = queue.Queue()
    client.on_message = lambda c, userdata, msg: q.put(msg)
    return q


async def _expect_message(q: queue.Queue[mqtt.MQTTMessage]) -> mqtt.MQTTMessage:
    await _wait_until(lambda: not q.empty())
    return q.get_nowait()


async def _expect_no_message(
    q: queue.Queue[mqtt.MQTTMessage], *, hold_seconds: float = 0.5
) -> None:
    await asyncio.sleep(hold_seconds)
    assert q.empty()


async def test_anonymous_client_connects_and_round_trips_when_allowed(tmp_path: Path) -> None:
    config = MqttBrokerConfig(host="127.0.0.1", port=_free_port(), allow_anonymous=True)
    async with _running_broker(config, tmp_path) as cfg:
        handle = _client()
        messages = _message_queue(handle.client)
        await _connect(handle.client, cfg.host, cfg.port)
        handle.client.loop_start()
        try:
            await _wait_until(lambda: handle.connected.reason_code is not None)
            assert handle.connected.reason_code is not None
            assert not handle.connected.reason_code.is_failure

            handle.client.subscribe("demo/topic", qos=1)
            await asyncio.sleep(0.2)
            handle.client.publish("demo/topic", b"hello", qos=1)
            message = await _expect_message(messages)
            assert message.payload == b"hello"
        finally:
            handle.client.loop_stop()
            handle.client.disconnect()


async def test_anonymous_connection_is_rejected_by_default(tmp_path: Path) -> None:
    """allow_anonymous defaults to False (XEDGE-454): a broker with no
    users configured must reject a client presenting no credentials --
    observed as an immediate disconnect (see module docstring), not a
    CONNACK failure code."""
    config = MqttBrokerConfig(host="127.0.0.1", port=_free_port())
    async with _running_broker(config, tmp_path) as cfg:
        handle = _client()
        await _connect(handle.client, cfg.host, cfg.port)
        handle.client.loop_start()
        try:
            await _wait_until(lambda: handle.disconnected.fired)
            assert handle.connected.reason_code is None
        finally:
            handle.client.loop_stop()
            handle.client.disconnect()


async def test_authenticated_client_connects_with_correct_password(tmp_path: Path) -> None:
    config = MqttBrokerConfig(
        host="127.0.0.1",
        port=_free_port(),
        users=(BrokerUserCredential(username="dev1", password="s3cret"),),
    )
    async with _running_broker(config, tmp_path) as cfg:
        handle = _client(username="dev1", password="s3cret")
        messages = _message_queue(handle.client)
        await _connect(handle.client, cfg.host, cfg.port)
        handle.client.loop_start()
        try:
            await _wait_until(lambda: handle.connected.reason_code is not None)
            assert handle.connected.reason_code is not None
            assert not handle.connected.reason_code.is_failure

            handle.client.subscribe("demo/topic", qos=1)
            await asyncio.sleep(0.2)
            handle.client.publish("demo/topic", b"authed", qos=1)
            message = await _expect_message(messages)
            assert message.payload == b"authed"
        finally:
            handle.client.loop_stop()
            handle.client.disconnect()


async def test_authentication_rejects_wrong_password(tmp_path: Path) -> None:
    config = MqttBrokerConfig(
        host="127.0.0.1",
        port=_free_port(),
        users=(BrokerUserCredential(username="dev1", password="s3cret"),),
    )
    async with _running_broker(config, tmp_path) as cfg:
        handle = _client(username="dev1", password="wrong-password")
        await _connect(handle.client, cfg.host, cfg.port)
        handle.client.loop_start()
        try:
            await _wait_until(lambda: handle.disconnected.fired)
            assert handle.connected.reason_code is None
        finally:
            handle.client.loop_stop()
            handle.client.disconnect()


async def test_publish_acl_allows_matching_topic_and_blocks_others(tmp_path: Path) -> None:
    """dev1 may publish only under telemetry/dev1/#; a publish outside
    that pattern is accepted at the protocol level (amqtt gives no
    publish-side error -- verified by reading topic_checking.py) but is
    never broadcast to subscribers."""
    config = MqttBrokerConfig(
        host="127.0.0.1",
        port=_free_port(),
        users=(
            BrokerUserCredential(username="dev1", password="s3cret"),
            BrokerUserCredential(username="observer", password="s3cret"),
        ),
        publish_acl={"dev1": ["telemetry/dev1/#"]},
        # dev1 needs no subscribe access for this test, but 'observer'
        # does -- and the ACL plugin loading at all (because publish_acl
        # above is non-empty) means an unlisted user gets zero subscribe
        # access, not unrestricted (see mqtt_broker.py's module docstring).
        subscribe_acl={"observer": ["#"]},
    )
    async with _running_broker(config, tmp_path) as cfg:
        publisher = _client(username="dev1", password="s3cret")
        observer = _client(username="observer", password="s3cret")
        messages = _message_queue(observer.client)
        await _connect(publisher.client, cfg.host, cfg.port)
        publisher.client.loop_start()
        await _connect(observer.client, cfg.host, cfg.port)
        observer.client.loop_start()
        try:
            await _wait_until(lambda: publisher.connected.reason_code is not None)
            await _wait_until(lambda: observer.connected.reason_code is not None)
            assert publisher.connected.reason_code is not None
            assert not publisher.connected.reason_code.is_failure
            assert observer.connected.reason_code is not None
            assert not observer.connected.reason_code.is_failure

            observer.client.subscribe("telemetry/#", qos=1)
            await _wait_until(lambda: observer.subscribed.reason_codes is not None)
            assert observer.subscribed.reason_codes is not None
            assert not observer.subscribed.reason_codes[0].is_failure

            publisher.client.publish("telemetry/other-device/reading", b"not-allowed", qos=1)
            await _expect_no_message(messages)

            publisher.client.publish("telemetry/dev1/reading", b"allowed", qos=1)
            message = await _expect_message(messages)
            assert message.payload == b"allowed"
        finally:
            publisher.client.loop_stop()
            publisher.client.disconnect()
            observer.client.loop_stop()
            observer.client.disconnect()


async def test_subscribe_acl_rejects_a_topic_outside_the_allowed_pattern(tmp_path: Path) -> None:
    config = MqttBrokerConfig(
        host="127.0.0.1",
        port=_free_port(),
        users=(BrokerUserCredential(username="dev1", password="s3cret"),),
        subscribe_acl={"dev1": ["telemetry/dev1/#"]},
    )
    async with _running_broker(config, tmp_path) as cfg:
        handle = _client(username="dev1", password="s3cret")
        await _connect(handle.client, cfg.host, cfg.port)
        handle.client.loop_start()
        try:
            await _wait_until(lambda: handle.connected.reason_code is not None)
            assert handle.connected.reason_code is not None
            assert not handle.connected.reason_code.is_failure

            handle.client.subscribe("other/topic", qos=1)
            await _wait_until(lambda: handle.subscribed.reason_codes is not None)
            assert handle.subscribed.reason_codes is not None
            assert handle.subscribed.reason_codes[0].is_failure
        finally:
            handle.client.loop_stop()
            handle.client.disconnect()


async def test_tls_listener_accepts_a_client_that_trusts_the_certificate(tmp_path: Path) -> None:
    cert_path = tmp_path / "broker-cert.pem"
    key_path = tmp_path / "broker-key.pem"
    load_or_create_server_certificate(cert_path, key_path, "127.0.0.1", 90)

    config = MqttBrokerConfig(
        host="127.0.0.1",
        port=_free_port(),
        allow_anonymous=True,
        tls_enabled=True,
        tls_certfile_path=str(cert_path),
        tls_keyfile_path=str(key_path),
    )
    async with _running_broker(config, tmp_path) as cfg:
        handle = _client(ca_certs=str(cert_path))
        messages = _message_queue(handle.client)
        await _connect(handle.client, cfg.host, cfg.port)
        handle.client.loop_start()
        try:
            await _wait_until(lambda: handle.connected.reason_code is not None)
            assert handle.connected.reason_code is not None
            assert not handle.connected.reason_code.is_failure

            handle.client.subscribe("demo/topic", qos=1)
            await asyncio.sleep(0.2)
            handle.client.publish("demo/topic", b"over-tls", qos=1)
            message = await _expect_message(messages)
            assert message.payload == b"over-tls"
        finally:
            handle.client.loop_stop()
            handle.client.disconnect()


async def test_starting_twice_raises_state_error(tmp_path: Path) -> None:
    config = MqttBrokerConfig(host="127.0.0.1", port=_free_port(), allow_anonymous=True)
    service = MqttBrokerService(config, tmp_path / "passwords")
    await service.start()
    try:
        assert service.is_running() is True
        with pytest.raises(MqttBrokerStateError):
            await service.start()
    finally:
        await service.stop()
    assert service.is_running() is False


async def test_tls_enabled_without_cert_paths_raises(tmp_path: Path) -> None:
    config = MqttBrokerConfig(host="127.0.0.1", port=_free_port(), tls_enabled=True)
    service = MqttBrokerService(config, tmp_path / "passwords")
    with pytest.raises(ValueError, match="tls_certfile_path"):
        await service.start()


async def test_stop_does_not_hang_after_a_connection_never_completed_a_handshake(
    tmp_path: Path,
) -> None:
    """Regression test for a real, reproduced amqtt hang (see module
    docstring): Broker.shutdown() can block forever if any TCP connection
    ever reached the listener without completing an MQTT handshake --
    exactly what a bare health-check/liveness probe against this port
    would do. stop() bounds this with its own internal timeout so this
    device's own shutdown sequence can never be blocked by it.

    The outer asyncio.wait_for is a test-level safety net, not the
    mechanism under test -- if the internal bound in stop() ever
    regresses, this fails in ~15s with a clear TimeoutError instead of
    hanging the whole suite."""
    config = MqttBrokerConfig(host="127.0.0.1", port=_free_port(), allow_anonymous=True)
    service = MqttBrokerService(config, tmp_path / "passwords")
    await service.start()

    _, writer = await asyncio.open_connection(config.host, config.port)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.2)

    await asyncio.wait_for(service.stop(), timeout=15.0)
    assert service.is_running() is False
