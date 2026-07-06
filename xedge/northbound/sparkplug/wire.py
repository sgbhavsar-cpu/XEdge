"""Minimal protobuf wire-format encoder/decoder (ADR-006, ADR-002).

Encodes and decodes exactly the field types Sparkplug B payloads use — no
schema/descriptor machinery, no dependency on the `protobuf` package. This
is deliberately narrow: the Sparkplug B Payload/Metric message shape is
small and stable, and hand-rolling both directions directly avoids a
heavyweight protoc/grpcio-tools build step for what is, at the wire level,
just varints, fixed-width values, and length-delimited blocks (Protocol
Buffers Encoding, Google, public spec).

The decoder (Sprint 31, XEDGE-223: incoming NCMD write-back commands) is
new alongside the encoder that has existed since Sprint 3 — both are
in-house per ADR-006/ADR-002 (a decoder is "build," matching the encoder's
own decision; pysparkplug remains a test-only black-box oracle, never a
runtime dependency, for the same licensing/control reasons already
documented on the encoder side).

Field numbers and wire types used here are the Eclipse Sparkplug B v3.0
Payload.proto definition (public specification) — see
xedge/northbound/sparkplug/payload.py for the field-number tables and their
provenance note.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

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


class ProtobufDecodeError(Exception):
    """Raised on a malformed/truncated protobuf byte string."""


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a base-128 varint starting at `pos`; returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ProtobufDecodeError("Truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7


def iter_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield `(field_number, wire_type, value)` for each top-level field.

    `value` is an `int` for a varint field, or the raw payload `bytes` for
    fixed32 (4 bytes)/fixed64 (8 bytes, little-endian on the wire, matching
    `encode_fixed32_field`/`encode_fixed64_field`)/length-delimited fields
    (the inner content — a string, bytes blob, or nested message, left for
    the caller to interpret since this module has no message schema).
    Repeated field numbers (e.g. multiple `Metric` entries in one
    `Payload`) yield once per occurrence, in wire order — same shape as
    protobuf's own "last one wins for singular fields, all yielded for
    repeated fields" semantics, just left to the caller to apply.
    """
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field_number, wire_type = tag >> 3, tag & 0x7
        if wire_type == WIRE_TYPE_VARINT:
            value, pos = decode_varint(data, pos)
            yield field_number, wire_type, value
        elif wire_type == WIRE_TYPE_FIXED64:
            if pos + 8 > len(data):
                raise ProtobufDecodeError("Truncated fixed64 field")
            yield field_number, wire_type, data[pos : pos + 8]
            pos += 8
        elif wire_type == WIRE_TYPE_LENGTH_DELIMITED:
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                raise ProtobufDecodeError("Truncated length-delimited field")
            yield field_number, wire_type, data[pos : pos + length]
            pos += length
        elif wire_type == WIRE_TYPE_FIXED32:
            if pos + 4 > len(data):
                raise ProtobufDecodeError("Truncated fixed32 field")
            yield field_number, wire_type, data[pos : pos + 4]
            pos += 4
        else:
            raise ProtobufDecodeError(f"Unsupported wire type: {wire_type}")
