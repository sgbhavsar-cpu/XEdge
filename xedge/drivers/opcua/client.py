"""OPC UA client driver (FR-SA-005..FR-SA-008).

Implemented directly against `asyncua` for the MVP (ADR-006 §7 amendment,
2026-07-04) rather than the eventual open62541 C-extension binding — see
that amendment for rationale and licensing constraints (GPL edition only
until LGPL-3.0 obligations are reviewed).

Polling mode only for this MVP (FR-SA-006 lists both subscriptions and
polling as acceptable); MonitoredItem-based subscriptions are a fast-follow
enhancement that can reuse the same TagUpdate construction.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from asyncua import ua as ua_types
from asyncua.client.client import Client
from asyncua.common.node import Node
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaError
from opentelemetry.trace import Status, StatusCode

from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
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

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0


class OpcUaDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class OpcUaClientDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._client: Client | None = None
        self._metrics = DriverMetrics()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise OpcUaDriverStateError("configure() must be called before this operation")
        return self._config

    def _require_client(self) -> Client:
        if self._client is None:
            raise OpcUaDriverStateError("connect() must be called before this operation")
        return self._client

    async def configure(self, config: DriverConfig) -> None:
        self._config = config

    async def connect(self) -> None:
        cfg = self._require_config().config
        client = Client(
            url=cfg["endpoint_url"],
            timeout=cfg.get("connect_timeout_seconds", _DEFAULT_CONNECT_TIMEOUT_SECONDS),
        )
        if cfg.get("username"):
            client.set_user(cfg["username"])
            if cfg.get("password"):
                client.set_password(cfg["password"])
        await client.connect()
        self._client = client

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except (TimeoutError, ConnectionError, OSError):
                pass
            self._client = None

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
        # Write routing (FR-UA-006) is later-sprint scope; this driver is
        # read-only for the MVP, matching the Modbus drivers' scope.
        return WriteResult(success=False, tag_id=tag_id, error_message="write not yet supported")

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        client = self._require_client()
        instance_id = self._config.instance_id if self._config else "opcua"
        interval_seconds = group["scan_rate_ms"] / 1000
        nodes = [(tag, client.get_node(tag["node_id"])) for tag in group["tags"]]
        while True:
            for tag, node in nodes:
                update = await self._read_tag(instance_id, tag, node)
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tag(self, instance_id: str, tag: dict[str, Any], node: Node) -> TagUpdate:
        tag_id = f"{instance_id}/{tag['id']}"
        with tracer.start_as_current_span(
            "driver.read",
            attributes={"driver.instance_id": instance_id, "tag.id": tag["id"]},
        ) as span:
            try:
                data_value = await node.read_data_value(raise_on_bad_status=False)
                status_code = data_value.StatusCode or ua_types.StatusCode(
                    ua_types.UInt32(StatusCodes.Bad)
                )
                quality = _map_status_code(status_code)
                variant = data_value.Value
                value = _coerce_value(variant.Value) if variant is not None else 0
                self._metrics.tag_read_count += 1
                self._metrics.last_successful_read = datetime.now(UTC)
                span.set_attribute("quality", quality.value)
                return TagUpdate(
                    tag_id=tag_id,
                    timestamp=datetime.now(UTC),
                    source_timestamp=data_value.SourceTimestamp,
                    value=value,
                    quality=quality,
                    source_driver=instance_id,
                    source_address=tag["node_id"],
                    metadata={"opcua_status_code": status_code.name},
                )
            except (TimeoutError, UaError, ConnectionError, OSError) as exc:
                # A read-level failure (e.g. node temporarily unavailable) marks
                # only this tag Bad; a dead session surfaces on the *next* read
                # or via the connection itself and is handled by the supervisor.
                self._metrics.error_count += 1
                span.set_attribute("quality", Quality.BAD.value)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                logger.warning(
                    "opcua.read_failed", instance_id=instance_id, tag_id=tag["id"], error=str(exc)
                )
                return TagUpdate(
                    tag_id=tag_id,
                    timestamp=datetime.now(UTC),
                    value=0,
                    quality=Quality.BAD,
                    source_driver=instance_id,
                    source_address=tag["node_id"],
                    metadata={"opcua_status_code": None, "opcua_error": str(exc)},
                )


def _map_status_code(status_code: ua_types.StatusCode) -> Quality:
    if status_code.is_good():
        return Quality.GOOD
    if status_code.is_uncertain():
        return Quality.UNCERTAIN
    return Quality.BAD


def _coerce_value(value: Any) -> TagValue:
    """Coerce an OPC UA Variant's Python value to our TagValue union.
    asyncua already returns bool/int/float/str/bytes for scalar node values;
    this narrows anything else (e.g. datetime, enum) to a string rather than
    letting an unsupported type reach the pipeline."""
    if isinstance(value, bool | int | float | str | bytes):
        return value
    return str(value)
