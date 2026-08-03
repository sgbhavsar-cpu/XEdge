"""Maps `snmp_client.schema.json`'s `data_type` strings to pysnmp's ASN.1
type constructors, and normalizes a response varbind's value back to
xEdge's `TagValue` union.

A GET/GETNEXT/GETBULK response is already self-typed by the device --
`decode()` needs no schema input, unlike Modbus's raw-register decode.
A SET request has no such source of truth (unlike a CIP tag, `pysnmp` has
no controller-side symbol table to infer a type from), so `encode()`
requires the tag's own configured `data_type`.
"""

from __future__ import annotations

from typing import Any

from pysnmp.hlapi.v3arch.asyncio import (
    Counter32,
    Counter64,
    Gauge32,
    Integer32,
    IpAddress,
    ObjectIdentifier,
    OctetString,
    TimeTicks,
    Unsigned32,
)

from xedge.drivers.base import TagValue

# Numeric ASN.1 types whose Python value is a plain int -- covers every
# writable numeric data_type this schema exposes. IpAddress and
# ObjectIdentifier are ASN.1-numeric too but are read/displayed as
# strings (dotted-quad / dotted-OID), not integers -- excluded here.
_NUMERIC_TYPES: tuple[Any, ...] = (
    Integer32,
    Counter32,
    Counter64,
    Gauge32,
    TimeTicks,
    Unsigned32,
)

_ENCODERS: dict[str, Any] = {
    "integer": Integer32,
    "octet_string": OctetString,
    "counter32": Counter32,
    "counter64": Counter64,
    "gauge32": Gauge32,
    "timeticks": TimeTicks,
    "ip_address": IpAddress,
    "unsigned32": Unsigned32,
    "object_identifier": ObjectIdentifier,
}


def encode(data_type: str, value: TagValue) -> Any:
    """Build the ASN.1 value object a SET request for `data_type` needs."""
    constructor = _ENCODERS[data_type]
    if data_type in ("octet_string", "ip_address", "object_identifier"):
        return constructor(str(value))
    return constructor(int(value))


def decode(value: Any) -> TagValue:
    """Normalize a response varbind's ASN.1 value to xEdge's TagValue
    union. Numeric types become `int` (so scaling/deadband apply the same
    way they do for every other driver); everything else (OctetString,
    IpAddress, ObjectIdentifier, Opaque, Bits, ...) uses `prettyPrint()`,
    not `str()` -- confirmed directly against the installed package that
    `str()` on a binary (non-UTF8) OctetString or an IpAddress produces
    mojibake, while `prettyPrint()` renders a binary OctetString as a
    clean `0xdeadbeef`-style hex string and an IpAddress as its dotted
    quad, same as every SNMP tool's own output.
    """
    if isinstance(value, _NUMERIC_TYPES):
        return int(value)
    return value.prettyPrint()  # type: ignore[no-any-return]
