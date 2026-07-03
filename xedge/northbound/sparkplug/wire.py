"""Minimal protobuf wire-format encoder (ADR-006, ADR-002).

Encodes exactly the field types Sparkplug B payloads use — no decoder, no
schema/descriptor machinery, no dependency on the `protobuf` package. This
is deliberately narrow: the Sparkplug B Payload/Metric message shape is
small and stable, and hand-encoding it directly avoids a heavyweight
protoc/grpcio-tools build step for what is, at the wire level, just varints,
fixed-width values, and length-delimited blocks (Protocol Buffers Encoding,
Google, public spec).

Field numbers and wire types used here are the Eclipse Sparkplug B v3.0
Payload.proto definition (public specification) — see
xedge/northbound/sparkplug/payload.py for the field-number tables and their
provenance note.
"""

from __future__ import annotations

import struct

WIRE_TYPE_VARINT = 0
WIRE_TYPE_FIXED64 = 1
WIRE_TYPE_LENGTH_DELIMITED = 2
WIRE_TYPE_FIXED32 = 5


def encode_varint(value: int) -> bytes:
    """Protobuf base-128 varint encoding (unsigned only — Sparkplug B never
    uses zigzag/signed varints; negative ints aren't part of its datatypes)."""
    if value < 0:
        raise ValueError(f"encode_varint does not support negative values: {value}")
    parts = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            parts.append(byte | 0x80)
        else:
            parts.append(byte)
            return bytes(parts)


def encode_tag(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def encode_varint_field(field_number: int, value: int) -> bytes:
    return encode_tag(field_number, WIRE_TYPE_VARINT) + encode_varint(value)


def encode_bool_field(field_number: int, value: bool) -> bytes:
    return encode_varint_field(field_number, 1 if value else 0)


def encode_fixed32_field(field_number: int, value: float) -> bytes:
    return encode_tag(field_number, WIRE_TYPE_FIXED32) + struct.pack("<f", value)


def encode_fixed64_field(field_number: int, value: float) -> bytes:
    return encode_tag(field_number, WIRE_TYPE_FIXED64) + struct.pack("<d", value)


def encode_bytes_field(field_number: int, value: bytes) -> bytes:
    return encode_tag(field_number, WIRE_TYPE_LENGTH_DELIMITED) + encode_varint(len(value)) + value


def encode_string_field(field_number: int, value: str) -> bytes:
    return encode_bytes_field(field_number, value.encode("utf-8"))


def encode_message_field(field_number: int, message_bytes: bytes) -> bytes:
    return encode_bytes_field(field_number, message_bytes)
