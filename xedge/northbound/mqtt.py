"""MQTT + Sparkplug B northbound connector (FR-NB-001..FR-NB-006, FR-NB-010).

Bridges the synchronous, thread-based paho-mqtt client into asyncio: the
network loop runs in paho's own background thread (`loop_start()`); calls
that must block (connect, publish-with-confirmation) run via
`run_in_executor` so they don't block the event loop, and the `on_connect`
callback (fired from paho's thread) hands off to asyncio via
`call_soon_threadsafe`.

TLS/mTLS (SR-TS-001/002, Sprint C4 XEDGE-441 — this shipped without it since
Sprint 13's original target, a real, load-bearing gap: credentials crossed
the wire in clear) is `tls_enabled`-gated below, verifying the broker
against `tls_ca_certs_path` (the system trust store if unset) and
optionally presenting a client certificate for the broker's own mTLS, if
it requires one — independent of, and not necessarily using, this
device's fleet-CA identity (XEDGE-440/442): a customer's MQTT broker is
typically a separate trust domain from xEdge's own fleet PKI. The
exponential-backoff reconnect state machine (FR-NB-010) beyond a single
connect attempt remains out of scope; this connector raises on connect
failure and lets the caller (dispatcher) retry.

NCMD subscription (Sprint 31, XEDGE-223 write-back) is opt-in via the
`write_router` constructor argument: if set, `connect()` also subscribes to
this edge node's NCMD topic and routes each decoded, non-null metric's
write through `WriteRouter` under the `"mqtt-ncmd"` system actor, exactly
like the REST write endpoint routes through the same `WriteRouter` under
the human username. No per-driver-instance DCMD subscription — this
connector never births per-instance Sparkplug Devices (see `_publish_birth`),
so a metric's name is the same `instance_id/tag_name` id NDATA already
publishes it under.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from xedge.core.pipeline import UnifiedTag
from xedge.core.write_router import WriteRouter
from xedge.drivers.base import Quality, WriteResult
from xedge.northbound.base import ConnectorMetrics, NorthboundConnector, PublishResult
from xedge.northbound.sparkplug.payload import (
    DataType,
    SparkplugMetric,
    decode_payload,
    encode_payload,
    infer_datatype,
)
from xedge.northbound.sparkplug.session import SparkplugSession, build_topic
from xedge.observability.logging import get_logger
from xedge.security.tls_context import build_client_tls_context

logger = get_logger(__name__)

_BIRTH_QOS = 0
_DEATH_QOS = 1
_BD_SEQ_METRIC_NAME = "bdSeq"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class SparkplugConnectorConfig:
    host: str
    port: int = 1883
    group_id: str = "xedge"
    edge_node_id: str = "edge01"
    client_id: str = ""
    keepalive_seconds: int = 60
    connect_timeout_seconds: float = 10.0
    publish_timeout_seconds: float = 10.0
    qos: int = 1
    username: str | None = None
    password: str | None = None
    tls_enabled: bool = False
    tls_ca_certs_path: str | None = None
    tls_certfile_path: str | None = None
    tls_keyfile_path: str | None = None
    # Disables broker *hostname* verification only (not certificate
    # validity) — paho's own tls_insecure_set(), for brokers reachable only
    # by an IP a certificate can't reasonably enumerate in advance. Matches
    # the `verify_tls`-style escape hatch this codebase already uses for
    # the fleet agent's own bootstrap connection.
    tls_insecure: bool = False


class MqttConnectorStateError(RuntimeError):
    """Raised when an operation is attempted out of order (e.g. publish
    before connect())."""


class MqttSparkplugConnector(NorthboundConnector):
    def __init__(
        self, config: SparkplugConnectorConfig, write_router: WriteRouter | None = None
    ) -> None:
        self._config = config
        self._session = SparkplugSession()
        self._client: mqtt.Client | None = None
        self._metrics = ConnectorMetrics()
        self._write_router = write_router

    def _topic(self, message_type: str) -> str:
        return build_topic(message_type, self._config.group_id, self._config.edge_node_id)

    def _require_client(self) -> mqtt.Client:
        if self._client is None:
            raise MqttConnectorStateError("connect() must be called before this operation")
        return self._client

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        connected_event = asyncio.Event()
        connect_failure: list[str] = []

        client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=self._config.client_id)
        if self._config.username:
            client.username_pw_set(self._config.username, self._config.password)
        if self._config.tls_enabled:
            context = build_client_tls_context(
                ca_certs_path=(
                    Path(self._config.tls_ca_certs_path) if self._config.tls_ca_certs_path else None
                ),
                certfile_path=(
                    Path(self._config.tls_certfile_path) if self._config.tls_certfile_path else None
                ),
                keyfile_path=(
                    Path(self._config.tls_keyfile_path) if self._config.tls_keyfile_path else None
                ),
            )
            client.tls_set_context(context)
            if self._config.tls_insecure:
                client.tls_insecure_set(True)

        death_payload = encode_payload(
            timestamp_ms=_now_ms(),
            seq=None,
            metrics=[
                SparkplugMetric(
                    name=_BD_SEQ_METRIC_NAME,
                    timestamp_ms=_now_ms(),
                    datatype=DataType.UINT64,
                    value=self._session.death_payload_bd_seq(),
                )
            ],
        )
        client.will_set(self._topic("NDEATH"), death_payload, qos=_DEATH_QOS, retain=False)

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
        if self._write_router is not None:
            client.on_message = self._make_on_message(loop, self._write_router)
        self._client = client

        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    client.connect,
                    self._config.host,
                    self._config.port,
                    self._config.keepalive_seconds,
                ),
                timeout=self._config.connect_timeout_seconds,
            )
        except TimeoutError:
            self._client = None
            raise
        client.loop_start()
        try:
            await asyncio.wait_for(
                connected_event.wait(), timeout=self._config.connect_timeout_seconds
            )
        except TimeoutError:
            client.loop_stop()
            self._client = None
            raise

        if connect_failure:
            client.loop_stop()
            self._client = None
            raise ConnectionError(f"MQTT CONNACK failure: {connect_failure[0]}")

        logger.info("mqtt.connected", host=self._config.host, port=self._config.port)
        if self._write_router is not None:
            client.subscribe(self._topic("NCMD"))
        await self._publish_birth()

    def _make_on_message(
        self, loop: asyncio.AbstractEventLoop, write_router: WriteRouter
    ) -> Callable[[mqtt.Client, object, mqtt.MQTTMessage], None]:
        """Builds the `on_message` callback (fires on paho's own network
        thread, per the module docstring — never call back into asyncio
        directly from here). Decodes an incoming NCMD payload and routes
        each non-null metric's write through `write_router`, fire-and-
        forget (`run_coroutine_threadsafe`, not awaited from this thread) —
        `WriteRouter.write` itself is the single source of truth for the
        actual outcome via its own audit log entry; a logged warning here
        covers only the narrower "the coroutine itself blew up" case."""

        def _on_message(_client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
            try:
                _timestamp_ms, _seq, metrics = decode_payload(message.payload)
            except Exception as exc:  # noqa: BLE001 — a malformed NCMD must not crash the MQTT thread
                logger.warning("mqtt.ncmd_decode_failed", error=str(exc))
                return
            for metric in metrics:
                if (
                    metric.is_null
                    or metric.value is None
                    or metric.name is None
                    or "/" not in metric.name
                ):
                    continue
                instance_id, _, tag_name = metric.name.partition("/")
                future = asyncio.run_coroutine_threadsafe(
                    write_router.write("mqtt-ncmd", instance_id, tag_name, metric.value), loop
                )
                future.add_done_callback(_log_ncmd_write_future_exception(metric.name))

        return _on_message

    async def _publish_birth(self) -> None:
        bd_seq = self._session.start_birth()
        metrics = [
            SparkplugMetric(
                name=_BD_SEQ_METRIC_NAME,
                timestamp_ms=_now_ms(),
                datatype=DataType.UINT64,
                value=bd_seq,
            )
        ]
        payload = encode_payload(timestamp_ms=_now_ms(), seq=0, metrics=metrics)
        await self._publish_raw(self._topic("NBIRTH"), payload, qos=_BIRTH_QOS)
        logger.info("sparkplug.nbirth_published", bd_seq=bd_seq)

    async def publish(self, batch: list[UnifiedTag]) -> PublishResult:
        if self._client is None:
            return PublishResult(success=False, count=0, error_message="not connected")
        if not batch:
            return PublishResult(success=True, count=0)

        metrics = [_to_sparkplug_metric(tag) for tag in batch]
        seq = self._session.next_seq()
        payload = encode_payload(timestamp_ms=_now_ms(), seq=seq, metrics=metrics)

        try:
            await self._publish_raw(self._topic("NDATA"), payload, qos=self._config.qos)
        except Exception as exc:  # noqa: BLE001 — reported via PublishResult, not raised
            self._metrics.error_count += 1
            logger.warning("mqtt.publish_failed", error=str(exc))
            return PublishResult(success=False, count=0, error_message=str(exc))

        self._metrics.published_count += len(batch)
        self._metrics.last_successful_publish = datetime.now(UTC)
        return PublishResult(success=True, count=len(batch))

    async def _publish_raw(self, topic: str, payload: bytes, qos: int) -> None:
        client = self._require_client()
        loop = asyncio.get_running_loop()

        def _do_publish() -> None:
            info = client.publish(topic, payload, qos=qos)
            if qos > 0:
                info.wait_for_publish(timeout=self._config.publish_timeout_seconds)
                if not info.is_published():
                    raise TimeoutError(f"Publish to {topic!r} did not confirm within timeout")

        await loop.run_in_executor(None, _do_publish)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        client = self._client
        loop = asyncio.get_running_loop()
        # A clean MQTT DISCONNECT suppresses the registered Will (NDEATH),
        # per the MQTT spec — correct: NDEATH should fire only on an
        # unexpected drop, not a graceful shutdown (FR-NB-006).
        await loop.run_in_executor(None, client.disconnect)
        client.loop_stop()
        self._session.advance_bd_seq()
        self._client = None

    def get_metrics(self) -> ConnectorMetrics:
        return self._metrics

    def is_alive(self) -> bool:
        return self._client is not None and self._client.is_connected()


def _log_ncmd_write_future_exception(
    tag_id: str,
) -> Callable[[concurrent.futures.Future[WriteResult]], None]:
    # asyncio.run_coroutine_threadsafe returns a concurrent.futures.Future
    # (it's meant to be waited on from a non-asyncio thread), not an
    # asyncio.Future — its done-callback runs on the event loop thread
    # regardless of which thread completed it, so a plain logger.warning
    # here is safe.
    def _callback(future: concurrent.futures.Future[WriteResult]) -> None:
        exc = future.exception()
        if exc is not None:
            logger.warning("mqtt.ncmd_write_failed", tag_id=tag_id, error=str(exc))

    return _callback


def _to_sparkplug_metric(tag: UnifiedTag) -> SparkplugMetric:
    """Map a UnifiedTag to a Sparkplug B metric per system-architecture.md §4.3:
    Bad quality -> is_null=true. Alias-based compact encoding (name omitted
    after birth) is a later optimization; every NDATA metric here carries
    its full name."""
    is_null = tag.quality == Quality.BAD
    return SparkplugMetric(
        name=tag.tag_id,
        timestamp_ms=int(tag.timestamp.timestamp() * 1000),
        datatype=infer_datatype(tag.value),
        value=None if is_null else tag.value,
        is_null=is_null,
    )
