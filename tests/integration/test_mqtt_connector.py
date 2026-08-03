"""MqttSparkplugConnector against a real (amqtt-backed) MQTT broker.

Decodes captured payloads with pysparkplug's officially-generated protobuf
classes as the black-box oracle (ADR-006), same pattern as the Modbus
codec's pymodbus cross-validation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from pysparkplug._payload import NBirth, NData

from tests.fixtures.fake_driver import FakeDriver
from tests.fixtures.mqtt_broker import MtlsBrokerAddress, TlsBrokerAddress
from xedge.api.tls import load_or_create_server_certificate
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverConfig, DriverRegistry, DriverSupervisor
from xedge.core.write_router import WriteRouter
from xedge.drivers.base import Quality, TagUpdate
from xedge.northbound.mqtt import MqttSparkplugConnector, SparkplugConnectorConfig
from xedge.northbound.sparkplug.payload import DataType, SparkplugMetric, encode_payload
from xedge.northbound.sparkplug.session import build_topic
from xedge.observability.audit_log import AuditLog


class _TopicCapture:
    """Subscribes to a Sparkplug topic tree and records (topic, payload)."""

    def __init__(self, host: str, port: int) -> None:
        self.messages: list[tuple[str, bytes]] = []
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self._client.on_message = lambda _c, _u, msg: self.messages.append((msg.topic, msg.payload))
        self._client.connect(host, port, 10)
        self._client.subscribe("spBv1.0/#")
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def find(self, message_type: str) -> list[tuple[str, bytes]]:
        return [(t, p) for t, p in self.messages if f"/{message_type}/" in t]


def _tag(tag_id: str, value: object, quality: Quality = Quality.GOOD) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        data_type="INT64",
        quality=quality,
        source_driver="modbus_tcp_01",
        source_address="0",
    )


async def test_connect_publishes_nbirth(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port)
    await asyncio.sleep(0.2)  # let the subscription land before we publish

    connector = MqttSparkplugConnector(SparkplugConnectorConfig(host=host, port=port))
    try:
        await connector.connect()
        await asyncio.sleep(0.2)

        births = capture.find("NBIRTH")
        assert len(births) == 1
        decoded = NBirth.decode(births[0][1])
        assert decoded.metrics[0].name == "bdSeq"
        assert decoded.metrics[0].value == 0
    finally:
        await connector.disconnect()
        capture.stop()


async def test_publish_sends_ndata_with_correct_values(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port)
    await asyncio.sleep(0.2)

    connector = MqttSparkplugConnector(SparkplugConnectorConfig(host=host, port=port))
    try:
        await connector.connect()
        result = await connector.publish(
            [
                _tag("modbus_tcp_01/temperature_01", 85),
                _tag("modbus_tcp_01/pump_running", True),
            ]
        )
        assert result.success
        assert result.count == 2
        await asyncio.sleep(0.2)

        ndata_messages = capture.find("NDATA")
        assert len(ndata_messages) == 1
        decoded = NData.decode(ndata_messages[0][1])
        # NBIRTH's seq is always 0 by spec (not drawn from the counter), so
        # the first NDATA after birth also starts the counter at 0.
        assert decoded.seq == 0
        assert decoded.metrics[0].name == "modbus_tcp_01/temperature_01"
        assert decoded.metrics[0].value == 85
        assert decoded.metrics[1].name == "modbus_tcp_01/pump_running"
        assert decoded.metrics[1].value is True
    finally:
        await connector.disconnect()
        capture.stop()


async def test_bad_quality_tag_encoded_as_null(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port)
    await asyncio.sleep(0.2)

    connector = MqttSparkplugConnector(SparkplugConnectorConfig(host=host, port=port))
    try:
        await connector.connect()
        await connector.publish([_tag("modbus_tcp_01/bad_tag", 0, quality=Quality.BAD)])
        await asyncio.sleep(0.2)

        ndata_messages = capture.find("NDATA")
        decoded = NData.decode(ndata_messages[0][1])
        assert decoded.metrics[0].is_null is True
    finally:
        await connector.disconnect()
        capture.stop()


async def test_clean_disconnect_does_not_publish_ndeath(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port)
    await asyncio.sleep(0.2)

    connector = MqttSparkplugConnector(SparkplugConnectorConfig(host=host, port=port))
    await connector.connect()
    await connector.disconnect()
    await asyncio.sleep(0.3)

    capture.stop()
    assert capture.find("NDEATH") == []


async def test_reconnect_advances_bd_seq(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port)
    await asyncio.sleep(0.2)

    connector = MqttSparkplugConnector(SparkplugConnectorConfig(host=host, port=port))
    await connector.connect()
    await connector.disconnect()
    await connector.connect()
    await asyncio.sleep(0.2)
    await connector.disconnect()
    capture.stop()

    births = capture.find("NBIRTH")
    assert len(births) == 2
    first_bd_seq = NBirth.decode(births[0][1]).metrics[0].value
    second_bd_seq = NBirth.decode(births[1][1]).metrics[0].value
    assert second_bd_seq == first_bd_seq + 1


async def test_connect_over_tls_publishes_nbirth(mqtt_broker_tls: TlsBrokerAddress) -> None:
    """XEDGE-441: server-TLS only, verified against the broker's own
    (self-signed) certificate -- the common "broker has a real
    certificate, no client cert required" case."""
    connector = MqttSparkplugConnector(
        SparkplugConnectorConfig(
            host=mqtt_broker_tls.host,
            port=mqtt_broker_tls.port,
            tls_enabled=True,
            tls_ca_certs_path=str(mqtt_broker_tls.ca_cert_path),
        )
    )
    try:
        await connector.connect()
        assert connector.is_alive()
    finally:
        await connector.disconnect()


async def test_connect_over_tls_without_the_right_ca_is_rejected(
    mqtt_broker_tls: TlsBrokerAddress, tmp_path: Path
) -> None:
    """Proves the CA verification is real, not a no-op -- a *different*
    self-signed cert must not be trusted for this broker."""
    wrong_cert_path = tmp_path / "wrong-cert.pem"
    load_or_create_server_certificate(wrong_cert_path, tmp_path / "wrong-key.pem", "127.0.0.1", 90)

    connector = MqttSparkplugConnector(
        SparkplugConnectorConfig(
            host=mqtt_broker_tls.host,
            port=mqtt_broker_tls.port,
            tls_enabled=True,
            tls_ca_certs_path=str(wrong_cert_path),
            connect_timeout_seconds=3.0,
        )
    )
    with pytest.raises(Exception):  # noqa: B017,PT011 — TLS failure surfaces as ssl.SSLError via paho
        await connector.connect()


async def test_connect_over_mtls_with_client_certificate(
    mqtt_broker_mtls: MtlsBrokerAddress,
) -> None:
    """XEDGE-441: the connector can present a client certificate for the
    broker's own mTLS, independent of the fleet's own CA (XEDGE-440) -- a
    customer broker's PKI and xEdge's fleet PKI are different trust
    domains, so this must work with *any* client cert, not specifically a
    fleet-CA-issued one.

    This only proves the connector can successfully complete an mTLS
    handshake and get *accepted* with a valid cert -- not that omitting
    one is rejected. amqtt's broker (the version this test suite is
    pinned to) always sets `ssl.CERT_OPTIONAL` for a listener with
    `cafile` configured, never `CERT_REQUIRED` (confirmed by reading
    amqtt/broker.py directly) -- it cannot actually enforce "client
    certificate mandatory," so there is no broker behavior available here
    to write a rejection test against. A real broker requiring client
    certificates (Mosquitto's `require_certificate true`, EMQX, HiveMQ)
    enforces that at the TLS layer the same way `manager_device_app`'s
    `ssl_cert_reqs=CERT_REQUIRED` does in tests/integration/
    test_fleet_agent.py -- this connector's job is just to present a cert
    when configured to, which is what this test actually verifies."""
    connector = MqttSparkplugConnector(
        SparkplugConnectorConfig(
            host=mqtt_broker_mtls.host,
            port=mqtt_broker_mtls.port,
            tls_enabled=True,
            tls_ca_certs_path=str(mqtt_broker_mtls.ca_cert_path),
            tls_certfile_path=str(mqtt_broker_mtls.client_cert_path),
            tls_keyfile_path=str(mqtt_broker_mtls.client_key_path),
        )
    )
    try:
        await connector.connect()
        assert connector.is_alive()
    finally:
        await connector.disconnect()


async def test_publish_metrics_tracked(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    connector = MqttSparkplugConnector(SparkplugConnectorConfig(host=host, port=port))
    try:
        await connector.connect()
        await connector.publish([_tag("t1", 1), _tag("t2", 2)])
        metrics = connector.get_metrics()
        assert metrics.published_count == 2
        assert metrics.last_successful_publish is not None
    finally:
        await connector.disconnect()


async def _running_supervisor_with_fake_driver() -> tuple[DriverSupervisor, FakeDriver]:
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=100)
    supervisor = DriverSupervisor(registry, queue)
    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    for _ in range(200):
        if driver.emitted_count >= 1:
            break
        await asyncio.sleep(0.01)
    return supervisor, driver


async def test_ncmd_write_command_reaches_the_driver_through_write_router(
    mqtt_broker: tuple[str, int], tmp_path: Path
) -> None:
    """End-to-end write-back (Sprint 31, XEDGE-223/229): a real external
    MQTT client publishes a real Sparkplug B NCMD payload; the connector
    decodes it and routes the write through a real WriteRouter to a real
    (fake) running driver instance."""
    host, port = mqtt_broker
    supervisor, driver = await _running_supervisor_with_fake_driver()
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    write_router = WriteRouter(supervisor, audit_log)
    config = SparkplugConnectorConfig(host=host, port=port, group_id="xedge", edge_node_id="edge01")
    connector = MqttSparkplugConnector(config, write_router)

    external_publisher = mqtt.Client(CallbackAPIVersion.VERSION2)
    external_publisher.connect(host, port, 10)
    external_publisher.loop_start()

    try:
        await connector.connect()
        await asyncio.sleep(0.2)  # let the NCMD subscription land

        ncmd_payload = encode_payload(
            timestamp_ms=1700000000123,
            seq=None,
            metrics=[
                SparkplugMetric(
                    name="fake_01/setpoint",
                    timestamp_ms=1700000000123,
                    datatype=DataType.DOUBLE,
                    value=42.5,
                )
            ],
        )
        ncmd_topic = build_topic("NCMD", config.group_id, config.edge_node_id)
        external_publisher.publish(ncmd_topic, ncmd_payload, qos=1)

        for _ in range(100):
            if driver.written:
                break
            await asyncio.sleep(0.05)

        assert driver.written == [("setpoint", 42.5)]

        write_events = [e for e in audit_log.tail(limit=10) if e["event"] == "tag.write"]
        assert len(write_events) == 1
        assert write_events[0]["actor"] == "mqtt-ncmd"
        assert write_events[0]["details"]["tag_id"] == "fake_01/setpoint"
        assert write_events[0]["details"]["success"] is True
    finally:
        external_publisher.loop_stop()
        external_publisher.disconnect()
        await connector.disconnect()
        await supervisor.stop_all()


async def test_ncmd_null_metric_is_ignored(mqtt_broker: tuple[str, int], tmp_path: Path) -> None:
    host, port = mqtt_broker
    supervisor, driver = await _running_supervisor_with_fake_driver()
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    write_router = WriteRouter(supervisor, audit_log)
    config = SparkplugConnectorConfig(host=host, port=port, group_id="xedge", edge_node_id="edge01")
    connector = MqttSparkplugConnector(config, write_router)

    external_publisher = mqtt.Client(CallbackAPIVersion.VERSION2)
    external_publisher.connect(host, port, 10)
    external_publisher.loop_start()

    try:
        await connector.connect()
        await asyncio.sleep(0.2)

        ncmd_payload = encode_payload(
            timestamp_ms=1700000000123,
            seq=None,
            metrics=[
                SparkplugMetric(
                    name="fake_01/setpoint",
                    timestamp_ms=1700000000123,
                    datatype=DataType.DOUBLE,
                    value=None,
                    is_null=True,
                )
            ],
        )
        ncmd_topic = build_topic("NCMD", config.group_id, config.edge_node_id)
        external_publisher.publish(ncmd_topic, ncmd_payload, qos=1)
        await asyncio.sleep(0.3)

        assert driver.written == []
    finally:
        external_publisher.loop_stop()
        external_publisher.disconnect()
        await connector.disconnect()
        await supervisor.stop_all()
