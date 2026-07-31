"""Modbus RTU driver over a real serial line (RS-232/RS-485), FR-SA-001.

Uses pyserial-asyncio purely as the serial transport (open/read/write) — all
Modbus framing (CRC-16, address/PDU layout, T3.5 inter-frame silence) is the
in-house rtu_codec, per ADR-006.

The per-instance `asyncio.Lock` this transport used through Sprint C1 is
gone as of Sprint C2 (XEDGE-424): `BaseModbusPollingDriver`'s
`RequestScheduler` now serializes every read and write onto this
connection (with writes ahead of pending reads), which already guarantees
at most one in-flight request at a time — the T3.5 inter-frame sleep below
still runs before every single transmission exactly as before, it just no
longer needs its own lock to do so.

Hardware note: this driver has no automated test coverage requiring a real
or virtual serial port (none is available in this environment); it is
exercised indirectly via rtu_codec's unit tests (framing/CRC, cross-checked
against pymodbus) and structurally mirrors ModbusRtuOverTcpDriver, which
*is* fully integration-tested against a fake server. Validate against real
hardware before production use.
"""

from __future__ import annotations

import asyncio
from typing import Any

import serial_asyncio

from xedge.drivers.base import DriverConfig
from xedge.drivers.modbus import codec, rtu_codec
from xedge.drivers.modbus.polling import BaseModbusPollingDriver, ModbusDriverStateError
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_READ_TIMEOUT_SECONDS = 3.0
_PARITY_MAP = {"none": "N", "even": "E", "odd": "O"}

# A Modbus RTU read request is 8 bytes on the wire (unit, function, address
# hi/lo, quantity hi/lo, CRC lo/hi). Its response is 5 bytes of overhead
# (unit, function, byte count, CRC lo/hi) plus 2 bytes per register.
_REQUEST_BYTES = 8
_RESPONSE_OVERHEAD_BYTES = 5
_BYTES_PER_REGISTER = 2
# T3.5 idle before each frame, per the Modbus over Serial Line spec.
_INTER_FRAME_CHARS = 3.5


def minimum_scan_interval_seconds(
    baud_rate: int,
    blocks: list[tuple[int, int]],
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: int = 1,
) -> float:
    """Shortest cycle a serial bus can physically sustain for `blocks`.

    XEDGE-413 lowered the configurable scan-rate floor to 1 ms, which is
    reachable on TCP but **not** on RS-485: a serial line moves a fixed number
    of bits per second, so a cycle cannot outrun the time to clock its own
    frames out and back. This computes that bound so the operator gets told,
    rather than silently configuring a rate the hardware cannot deliver.

    `blocks` is (function_code, quantity) per planned read. Bit function codes
    pack 8 values per byte, register codes use 2 bytes each.
    """
    bits_per_char = 1 + data_bits + (0 if parity == "none" else 1) + stop_bits
    total_chars = 0.0
    for function_code, quantity in blocks:
        if codec.is_bit_function(codec.FunctionCode(function_code)):
            data_bytes = -(-quantity // 8)  # ceiling division: 8 coils per byte
        else:
            data_bytes = quantity * _BYTES_PER_REGISTER
        total_chars += _INTER_FRAME_CHARS + _REQUEST_BYTES + _RESPONSE_OVERHEAD_BYTES + data_bytes
    return total_chars * bits_per_char / baud_rate


__all__ = ["ModbusRtuSerialDriver", "minimum_scan_interval_seconds"]


class ModbusRtuSerialDriver(BaseModbusPollingDriver):
    def __init__(self) -> None:
        super().__init__()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._inter_frame_delay_seconds = 0.0

    def _require_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._reader, self._writer

    async def configure(self, config: DriverConfig) -> None:
        await super().configure(config)
        self._warn_on_unachievable_scan_rates(config)

    def _warn_on_unachievable_scan_rates(self, config: DriverConfig) -> None:
        """XEDGE-413 / open item Q-2. The schema floor is 1 ms for every
        transport, but RS-485 cannot deliver that — the bus moves a fixed
        number of bits per second. Rather than let a group silently overrun
        every single cycle, compute the physical bound at configure() time and
        say so once, naming the rate that would actually work.
        """
        cfg: dict[str, Any] = config.config
        baud_rate = cfg.get("baud_rate", 9600)
        for group in config.tag_groups:
            blocks = self._blocks_by_group.get(group["id"], [])
            if not blocks:
                continue
            floor_seconds = minimum_scan_interval_seconds(
                baud_rate,
                [(int(block.function_code), block.quantity) for block in blocks],
                data_bits=cfg.get("data_bits", 8),
                parity=cfg.get("parity", "none"),
                stop_bits=cfg.get("stop_bits", 1),
            )
            configured_seconds = group["scan_rate_ms"] / 1000
            if configured_seconds < floor_seconds:
                logger.warning(
                    "modbus.scan_rate_below_serial_floor",
                    instance_id=config.instance_id,
                    tag_group=group["id"],
                    configured_scan_rate_ms=group["scan_rate_ms"],
                    achievable_scan_rate_ms=round(floor_seconds * 1000, 1),
                    baud_rate=baud_rate,
                    request_count=len(blocks),
                )

    async def _connect_transport(self) -> None:
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

        # T3.5 silence before transmitting, so the bus has settled since any
        # prior frame (ours or, on a shared multi-drop line, another
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
            return codec.decode_bits_response(response_pdu, quantity)
        return codec.decode_registers_response(response_pdu)

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
        """FC05/06/15/16 responses aren't byte-count-prefixed like read
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

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """FC16 (Sprint C2, XEDGE-423) — a tag whose data_type spans more
        than one register."""
        cfg = self._require_config().config
        reader, writer = self._require_connection()
        request_pdu = codec.encode_write_multiple_registers(address, values)

        await asyncio.sleep(self._inter_frame_delay_seconds)
        frame = rtu_codec.encode_rtu_frame(cfg.get("unit_id", 1), request_pdu)
        writer.write(frame)
        await writer.drain()

        response_frame = await asyncio.wait_for(
            self._read_write_response_frame(reader),
            timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
        )

        _, response_pdu = rtu_codec.decode_rtu_frame(response_frame)
        codec.decode_write_multiple_response(response_pdu)
