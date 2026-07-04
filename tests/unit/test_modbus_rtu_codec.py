from __future__ import annotations

import pytest
from pymodbus.framer.rtu import FramerRTU

from xedge.drivers.modbus import rtu_codec


def _oracle_wire_crc_bytes(body: bytes) -> bytes:
    """pymodbus's compute_CRC returns an int in a swapped internal
    convention (see its source: it swaps bytes, then callers use
    `.to_bytes(2, 'big')`) — reproduce that exact conversion here so the
    comparison is against real wire bytes, not raw (differently-conventioned)
    integer values."""
    return FramerRTU.compute_CRC(body).to_bytes(2, "big")


@pytest.mark.parametrize(
    "body",
    [
        bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A]),
        bytes([0x11, 0x03, 0x00, 0x6B, 0x00, 0x03]),
        bytes([0x01, 0x05, 0x00, 0xAC, 0xFF, 0x00]),
        bytes([0xFF, 0x02, 0x00, 0x00, 0x00, 0x01]),
    ],
)
def test_compute_crc16_matches_pymodbus_oracle_wire_bytes(body: bytes) -> None:
    my_crc = rtu_codec.compute_crc16(body)
    my_wire_bytes = bytes([my_crc & 0xFF, (my_crc >> 8) & 0xFF])
    assert my_wire_bytes == _oracle_wire_crc_bytes(body)


def test_encode_rtu_frame_matches_pymodbus_oracle_wire_frame() -> None:
    pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x0A])
    frame = rtu_codec.encode_rtu_frame(address=1, pdu=pdu)
    body = bytes([1]) + pdu
    assert frame == body + _oracle_wire_crc_bytes(body)


def test_decode_rtu_frame_roundtrip() -> None:
    pdu = bytes([0x03, 0x02, 0x00, 0x64])
    frame = rtu_codec.encode_rtu_frame(address=17, pdu=pdu)
    address, decoded_pdu = rtu_codec.decode_rtu_frame(frame)
    assert address == 17
    assert decoded_pdu == pdu


def test_decode_rtu_frame_rejects_corrupted_crc() -> None:
    frame = bytearray(
        rtu_codec.encode_rtu_frame(address=1, pdu=bytes([0x03, 0x00, 0x00, 0x00, 0x0A]))
    )
    frame[-1] ^= 0xFF  # corrupt one CRC byte
    with pytest.raises(rtu_codec.ModbusRtuFramingError, match="CRC mismatch"):
        rtu_codec.decode_rtu_frame(bytes(frame))


def test_decode_rtu_frame_rejects_corrupted_body() -> None:
    frame = bytearray(
        rtu_codec.encode_rtu_frame(address=1, pdu=bytes([0x03, 0x00, 0x00, 0x00, 0x0A]))
    )
    frame[2] ^= 0xFF  # corrupt a body byte, leaving the original CRC
    with pytest.raises(rtu_codec.ModbusRtuFramingError, match="CRC mismatch"):
        rtu_codec.decode_rtu_frame(bytes(frame))


def test_decode_rtu_frame_rejects_too_short() -> None:
    with pytest.raises(rtu_codec.ModbusRtuFramingError, match="too short"):
        rtu_codec.decode_rtu_frame(bytes([0x01, 0x02]))


def test_encode_rtu_frame_rejects_bad_address() -> None:
    with pytest.raises(ValueError, match="address"):
        rtu_codec.encode_rtu_frame(address=256, pdu=b"\x03")


@pytest.mark.parametrize(
    ("baud_rate", "expected"),
    [
        (9600, pytest.approx(11 / 9600 * 3.5)),
        (19200, 1.75e-3),
        (115200, 1.75e-3),
    ],
)
def test_inter_frame_delay_seconds(baud_rate: int, expected: float) -> None:
    assert rtu_codec.inter_frame_delay_seconds(baud_rate) == expected
