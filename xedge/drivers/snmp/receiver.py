"""SNMP TRAP/INFORM receiver driver (FR-SB-006; Sprint C8, XEDGE-484).

Event-driven, not polled -- the same shape as
`xedge.drivers.mqtt_subscriber.client.MqttSubscriberDriver`: an inbound
notification arrives whenever the sending device chooses to send one, not
on a `scan_rate_ms` schedule. `run()` does no polling loop at all; it just
waits until cancelled while `pysnmp`'s own `ntfrcv.NotificationReceiver`
callback -- registered once in `connect()` -- pushes a `TagUpdate` onto
`output` for each matching varbind as notifications arrive.

Confirmed directly against a real local sender/receiver pair before
writing this (not assumed from docs): `ntfrcv.NotificationReceiver`
correctly handles both TRAP (fire-and-forget) and INFORM (the sender gets
a real acknowledgement back) through the exact same callback -- no branch
on notification type is needed on the receiving side, unlike the
originator side (`xedge.core.snmp_notify`), which does have to say which
kind it wants sent.

The callback pysnmp invokes is a **synchronous** function, not a
coroutine -- but it still runs on this process's own asyncio loop (pysnmp
schedules it from within its own transport callback, not a separate
thread), so calling `asyncio.Queue.put_nowait` directly from it is safe
without any cross-thread handoff. `put_nowait` (not `put`) is deliberate:
a synchronous callback cannot `await` a full queue the way every other
driver's `run()` loop can, so a full queue drops the update (logged) with
backpressure surfacing as a metric instead of the callback blocking
pysnmp's own dispatcher.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config as snmp_config
from pysnmp.entity import engine as snmp_engine_module
from pysnmp.entity.rfc3413 import ntfrcv

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
from xedge.drivers.snmp import asn1types
from xedge.drivers.snmp.client import AUTH_PROTOCOLS, PRIV_PROTOCOLS, SnmpConfigError
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_PORT = 162

__all__ = ["SnmpTrapReceiverDriver", "SnmpTrapReceiverStateError"]


class SnmpTrapReceiverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class SnmpTrapReceiverDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._metrics = DriverMetrics()
        self._tags_by_oid: dict[str, dict[str, Any]] = {}
        self._engine: snmp_engine_module.SnmpEngine | None = None
        self._output: asyncio.Queue[TagUpdate] | None = None
        self._stop_event: asyncio.Event | None = None

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise SnmpTrapReceiverStateError("configure() must be called before this operation")
        return self._config

    async def configure(self, config: DriverConfig) -> None:
        self._config = config
        self._tags_by_oid = {
            tag["trap_oid"]: tag for group in config.tag_groups for tag in group["tags"]
        }

    async def connect(self) -> None:
        cfg = self._require_config().config
        engine_obj = snmp_engine_module.SnmpEngine()
        try:
            snmp_config.add_transport(
                engine_obj,
                udp.DOMAIN_NAME,
                udp.UdpTransport().open_server_mode(
                    (cfg.get("host", "0.0.0.0"), cfg.get("port", _DEFAULT_PORT))  # nosec B104
                ),
            )
        except Exception as exc:  # noqa: BLE001 -- any bind failure is a connect failure
            raise DriverConnectionError(f"failed to bind SNMP trap listener: {exc}") from exc

        version = cfg["version"]
        if version in ("v1", "v2c"):
            if not cfg.get("community"):
                raise SnmpConfigError(f"version {version!r} requires 'community'")
            snmp_config.add_v1_system(engine_obj, "xedge-trap-receiver", cfg["community"])
        else:
            if not cfg.get("v3_username"):
                raise SnmpConfigError("version 'v3' requires 'v3_username'")
            auth_protocol_name = cfg.get("v3_auth_protocol", "none")
            priv_protocol_name = cfg.get("v3_priv_protocol", "none")
            if priv_protocol_name != "none" and auth_protocol_name == "none":
                raise SnmpConfigError(
                    "v3_priv_protocol requires v3_auth_protocol != 'none' "
                    "(RFC 3414: privacy without authentication is not a valid USM security level)"
                )
            snmp_config.add_v3_user(
                engine_obj,
                cfg["v3_username"],
                authProtocol=AUTH_PROTOCOLS[auth_protocol_name],
                authKey=cfg.get("v3_auth_password") or None,
                privProtocol=PRIV_PROTOCOLS[priv_protocol_name],
                privKey=cfg.get("v3_priv_password") or None,
            )

        ntfrcv.NotificationReceiver(engine_obj, self._on_notification)
        engine_obj.open_dispatcher()
        self._engine = engine_obj

    async def disconnect(self) -> None:
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        self._output = output
        self._stop_event = asyncio.Event()
        try:
            await self._stop_event.wait()
        finally:
            self._output = None

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        # Inbound-only, same as every notification/subscriber-shaped driver
        # in this codebase (mqtt_subscriber) -- there is nothing to write
        # to; this instance only ever receives.
        return WriteResult(success=False, tag_id=tag_id, error_message="write not supported")

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    def _on_notification(
        self,
        snmp_engine: Any,
        state_reference: Any,
        context_engine_id: Any,
        context_name: Any,
        var_binds: list[tuple[Any, Any]],
        cb_ctx: Any,
    ) -> None:
        instance_id = self._config.instance_id if self._config else "snmp_trap_receiver"
        output = self._output
        now = datetime.now(UTC)
        for oid, value in var_binds:
            oid_str = str(oid)
            tag = self._tags_by_oid.get(oid_str)
            if tag is None:
                continue
            self._metrics.tag_read_count += 1
            self._metrics.last_successful_read = now
            update = TagUpdate(
                tag_id=f"{instance_id}/{tag['id']}",
                timestamp=now,
                value=asn1types.decode(value),
                quality=Quality.GOOD,
                source_driver=instance_id,
                source_address=oid_str,
                metadata={"snmp_type": type(value).__name__},
            )
            if output is None:
                logger.warning(
                    "snmp_trap_receiver.dropped_before_run",
                    instance_id=instance_id,
                    tag_id=tag["id"],
                )
                continue
            try:
                output.put_nowait(update)
            except asyncio.QueueFull:
                self._metrics.error_count += 1
                logger.warning(
                    "snmp_trap_receiver.queue_full_dropped_update",
                    instance_id=instance_id,
                    tag_id=tag["id"],
                )
