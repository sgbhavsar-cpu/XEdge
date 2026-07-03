"""Modbus TCP driver (FR-SA-002, FR-SA-004, FR-SA-008, FR-SA-009).

Uses the in-house codec (xedge.drivers.modbus.codec, ADR-006) over a plain
asyncio TCP connection. One polling task per configured tag group; a
Modbus-level exception response marks only that tag Bad (FR-DP-005) without
tearing down the connection, while a transport failure (timeout, connection
reset) propagates so the DriverSupervisor restarts the whole instance
(NFR-R-006).
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

_FUNCTION_CODE_BY_NAME: dict[str, codec.FunctionCode] = {
    "read_coils": codec.FunctionCode.READ_COILS,
    "read_discrete_inputs": codec.FunctionCode.READ_DISCRETE_INPUTS,
    "read_holding_registers": codec.FunctionCode.READ_HOLDING_REGISTERS,
    "read_input_registers": codec.FunctionCode.READ_INPUT_REGISTERS,
}

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 3.0
_MAX_TRANSACTION_ID = 0x10000


class ModbusDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order (e.g. connect()
    before configure(), or a read before connect())."""


class ModbusTcpDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transaction_id = 0
        self._metrics = DriverMetrics()
        self._request_lock = asyncio.Lock()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise ModbusDriverStateError("configure() must be called before this operation")
        return self._config

    def _require_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._reader, self._writer

    async def configure(self, config: DriverConfig) -> None:
        self._config = config

    async def connect(self) -> None:
        cfg = self._require_config().config
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(cfg["host"], cfg.get("port", 502)),
            timeout=cfg.get("connect_timeout_seconds", _DEFAULT_CONNECT_TIMEOUT_SECONDS),
        )

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

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        # Modbus write function codes (FC05/06/15/16) are Sprint 4 scope
        # (XEDGE-037); this driver is read-only for now.
        return WriteResult(success=False, tag_id=tag_id, error_message="write not yet supported")

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        interval_seconds = group["scan_rate_ms"] / 1000
        instance_id = self._config.instance_id if self._config else "modbus_tcp"
        while True:
            for tag in group["tags"]:
                update = await self._read_tag(instance_id, tag)
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tag(self, instance_id: str, tag: dict[str, Any]) -> TagUpdate:
        function_code = _FUNCTION_CODE_BY_NAME[tag["function_code"]]
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

    async def _read_one(self, function_code: codec.FunctionCode, address: int) -> TagValue:
        cfg = self._require_config().config
        reader, writer = self._require_connection()
        quantity = 1
        request_pdu = codec.encode_read_request(function_code, address, quantity)

        async with self._request_lock:
            self._transaction_id = (self._transaction_id + 1) % _MAX_TRANSACTION_ID
            frame = codec.encode_mbap(self._transaction_id, cfg.get("unit_id", 1), request_pdu)
            writer.write(frame)
            await writer.drain()

            response_frame = await asyncio.wait_for(
                self._read_frame(reader),
                timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
            )

        _, _, response_pdu = codec.decode_mbap(response_frame)
        if codec.is_bit_function(function_code):
            bits = codec.decode_bits_response(response_pdu, quantity)
            return bits[0]
        registers = codec.decode_registers_response(response_pdu)
        return registers[0]

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(codec.MBAP_HEADER_LENGTH)
        remainder_length = codec.frame_remainder_length(header)
        remainder = await reader.readexactly(remainder_length)
        return header + remainder
