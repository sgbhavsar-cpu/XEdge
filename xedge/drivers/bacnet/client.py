"""BACnet/IP client driver (Sprint 21, XEDGE-160/162/164).

Built directly on `bacpypes3` (MIT, ADR-006 "buy" decision — unlike the
in-house clean-room protocol stacks, this is a direct library integration).
ReadProperty polling only for this MVP (no COV subscriptions, no device
discovery): each tag names its target device address and object directly,
the same granularity the Modbus/OPC UA drivers already use.

Two structural differences from `xedge.drivers.opcua.client.OpcUaClientDriver`,
both confirmed by exercising bacpypes3 directly (not just its docs) against a
real local BACnet/IP device before writing this:

1. BACnet/IP has no "connect to a remote device" step — `Application` just
   binds a local UDP socket; there is no handshake with a remote device the
   way `asyncua.Client.connect()` performs a TCP connect. An unreachable or
   nonexistent remote device is therefore never a `connect()`-time failure —
   it only ever surfaces per-tag, at read time, as a Bad-quality `TagUpdate`.
2. A local UDP bind failure (e.g. the configured port already in use) does
   **not** raise a catchable exception from `Application.from_object_list()`
   — bacpypes3 binds asynchronously in a background task and, on failure,
   only logs "Exception in callback" via asyncio's default unhandled-task-
   exception handler, then silently retries forever. To still get an
   actionable, supervisor-visible failure at `connect()` time, this driver
   does its own preflight UDP bind-and-release check before handing the
   address to bacpypes3.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from typing import Any

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.app import Application
from bacpypes3.basetypes import BinaryPV
from bacpypes3.constructeddata import AnyAtomic
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject
from opentelemetry.trace import Status, StatusCode

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
from xedge.observability.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_DEFAULT_REQUEST_TIMEOUT_SECONDS = 3.0
_DEFAULT_PROPERTY_IDENTIFIER = "present-value"


class BacnetDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class BacnetIpDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._app: Application | None = None
        self._metrics = DriverMetrics()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise BacnetDriverStateError("configure() must be called before this operation")
        return self._config

    def _require_app(self) -> Application:
        if self._app is None:
            raise BacnetDriverStateError("connect() must be called before this operation")
        return self._app

    async def configure(self, config: DriverConfig) -> None:
        self._config = config

    async def connect(self) -> None:
        cfg = self._require_config().config
        local_address = cfg["local_address"]
        _check_local_address_available(local_address)

        device_instance = cfg["device_instance"]
        device_object = DeviceObject(
            objectIdentifier=("device", device_instance),
            objectName=f"xedge-{self._require_config().instance_id}",
        )
        network_port_object = NetworkPortObject(
            local_address,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )
        try:
            self._app = Application.from_object_list([device_object, network_port_object])
        except Exception as exc:  # noqa: BLE001 — any construction failure is a connect failure
            raise DriverConnectionError(f"failed to start BACnet/IP application: {exc}") from exc

    async def disconnect(self) -> None:
        if self._app is not None:
            try:
                self._app.close()
            except Exception as exc:  # noqa: BLE001 — disconnect must never raise
                logger.warning("bacnet.disconnect_error", error=str(exc))
            self._app = None

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        config = self._require_config()
        group_tasks = [
            asyncio.create_task(self._poll_group(group, output)) for group in config.tag_groups
        ]
        try:
            await asyncio.gather(*group_tasks)
        finally:
            for task in group_tasks:
                task.cancel()
            await asyncio.gather(*group_tasks, return_exceptions=True)

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        # Write-back (FR-NB-009) has no caller anywhere in xEdge yet; kept as
        # a stub matching every other driver's current scope.
        return WriteResult(success=False, tag_id=tag_id, error_message="write not yet supported")

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        instance_id = self._config.instance_id if self._config else "bacnet"
        interval_seconds = group["scan_rate_ms"] / 1000
        timeout_seconds = self._require_config().config.get(
            "request_timeout_seconds", _DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        tags = group["tags"]
        while True:
            for tag in tags:
                update = await self._read_tag(instance_id, tag, timeout_seconds)
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tag(
        self, instance_id: str, tag: dict[str, Any], timeout_seconds: float
    ) -> TagUpdate:
        tag_id = f"{instance_id}/{tag['id']}"
        device_address = tag["device_address"]
        object_identifier = tag["object_identifier"]
        property_identifier = tag.get("property_identifier", _DEFAULT_PROPERTY_IDENTIFIER)
        app = self._require_app()
        with tracer.start_as_current_span(
            "driver.read",
            attributes={"driver.instance_id": instance_id, "tag.id": tag["id"]},
        ) as span:
            try:
                raw_value = await asyncio.wait_for(
                    app.read_property(device_address, object_identifier, property_identifier),
                    timeout=timeout_seconds,
                )
                if isinstance(raw_value, AnyAtomic):
                    raw_value = raw_value.get_value()
                value = _coerce_value(raw_value)
                self._metrics.tag_read_count += 1
                self._metrics.last_successful_read = datetime.now(UTC)
                span.set_attribute("quality", Quality.GOOD.value)
                return TagUpdate(
                    tag_id=tag_id,
                    timestamp=datetime.now(UTC),
                    value=value,
                    quality=Quality.GOOD,
                    source_driver=instance_id,
                    source_address=f"{device_address}/{object_identifier}",
                )
            # ErrorRejectAbortNack (unknown object/device, timeout, reject, abort)
            # is a BaseException subclass, not Exception — must be named
            # explicitly or it silently escapes a bare `except Exception`.
            except (ErrorRejectAbortNack, TimeoutError, OSError) as exc:
                self._metrics.error_count += 1
                span.set_attribute("quality", Quality.BAD.value)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                logger.warning(
                    "bacnet.read_failed", instance_id=instance_id, tag_id=tag["id"], error=str(exc)
                )
                return TagUpdate(
                    tag_id=tag_id,
                    timestamp=datetime.now(UTC),
                    value=0,
                    quality=Quality.BAD,
                    source_driver=instance_id,
                    source_address=f"{device_address}/{object_identifier}",
                    metadata={"bacnet_error": str(exc)},
                )


def _check_local_address_available(local_address: str) -> None:
    """Preflight UDP bind check (see module docstring point 2) — bacpypes3
    itself won't raise a catchable exception for a local bind conflict, so
    this is the only way `connect()` can surface one to the supervisor."""
    host, _, port_str = local_address.rpartition(":")
    if not host or not port_str.isdigit():
        raise DriverConnectionError(
            f"local_address must be 'host:port', got {local_address!r}"
        )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, int(port_str)))
    except OSError as exc:
        raise DriverConnectionError(
            f"cannot bind BACnet/IP local_address {local_address!r}: {exc}"
        ) from exc
    finally:
        sock.close()


def _coerce_value(value: Any) -> TagValue:
    """Coerce a bacpypes3 primitive to xEdge's TagValue union. bacpypes3's
    numeric types (Real, Unsigned, Integer) are real float/int subclasses
    (confirmed directly), so they pass the isinstance checks below, but are
    still normalized to plain builtins rather than letting a library-specific
    subclass propagate into the pipeline/JSON serialization. BinaryPV (a
    binary object's present-value) is an Enumerated, not a bool — it's
    special-cased first since bool is also technically int-like."""
    if isinstance(value, BinaryPV):
        return bool(int(value))
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)
