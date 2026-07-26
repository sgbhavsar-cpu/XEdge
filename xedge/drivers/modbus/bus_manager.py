"""Shared RS-485 serial bus manager (Sprint C3, XEDGE-431/432; ADR-011 Part 1).

A Modbus RTU driver instance is scoped to one `unit_id`. Polling several
slaves on one RS-485 multi-drop bus therefore needs several instances — but
before this module, each instance opened the serial port independently, so
two instances configured against the same `/dev/ttyUSB0` both tried to own
the handle: a lock conflict or interleaved garbage on the wire, depending on
the driver and adapter. This was the single largest functional gap in the
protocol the customer uses most.

`SerialBusManager` is the fix: one manager per physical port, created
lazily and shared by every `ModbusRtuSerialDriver` instance configured
against that path (looked up by `get_bus_manager`, a module-level registry —
driver instances have no other way to find each other, since each is
created independently by `DriverRegistry.create()` per ADR-008). Driver
instances stay one-per-slave; they acquire the manager rather than opening
their own connection.

The manager is also **the** scheduler for every instance sharing its port —
not a second scheduler alongside each instance's own. See
`ModbusRtuSerialDriver._scheduler`'s override in `serial.py`: a shared bus
needs one write-priority queue across every slave on it, not one each.

RTS pre/post-transmit delay (XEDGE-432) is applied here, once, for every
instance on the bus — RS-485 converters that do not auto-toggle their
transceiver's direction need RTS asserted before transmitting and held
briefly after, which `pyserial-asyncio` does not expose on its own but the
underlying `pyserial.Serial` object (reachable via
`transport.serial`, a documented pyserial-asyncio pattern) does via its
`.rts` property.

**Unverified against real hardware** (XEDGE-DR-001 open item Q-4 — target
gateway hardware is unknown). RTS timing requirements are converter-specific;
this ships configurable but unverified, and `_set_rts` degrades to a logged
no-op rather than crashing the driver if the underlying transport does not
support modem-control lines at all (true of a pty in the test suite, and
possibly true of some real adapters).
"""

from __future__ import annotations

import asyncio
from typing import Any

import serial_asyncio

from xedge.drivers.modbus import codec, rtu_codec
from xedge.drivers.modbus.scheduler import RequestScheduler
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_READ_TIMEOUT_SECONDS = 3.0
_PARITY_MAP = {"none": "N", "even": "E", "odd": "O"}


class SerialBusManagerStateError(RuntimeError):
    """Raised when a bus operation is attempted before any instance has
    successfully `acquire()`-d the manager."""


class SerialBusManager:
    """Owns one physical serial port on behalf of every driver instance
    configured against it. Reference-counted: opens the port when the first
    instance acquires it, closes it when the last one releases it — driver
    instances start/stop independently (hot-reload, enable/disable), and the
    port must survive any one of them coming and going as long as another is
    still using it.
    """

    def __init__(self, port_path: str) -> None:
        self.port_path = port_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.scheduler = RequestScheduler()
        self._ref_count = 0
        self._inter_frame_delay_seconds = 0.0
        self._rts_pre_delay_seconds = 0.0
        self._rts_post_delay_seconds = 0.0
        self._rts_supported = True
        self._read_timeout_seconds = _DEFAULT_READ_TIMEOUT_SECONDS

    @property
    def is_open(self) -> bool:
        return self._ref_count > 0

    async def acquire(self, config: dict[str, Any]) -> None:
        """Called from a driver instance's `connect()`. The first caller
        actually opens the port, using *its* config for the physical line
        settings (baud/parity/stop bits/RTS timing) — every instance
        sharing a port must agree on these, since they describe the wire,
        not any one slave; the schema validates each instance's config
        independently, so a mismatch between instances on the same port is
        an operator error this manager cannot itself detect (see
        XEDGE-433's uniqueness check for the adjacent slave-ID case it
        *can* detect). Later callers reuse the connection already open.
        """
        self._ref_count += 1
        if self._ref_count > 1:
            return
        baud_rate = config.get("baud_rate", 9600)
        self._inter_frame_delay_seconds = rtu_codec.inter_frame_delay_seconds(baud_rate)
        self._rts_pre_delay_seconds = config.get("rts_pre_delay_us", 0) / 1_000_000
        self._rts_post_delay_seconds = config.get("rts_post_delay_us", 0) / 1_000_000
        self._read_timeout_seconds = config.get(
            "read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS
        )
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.port_path,
                baudrate=baud_rate,
                bytesize=config.get("data_bits", 8),
                parity=_PARITY_MAP[config.get("parity", "none")],
                stopbits=config.get("stop_bits", 1),
            )
        except Exception:
            self._ref_count -= 1
            raise
        self.scheduler.start()

    async def release(self) -> None:
        """Called from a driver instance's `disconnect()`. Closes the port
        once the last referencing instance releases it. Safe to call more
        times than `acquire()` succeeded (mirrors `BaseDriver.disconnect()`'s
        "safe even if connect() never succeeded" contract) — a failed
        `acquire()` already decremented the count itself, so a paired
        `release()` from the caller's own cleanup path would otherwise
        under-run it.
        """
        if self._ref_count == 0:
            return
        self._ref_count -= 1
        if self._ref_count > 0:
            return
        await self.scheduler.stop()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._reader = None
        self._writer = None

    def _require_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise SerialBusManagerStateError("acquire() must succeed before this operation")
        return self._reader, self._writer

    # --- submission entry points, called by ModbusRtuSerialDriver via its
    # `_scheduler` override (which *is* this manager's `scheduler`) ---

    async def read_block(
        self, unit_id: int, function_code: codec.FunctionCode, address: int, quantity: int
    ) -> list[int] | list[bool]:
        reader, writer = self._require_connection()
        request_pdu = codec.encode_read_request(function_code, address, quantity)
        await self._transmit(writer, unit_id, request_pdu)
        response_frame = await asyncio.wait_for(
            self._read_frame(reader), timeout=self._read_timeout_seconds
        )
        _, response_pdu = rtu_codec.decode_rtu_frame(response_frame)
        if codec.is_bit_function(function_code):
            return codec.decode_bits_response(response_pdu, quantity)
        return codec.decode_registers_response(response_pdu)

    async def write_one(
        self, unit_id: int, function_code: codec.FunctionCode, address: int, value: int | bool
    ) -> None:
        reader, writer = self._require_connection()
        request_pdu = (
            codec.encode_write_single_coil(address, bool(value))
            if function_code == codec.FunctionCode.WRITE_SINGLE_COIL
            else codec.encode_write_single_register(address, int(value))
        )
        await self._transmit(writer, unit_id, request_pdu)
        response_frame = await asyncio.wait_for(
            self._read_write_response_frame(reader), timeout=self._read_timeout_seconds
        )
        _, response_pdu = rtu_codec.decode_rtu_frame(response_frame)
        codec.decode_write_single_response(response_pdu)

    async def write_registers(self, unit_id: int, address: int, values: list[int]) -> None:
        reader, writer = self._require_connection()
        request_pdu = codec.encode_write_multiple_registers(address, values)
        await self._transmit(writer, unit_id, request_pdu)
        response_frame = await asyncio.wait_for(
            self._read_write_response_frame(reader), timeout=self._read_timeout_seconds
        )
        _, response_pdu = rtu_codec.decode_rtu_frame(response_frame)
        codec.decode_write_multiple_response(response_pdu)

    # --- wire-level helpers ---

    async def _transmit(
        self, writer: asyncio.StreamWriter, unit_id: int, request_pdu: bytes
    ) -> None:
        # T3.5 silence before transmitting, so the bus has settled since any
        # prior frame — ours or another slave's — enforced once here for
        # the whole bus rather than per-instance (Modbus over Serial Line
        # spec; ADR-011 Part 2).
        await asyncio.sleep(self._inter_frame_delay_seconds)
        self._set_rts(True)
        if self._rts_pre_delay_seconds > 0:
            await asyncio.sleep(self._rts_pre_delay_seconds)
        frame = rtu_codec.encode_rtu_frame(unit_id, request_pdu)
        writer.write(frame)
        await writer.drain()
        if self._rts_post_delay_seconds > 0:
            await asyncio.sleep(self._rts_post_delay_seconds)
        self._set_rts(False)

    def _set_rts(self, value: bool) -> None:
        """Best-effort: a pty (this package's own test fixture) and some
        real USB-RS485 adapters don't support modem-control ioctls at all.
        Failing that softly, once, with a logged warning is safer than
        crashing every single transmission on a bus where it doesn't work —
        the alternative to a converter needing RTS toggling is not needing
        it, never a hard requirement either way."""
        if not self._rts_supported:
            return
        serial_obj = getattr(getattr(self._writer, "transport", None), "serial", None)
        if serial_obj is None:
            return
        try:
            serial_obj.rts = value
        except (OSError, NotImplementedError) as exc:
            self._rts_supported = False
            logger.warning(
                "modbus.rts_unsupported",
                port_path=self.port_path,
                error=str(exc),
            )

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


_managers: dict[str, SerialBusManager] = {}


def get_bus_manager(port_path: str) -> SerialBusManager:
    """Get-or-lazily-create the shared manager for `port_path` (ADR-011
    Part 1). Module-level registry, same "shared singleton behind a getter"
    shape as `xedge.observability.logging.get_log_ring_buffer` — driver
    instances are created independently by `DriverRegistry.create()`
    (ADR-008) and have no other way to find one another, so the manager
    they need to share has to live somewhere outside any one of them.

    Entries are never removed — a live gateway has a fixed, small set of
    physical serial ports for its whole process lifetime, the same
    permanent-for-the-process shape `_log_ring_buffer` already has.
    """
    manager = _managers.get(port_path)
    if manager is None:
        manager = SerialBusManager(port_path)
        _managers[port_path] = manager
    return manager
