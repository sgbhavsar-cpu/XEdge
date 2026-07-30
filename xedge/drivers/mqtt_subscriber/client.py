"""Generic MQTT Subscriber driver (Sprint C5, XEDGE-450; CRD §4.10 —
"receive data from external sources... payload parsing/mapping," entirely
absent before this).

A driver, not a northbound connector: this brings external data IN and
turns it into TagUpdates, the same job every other driver does — it just
happens to speak MQTT southbound instead of Modbus/BACnet/OPC UA. Kept
architecturally distinct from `xedge.northbound.mqtt.MqttSparkplugConnector`,
which only ever publishes OUT; that connector never claims to receive data
from anywhere but its own NCMD write-back topic, and this driver has no
relationship to Sparkplug B at all — an external publisher here can use
whatever payload shape this driver's config declares.

Threading model matches `MqttSparkplugConnector`'s already-proven pattern:
paho-mqtt's network loop runs on its own thread (`loop_start()`); the
`on_message`/`on_connect` callbacks (fired from that thread) hand off to
this driver's asyncio event loop via `call_soon_threadsafe` only — never
touching the asyncio queue directly from paho's thread.

Each configured tag maps to exactly one topic and one fixed tag_id
(`{instance_id}/{tag_id}`), matching every other driver's tag-identity
model. A wildcarded topic (`+`/`#`) is allowed — MQTT itself allows it —
but every message matching it updates the *same* tag_id; there is no
per-concrete-topic fan-out into dynamically created tags. That would need
the pipeline to support tag sets not fully known at config time, which it
doesn't today (e.g. `xedge.core.main._infer_tag_initial_values` enumerates
every tag_id up front) — a real limitation, not an oversight, stated here
rather than silently capped.

No `data_type` config knob, unlike protocols with a wire-level type system
(Modbus registers, OPC UA nodes): matching how every other driver in this
codebase actually works, a tag's type is whatever Python type its decoded
value naturally is (`xedge.core.pipeline` infers `UnifiedTag.data_type`
from the value itself, e.g. `bool` -> `"BOOL"`), not something declared
ahead of time.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
    DriverConnectionError,
    DriverMetrics,
    Quality,
    TagUpdate,
    TagValue,
    WriteResult,
)
from xedge.observability.logging import get_logger
from xedge.security.tls_context import build_client_tls_context

logger = get_logger(__name__)

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_RECEIVED_QUEUE_MAXSIZE = 1000


class MqttSubscriberStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class PayloadDecodeError(Exception):
    """Raised by `_decode_message` for a payload that cannot be turned into
    a tag value under the configured `payload_format`/`json_path`."""


@dataclass(frozen=True, slots=True)
class _TopicMapping:
    tag_id: str
    topic: str
    qos: int
    payload_format: str
    json_path: str | None
    scale: float
    offset: float


class MqttSubscriberDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._client: mqtt.Client | None = None
        # Keyed by the *configured* topic filter, which may itself be a
        # wildcard -- see _find_mapping for how an incoming concrete topic
        # resolves back to one of these.
        self._mappings: dict[str, _TopicMapping] = {}
        self._received_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(
            maxsize=_RECEIVED_QUEUE_MAXSIZE
        )
        self._metrics = DriverMetrics()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise MqttSubscriberStateError("configure() must be called before this operation")
        return self._config

    async def configure(self, config: DriverConfig) -> None:
        self._config = config
        self._mappings = {}
        for group in config.tag_groups:
            for tag in group["tags"]:
                scaling = tag.get("scaling", {})
                mapping = _TopicMapping(
                    tag_id=f"{config.instance_id}/{tag['id']}",
                    topic=tag["topic"],
                    qos=tag.get("qos", 0),
                    payload_format=tag.get("payload_format", "raw"),
                    json_path=tag.get("json_path"),
                    scale=scaling.get("scale", 1.0),
                    offset=scaling.get("offset", 0.0),
                )
                self._mappings[mapping.topic] = mapping

    async def connect(self) -> None:
        cfg = self._require_config().config
        loop = asyncio.get_running_loop()
        connected_event = asyncio.Event()
        connect_failure: list[str] = []

        client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=cfg.get("client_id", ""))
        if cfg.get("username"):
            client.username_pw_set(cfg["username"], cfg.get("password"))
        if cfg.get("tls_enabled", False):
            context = build_client_tls_context(
                ca_certs_path=cfg.get("tls_ca_certs_path"),
                certfile_path=cfg.get("tls_certfile_path"),
                keyfile_path=cfg.get("tls_keyfile_path"),
            )
            client.tls_set_context(context)
            if cfg.get("tls_insecure", False):
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
            else:
                for mapping in self._mappings.values():
                    client.subscribe(mapping.topic, qos=mapping.qos)
            loop.call_soon_threadsafe(connected_event.set)

        def _on_message(_client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
            loop.call_soon_threadsafe(
                self._received_queue.put_nowait, (message.topic, message.payload)
            )

        client.on_connect = _on_connect
        client.on_message = _on_message
        self._client = client

        await loop.run_in_executor(
            None,
            client.connect,
            cfg["host"],
            cfg.get("port", 1883),
            cfg.get("keepalive_seconds", 60),
        )
        client.loop_start()
        try:
            await asyncio.wait_for(
                connected_event.wait(),
                timeout=cfg.get("connect_timeout_seconds", _DEFAULT_CONNECT_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            client.loop_stop()
            self._client = None
            raise

        if connect_failure:
            client.loop_stop()
            self._client = None
            raise DriverConnectionError(f"MQTT CONNACK failure: {connect_failure[0]}")

        logger.info("mqtt_subscriber.connected", host=cfg["host"], port=cfg.get("port", 1883))

    async def disconnect(self) -> None:
        if self._client is None:
            return
        client = self._client
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, client.disconnect)
        client.loop_stop()
        self._client = None

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        instance_id = self._require_config().instance_id
        while True:
            topic, payload = await self._received_queue.get()
            mapping = self._find_mapping(topic)
            if mapping is None:
                logger.warning("mqtt_subscriber.unmapped_topic", topic=topic)
                continue
            update = _decode_message(instance_id, mapping, payload)
            self._metrics.tag_read_count += 1
            self._metrics.last_successful_read = datetime.now(UTC)
            if update.quality is Quality.BAD:
                self._metrics.error_count += 1
            await output.put(update)

    def _find_mapping(self, concrete_topic: str) -> _TopicMapping | None:
        exact = self._mappings.get(concrete_topic)
        if exact is not None:
            return exact
        # A wildcard subscription's concrete topic never matches the dict
        # key (the filter itself) -- fall back to a linear scan. Rare path
        # (most configs use concrete topics); correctness over speed here.
        return next(
            (m for m in self._mappings.values() if mqtt.topic_matches_sub(m.topic, concrete_topic)),
            None,
        )

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        return WriteResult(
            success=False, tag_id=tag_id, error_message="mqtt_subscriber tags are read-only"
        )

    def get_metrics(self) -> DriverMetrics:
        return self._metrics


def _decode_message(instance_id: str, mapping: _TopicMapping, payload: bytes) -> TagUpdate:
    try:
        value = _extract_value(mapping, payload)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = value * mapping.scale + mapping.offset
        return TagUpdate(
            tag_id=mapping.tag_id,
            timestamp=datetime.now(UTC),
            value=value,
            quality=Quality.GOOD,
            source_driver=instance_id,
            source_address=mapping.topic,
        )
    except (PayloadDecodeError, ValueError, KeyError, IndexError, UnicodeDecodeError) as exc:
        logger.warning(
            "mqtt_subscriber.decode_failed",
            tag_id=mapping.tag_id,
            topic=mapping.topic,
            error=str(exc),
        )
        return TagUpdate(
            tag_id=mapping.tag_id,
            timestamp=datetime.now(UTC),
            value=0,
            quality=Quality.BAD,
            source_driver=instance_id,
            source_address=mapping.topic,
            metadata={"decode_error": str(exc)},
        )


def _extract_value(mapping: _TopicMapping, payload: bytes) -> TagValue:
    if mapping.payload_format == "json":
        data = json.loads(payload.decode("utf-8"))
        value = _extract_json_path(data, mapping.json_path) if mapping.json_path else data
        return _coerce_json_value(value)
    return _coerce_raw_text(payload.decode("utf-8"))


def _extract_json_path(data: Any, path: str) -> Any:
    current = data
    for key in path.split("."):
        if isinstance(current, dict):
            if key not in current:
                raise PayloadDecodeError(f"JSON path {path!r}: no key {key!r} in object")
            current = current[key]
        elif isinstance(current, list):
            current = current[int(key)]
        else:
            raise PayloadDecodeError(
                f"JSON path {path!r}: cannot index {type(current).__name__} with {key!r}"
            )
    return current


def _coerce_json_value(value: Any) -> TagValue:
    if isinstance(value, (bool, int, float, str)):
        return value
    raise PayloadDecodeError(
        f"JSON value of type {type(value).__name__} is not a supported tag value"
    )


def _coerce_raw_text(text: str) -> TagValue:
    stripped = text.strip()
    if stripped.lower() in ("true", "false"):
        return stripped.lower() == "true"
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped
