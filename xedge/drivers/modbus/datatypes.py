"""Multi-register Modbus data types (Sprint C1, XEDGE-412).

XEDGE-CRD-001 §4.1 asks to "combine multiple registers into a single value".
Modbus itself has no concept of a data type — FC03/FC04 return an ordered
list of 16-bit registers, and how a 32- or 64-bit quantity is laid out across
them is a device convention, not a protocol rule. Devices disagree in two
independent ways, so both are configurable:

* **word order** — which 16-bit register holds the most significant half.
  Big-endian words (the majority, and the Modbus spec's own convention for
  multi-register quantities) put it first; a large minority of devices,
  including many Schneider and Wago products, put it last. Commonly written
  ABCD vs. CDAB.
* **byte order** — endianness *within* each register. Almost always
  big-endian; byte-swapped devices exist. Commonly written ABCD vs. BADC.

The two combine, so all four permutations (ABCD/CDAB/BADC/DCBA) are
reachable. Pure functions, no I/O — the transport hands over the register
list the codec decoded.
"""

from __future__ import annotations

import struct
from typing import Literal

WordOrder = Literal["big", "little"]
ByteOrder = Literal["big", "little"]

DEFAULT_DATA_TYPE = "uint16"
DEFAULT_WORD_ORDER: WordOrder = "big"
DEFAULT_BYTE_ORDER: ByteOrder = "big"

# struct format character per data type, and how many 16-bit registers it
# spans. The struct codes are all big-endian here; word and byte order are
# applied by reordering the raw bytes before unpacking, which keeps this
# table to one entry per type instead of one per type-and-ordering.
_TYPE_SPEC: dict[str, tuple[str, int]] = {
    "uint16": ("H", 1),
    "int16": ("h", 1),
    "uint32": ("I", 2),
    "int32": ("i", 2),
    "float32": ("f", 2),
    "uint64": ("Q", 4),
    "int64": ("q", 4),
    "float64": ("d", 4),
}

DATA_TYPE_NAMES = tuple(_TYPE_SPEC)


class ModbusDataTypeError(ValueError):
    """Raised for an unknown data type name, or a register list too short for
    the requested type."""


def register_count(data_type: str) -> int:
    """How many 16-bit registers `data_type` occupies on the wire.

    This is what makes read batching possible: the planner needs each tag's
    register span to know which addresses a block has to cover
    (xedge.drivers.modbus.planner).
    """
    spec = _TYPE_SPEC.get(data_type)
    if spec is None:
        raise ModbusDataTypeError(
            f"Unknown Modbus data type {data_type!r}; expected one of {', '.join(DATA_TYPE_NAMES)}"
        )
    return spec[1]


def is_float(data_type: str) -> bool:
    """Whether `data_type` decodes to a float rather than an int. Used to pick
    a sensible placeholder value for a Bad-quality tag, and to infer the OPC UA
    server's node type from config alone."""
    return data_type in ("float32", "float64")


def decode(
    registers: list[int],
    data_type: str = DEFAULT_DATA_TYPE,
    word_order: WordOrder = DEFAULT_WORD_ORDER,
    byte_order: ByteOrder = DEFAULT_BYTE_ORDER,
) -> int | float:
    """Combine `registers` into a single value of `data_type`.

    `registers` are unsigned 16-bit values in the order the device returned
    them, i.e. ascending address — exactly what
    `codec.decode_registers_response` produces. Extra trailing registers are
    ignored, so a block read can pass a slice without trimming it first.
    """
    format_char, count = _TYPE_SPEC.get(data_type, ("", 0)) or ("", 0)
    if not format_char:
        raise ModbusDataTypeError(
            f"Unknown Modbus data type {data_type!r}; expected one of {', '.join(DATA_TYPE_NAMES)}"
        )
    if len(registers) < count:
        raise ModbusDataTypeError(f"{data_type} needs {count} register(s), got {len(registers)}")

    words = registers[:count]
    if word_order == "little":
        words = list(reversed(words))

    raw = b"".join(
        struct.pack(">H" if byte_order == "big" else "<H", word & 0xFFFF) for word in words
    )
    value: int | float = struct.unpack(f">{format_char}", raw)[0]
    return value


def encode(
    value: int | float,
    data_type: str = DEFAULT_DATA_TYPE,
    word_order: WordOrder = DEFAULT_WORD_ORDER,
    byte_order: ByteOrder = DEFAULT_BYTE_ORDER,
) -> list[int]:
    """Split `value` into the register list a device expects, inverting
    `decode` exactly. Used by the multi-register write path (FC16)."""
    format_char, count = _TYPE_SPEC.get(data_type, ("", 0)) or ("", 0)
    if not format_char:
        raise ModbusDataTypeError(
            f"Unknown Modbus data type {data_type!r}; expected one of {', '.join(DATA_TYPE_NAMES)}"
        )

    coerced = float(value) if is_float(data_type) else int(value)
    try:
        raw = struct.pack(f">{format_char}", coerced)
    except struct.error as exc:
        raise ModbusDataTypeError(f"{value!r} is out of range for {data_type}: {exc}") from exc

    unpack_char = ">H" if byte_order == "big" else "<H"
    words = [struct.unpack(unpack_char, raw[i : i + 2])[0] for i in range(0, count * 2, 2)]
    if word_order == "little":
        words = list(reversed(words))
    return [int(word) for word in words]
