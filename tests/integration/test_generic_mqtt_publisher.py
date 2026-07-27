"""GenericMqttPublisher against a real (amqtt-backed) MQTT broker — same
"test against real infrastructure, not mocks" pattern as
tests/integration/test_mqtt_connector.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.northbound.generic_mqtt import GenericMqttPublisher, GenericMqttPublisherConfig


class _TopicCapture:
    def __init__(self, host: str, port: int, topic_filter: str) -> None:
        self.messages: list[tuple[str, bytes]] = []
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self._client.on_message = lambda _c, _u, msg: self.messages.append((msg.topic, msg.payload))
        self._client.connect(host, port, 10)
        self._client.subscribe(topic_filter)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _tag(tag_id: str, value: object, quality: Quality = Quality.GOOD) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        data_type="INT64",
        quality=quality,
        source_driver="d1",
        source_address="0",
    )


async def test_per_tag_mode_publishes_one_message_per_tag(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port, "xedge/#")
    await asyncio.sleep(0.2)

    connector = GenericMqttPublisher(GenericMqttPublisherConfig(host=host, port=port))
    try:
        await connector.connect()
        result = await connector.publish([_tag("line1/temp", 42), _tag("line1/pressure", 7)])
        assert result.success
        assert result.count == 2
        await asyncio.sleep(0.2)

        topics = {topic for topic, _ in capture.messages}
        assert topics == {"xedge/line1/temp", "xedge/line1/pressure"}
        payload = json.loads(next(p for t, p in capture.messages if t == "xedge/line1/temp"))
        assert payload["tag_id"] == "line1/temp"
        assert payload["value"] == 42
        assert payload["quality"] == "Good"
        datetime.fromisoformat(payload["timestamp"])  # raises if not a valid ISO timestamp
    finally:
        await connector.disconnect()
        capture.stop()


async def test_batch_mode_publishes_one_message_with_all_tags(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port, "telemetry/batch")
    await asyncio.sleep(0.2)

    connector = GenericMqttPublisher(
        GenericMqttPublisherConfig(
            host=host, port=port, payload_mode="batch", topic_template="telemetry/batch"
        )
    )
    try:
        await connector.connect()
        result = await connector.publish([_tag("t1", 1), _tag("t2", 2)])
        assert result.success
        await asyncio.sleep(0.2)

        assert len(capture.messages) == 1
        payload = json.loads(capture.messages[0][1])
        assert [p["tag_id"] for p in payload] == ["t1", "t2"]
        assert [p["value"] for p in payload] == [1, 2]
    finally:
        await connector.disconnect()
        capture.stop()


async def test_bad_quality_tag_is_encoded_as_null_value(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port, "xedge/#")
    await asyncio.sleep(0.2)

    connector = GenericMqttPublisher(GenericMqttPublisherConfig(host=host, port=port))
    try:
        await connector.connect()
        await connector.publish([_tag("bad_tag", 0, quality=Quality.BAD)])
        await asyncio.sleep(0.2)

        payload = json.loads(capture.messages[0][1])
        assert payload["value"] is None
        assert payload["quality"] == "Bad"
    finally:
        await connector.disconnect()
        capture.stop()


async def test_custom_field_names_and_omitted_fields(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    capture = _TopicCapture(host, port, "xedge/#")
    await asyncio.sleep(0.2)

    connector = GenericMqttPublisher(
        GenericMqttPublisherConfig(
            host=host,
            port=port,
            include_timestamp=False,
            include_quality=False,
            field_names={"tag_id": "name", "value": "val"},
        )
    )
    try:
        await connector.connect()
        await connector.publish([_tag("line1/temp", 99)])
        await asyncio.sleep(0.2)

        payload = json.loads(capture.messages[0][1])
        assert payload == {"name": "line1/temp", "val": 99}
    finally:
        await connector.disconnect()
        capture.stop()


async def test_publish_metrics_tracked(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    connector = GenericMqttPublisher(GenericMqttPublisherConfig(host=host, port=port))
    try:
        await connector.connect()
        await connector.publish([_tag("t1", 1), _tag("t2", 2)])
        metrics = connector.get_metrics()
        assert metrics.published_count == 2
        assert metrics.last_successful_publish is not None
    finally:
        await connector.disconnect()


async def test_publish_before_connect_reports_failure_without_raising() -> None:
    connector = GenericMqttPublisher(GenericMqttPublisherConfig(host="127.0.0.1", port=1))
    result = await connector.publish([_tag("t1", 1)])
    assert result.success is False
    assert result.error_message == "not connected"


def test_batch_mode_rejects_a_topic_template_using_tag_id() -> None:
    with pytest.raises(ValueError, match="payload_mode is 'batch'"):
        GenericMqttPublisher(
            GenericMqttPublisherConfig(
                host="127.0.0.1", payload_mode="batch", topic_template="x/{tag_id}"
            )
        )


def test_invalid_payload_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="payload_mode"):
        GenericMqttPublisher(GenericMqttPublisherConfig(host="127.0.0.1", payload_mode="nonsense"))
