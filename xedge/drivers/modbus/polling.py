"""Shared tag-group polling loop for all Modbus transport variants (TCP,
RTU-over-TCP, RTU-serial). A Modbus-level exception response marks only
that tag Bad (FR-DP-005) without tearing down the connection, while a
transport failure propagates so the DriverSupervisor restarts the whole
instance (NFR-R-006).

Subclasses implement only the transport: `connect()`, `disconnect()`, and
`_read_one(function_code, address)`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

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
from xedge.drivers.modbus import codec
from xedge.observability.logging import get_logger
from xedge.observability.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

FUNCTION_CODE_BY_NAME: dict[str, codec.FunctionCode] = {
    "read_coils": codec.FunctionCode.READ_COILS,
    "read_discrete_inputs": codec.FunctionCode.READ_DISCRETE_INPUTS,
    "read_holding_registers": codec.FunctionCode.READ_HOLDING_REGISTERS,
    "read_input_registers": codec.FunctionCode.READ_INPUT_REGISTERS,
}

# Write function code for each *readable* function code, where the
# underlying Modbus memory class is writable at all (FR-NB-009 write-back,
# XEDGE-223). Discrete inputs and input registers are read-only memory
# classes on real devices — there is no Modbus write function code for
# them, so a write attempt against a tag configured with either is
# rejected before ever reaching the wire.
_WRITE_FUNCTION_CODE_FOR_READ: dict[str, codec.FunctionCode] = {
    "read_coils": codec.FunctionCode.WRITE_SINGLE_COIL,
    "read_holding_registers": codec.FunctionCode.WRITE_SINGLE_REGISTER,
}


def _inverse_scale(value: TagValue, scaling: dict[str, Any] | None) -> int:
    """Invert xedge.core.pipeline.normalize's forward scaling
    (`engineering_value = raw * scale + offset`) so a write request
    expressed in engineering units lands as the correct raw register
    value: `raw = round((engineering_value - offset) / scale)`."""
    if scaling is None:
        return int(value)
    scale: float = scaling.get("scale", 1.0)
    offset: float = scaling.get("offset", 0.0)
    return round((float(value) - offset) / scale)


class ModbusDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order (e.g. connect()
    before configure(), or a read before connect())."""


class BaseModbusPollingDriver(BaseDriver):
    """Common tag-group polling loop, TagUpdate construction, and metrics
    tracking. Not transport-specific — see module docstring."""

    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._metrics = DriverMetrics()
        self._tags_by_id: dict[str, dict[str, Any]] = {}

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise ModbusDriverStateError("configure() must be called before this operation")
        return self._config

    async def configure(self, config: DriverConfig) -> None:
        self._config = config
        self._tags_by_id = {tag["id"]: tag for group in config.tag_groups for tag in group["tags"]}

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
        """FR-NB-009 write-back (Sprint 31, XEDGE-223), covering FC05/06 —
        single coil / single register only; FC16 (write_multiple_registers)
        has no caller yet (every write here targets exactly one tag) but is
        available in xedge.drivers.modbus.codec for a future multi-register
        write path."""
        tag = self._tags_by_id.get(tag_id)
        if tag is None:
            return WriteResult(success=False, tag_id=tag_id, error_message="Unknown tag")
        read_function_name = tag["function_code"]
        write_function_code = _WRITE_FUNCTION_CODE_FOR_READ.get(read_function_name)
        if write_function_code is None:
            return WriteResult(
                success=False,
                tag_id=tag_id,
                error_message=f"{read_function_name} is a read-only Modbus memory class",
            )
        address = tag["address"]
        try:
            if write_function_code == codec.FunctionCode.WRITE_SINGLE_COIL:
                raw_value: int | bool = bool(value)
            else:
                raw_value = _inverse_scale(value, tag.get("scaling"))
            await self._write_one(write_function_code, address, raw_value)
        except codec.ModbusException as exc:
            self._metrics.error_count += 1
            logger.warning("modbus.write_rejected", tag_id=tag_id, error=str(exc))
            return WriteResult(success=False, tag_id=tag_id, error_message=str(exc))
        return WriteResult(success=True, tag_id=tag_id)

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _read_one(self, function_code: codec.FunctionCode, address: int) -> TagValue:
        raise NotImplementedError

    async def _write_one(
        self, function_code: codec.FunctionCode, address: int, value: int | bool
    ) -> None:
        raise NotImplementedError

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        interval_seconds = group["scan_rate_ms"] / 1000
        instance_id = self._config.instance_id if self._config else "modbus"
        while True:
            for tag in group["tags"]:
                update = await self._read_tag(instance_id, tag)
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tag(self, instance_id: str, tag: dict[str, Any]) -> TagUpdate:
        function_code = FUNCTION_CODE_BY_NAME[tag["function_code"]]
        address = tag["address"]
        with tracer.start_as_current_span(
            "driver.read",
            attributes={"driver.instance_id": instance_id, "tag.id": tag["id"]},
        ) as span:
            started_at = time.monotonic()
            try:
                value = await self._read_one(function_code, address)
                latency_ms = (time.monotonic() - started_at) * 1000
                self._metrics.tag_read_count += 1
                self._metrics.last_successful_read = datetime.now(UTC)
                span.set_attribute("quality", Quality.GOOD.value)
                return TagUpdate(
                    tag_id=f"{instance_id}/{tag['id']}",
                    timestamp=datetime.now(UTC),
                    value=value,
                    quality=Quality.GOOD,
                    source_driver=instance_id,
                    source_address=str(address),
                    metadata={
                        "modbus_exception": None,
                        "request_latency_ms": round(latency_ms, 2),
                    },
                )
            except codec.ModbusException as exc:
                # Protocol-level rejection from the device (e.g. illegal address):
                # mark this tag Bad, keep the connection and polling loop alive.
                self._metrics.error_count += 1
                span.set_attribute("quality", Quality.BAD.value)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                logger.warning(
                    "modbus.tag_exception",
                    instance_id=instance_id,
                    tag_id=tag["id"],
                    exception_code=exc.exception_code,
                )
                placeholder_value: TagValue = False if codec.is_bit_function(function_code) else 0
                return TagUpdate(
                    tag_id=f"{instance_id}/{tag['id']}",
                    timestamp=datetime.now(UTC),
                    value=placeholder_value,
                    quality=Quality.BAD,
                    source_driver=instance_id,
                    source_address=str(address),
                    metadata={"modbus_exception": exc.exception_code},
                )
