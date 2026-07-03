from __future__ import annotations

import struct

import pytest

from xedge.northbound.sparkplug import wire


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16384, b"\x80\x80\x01"),
        (2**35, b"\x80\x80\x80\x80\x80\x01"),
    ],
)
def test_encode_varint(value: int, expected: bytes) -> None:
    assert wire.encode_varint(value) == expected


def test_encode_varint_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        wire.encode_varint(-1)


def test_encode_tag_combines_field_number_and_wire_type() -> None:
    # field 1, wire type 0 (varint): (1 << 3) | 0 = 8
    assert wire.encode_tag(1, wire.WIRE_TYPE_VARINT) == b"\x08"
    # field 2, wire type 2 (length-delimited): (2 << 3) | 2 = 18
    assert wire.encode_tag(2, wire.WIRE_TYPE_LENGTH_DELIMITED) == b"\x12"


def test_encode_varint_field() -> None:
    # field 3, value 42: tag=(3<<3)|0=24=0x18, varint(42)=0x2a
    assert wire.encode_varint_field(3, 42) == b"\x18\x2a"


def test_encode_bool_field() -> None:
    assert wire.encode_bool_field(7, True) == wire.encode_varint_field(7, 1)
    assert wire.encode_bool_field(7, False) == wire.encode_varint_field(7, 0)


def test_encode_fixed32_field_matches_struct_pack() -> None:
    encoded = wire.encode_fixed32_field(12, 3.5)
    tag = wire.encode_tag(12, wire.WIRE_TYPE_FIXED32)
    assert encoded == tag + struct.pack("<f", 3.5)
    assert len(encoded) == len(tag) + 4


def test_encode_fixed64_field_matches_struct_pack() -> None:
    encoded = wire.encode_fixed64_field(13, 2.718281828)
    tag = wire.encode_tag(13, wire.WIRE_TYPE_FIXED64)
    assert encoded == tag + struct.pack("<d", 2.718281828)
    assert len(encoded) == len(tag) + 8


def test_encode_string_field() -> None:
    encoded = wire.encode_string_field(1, "hi")
    tag = wire.encode_tag(1, wire.WIRE_TYPE_LENGTH_DELIMITED)
    assert encoded == tag + b"\x02hi"


def test_encode_bytes_field_empty() -> None:
    encoded = wire.encode_bytes_field(1, b"")
    tag = wire.encode_tag(1, wire.WIRE_TYPE_LENGTH_DELIMITED)
    assert encoded == tag + b"\x00"


def test_encode_message_field_wraps_length_delimited() -> None:
    inner = b"\x01\x02\x03"
    encoded = wire.encode_message_field(2, inner)
    tag = wire.encode_tag(2, wire.WIRE_TYPE_LENGTH_DELIMITED)
    assert encoded == tag + bytes([len(inner)]) + inner
