"""MqttSubscriberDriver against a real (amqtt-backed) MQTT broker, with a
real external publisher — same "test against real infrastructure, not
mocks" pattern as tests/integration/test_mqtt_connector.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.mqtt_subscriber.client import MqttSubscriberDriver


def _driver_config(tags: list[dict], instance_id: str = "mqtt_sub_01", **config_overrides: object):
    host, port = config_overrides.pop("host"), config_overrides.pop("port")
    return DriverConfig(
        instance_id=instance_id,
        driver_type="mqtt_subscriber",
        config={"host": host, "port": port, "connect_timeout_seconds": 3.0, **config_overrides},
        tag_groups=[{"id": "group1", "tags": tags}],
    )


async def _run_and_collect(
    driver: MqttSubscriberDriver,
    config: DriverConfig,
    publish: Callable[[], Awaitable[None]],
    expected_count: int,
) -> list[TagUpdate]:
    await driver.configure(config)
    await driver.connect()
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
    run_task = asyncio.create_task(driver.run(queue))
    try:
        await asyncio.sleep(0.2)  # let the subscription land before publishing
        await publish()
        updates = [await asyncio.wait_for(queue.get(), timeout=3.0) for _ in range(expected_count)]
        return updates
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await driver.disconnect()


def _publisher(host: str, port: int) -> mqtt.Client:
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    client.connect(host, port, 10)
    client.loop_start()
    return client


async def test_raw_payload_is_auto_coerced_to_float(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [{"id": "temperature", "topic": "sensors/room1/temperature"}], host=host, port=port
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("sensors/room1/temperature", b"23.5", qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].value == pytest.approx(23.5)
        assert updates[0].quality == Quality.GOOD
        assert updates[0].tag_id == "mqtt_sub_01/temperature"
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_raw_payload_bool_and_string_coercion(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [
            {"id": "running", "topic": "line1/running"},
            {"id": "label", "topic": "line1/label"},
        ],
        host=host,
        port=port,
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("line1/running", b"true", qos=1)
        publisher.publish("line1/label", b"batch-42", qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 2)
        by_tag = {u.tag_id: u for u in updates}
        assert by_tag["mqtt_sub_01/running"].value is True
        assert by_tag["mqtt_sub_01/label"].value == "batch-42"
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_json_payload_with_path_extraction(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [
            {
                "id": "temperature",
                "topic": "sensors/room1/data",
                "payload_format": "json",
                "json_path": "reading.value",
            }
        ],
        host=host,
        port=port,
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        payload = json.dumps({"reading": {"value": 42.7, "unit": "C"}}).encode("utf-8")
        publisher.publish("sensors/room1/data", payload, qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].value == pytest.approx(42.7)
        assert updates[0].quality == Quality.GOOD
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_json_payload_without_path_uses_whole_scalar(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [{"id": "count", "topic": "line1/count", "payload_format": "json"}], host=host, port=port
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("line1/count", b"7", qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].value == 7
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_malformed_json_maps_to_bad_quality(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [{"id": "broken", "topic": "line1/broken", "payload_format": "json"}], host=host, port=port
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("line1/broken", b"{not valid json", qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].quality == Quality.BAD
        assert updates[0].value == 0
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_missing_json_path_maps_to_bad_quality(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [
            {
                "id": "missing",
                "topic": "line1/missing",
                "payload_format": "json",
                "json_path": "no.such.field",
            }
        ],
        host=host,
        port=port,
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("line1/missing", b'{"other": 1}', qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].quality == Quality.BAD
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_scaling_applied_to_numeric_raw_value(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [{"id": "scaled", "topic": "line1/raw", "scaling": {"scale": 2.0, "offset": 1.0}}],
        host=host,
        port=port,
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("line1/raw", b"10", qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].value == pytest.approx(21.0)  # 10 * 2.0 + 1.0
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_wildcard_topic_subscription_matches_concrete_topic(
    mqtt_broker: tuple[str, int],
) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [{"id": "any_room_temp", "topic": "sensors/+/temperature"}], host=host, port=port
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("sensors/room7/temperature", b"19.2", qos=1)

    try:
        updates = await _run_and_collect(driver, config, publish, 1)
        assert updates[0].value == pytest.approx(19.2)
        assert updates[0].source_address == "sensors/+/temperature"
    finally:
        publisher.loop_stop()
        publisher.disconnect()


async def test_write_returns_not_supported(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config([{"id": "t1", "topic": "x/y"}], host=host, port=port)
    driver = MqttSubscriberDriver()
    await driver.configure(config)
    await driver.connect()
    try:
        result = await driver.write("some_tag", 1)
        assert result.success is False
    finally:
        await driver.disconnect()


async def test_get_metrics_tracks_reads_and_errors(mqtt_broker: tuple[str, int]) -> None:
    host, port = mqtt_broker
    config = _driver_config(
        [
            {"id": "good", "topic": "line1/good"},
            {"id": "bad", "topic": "line1/bad", "payload_format": "json"},
        ],
        host=host,
        port=port,
    )
    driver = MqttSubscriberDriver()
    publisher = _publisher(host, port)

    async def publish() -> None:
        publisher.publish("line1/good", b"1", qos=1)
        publisher.publish("line1/bad", b"not json", qos=1)

    try:
        await _run_and_collect(driver, config, publish, 2)
        metrics = driver.get_metrics()
        assert metrics.tag_read_count == 2
        assert metrics.error_count == 1
    finally:
        publisher.loop_stop()
        publisher.disconnect()
