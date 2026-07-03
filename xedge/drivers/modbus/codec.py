"""In-house Modbus MBAP + PDU codec (ADR-006, FR-SA-002/003/004).

Clean-room implementation from the Modbus Application Protocol V1.1b3 and
Modbus Messaging on TCP/IP Implementation Guide V1.0b specifications (both
freely published by the Modbus Organization). Pure encode/decode functions
with no I/O — the transport layer (xedge.drivers.modbus.tcp) owns the
socket. pymodbus is used only as a black-box test oracle (never read as a
reference implementation) to cross-validate wire compatibility; see
docs/planning/license-audit.md §5 for the provenance record.

Provenance record — Modbus TCP/RTU codec
Specification(s) used: Modbus Application Protocol V1.1b3; Modbus Messaging
on TCP/IP Implementation Guide V1.0b (both Modbus Organization, public).
Reference/oracle implementations used for black-box testing only: pymodbus 3.x.
Confirmation: no GPL-licensed source of a reference implementation was read
or consulted during development of this module.
"""

from __future__ import annotations

import struct
from enum import IntEnum

MBAP_HEADER_LENGTH = 7
PROTOCOL_IDENTIFIER_MODBUS = 0x0000
EXCEPTION_RESPONSE_FLAG = 0x80


class FunctionCode(IntEnum):
    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04


class ExceptionCode(IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SLAVE_DEVICE_FAILURE = 0x04
    ACKNOWLEDGE = 0x05
    SLAVE_DEVICE_BUSY = 0x06
    MEMORY_PARITY_ERROR = 0x08
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    GATEWAY_TARGET_DEVICE_FAILED_TO_RESPOND = 0x0B


_BIT_FUNCTION_CODES = (FunctionCode.READ_COILS, FunctionCode.READ_DISCRETE_INPUTS)
_REGISTER_FUNCTION_CODES = (FunctionCode.READ_HOLDING_REGISTERS, FunctionCode.READ_INPUT_REGISTERS)


class ModbusFramingError(Exception):
    """Raised when a received frame is malformed (bad protocol ID, truncated, etc.)."""


class ModbusException(Exception):
    """Raised when the device returns a Modbus exception response.

    This represents a valid, well-formed protocol-level rejection (e.g. an
    illegal register address) — distinct from a transport failure. Callers
    should map this to Quality.BAD for the affected tag without tearing down
    the connection.
    """

    def __init__(self, function_code: int, exception_code: int) -> None:
        self.function_code = function_code
        self.exception_code = exception_code
        try:
            name = ExceptionCode(exception_code).name
        except ValueError:
            name = f"UNKNOWN(0x{exception_code:02X})"
        super().__init__(f"Modbus exception on FC{function_code:#04x}: {name}")


def encode_mbap(transaction_id: int, unit_id: int, pdu: bytes) -> bytes:
    """Wrap a PDU in an MBAP header. `transaction_id` and `unit_id` must fit
    in 16 and 8 bits respectively (caller's responsibility to wrap/mask)."""
    length = len(pdu) + 1  # +1 for the unit identifier byte, per the MBAP spec
    header = struct.pack(">HHHB", transaction_id, PROTOCOL_IDENTIFIER_MODBUS, length, unit_id)
    return header + pdu


def decode_mbap(frame: bytes) -> tuple[int, int, bytes]:
    """Split a full MBAP+PDU frame into (transaction_id, unit_id, pdu).

    `frame` must be exactly one complete frame (header length field + 6
    already accounted for) — see `frame_length_from_header` for how a
    stream-based transport determines how many bytes to read.
    """
    if len(frame) < MBAP_HEADER_LENGTH:
        raise ModbusFramingError(f"Frame too short for MBAP header: {len(frame)} bytes")
    transaction_id, protocol_id, length, unit_id = struct.unpack(
        ">HHHB", frame[:MBAP_HEADER_LENGTH]
    )
    if protocol_id != PROTOCOL_IDENTIFIER_MODBUS:
        raise ModbusFramingError(f"Unexpected protocol identifier: {protocol_id:#06x}")
    pdu = frame[MBAP_HEADER_LENGTH : MBAP_HEADER_LENGTH + length - 1]
    if len(pdu) != length - 1:
        raise ModbusFramingError(
            f"Truncated frame: expected {length - 1} PDU bytes, got {len(pdu)}"
        )
    return transaction_id, unit_id, pdu


def frame_remainder_length(mbap_header: bytes) -> int:
    """Given the first 7 bytes read from the stream, return how many more
    bytes to read to have a complete frame (the unit id byte is already
    included in `mbap_header`, so this is `length - 1`)."""
    if len(mbap_header) != MBAP_HEADER_LENGTH:
        raise ModbusFramingError(
            f"Expected {MBAP_HEADER_LENGTH}-byte MBAP header, got {len(mbap_header)}"
        )
    (length,) = struct.unpack(">H", mbap_header[4:6])
    return int(length) - 1


def encode_read_request(function_code: FunctionCode, address: int, quantity: int) -> bytes:
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of range: {address}")
    if not 1 <= quantity <= 2000:
        raise ValueError(f"quantity out of range: {quantity}")
    return struct.pack(">BHH", function_code, address, quantity)


def _check_exception(pdu: bytes) -> None:
    function_code = pdu[0]
    if function_code & EXCEPTION_RESPONSE_FLAG:
        raise ModbusException(function_code & ~EXCEPTION_RESPONSE_FLAG, pdu[1])


def decode_bits_response(pdu: bytes, quantity: int) -> list[bool]:
    """Decode a FC01/FC02 response PDU into `quantity` booleans."""
    _check_exception(pdu)
    byte_count = pdu[1]
    data = pdu[2 : 2 + byte_count]
    bits = [bool(data[i // 8] & (1 << (i % 8))) for i in range(quantity)]
    return bits


def decode_registers_response(pdu: bytes) -> list[int]:
    """Decode a FC03/FC04 response PDU into a list of unsigned 16-bit register values."""
    _check_exception(pdu)
    byte_count = pdu[1]
    data = pdu[2 : 2 + byte_count]
    count = byte_count // 2
    return list(struct.unpack(f">{count}H", data))


def is_bit_function(function_code: FunctionCode) -> bool:
    return function_code in _BIT_FUNCTION_CODES


def is_register_function(function_code: FunctionCode) -> bool:
    return function_code in _REGISTER_FUNCTION_CODES
