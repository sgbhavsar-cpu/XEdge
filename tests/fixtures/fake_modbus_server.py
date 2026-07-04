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

from xedge.drivers.modbus import codec, rtu_codec


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

    def handle_request(self, pdu: bytes) -> bytes:
        function_code = pdu[0]
        address, quantity = struct.unpack(">HH", pdu[1:5])

        forced_exception = self.exceptions.get((function_code, address))
        if forced_exception is not None:
            return bytes([function_code | codec.EXCEPTION_RESPONSE_FLAG, forced_exception])

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
                head = await reader.readexactly(2)  # address, function code
                request_pdu_head = head[1:2]
                # Read requests (FC01/02/03/04) all have a fixed 4-byte
                # remainder after the function code: address(2) + quantity(2).
                remainder = await reader.readexactly(4)
                request_pdu = request_pdu_head + remainder
                await reader.readexactly(2)  # request CRC — not re-validated by this fake

                response_pdu = self.handle_request(request_pdu)
                frame = rtu_codec.encode_rtu_frame(head[0], response_pdu)
                writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
