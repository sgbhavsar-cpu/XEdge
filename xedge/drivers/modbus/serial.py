"""Modbus RTU driver over a real serial line (RS-232/RS-485), FR-SA-001.

Uses pyserial-asyncio purely as the serial transport (open/read/write) — all
Modbus framing (CRC-16, address/PDU layout, T3.5 inter-frame silence) is the
in-house rtu_codec, per ADR-006.

Hardware note: this driver has no automated test coverage requiring a real
or virtual serial port (none is available in this environment); it is
exercised indirectly via rtu_codec's unit tests (framing/CRC, cross-checked
against pymodbus) and structurally mirrors ModbusRtuOverTcpDriver, which
*is* fully integration-tested against a fake server. Validate against real
hardware before production use.
"""

from __future__ import annotations

import asyncio

import serial_asyncio

from xedge.drivers.base import TagValue
from xedge.drivers.modbus import codec, rtu_codec
from xedge.drivers.modbus.polling import BaseModbusPollingDriver, ModbusDriverStateError

_DEFAULT_READ_TIMEOUT_SECONDS = 3.0
_PARITY_MAP = {"none": "N", "even": "E", "odd": "O"}

__all__ = ["ModbusRtuSerialDriver"]


class ModbusRtuSerialDriver(BaseModbusPollingDriver):
    def __init__(self) -> None:
        super().__init__()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_lock = asyncio.Lock()
        self._inter_frame_delay_seconds = 0.0

    def _require_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._reader, self._writer

    async def connect(self) -> None:
        cfg = self._require_config().config
        baud_rate = cfg.get("baud_rate", 9600)
        self._inter_frame_delay_seconds = rtu_codec.inter_frame_delay_seconds(baud_rate)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=cfg["port"],
            baudrate=baud_rate,
            bytesize=cfg.get("data_bits", 8),
            parity=_PARITY_MAP[cfg.get("parity", "none")],
            stopbits=cfg.get("stop_bits", 1),
        )

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    async def _read_one(self, function_code: codec.FunctionCode, address: int) -> TagValue:
        cfg = self._require_config().config
        reader, writer = self._require_connection()
        quantity = 1
        request_pdu = codec.encode_read_request(function_code, address, quantity)

        async with self._request_lock:
            # T3.5 silence before transmitting, so the bus has settled since
            # any prior frame (ours or, on a shared multi-drop line, another
            # master's) — required for slaves to correctly detect frame
            # boundaries (Modbus over Serial Line spec).
            await asyncio.sleep(self._inter_frame_delay_seconds)
            frame = rtu_codec.encode_rtu_frame(cfg.get("unit_id", 1), request_pdu)
            writer.write(frame)
            await writer.drain()

            response_frame = await asyncio.wait_for(
                self._read_frame(reader),
                timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
            )

        _, response_pdu = rtu_codec.decode_rtu_frame(response_frame)
        if codec.is_bit_function(function_code):
            bits = codec.decode_bits_response(response_pdu, quantity)
            return bits[0]
        registers = codec.decode_registers_response(response_pdu)
        return registers[0]

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        head = await reader.readexactly(2)  # unit address, function code (or exception)
        function_code_byte = head[1]
        if function_code_byte & codec.EXCEPTION_RESPONSE_FLAG:
            rest = await reader.readexactly(1 + 2)  # exception code + CRC
        else:
            byte_count_byte = await reader.readexactly(1)
            byte_count = byte_count_byte[0]
            rest = byte_count_byte + await reader.readexactly(byte_count + 2)  # data + CRC
        return head + rest

    async def _read_write_response_frame(self, reader: asyncio.StreamReader) -> bytes:
        """FC05/06/16 responses aren't byte-count-prefixed like read
        responses (see `_read_frame`) — success echoes a fixed 4-byte
        address+value/quantity payload; only an exception response is
        shorter."""
        head = await reader.readexactly(2)  # unit address, function code (or exception)
        function_code_byte = head[1]
        if function_code_byte & codec.EXCEPTION_RESPONSE_FLAG:
            rest = await reader.readexactly(1 + 2)  # exception code + CRC
        else:
            rest = await reader.readexactly(4 + 2)  # address + value/quantity + CRC
        return head + rest

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

        async with self._request_lock:
            await asyncio.sleep(self._inter_frame_delay_seconds)
            frame = rtu_codec.encode_rtu_frame(cfg.get("unit_id", 1), request_pdu)
            writer.write(frame)
            await writer.drain()

            response_frame = await asyncio.wait_for(
                self._read_write_response_frame(reader),
                timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
            )

        _, response_pdu = rtu_codec.decode_rtu_frame(response_frame)
        codec.decode_write_single_response(response_pdu)
