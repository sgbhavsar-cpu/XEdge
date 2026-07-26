"""Multi-register data type tests (Sprint C1, XEDGE-412).

Reference values are taken from the IEEE 754 / two's-complement encodings
directly rather than from another Modbus library, keeping the ADR-006
clean-room posture: no reference implementation was consulted.
"""

from __future__ import annotations

import pytest

from xedge.drivers.modbus.datatypes import (
    DATA_TYPE_NAMES,
    ModbusDataTypeError,
    decode,
    encode,
    is_float,
    register_count,
)


class TestRegisterCount:
    @pytest.mark.parametrize(
        ("data_type", "expected"),
        [
            ("uint16", 1),
            ("int16", 1),
            ("uint32", 2),
            ("int32", 2),
            ("float32", 2),
            ("uint64", 4),
            ("int64", 4),
            ("float64", 4),
        ],
    )
    def test_span_per_type(self, data_type: str, expected: int) -> None:
        assert register_count(data_type) == expected

    def test_unknown_type_names_the_valid_options(self) -> None:
        with pytest.raises(ModbusDataTypeError, match="uint16"):
            register_count("float16")


class TestSingleRegister:
    def test_uint16_passes_through(self) -> None:
        assert decode([40000], "uint16") == 40000

    def test_int16_is_signed(self) -> None:
        # 0xFFFF as two's complement is -1; as unsigned it is 65535.
        assert decode([0xFFFF], "int16") == -1
        assert decode([0xFFFF], "uint16") == 65535

    def test_int16_negative_boundary(self) -> None:
        assert decode([0x8000], "int16") == -32768


class TestWordOrder:
    """0x0001_0000 == 65536. Big-endian words put the high half first."""

    def test_big_endian_words_high_half_first(self) -> None:
        assert decode([0x0001, 0x0000], "uint32", word_order="big") == 65536

    def test_little_endian_words_high_half_last(self) -> None:
        assert decode([0x0000, 0x0001], "uint32", word_order="little") == 65536

    def test_word_order_changes_the_result(self) -> None:
        registers = [0x1234, 0x5678]
        assert decode(registers, "uint32", word_order="big") == 0x12345678
        assert decode(registers, "uint32", word_order="little") == 0x56781234


class TestByteOrder:
    def test_little_endian_bytes_swap_within_each_register(self) -> None:
        assert decode([0x1234], "uint16", byte_order="little") == 0x3412

    def test_all_four_permutations_are_distinct(self) -> None:
        registers = [0x1122, 0x3344]
        results = {
            decode(registers, "uint32", word_order=w, byte_order=b)
            for w in ("big", "little")
            for b in ("big", "little")
        }
        assert len(results) == 4, "ABCD/CDAB/BADC/DCBA must all differ for this input"


class TestFloats:
    def test_float32_known_ieee754_value(self) -> None:
        # 1.0f is 0x3F800000.
        assert decode([0x3F80, 0x0000], "float32") == 1.0

    def test_float32_negative(self) -> None:
        # -2.5f is 0xC0200000.
        assert decode([0xC020, 0x0000], "float32") == -2.5

    def test_float64_known_ieee754_value(self) -> None:
        # 1.0d is 0x3FF0000000000000.
        assert decode([0x3FF0, 0x0000, 0x0000, 0x0000], "float64") == 1.0

    def test_float32_word_swapped_device(self) -> None:
        """The single most common real-world Modbus float problem: a device
        that reports IEEE 754 with the low word first."""
        assert decode([0x0000, 0x3F80], "float32", word_order="little") == 1.0

    def test_is_float_only_for_float_types(self) -> None:
        assert [t for t in DATA_TYPE_NAMES if is_float(t)] == ["float32", "float64"]


class TestSixtyFourBit:
    def test_uint64_full_width(self) -> None:
        assert decode([0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF], "uint64") == 2**64 - 1

    def test_int64_negative_one(self) -> None:
        assert decode([0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF], "int64") == -1


class TestSlicing:
    def test_extra_trailing_registers_are_ignored(self) -> None:
        """A block read hands over a slice of a larger response; the decoder
        takes only the registers the type needs, so the caller does not have
        to trim first."""
        assert decode([0x0001, 0x0000, 0xDEAD, 0xBEEF], "uint32") == 65536

    def test_too_few_registers_is_an_error_not_a_wrong_answer(self) -> None:
        with pytest.raises(ModbusDataTypeError, match="needs 4 register"):
            decode([0x0001, 0x0000], "uint64")


class TestRoundTrip:
    @pytest.mark.parametrize("data_type", ["uint16", "int16", "uint32", "int32", "uint64", "int64"])
    @pytest.mark.parametrize("word_order", ["big", "little"])
    @pytest.mark.parametrize("byte_order", ["big", "little"])
    def test_integer_round_trip(self, data_type: str, word_order: str, byte_order: str) -> None:
        value = -12345 if data_type.startswith("int") else 12345
        registers = encode(value, data_type, word_order=word_order, byte_order=byte_order)  # type: ignore[arg-type]
        assert len(registers) == register_count(data_type)
        assert all(0 <= r <= 0xFFFF for r in registers), "each word must be a valid register"
        assert (
            decode(registers, data_type, word_order=word_order, byte_order=byte_order)  # type: ignore[arg-type]
            == value
        )

    @pytest.mark.parametrize("data_type", ["float32", "float64"])
    @pytest.mark.parametrize("word_order", ["big", "little"])
    def test_float_round_trip(self, data_type: str, word_order: str) -> None:
        value = -2.5  # exactly representable in both widths
        registers = encode(value, data_type, word_order=word_order)  # type: ignore[arg-type]
        assert decode(registers, data_type, word_order=word_order) == value  # type: ignore[arg-type]

    def test_out_of_range_value_is_rejected(self) -> None:
        with pytest.raises(ModbusDataTypeError, match="out of range"):
            encode(70000, "uint16")

    def test_encode_rejects_unknown_type(self) -> None:
        with pytest.raises(ModbusDataTypeError):
            encode(1, "float16")
