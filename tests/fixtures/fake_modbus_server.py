"""In-house Modbus TCP/RTU-over-TCP servers used only to test our own driver
and codec end-to-end without external dependencies. Built directly against
the Modbus spec (same provenance as xedge.drivers.modbus.codec) — this is
test infrastructure, not a shipped artifact, so ADR-006's clean-room rule
(which governs the shipped driver) doesn't apply here; it exists alongside
the pymodbus-backed oracle tests for fast, deterministic coverage.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Awaitable, Callable

from xedge.drivers.modbus import codec, rtu_codec

# Function codes whose request PDU has a *variable*-length remainder
# (address + quantity + a byte-count field + that many data bytes), unlike
# every other supported function code, where the remainder is a fixed 4
# bytes (address + quantity, or address + value). Read one via
# `read_rtu_request_pdu` below.
_VARIABLE_LENGTH_REQUEST_CODES = frozenset(
    {codec.FunctionCode.WRITE_MULTIPLE_COILS, codec.FunctionCode.WRITE_MULTIPLE_REGISTERS}
)


async def read_rtu_request_pdu(read_exact: Callable[[int], Awaitable[bytes]]) -> tuple[int, bytes]:
    """Read one complete RTU-framed Modbus *request* — address + PDU + CRC —
    from any async byte source exposing `read_exact(n) -> bytes`, and return
    (unit_id, pdu) with the CRC consumed but not re-validated (these fixtures
    test our driver's request shape, not CRC correctness — rtu_codec's own
    tests already cover CRC).

    Shared by `FakeModbusRtuServer` (TCP-backed) and the pty-based serial
    fixture (fd-backed) so the request-framing logic — including FC15/16's
    variable-length remainder, added here rather than left as a second gap
    alongside the one this fixture already had — is written once.
    """
    head = await read_exact(2)  # unit id, function code
    unit_id, function_code = head[0], head[1]
    if function_code in _VARIABLE_LENGTH_REQUEST_CODES:
        # address(2) + quantity(2) + byte_count(1)
        prefix = await read_exact(5)
        byte_count = prefix[-1]
        data = await read_exact(byte_count)
        pdu = head[1:] + prefix + data
    else:
        # FC01-04 (read) and FC05/06 (single write) are all
        # address(2) + quantity_or_value(2) = 4 bytes.
        pdu = head[1:] + await read_exact(4)
    await read_exact(2)  # CRC
    return unit_id, pdu


def _pack_bits(values: list[bool]) -> bytes:
    byte_count = (len(values) + 7) // 8
    data = bytearray(byte_count)
    for i, bit in enumerate(values):
        if bit:
            data[i // 8] |= 1 << (i % 8)
    return bytes(data)


class _ModbusDatastore:
    """Shared register/coil datastore and PDU request handling, independent
    of framing (MBAP vs. RTU)."""

    def __init__(self) -> None:
        self.holding_registers: dict[int, int] = {}
        self.input_registers: dict[int, int] = {}
        self.coils: dict[int, bool] = {}
        self.discrete_inputs: dict[int, bool] = {}
        self.exceptions: dict[tuple[int, int], int] = {}
        # (function_code, address, quantity) per request received, in order.
        # Sprint C1 (XEDGE-411) — read batching is a claim about how many
        # round trips a scan cycle costs, so tests need to count and size the
        # actual requests rather than infer batching from the values returned.
        self.request_log: list[tuple[int, int, int]] = []

    @property
    def read_request_count(self) -> int:
        return sum(1 for fc, _, _ in self.request_log if fc in (0x01, 0x02, 0x03, 0x04))

    def handle_request(self, pdu: bytes) -> bytes:
        function_code = pdu[0]
        # Second/third fields are (address, quantity) for read requests but
        # (address, value) for FC05/06 single-write requests — same 4-byte
        # layout either way, so unpacking once and branching on meaning
        # below is correct for both.
        address, second_field = struct.unpack(">HH", pdu[1:5])
        self.request_log.append((function_code, address, second_field))

        forced_exception = self.exceptions.get((function_code, address))
        if forced_exception is not None:
            return bytes([function_code | codec.EXCEPTION_RESPONSE_FLAG, forced_exception])

        quantity = second_field
        if function_code == codec.FunctionCode.READ_HOLDING_REGISTERS:
            values = [self.holding_registers.get(address + i, 0) for i in range(quantity)]
            data = struct.pack(f">{quantity}H", *values)
            return bytes([function_code, len(data)]) + data
        if function_code == codec.FunctionCode.READ_INPUT_REGISTERS:
            values = [self.input_registers.get(address + i, 0) for i in range(quantity)]
            data = struct.pack(f">{quantity}H", *values)
            return bytes([function_code, len(data)]) + data
        if function_code == codec.FunctionCode.READ_COILS:
            bits = [self.coils.get(address + i, False) for i in range(quantity)]
            data = _pack_bits(bits)
            return bytes([function_code, len(data)]) + data
        if function_code == codec.FunctionCode.READ_DISCRETE_INPUTS:
            bits = [self.discrete_inputs.get(address + i, False) for i in range(quantity)]
            data = _pack_bits(bits)
            return bytes([function_code, len(data)]) + data
        if function_code == codec.FunctionCode.WRITE_SINGLE_COIL:
            self.coils[address] = second_field == 0xFF00
            return pdu[:5]  # success response is an echo of the request
        if function_code == codec.FunctionCode.WRITE_SINGLE_REGISTER:
            self.holding_registers[address] = second_field
            return pdu[:5]  # success response is an echo of the request
        if function_code == codec.FunctionCode.WRITE_MULTIPLE_REGISTERS:
            # pdu[5] is the byte-count field; register data follows it.
            values = struct.unpack(f">{quantity}H", pdu[6 : 6 + quantity * 2])
            for i, value in enumerate(values):
                self.holding_registers[address + i] = value
            return pdu[:5]  # response echoes function code + address + quantity
        if function_code == codec.FunctionCode.WRITE_MULTIPLE_COILS:
            byte_count = pdu[5]
            data = pdu[6 : 6 + byte_count]
            for i in range(quantity):
                self.coils[address + i] = bool(data[i // 8] & (1 << (i % 8)))
            return pdu[:5]  # response echoes function code + address + quantity

        return bytes(
            [function_code | codec.EXCEPTION_RESPONSE_FLAG, codec.ExceptionCode.ILLEGAL_FUNCTION]
        )


class FakeModbusServer(_ModbusDatastore):
    """In-memory Modbus TCP (MBAP-framed) server."""

    def __init__(self) -> None:
        super().__init__()
        self.host = "127.0.0.1"
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        # How many separate TCP connections this server has accepted
        # (Sprint C3, XEDGE-436) — the one thing that actually distinguishes
        # a `persistent` connection_mode driver (one, reused for every
        # transaction) from an `on_demand` one (a fresh connection dialed
        # and closed per transaction) on the wire, the same rationale
        # `request_log` already exists for batching.
        self.connection_count = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connection_count += 1
        try:
            while True:
                header = await reader.readexactly(codec.MBAP_HEADER_LENGTH)
                remainder = await reader.readexactly(codec.frame_remainder_length(header))
                transaction_id, unit_id, pdu = codec.decode_mbap(header + remainder)
                response_pdu = self.handle_request(pdu)
                writer.write(codec.encode_mbap(transaction_id, unit_id, response_pdu))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()


class FakeModbusRtuServer(_ModbusDatastore):
    """In-memory Modbus RTU-over-TCP (address+PDU+CRC framed) server."""

    def __init__(self) -> None:
        super().__init__()
        self.host = "127.0.0.1"
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                unit_id, request_pdu = await read_rtu_request_pdu(reader.readexactly)
                response_pdu = self.handle_request(request_pdu)
                frame = rtu_codec.encode_rtu_frame(unit_id, response_pdu)
                writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
