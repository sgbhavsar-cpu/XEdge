"""Read-request batching planner (Sprint C1, XEDGE-411).

XEDGE-CRD-001 §4.3 asks for "read batching / block read" with a configurable
maximum block size. Before this, every tag was read individually: a 100-tag
group meant 100 request/response round trips per scan cycle, which on a
serial bus at 9600 baud is arithmetically incapable of meeting any scan rate
the operator is likely to configure.

This module is a pure function — tags in, read blocks out — deliberately
separate from the polling loop so the coalescing rules can be tested against
address layouts directly, without a transport.

Two properties are load-bearing and easy to get wrong:

* **Gaps default to zero.** Reading registers the operator did not ask for is
  a real optimisation (one round trip instead of two) but it is not free:
  many devices answer ILLEGAL_DATA_ADDRESS for an unmapped register, which
  would fail the *whole block* rather than one tag. So blocks are strictly
  contiguous unless the operator opts into a gap tolerance for a device they
  know tolerates it.
* **Overlapping tags are legal.** Two tags may share an address, and a 32-bit
  tag may overlap a 16-bit one. Each tag carries its own offset into the
  block, so overlap costs nothing and needs no de-duplication.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from xedge.drivers.modbus import codec, datatypes

DEFAULT_MAX_BLOCK_GAP = 0


@dataclass(frozen=True, slots=True)
class PlannedTag:
    """One tag's position inside a block's response."""

    tag: dict[str, Any]
    offset: int
    """Index of this tag's first value within the block's decoded
    register/bit list — i.e. `tag.address - block.address`."""

    span: int
    """How many registers this tag occupies (always 1 for bit functions)."""


@dataclass(frozen=True, slots=True)
class ReadBlock:
    """A single read request covering one or more tags."""

    function_code: codec.FunctionCode
    address: int
    quantity: int
    tags: tuple[PlannedTag, ...]

    @property
    def is_batched(self) -> bool:
        return len(self.tags) > 1


def tag_span(tag: dict[str, Any], function_code: codec.FunctionCode) -> int:
    """Registers occupied by `tag`. Bit functions address individual coils or
    discrete inputs, so a data type is meaningless there and the span is
    always 1 — a `data_type` on such a tag is ignored rather than an error,
    since the schema already constrains where it is meaningful."""
    if codec.is_bit_function(function_code):
        return 1
    return datatypes.register_count(tag.get("data_type", datatypes.DEFAULT_DATA_TYPE))


def plan_read_blocks(
    tags: Sequence[dict[str, Any]],
    function_code_by_name: dict[str, codec.FunctionCode],
    max_block_size: int | None = None,
    max_block_gap: int = DEFAULT_MAX_BLOCK_GAP,
) -> list[ReadBlock]:
    """Group `tags` into the fewest read requests that cover them all.

    Tags are partitioned by function code (a single request cannot span two),
    sorted by address, then greedily coalesced while both limits hold:
    the gap to the next tag is within `max_block_gap`, and the resulting
    quantity is within `max_block_size` and the protocol ceiling for that
    function code.

    `max_block_size` of None means "use the protocol ceiling"; a value above
    the ceiling is clamped down to it rather than rejected, since a device
    cannot answer a larger request whatever the config says.

    Blocks come back in (function code, address) order. Within a block, tags
    are in address order — note this need not match config order, so callers
    must not rely on the emission sequence matching the configured tag list.
    """
    by_function: dict[codec.FunctionCode, list[dict[str, Any]]] = {}
    for tag in tags:
        function_code = function_code_by_name[tag["function_code"]]
        by_function.setdefault(function_code, []).append(tag)

    blocks: list[ReadBlock] = []
    for function_code in sorted(by_function):
        limit = codec.max_read_quantity(function_code)
        if max_block_size is not None:
            limit = min(limit, max(1, max_block_size))
        blocks.extend(
            _plan_one_function_code(by_function[function_code], function_code, limit, max_block_gap)
        )
    return blocks


def _plan_one_function_code(
    tags: list[dict[str, Any]],
    function_code: codec.FunctionCode,
    limit: int,
    max_block_gap: int,
) -> list[ReadBlock]:
    spans = sorted(
        ((tag["address"], tag_span(tag, function_code), tag) for tag in tags),
        key=lambda entry: (entry[0], entry[1]),
    )

    blocks: list[ReadBlock] = []
    start = spans[0][0]
    end = start  # exclusive
    current: list[tuple[int, int, dict[str, Any]]] = []

    for address, span, tag in spans:
        candidate_end = max(end, address + span)
        # Negative for overlapping tags, which is fine — an overlap always
        # satisfies the gap check, and each tag keeps its own offset.
        gap = address - end
        # A tag always forms at least its own block: `current` being empty
        # short-circuits both conditions below, so a lone tag wider than
        # `limit` (a float64 under `max_block_size: 1`) is still read. A
        # 4-register value cannot be fetched in fewer than 4 registers, so
        # honouring the limit there would mean never reading the tag at all.
        fits = candidate_end - start <= limit and (not current or gap <= max_block_gap)
        if current and not fits:
            blocks.append(_build_block(function_code, start, end, current))
            start, end, current = address, address + span, [(address, span, tag)]
            continue
        end = candidate_end
        if not current:
            start = address
        current.append((address, span, tag))

    if current:
        blocks.append(_build_block(function_code, start, end, current))
    return blocks


def _build_block(
    function_code: codec.FunctionCode,
    start: int,
    end: int,
    entries: list[tuple[int, int, dict[str, Any]]],
) -> ReadBlock:
    return ReadBlock(
        function_code=function_code,
        address=start,
        quantity=end - start,
        tags=tuple(
            PlannedTag(tag=tag, offset=address - start, span=span) for address, span, tag in entries
        ),
    )
