"""Sparkplug B Payload/Metric encoding (FR-NB-002, FR-NB-003, ADR-002, ADR-006).

Provenance record — Sparkplug B encoder
Specification(s) used: Eclipse Sparkplug B Specification v3.0 (Eclipse
Foundation, public); the standardized Payload.proto field numbers and
DataType enumeration it defines (also public — these are wire-protocol
identifiers, not implementation source).
Reference implementations used for black-box testing only: pysparkplug
(Apache-2.0) — its bundled, officially-generated protobuf classes decode
what this module encodes, in tests, to confirm wire compatibility. tahu's
reference encoder is not production-grade and is not used at runtime or
read as source.
Confirmation: no GPL-licensed source was read or consulted during
development of this module (n/a here — no GPL candidate exists for
Sparkplug B; the in-house build is for control/differentiation, not
licensing, per ADR-002/ADR-006).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from xedge.drivers.base import TagValue
from xedge.northbound.sparkplug import wire


class DataType(IntEnum):
    """Sparkplug B v3.0 DataType enumeration (Payload.proto, public spec).
    Only the subset xEdge currently emits is listed; extend as needed."""

    UNKNOWN = 0
    INT8 = 1
    INT16 = 2
    INT32 = 3
    INT64 = 4
    UINT8 = 5
    UINT16 = 6
    UINT32 = 7
    UINT64 = 8
    FLOAT = 9
    DOUBLE = 10
    BOOLEAN = 11
    STRING = 12
    DATETIME = 13
    TEXT = 14


# Metric value field numbers (Payload.Metric message, public Payload.proto).
_FIELD_INT_VALUE = 10
_FIELD_LONG_VALUE = 11
_FIELD_FLOAT_VALUE = 12
_FIELD_DOUBLE_VALUE = 13
_FIELD_BOOLEAN_VALUE = 14
_FIELD_STRING_VALUE = 15
_FIELD_BYTES_VALUE = 16

_FIELD_METRIC_NAME = 1
_FIELD_METRIC_ALIAS = 2
_FIELD_METRIC_TIMESTAMP = 3
_FIELD_METRIC_DATATYPE = 4
_FIELD_METRIC_IS_NULL = 7

_FIELD_PAYLOAD_TIMESTAMP = 1
_FIELD_PAYLOAD_METRICS = 2
_FIELD_PAYLOAD_SEQ = 3

_INT_FIELD_DATATYPES = (
    DataType.INT8,
    DataType.INT16,
    DataType.INT32,
    DataType.UINT8,
    DataType.UINT16,
    DataType.UINT32,
)
_LONG_FIELD_DATATYPES = (DataType.INT64, DataType.UINT64, DataType.DATETIME)
_STRING_FIELD_DATATYPES = (DataType.STRING, DataType.TEXT)


@dataclass(frozen=True, slots=True)
class SparkplugMetric:
    name: str | None
    timestamp_ms: int
    datatype: DataType
    value: TagValue | None = None
    alias: int | None = None
    is_null: bool = False


def infer_datatype(value: TagValue) -> DataType:
    """Map a TagValue's Python type to a Sparkplug B DataType.

    Modbus register/coil values (Sprint 2 scope) are always bool or an
    unsigned 16-bit int, safely represented as UINT32 — see ADR-006 driver
    table. Float/str/bytes support anticipates later-sprint engineering-unit
    scaling and virtual tags.
    """
    if isinstance(value, bool):
        return DataType.BOOLEAN
    if isinstance(value, int):
        return DataType.UINT32
    if isinstance(value, float):
        return DataType.DOUBLE
    if isinstance(value, str):
        return DataType.STRING
    raise TypeError(f"No Sparkplug B datatype mapping for value type: {type(value)!r}")


def _encode_metric_value(datatype: DataType, value: TagValue) -> bytes:
    if datatype in _INT_FIELD_DATATYPES:
        return wire.encode_varint_field(_FIELD_INT_VALUE, int(value))
    if datatype in _LONG_FIELD_DATATYPES:
        return wire.encode_varint_field(_FIELD_LONG_VALUE, int(value))
    if datatype == DataType.FLOAT:
        return wire.encode_fixed32_field(_FIELD_FLOAT_VALUE, float(value))
    if datatype == DataType.DOUBLE:
        return wire.encode_fixed64_field(_FIELD_DOUBLE_VALUE, float(value))
    if datatype == DataType.BOOLEAN:
        return wire.encode_bool_field(_FIELD_BOOLEAN_VALUE, bool(value))
    if datatype in _STRING_FIELD_DATATYPES:
        return wire.encode_string_field(_FIELD_STRING_VALUE, str(value))
    raise ValueError(f"Unsupported Sparkplug B datatype for encoding: {datatype!r}")


def encode_metric(metric: SparkplugMetric) -> bytes:
    parts = bytearray()
    if metric.name is not None:
        parts += wire.encode_string_field(_FIELD_METRIC_NAME, metric.name)
    if metric.alias is not None:
        parts += wire.encode_varint_field(_FIELD_METRIC_ALIAS, metric.alias)
    parts += wire.encode_varint_field(_FIELD_METRIC_TIMESTAMP, metric.timestamp_ms)
    parts += wire.encode_varint_field(_FIELD_METRIC_DATATYPE, int(metric.datatype))
    if metric.is_null or metric.value is None:
        parts += wire.encode_bool_field(_FIELD_METRIC_IS_NULL, True)
    else:
        parts += _encode_metric_value(metric.datatype, metric.value)
    return bytes(parts)


def encode_payload(timestamp_ms: int, seq: int | None, metrics: Sequence[SparkplugMetric]) -> bytes:
    """Encode a full Sparkplug B Payload message.

    `seq` is omitted (None) for NDEATH, which per spec carries only the
    `bdSeq` metric and no top-level sequence number.
    """
    parts = bytearray()
    parts += wire.encode_varint_field(_FIELD_PAYLOAD_TIMESTAMP, timestamp_ms)
    for metric in metrics:
        parts += wire.encode_message_field(_FIELD_PAYLOAD_METRICS, encode_metric(metric))
    if seq is not None:
        parts += wire.encode_varint_field(_FIELD_PAYLOAD_SEQ, seq)
    return bytes(parts)


def decode_metric(data: bytes) -> SparkplugMetric:
    """Decode a single `Metric` submessage — the counterpart to
    `encode_metric` (Sprint 31, XEDGE-223: incoming NCMD write commands)."""
    name: str | None = None
    timestamp_ms = 0
    datatype = DataType.UNKNOWN
    is_null = False
    int_value: int | None = None
    fixed32_bytes: bytes | None = None
    fixed64_bytes: bytes | None = None
    bool_value: bool | None = None
    string_value: str | None = None

    for field_number, _wire_type, raw in wire.iter_fields(data):
        if field_number == _FIELD_METRIC_NAME:
            name = raw.decode("utf-8")  # type: ignore[union-attr]
        elif field_number == _FIELD_METRIC_TIMESTAMP:
            timestamp_ms = raw  # type: ignore[assignment]
        elif field_number == _FIELD_METRIC_DATATYPE:
            datatype = DataType(raw)  # type: ignore[arg-type]
        elif field_number == _FIELD_METRIC_IS_NULL:
            is_null = bool(raw)
        elif field_number in (_FIELD_INT_VALUE, _FIELD_LONG_VALUE):
            int_value = raw  # type: ignore[assignment]
        elif field_number == _FIELD_FLOAT_VALUE:
            fixed32_bytes = raw  # type: ignore[assignment]
        elif field_number == _FIELD_DOUBLE_VALUE:
            fixed64_bytes = raw  # type: ignore[assignment]
        elif field_number == _FIELD_BOOLEAN_VALUE:
            bool_value = bool(raw)
        elif field_number == _FIELD_STRING_VALUE:
            string_value = raw.decode("utf-8")  # type: ignore[union-attr]

    value: TagValue | None = None
    if not is_null:
        if datatype in _INT_FIELD_DATATYPES or datatype in _LONG_FIELD_DATATYPES:
            value = int_value
        elif datatype == DataType.FLOAT and fixed32_bytes is not None:
            value = struct.unpack("<f", fixed32_bytes)[0]
        elif datatype == DataType.DOUBLE and fixed64_bytes is not None:
            value = struct.unpack("<d", fixed64_bytes)[0]
        elif datatype == DataType.BOOLEAN:
            value = bool_value
        elif datatype in _STRING_FIELD_DATATYPES:
            value = string_value

    return SparkplugMetric(
        name=name, timestamp_ms=timestamp_ms, datatype=datatype, value=value, is_null=is_null
    )


def decode_payload(data: bytes) -> tuple[int, int | None, list[SparkplugMetric]]:
    """Decode a full Sparkplug B Payload message into
    `(timestamp_ms, seq, metrics)` — the counterpart to `encode_payload`.
    `seq` is `None` if the payload carried no top-level sequence number
    (matching NDEATH's own encode-side omission)."""
    timestamp_ms = 0
    seq: int | None = None
    metrics: list[SparkplugMetric] = []

    for field_number, _wire_type, raw in wire.iter_fields(data):
        if field_number == _FIELD_PAYLOAD_TIMESTAMP:
            timestamp_ms = raw  # type: ignore[assignment]
        elif field_number == _FIELD_PAYLOAD_METRICS:
            metrics.append(decode_metric(raw))  # type: ignore[arg-type]
        elif field_number == _FIELD_PAYLOAD_SEQ:
            seq = raw  # type: ignore[assignment]

    return timestamp_ms, seq, metrics
