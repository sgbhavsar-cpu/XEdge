"""Read-batching planner tests (Sprint C1, XEDGE-411).

The planner decides how many round trips a scan cycle costs, and a coalescing
bug is the kind that produces *plausible but wrong* values — a tag silently
reading its neighbour's register. So these tests assert on exact block
boundaries and exact per-tag offsets, not just block counts.
"""

from __future__ import annotations

from typing import Any

import pytest

from xedge.drivers.modbus import codec
from xedge.drivers.modbus.planner import ReadBlock, plan_read_blocks, tag_span
from xedge.drivers.modbus.polling import FUNCTION_CODE_BY_NAME


def _tag(
    tag_id: str,
    address: int,
    function_code: str = "read_holding_registers",
    data_type: str | None = None,
) -> dict[str, Any]:
    tag: dict[str, Any] = {"id": tag_id, "address": address, "function_code": function_code}
    if data_type is not None:
        tag["data_type"] = data_type
    return tag


def _plan(tags: list[dict[str, Any]], **kwargs: Any) -> list[ReadBlock]:
    return plan_read_blocks(tags, FUNCTION_CODE_BY_NAME, **kwargs)


def _ids(block: ReadBlock) -> list[str]:
    return [planned.tag["id"] for planned in block.tags]


class TestCoalescing:
    def test_contiguous_tags_become_one_request(self) -> None:
        blocks = _plan([_tag("a", 0), _tag("b", 1), _tag("c", 2)])
        assert len(blocks) == 1
        assert (blocks[0].address, blocks[0].quantity) == (0, 3)
        assert _ids(blocks[0]) == ["a", "b", "c"]

    def test_each_tag_records_its_offset_into_the_block(self) -> None:
        blocks = _plan([_tag("a", 10), _tag("b", 11), _tag("c", 12)])
        assert [(p.tag["id"], p.offset) for p in blocks[0].tags] == [("a", 0), ("b", 1), ("c", 2)]

    def test_gap_splits_into_separate_requests_by_default(self) -> None:
        """Strictly contiguous by default: reading an unmapped register can
        make a device fail the whole block with ILLEGAL_DATA_ADDRESS."""
        blocks = _plan([_tag("a", 0), _tag("b", 1), _tag("c", 50)])
        assert [(b.address, b.quantity) for b in blocks] == [(0, 2), (50, 1)]

    def test_gap_tolerance_merges_across_small_holes(self) -> None:
        blocks = _plan([_tag("a", 0), _tag("b", 4)], max_block_gap=8)
        assert len(blocks) == 1
        assert (blocks[0].address, blocks[0].quantity) == (0, 5)
        assert [p.offset for p in blocks[0].tags] == [0, 4]

    def test_gap_tolerance_does_not_merge_beyond_its_limit(self) -> None:
        blocks = _plan([_tag("a", 0), _tag("b", 20)], max_block_gap=8)
        assert len(blocks) == 2

    def test_config_order_does_not_matter(self) -> None:
        shuffled = _plan([_tag("c", 2), _tag("a", 0), _tag("b", 1)])
        assert len(shuffled) == 1
        assert _ids(shuffled[0]) == ["a", "b", "c"], "tags are ordered by address within a block"

    def test_single_tag_is_a_single_register_read(self) -> None:
        blocks = _plan([_tag("only", 7)])
        assert [(b.address, b.quantity, b.is_batched) for b in blocks] == [(7, 1, False)]


class TestFunctionCodePartitioning:
    def test_different_function_codes_never_share_a_request(self) -> None:
        """A read request carries exactly one function code, so adjacent
        addresses in different memory classes cannot be merged."""
        blocks = _plan(
            [
                _tag("coil", 0, "read_coils"),
                _tag("holding", 0, "read_holding_registers"),
                _tag("input", 0, "read_input_registers"),
                _tag("discrete", 0, "read_discrete_inputs"),
            ]
        )
        assert len(blocks) == 4
        assert len({b.function_code for b in blocks}) == 4

    def test_same_function_code_still_coalesces_across_partitions(self) -> None:
        blocks = _plan(
            [
                _tag("c0", 0, "read_coils"),
                _tag("h0", 0, "read_holding_registers"),
                _tag("c1", 1, "read_coils"),
                _tag("h1", 1, "read_holding_registers"),
            ]
        )
        assert len(blocks) == 2
        assert all(b.quantity == 2 for b in blocks)


class TestBlockSizeLimits:
    def test_protocol_register_ceiling_is_125_not_2000(self) -> None:
        """FC03/FC04 top out at 125 registers per the 253-byte PDU ceiling."""
        blocks = _plan([_tag(f"t{i}", i) for i in range(130)])
        assert [b.quantity for b in blocks] == [125, 5]
        assert sum(len(b.tags) for b in blocks) == 130

    def test_protocol_bit_ceiling_is_2000(self) -> None:
        blocks = _plan([_tag(f"t{i}", i, "read_coils") for i in range(2100)])
        assert [b.quantity for b in blocks] == [2000, 100]

    def test_configured_max_block_size_is_honoured(self) -> None:
        blocks = _plan([_tag(f"t{i}", i) for i in range(25)], max_block_size=10)
        assert [b.quantity for b in blocks] == [10, 10, 5]

    def test_configured_size_above_the_protocol_ceiling_is_clamped(self) -> None:
        """A device cannot answer a 500-register request whatever the config
        says, so clamp rather than emit an un-answerable request."""
        blocks = _plan([_tag(f"t{i}", i) for i in range(130)], max_block_size=500)
        assert max(b.quantity for b in blocks) == codec.MAX_READ_REGISTERS

    def test_max_block_size_of_one_disables_batching(self) -> None:
        blocks = _plan([_tag("a", 0), _tag("b", 1), _tag("c", 2)], max_block_size=1)
        assert [b.quantity for b in blocks] == [1, 1, 1]
        assert all(not b.is_batched for b in blocks)

    def test_a_wide_tag_is_read_even_when_it_exceeds_the_limit(self) -> None:
        """A 4-register value cannot be fetched in fewer than 4 registers;
        honouring max_block_size here would mean never reading the tag."""
        blocks = _plan([_tag("energy", 0, data_type="float64")], max_block_size=1)
        assert [(b.address, b.quantity) for b in blocks] == [(0, 4)]


class TestMultiRegisterTags:
    def test_span_comes_from_the_data_type(self) -> None:
        blocks = _plan([_tag("flow", 0, data_type="float32"), _tag("temp", 2)])
        assert len(blocks) == 1
        assert (blocks[0].address, blocks[0].quantity) == (0, 3)
        assert [(p.tag["id"], p.offset, p.span) for p in blocks[0].tags] == [
            ("flow", 0, 2),
            ("temp", 2, 1),
        ]

    def test_wide_tag_leaves_no_false_gap_behind_it(self) -> None:
        """A float32 at 0 occupies 0 and 1, so a tag at 2 is contiguous — not
        separated by a one-register gap."""
        blocks = _plan([_tag("flow", 0, data_type="float32"), _tag("temp", 2)], max_block_gap=0)
        assert len(blocks) == 1

    def test_gap_after_a_wide_tag_still_splits(self) -> None:
        blocks = _plan([_tag("flow", 0, data_type="float32"), _tag("temp", 9)])
        assert [(b.address, b.quantity) for b in blocks] == [(0, 2), (9, 1)]

    @pytest.mark.parametrize(
        ("data_type", "expected_span"),
        [("uint16", 1), ("int32", 2), ("float32", 2), ("float64", 4), ("uint64", 4)],
    )
    def test_tag_span_matches_the_data_type(self, data_type: str, expected_span: int) -> None:
        tag = _tag("t", 0, data_type=data_type)
        assert tag_span(tag, codec.FunctionCode.READ_HOLDING_REGISTERS) == expected_span

    def test_data_type_is_ignored_for_bit_functions(self) -> None:
        """Coils address individual bits; a data type there is meaningless
        rather than an error."""
        tag = _tag("t", 0, "read_coils", data_type="float64")
        assert tag_span(tag, codec.FunctionCode.READ_COILS) == 1


class TestOverlappingTags:
    def test_two_tags_at_the_same_address_both_appear(self) -> None:
        """Legal and occasionally deliberate — e.g. exposing a status word
        both raw and as a scaled value."""
        blocks = _plan([_tag("raw", 4), _tag("scaled", 4)])
        assert len(blocks) == 1
        assert (blocks[0].address, blocks[0].quantity) == (4, 1)
        assert sorted(_ids(blocks[0])) == ["raw", "scaled"]
        assert all(p.offset == 0 for p in blocks[0].tags)

    def test_a_wide_tag_overlapping_a_narrow_one_does_not_inflate_the_block(self) -> None:
        blocks = _plan([_tag("word_hi", 0), _tag("combined", 0, data_type="uint32")])
        assert len(blocks) == 1
        assert (blocks[0].address, blocks[0].quantity) == (0, 2)

    def test_partially_overlapping_wide_tags(self) -> None:
        blocks = _plan(
            [_tag("a", 0, data_type="uint32"), _tag("b", 1, data_type="uint32")],
        )
        assert len(blocks) == 1
        assert (blocks[0].address, blocks[0].quantity) == (0, 3)
        assert [(p.tag["id"], p.offset) for p in blocks[0].tags] == [("a", 0), ("b", 1)]


class TestCoverage:
    def test_every_tag_lands_in_exactly_one_block(self) -> None:
        tags = [_tag(f"t{i}", i * 3) for i in range(40)]
        blocks = _plan(tags, max_block_gap=1, max_block_size=17)
        planned_ids = [p.tag["id"] for b in blocks for p in b.tags]
        assert sorted(planned_ids) == sorted(t["id"] for t in tags)
        assert len(planned_ids) == len(set(planned_ids)), "no tag read twice"

    def test_every_tag_lies_fully_inside_its_block(self) -> None:
        tags = [
            _tag("a", 0, data_type="float32"),
            _tag("b", 2),
            _tag("c", 3, data_type="uint64"),
            _tag("d", 7),
        ]
        for block in _plan(tags):
            for planned in block.tags:
                assert planned.offset >= 0
                assert planned.offset + planned.span <= block.quantity, (
                    f"{planned.tag['id']} runs past the end of its block"
                )

    def test_requests_are_encodable(self) -> None:
        """Every emitted block must survive codec validation — the planner
        must not produce a request the codec will reject."""
        tags = [_tag(f"t{i}", i) for i in range(300)]
        for block in _plan(tags):
            codec.encode_read_request(block.function_code, block.address, block.quantity)

    def test_no_tags_plans_no_requests(self) -> None:
        assert _plan([]) == []
