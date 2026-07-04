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

logger = get_logger(__name__)

FUNCTION_CODE_BY_NAME: dict[str, codec.FunctionCode] = {
    "read_coils": codec.FunctionCode.READ_COILS,
    "read_discrete_inputs": codec.FunctionCode.READ_DISCRETE_INPUTS,
    "read_holding_registers": codec.FunctionCode.READ_HOLDING_REGISTERS,
    "read_input_registers": codec.FunctionCode.READ_INPUT_REGISTERS,
}


class ModbusDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order (e.g. connect()
    before configure(), or a read before connect())."""


class BaseModbusPollingDriver(BaseDriver):
    """Common tag-group polling loop, TagUpdate construction, and metrics
    tracking. Not transport-specific — see module docstring."""

    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._metrics = DriverMetrics()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise ModbusDriverStateError("configure() must be called before this operation")
        return self._config

    async def configure(self, config: DriverConfig) -> None:
        self._config = config

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
        # Modbus write function codes (FC05/06/15/16) are Sprint 4 scope
        # (XEDGE-037); these drivers are read-only for now.
        return WriteResult(success=False, tag_id=tag_id, error_message="write not yet supported")

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _read_one(self, function_code: codec.FunctionCode, address: int) -> TagValue:
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
        started_at = time.monotonic()
        try:
            value = await self._read_one(function_code, address)
            latency_ms = (time.monotonic() - started_at) * 1000
            self._metrics.tag_read_count += 1
            self._metrics.last_successful_read = datetime.now(UTC)
            return TagUpdate(
                tag_id=f"{instance_id}/{tag['id']}",
                timestamp=datetime.now(UTC),
                value=value,
                quality=Quality.GOOD,
                source_driver=instance_id,
                source_address=str(address),
                metadata={"modbus_exception": None, "request_latency_ms": round(latency_ms, 2)},
            )
        except codec.ModbusException as exc:
            # Protocol-level rejection from the device (e.g. illegal address):
            # mark this tag Bad, keep the connection and polling loop alive.
            self._metrics.error_count += 1
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
