"""Modbus TCP driver (FR-SA-002, FR-SA-004, FR-SA-008, FR-SA-009).

Uses the in-house codec (xedge.drivers.modbus.codec, ADR-006) over a plain
asyncio TCP connection, framed with the MBAP header. Polling loop and
TagUpdate construction are shared with the other Modbus transport variants —
see xedge.drivers.modbus.polling.

The per-instance `asyncio.Lock` this transport used through Sprint C1 is
gone as of Sprint C2 (XEDGE-424): `BaseModbusPollingDriver`'s
`RequestScheduler` now serializes every read and write onto this
connection (with writes ahead of pending reads), which already guarantees
at most one in-flight request — a second lock here would be redundant, not
additional safety.
"""

from __future__ import annotations

import asyncio

from xedge.drivers.modbus import codec
from xedge.drivers.modbus.polling import BaseModbusPollingDriver, ModbusDriverStateError

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 3.0
_MAX_TRANSACTION_ID = 0x10000

__all__ = ["ModbusDriverStateError", "ModbusTcpDriver"]


class ModbusTcpDriver(BaseModbusPollingDriver):
    def __init__(self) -> None:
        super().__init__()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transaction_id = 0

    def _require_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._reader, self._writer

    def _next_transaction_id(self) -> int:
        self._transaction_id = (self._transaction_id + 1) % _MAX_TRANSACTION_ID
        return self._transaction_id

    async def _connect_transport(self) -> None:
        cfg = self._require_config().config
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(cfg["host"], cfg.get("port", 502)),
            timeout=cfg.get("connect_timeout_seconds", _DEFAULT_CONNECT_TIMEOUT_SECONDS),
        )

    async def _disconnect_transport(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    async def _read_block(
        self, function_code: codec.FunctionCode, address: int, quantity: int
    ) -> list[int] | list[bool]:
        cfg = self._require_config().config
        reader, writer = self._require_connection()
        request_pdu = codec.encode_read_request(function_code, address, quantity)

        frame = codec.encode_mbap(self._next_transaction_id(), cfg.get("unit_id", 1), request_pdu)
        writer.write(frame)
        await writer.drain()

        response_frame = await asyncio.wait_for(
            self._read_frame(reader),
            timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
        )

        _, _, response_pdu = codec.decode_mbap(response_frame)
        if codec.is_bit_function(function_code):
            return codec.decode_bits_response(response_pdu, quantity)
        return codec.decode_registers_response(response_pdu)

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(codec.MBAP_HEADER_LENGTH)
        remainder_length = codec.frame_remainder_length(header)
        remainder = await reader.readexactly(remainder_length)
        return header + remainder

    async def _write_one(
        self, function_code: codec.FunctionCode, address: int, value: int | bool
    ) -> None:
        cfg = self._require_config().config
        reader, writer = self._require_connection()
        request_pdu = (
            codec.encode_write_single_coil(address, bool(value))
            if function_code == codec.FunctionCode.WRITE_SINGLE_COIL
            else codec.encode_write_single_register(address, int(value))
        )

        frame = codec.encode_mbap(self._next_transaction_id(), cfg.get("unit_id", 1), request_pdu)
        writer.write(frame)
        await writer.drain()

        # MBAP's explicit length field means _read_frame (unlike the RTU
        # transports') needs no write-response-specific variant.
        response_frame = await asyncio.wait_for(
            self._read_frame(reader),
            timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
        )

        _, _, response_pdu = codec.decode_mbap(response_frame)
        codec.decode_write_single_response(response_pdu)

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """FC16 (Sprint C2, XEDGE-423) — a tag whose data_type spans more
        than one register."""
        cfg = self._require_config().config
        reader, writer = self._require_connection()
        request_pdu = codec.encode_write_multiple_registers(address, values)

        frame = codec.encode_mbap(self._next_transaction_id(), cfg.get("unit_id", 1), request_pdu)
        writer.write(frame)
        await writer.drain()

        response_frame = await asyncio.wait_for(
            self._read_frame(reader),
            timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
        )

        _, _, response_pdu = codec.decode_mbap(response_frame)
        codec.decode_write_multiple_response(response_pdu)
