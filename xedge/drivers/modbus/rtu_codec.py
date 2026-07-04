"""Modbus RTU framing: CRC-16/MODBUS + address/PDU/CRC frame layout
(FR-SA-001, FR-SA-003, ADR-006).

Used both by real serial RTU (xedge.drivers.modbus.serial) and RTU-over-TCP
(xedge.drivers.modbus.rtu_over_tcp) — the two differ only in transport, not
framing. The PDU itself (function code + data) reuses xedge.drivers.modbus.codec.

Provenance record — Modbus RTU framing
Specification(s) used: Modbus over Serial Line Specification and
Implementation Guide V1.02 (Modbus Organization, public). CRC-16/MODBUS
(polynomial 0xA001, init 0xFFFF) is a public, standardized algorithm.
Reference/oracle implementations used for black-box testing only: pymodbus
3.x's `FramerRTU.compute_CRC` (BSD-3-Clause), called only as a static
utility to cross-check our CRC output, not read as a reference for framing
logic.
Confirmation: no GPL-licensed source was read or consulted during
development of this module.
"""

from __future__ import annotations

_CRC_POLYNOMIAL = 0xA001
_CRC_INITIAL = 0xFFFF
MIN_RTU_FRAME_LENGTH = 4  # 1 address + 1 function code (minimum PDU) + 2 CRC


class ModbusRtuFramingError(Exception):
    """Raised when a received RTU frame fails CRC validation or is too short."""


def compute_crc16(data: bytes) -> int:
    """CRC-16/MODBUS over `data` (address byte + PDU, no CRC bytes)."""
    crc = _CRC_INITIAL
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ _CRC_POLYNOMIAL
            else:
                crc >>= 1
    return crc


def encode_rtu_frame(address: int, pdu: bytes) -> bytes:
    """Wrap a PDU in an RTU frame: address + PDU + CRC-16 (little-endian on the wire)."""
    if not 0 <= address <= 255:
        raise ValueError(f"address out of range: {address}")
    body = bytes([address]) + pdu
    crc = compute_crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def decode_rtu_frame(frame: bytes) -> tuple[int, bytes]:
    """Validate CRC and split a full RTU frame into (address, pdu)."""
    if len(frame) < MIN_RTU_FRAME_LENGTH:
        raise ModbusRtuFramingError(f"Frame too short for RTU framing: {len(frame)} bytes")
    body, received_crc_bytes = frame[:-2], frame[-2:]
    received_crc = received_crc_bytes[0] | (received_crc_bytes[1] << 8)
    computed_crc = compute_crc16(body)
    if received_crc != computed_crc:
        raise ModbusRtuFramingError(
            f"CRC mismatch: received {received_crc:#06x}, computed {computed_crc:#06x}"
        )
    return body[0], body[1:]


def inter_frame_delay_seconds(baud_rate: int) -> float:
    """T3.5 inter-frame silence per the Modbus over Serial Line spec: 3.5
    character times, or a fixed 1.75ms at baud rates >= 19200 (where
    character-time-based silence would be too short to reliably detect)."""
    if baud_rate >= 19200:
        return 1.75e-3
    character_time_seconds = 11 / baud_rate  # 11 bits/char: start+8 data+parity+stop (worst case)
    return character_time_seconds * 3.5
