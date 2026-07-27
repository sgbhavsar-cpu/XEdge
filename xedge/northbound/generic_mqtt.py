"""Generic MQTT publisher (Sprint C5, XEDGE-451; CRD §4.10): "own payload
structure" as an alternative to `xedge.northbound.mqtt.MqttSparkplugConnector`,
which is hard-wired to Sparkplug B and cannot be reconfigured into anything
else. This connector publishes plain JSON, with configurable topic and
field names, to whatever broker/topic the operator wants — no Sparkplug
birth/death/sequence-number state machine, no protobuf.

Deliberately a *separate* connector rather than a mode bolted onto
`MqttSparkplugConnector`: that class's `SparkplugSession` (bdSeq, NBIRTH/
NDEATH lifecycle) has no equivalent concept here, and retrofitting an
"if not sparkplug" branch through every one of its methods would leave
both halves harder to read than two connectors implementing the same
`NorthboundConnector` interface side by side. Same paho-mqtt-in-asyncio
threading pattern either way (proven in `MqttSparkplugConnector`): the
network loop runs on paho's own thread; `on_connect` hands off to asyncio
via `call_soon_threadsafe`; blocking calls (connect, publish-with-
confirmation) run via `run_in_executor`.

"Event-driven" publishing (CRD §4.10) is approximated by a short
`xedge.northbound.dispatcher.NorthboundDispatcher.publish_interval_seconds`
rather than a dedicated per-value change-notification path — the
dispatcher drains and publishes on a timer for every connector uniformly;
building true publish-the-instant-a-value-changes semantics would mean
restructuring how the ring buffer feeds the dispatcher, which is a bigger
change than this sprint's stated scope. Manual re-publish (XEDGE-452) is
the dispatcher's `trigger_publish()`, generic to any connector, not
special-cased here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from xedge.core.pipeline import UnifiedTag
from xedge.drivers.base import Quality
from xedge.northbound.base import ConnectorMetrics, NorthboundConnector, PublishResult
from xedge.observability.logging import get_logger
from xedge.security.tls_context import build_client_tls_context

logger = get_logger(__name__)

_DEFAULT_FIELD_NAMES: dict[str, str] = {
    "tag_id": "tag_id",
    "value": "value",
    "timestamp": "timestamp",
    "quality": "quality",
}


class GenericMqttConnectorStateError(RuntimeError):
    """Raised when an operation is attempted out of order (e.g. publish
    before connect())."""


@dataclass(frozen=True, slots=True)
class GenericMqttPublisherConfig:
    host: str
    port: int = 1883
    client_id: str = ""
    keepalive_seconds: int = 60
    connect_timeout_seconds: float = 10.0
    publish_timeout_seconds: float = 10.0
    qos: int = 1
    retain: bool = False
    username: str | None = None
    password: str | None = None
    tls_enabled: bool = False
    tls_ca_certs_path: str | None = None
    tls_certfile_path: str | None = None
    tls_keyfile_path: str | None = None
    tls_insecure: bool = False
    # {tag_id} is substituted per message in "per_tag" mode; must not be
    # used (there is no single tag_id) in "batch" mode.
    topic_template: str = "xedge/{tag_id}"
    payload_mode: str = "per_tag"  # "per_tag" | "batch"
    include_timestamp: bool = True
    include_quality: bool = True
    field_names: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_FIELD_NAMES))


class GenericMqttPublisher(NorthboundConnector):
    def __init__(self, config: GenericMqttPublisherConfig) -> None:
        if config.payload_mode not in ("per_tag", "batch"):
            raise ValueError(
                f"payload_mode must be 'per_tag' or 'batch', got {config.payload_mode!r}"
            )
        if config.payload_mode == "batch" and "{tag_id}" in config.topic_template:
            raise ValueError("topic_template cannot use {tag_id} when payload_mode is 'batch'")
        self._config = config
        self._client: mqtt.Client | None = None
        self._metrics = ConnectorMetrics()

    def _require_client(self) -> mqtt.Client:
        if self._client is None:
            raise GenericMqttConnectorStateError("connect() must be called before this operation")
        return self._client

    async def connect(self) -> None:
        cfg = self._config
        loop = asyncio.get_running_loop()
        connected_event = asyncio.Event()
        connect_failure: list[str] = []

        client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=cfg.client_id)
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls_enabled:
            context = build_client_tls_context(
                ca_certs_path=Path(cfg.tls_ca_certs_path) if cfg.tls_ca_certs_path else None,
                certfile_path=Path(cfg.tls_certfile_path) if cfg.tls_certfile_path else None,
                keyfile_path=Path(cfg.tls_keyfile_path) if cfg.tls_keyfile_path else None,
            )
            client.tls_set_context(context)
            if cfg.tls_insecure:
                client.tls_insecure_set(True)

        def _on_connect(
            client: mqtt.Client,
            userdata: object,
            flags: object,
            reason_code: object,
            properties: object = None,
        ) -> None:
            if getattr(reason_code, "is_failure", False):
                connect_failure.append(str(reason_code))
            loop.call_soon_threadsafe(connected_event.set)

        client.on_connect = _on_connect
        self._client = client

        await loop.run_in_executor(
            None, client.connect, cfg.host, cfg.port, cfg.keepalive_seconds
        )
        client.loop_start()
        try:
            await asyncio.wait_for(connected_event.wait(), timeout=cfg.connect_timeout_seconds)
        except TimeoutError:
            client.loop_stop()
            self._client = None
            raise

        if connect_failure:
            client.loop_stop()
            self._client = None
            raise ConnectionError(f"MQTT CONNACK failure: {connect_failure[0]}")

        logger.info("generic_mqtt.connected", host=cfg.host, port=cfg.port)

    async def publish(self, batch: list[UnifiedTag]) -> PublishResult:
        if self._client is None:
            return PublishResult(success=False, count=0, error_message="not connected")
        if not batch:
            return PublishResult(success=True, count=0)

        try:
            if self._config.payload_mode == "batch":
                await self._publish_raw(
                    self._config.topic_template, _encode_batch(batch, self._config)
                )
            else:
                for tag in batch:
                    topic = self._config.topic_template.format(tag_id=tag.tag_id)
                    await self._publish_raw(topic, _encode_one(tag, self._config))
        except Exception as exc:  # noqa: BLE001 — reported via PublishResult, not raised
            self._metrics.error_count += 1
            logger.warning("generic_mqtt.publish_failed", error=str(exc))
            return PublishResult(success=False, count=0, error_message=str(exc))

        self._metrics.published_count += len(batch)
        self._metrics.last_successful_publish = datetime.now(UTC)
        return PublishResult(success=True, count=len(batch))

    async def _publish_raw(self, topic: str, payload: bytes) -> None:
        client = self._require_client()
        loop = asyncio.get_running_loop()
        cfg = self._config

        def _do_publish() -> None:
            info = client.publish(topic, payload, qos=cfg.qos, retain=cfg.retain)
            if cfg.qos > 0:
                info.wait_for_publish(timeout=cfg.publish_timeout_seconds)
                if not info.is_published():
                    raise TimeoutError(f"Publish to {topic!r} did not confirm within timeout")

        await loop.run_in_executor(None, _do_publish)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        client = self._client
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, client.disconnect)
        client.loop_stop()
        self._client = None

    def get_metrics(self) -> ConnectorMetrics:
        return self._metrics

    def is_alive(self) -> bool:
        return self._client is not None and self._client.is_connected()


def _tag_to_dict(tag: UnifiedTag, config: GenericMqttPublisherConfig) -> dict[str, Any]:
    names = config.field_names
    record: dict[str, Any] = {
        names.get("tag_id", "tag_id"): tag.tag_id,
        names.get("value", "value"): None if tag.quality == Quality.BAD else tag.value,
    }
    if config.include_timestamp:
        record[names.get("timestamp", "timestamp")] = tag.timestamp.isoformat()
    if config.include_quality:
        record[names.get("quality", "quality")] = tag.quality.value
    return record


def _encode_one(tag: UnifiedTag, config: GenericMqttPublisherConfig) -> bytes:
    return json.dumps(_tag_to_dict(tag, config)).encode("utf-8")


def _encode_batch(batch: list[UnifiedTag], config: GenericMqttPublisherConfig) -> bytes:
    return json.dumps([_tag_to_dict(tag, config) for tag in batch]).encode("utf-8")
