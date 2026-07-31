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
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_REGISTERS = 0x10


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

# Maximum quantity per read request, per Modbus Application Protocol V1.1b3
# §6.1-6.4. Both derive from the 253-byte PDU ceiling: 2000 bits is 250 bytes
# of packed coils, 125 registers is 250 bytes of register data, each leaving
# room for the function code and byte-count fields.
#
# Only enforced from Sprint C1 (XEDGE-411) onward in any meaningful sense —
# every read issued before block batching asked for exactly one value, so the
# single 2000 ceiling that used to apply to both was never reachable and its
# incorrectness for register functions never surfaced.
MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125


def max_read_quantity(function_code: FunctionCode) -> int:
    """Protocol ceiling on a single read request's quantity for this function
    code."""
    return MAX_READ_BITS if is_bit_function(function_code) else MAX_READ_REGISTERS


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
    limit = max_read_quantity(function_code)
    if not 1 <= quantity <= limit:
        raise ValueError(
            f"quantity out of range for FC{function_code:#04x}: {quantity} (max {limit})"
        )
    if address + quantity - 1 > 0xFFFF:
        raise ValueError(
            f"read of {quantity} from address {address} runs past the end of the "
            f"16-bit address space"
        )
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


def encode_write_single_coil(address: int, value: bool) -> bytes:
    """FC05 request PDU. Per spec, ON is encoded as 0xFF00, OFF as 0x0000 —
    not a plain boolean byte."""
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of range: {address}")
    return struct.pack(">BHH", FunctionCode.WRITE_SINGLE_COIL, address, 0xFF00 if value else 0x0000)


def encode_write_single_register(address: int, value: int) -> bytes:
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of range: {address}")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"register value out of range: {value}")
    return struct.pack(">BHH", FunctionCode.WRITE_SINGLE_REGISTER, address, value)


def encode_write_multiple_registers(address: int, values: list[int]) -> bytes:
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of range: {address}")
    if not 1 <= len(values) <= 123:
        raise ValueError(f"quantity out of range: {len(values)}")
    byte_count = len(values) * 2
    return struct.pack(
        f">BHHB{len(values)}H",
        FunctionCode.WRITE_MULTIPLE_REGISTERS,
        address,
        len(values),
        byte_count,
        *values,
    )


def decode_write_single_response(pdu: bytes) -> tuple[int, int]:
    """FC05/FC06 response PDU: an echo of the request (address, value) —
    the write is confirmed once this decodes without a ModbusException."""
    _check_exception(pdu)
    address, value = struct.unpack(">HH", pdu[1:5])
    return address, value


def decode_write_multiple_response(pdu: bytes) -> tuple[int, int]:
    """FC16 response PDU: (address, quantity written)."""
    _check_exception(pdu)
    address, quantity = struct.unpack(">HH", pdu[1:5])
    return address, quantity
