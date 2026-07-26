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

Sprint C3 (XEDGE-436) adds `connection_mode`: `persistent` (default, as
above) or `on_demand`, which holds no connection between transactions —
every read and write dials its own and closes it right after, for devices
or gateways that only accept one connection at a time from several polling
masters. Both modes dial through the same `_open_socket()`, which is also
where `connection_retry_count` (retries a failed *connection attempt*,
distinct from `retry_count`'s retry of a *read* on an already-open
connection) and, for `persistent` only, `keepalive_interval_seconds` live.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from xedge.drivers.modbus import codec
from xedge.drivers.modbus.polling import BaseModbusPollingDriver, ModbusDriverStateError
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 3.0
_DEFAULT_CONNECTION_RETRY_COUNT = 0
_DEFAULT_CONNECTION_RETRY_BACKOFF_SECONDS = 0.5
_MAX_TRANSACTION_ID = 0x10000

# `connection_mode` values (XEDGE-436; ADR-011 Part 2's "submission
# policies, not loop rewrites" applies equally here — this is a transport
# concern, so it's a config-driven branch inside `tcp.py`, not a second
# driver class). `persistent` is the default and every behavior this driver
# had before this field existed.
CONNECTION_MODE_PERSISTENT = "persistent"
CONNECTION_MODE_ON_DEMAND = "on_demand"

__all__ = ["ModbusDriverStateError", "ModbusTcpDriver"]


class ModbusTcpDriver(BaseModbusPollingDriver):
    def __init__(self) -> None:
        super().__init__()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transaction_id = 0
        self._connection_mode = CONNECTION_MODE_PERSISTENT

    def _require_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise ModbusDriverStateError("connect() must be called before this operation")
        return self._reader, self._writer

    def _next_transaction_id(self) -> int:
        self._transaction_id = (self._transaction_id + 1) % _MAX_TRANSACTION_ID
        return self._transaction_id

    async def _connect_transport(self) -> None:
        cfg = self._require_config().config
        self._connection_mode = cfg.get("connection_mode", CONNECTION_MODE_PERSISTENT)
        if self._connection_mode == CONNECTION_MODE_PERSISTENT:
            self._reader, self._writer = await self._open_socket(cfg)
            keepalive_interval = cfg.get("keepalive_interval_seconds")
            if keepalive_interval:
                self._apply_keepalive(self._writer, float(keepalive_interval))
        # else on_demand: no connection to hold open at all — the first
        # one is dialed by the first read or write, in _send_request below.
        #
        # Started only once a persistent connection is actually up (a
        # failed _open_socket() raises before this line, so the scheduler
        # is never left running with nothing behind it) or immediately for
        # on_demand, which has no upfront connection to wait for.
        self._scheduler.start()

    async def _disconnect_transport(self) -> None:
        # Stop accepting/serving new work before tearing down the
        # connection it depends on, not after.
        await self._scheduler.stop()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    async def _open_socket(
        self, cfg: dict[str, Any]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Dial a new TCP connection, retrying up to `connection_retry_count`
        times (XEDGE-436) before letting the final attempt's error
        propagate. Used both for the one persistent connection (from
        `_connect_transport`) and for each on_demand transaction's own
        short-lived one (from `_send_request`) — connecting the socket
        works the same way regardless of how long it's kept afterward.
        """
        retry_count = int(cfg.get("connection_retry_count", _DEFAULT_CONNECTION_RETRY_COUNT))
        backoff = float(
            cfg.get("connection_retry_backoff_seconds", _DEFAULT_CONNECTION_RETRY_BACKOFF_SECONDS)
        )
        attempt = 0
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(cfg["host"], cfg.get("port", 502)),
                    timeout=cfg.get("connect_timeout_seconds", _DEFAULT_CONNECT_TIMEOUT_SECONDS),
                )
            except (TimeoutError, OSError):
                if attempt >= retry_count:
                    raise
                attempt += 1
                if backoff > 0:
                    await asyncio.sleep(backoff)

    def _apply_keepalive(self, writer: asyncio.StreamWriter, interval_seconds: float) -> None:
        """Best-effort SO_KEEPALIVE + interval tuning (XEDGE-436), so a dead
        peer or a silently-dropped NAT/firewall session is noticed instead
        of hanging the next read forever. `TCP_KEEPIDLE`/`TCP_KEEPINTVL` are
        Linux-only socket module attributes (absent under those names on
        Windows/macOS) — getattr-guarded the same way `bus_manager.py`
        detects RTS support, so a persistent connection on a dev machine
        degrades to plain SO_KEEPALIVE instead of raising.
        """
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            interval = max(1, int(interval_seconds))
            for opt_name in ("TCP_KEEPIDLE", "TCP_KEEPINTVL"):
                opt = getattr(socket, opt_name, None)
                if opt is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, interval)
        except OSError as exc:
            logger.warning("modbus_tcp.keepalive_unsupported", error=str(exc))

    async def _send_request(self, request_pdu: bytes) -> bytes:
        """Encode `request_pdu` into an MBAP frame, send it, and return the
        decoded response PDU — the one piece of wire I/O every read/write
        hook needs, so `connection_mode` (XEDGE-436) only has to be
        branched on here rather than in each of them separately.
        """
        cfg = self._require_config().config
        frame = codec.encode_mbap(self._next_transaction_id(), cfg.get("unit_id", 1), request_pdu)
        if self._connection_mode == CONNECTION_MODE_ON_DEMAND:
            reader, writer = await self._open_socket(cfg)
            try:
                return await self._write_frame_and_read_response(cfg, reader, writer, frame)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
        reader, writer = self._require_connection()
        return await self._write_frame_and_read_response(cfg, reader, writer, frame)

    async def _write_frame_and_read_response(
        self,
        cfg: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        frame: bytes,
    ) -> bytes:
        writer.write(frame)
        await writer.drain()
        response_frame = await asyncio.wait_for(
            self._read_frame(reader),
            timeout=cfg.get("read_timeout_seconds", _DEFAULT_READ_TIMEOUT_SECONDS),
        )
        _, _, response_pdu = codec.decode_mbap(response_frame)
        return response_pdu

    async def _read_block(
        self, function_code: codec.FunctionCode, address: int, quantity: int
    ) -> list[int] | list[bool]:
        request_pdu = codec.encode_read_request(function_code, address, quantity)
        response_pdu = await self._send_request(request_pdu)
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
        request_pdu = (
            codec.encode_write_single_coil(address, bool(value))
            if function_code == codec.FunctionCode.WRITE_SINGLE_COIL
            else codec.encode_write_single_register(address, int(value))
        )
        response_pdu = await self._send_request(request_pdu)
        codec.decode_write_single_response(response_pdu)

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """FC16 (Sprint C2, XEDGE-423) — a tag whose data_type spans more
        than one register."""
        request_pdu = codec.encode_write_multiple_registers(address, values)
        response_pdu = await self._send_request(request_pdu)
        codec.decode_write_multiple_response(response_pdu)
