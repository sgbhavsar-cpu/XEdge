"""RS-485 achievable scan-rate floor (Sprint C1, XEDGE-413 / open item Q-2).

The customer accepted that the 1 ms schema floor cannot be met on RS-485. That
acceptance is only meaningful if xEdge can say what *is* achievable, so the
bound is computed rather than guessed, and the operator is warned when their
configured rate is below it.
"""

from __future__ import annotations

from xedge.drivers.modbus import codec
from xedge.drivers.modbus.serial import minimum_scan_interval_seconds

_HOLDING = int(codec.FunctionCode.READ_HOLDING_REGISTERS)
_COILS = int(codec.FunctionCode.READ_COILS)


class TestAchievableFloor:
    def test_single_register_read_at_9600_baud(self) -> None:
        """3.5 idle + 8 request + 5 response overhead + 2 data = 18.5 chars,
        10 bits each at 8N1, over 9600 baud."""
        floor = minimum_scan_interval_seconds(9600, [(_HOLDING, 1)])
        assert floor == 18.5 * 10 / 9600
        assert 0.019 < floor < 0.02

    def test_the_1ms_schema_floor_is_unreachable_on_rs485(self) -> None:
        """The concrete fact the customer accepted. Even one register at the
        fastest standard baud rate cannot be polled at 1 ms."""
        for baud in (9600, 19200, 38400, 57600, 115200):
            assert minimum_scan_interval_seconds(baud, [(_HOLDING, 1)]) > 0.001

    def test_higher_baud_lowers_the_floor_proportionally(self) -> None:
        slow = minimum_scan_interval_seconds(9600, [(_HOLDING, 10)])
        fast = minimum_scan_interval_seconds(19200, [(_HOLDING, 10)])
        assert fast == slow / 2

    def test_more_registers_raise_the_floor(self) -> None:
        assert minimum_scan_interval_seconds(9600, [(_HOLDING, 50)]) > (
            minimum_scan_interval_seconds(9600, [(_HOLDING, 10)])
        )

    def test_batching_beats_separate_requests_for_the_same_registers(self) -> None:
        """Why XEDGE-411 matters most on serial: each request carries fixed
        framing overhead, so one 10-register read beats ten 1-register reads
        even though the payload is identical."""
        batched = minimum_scan_interval_seconds(9600, [(_HOLDING, 10)])
        unbatched = minimum_scan_interval_seconds(9600, [(_HOLDING, 1)] * 10)
        assert batched < unbatched / 3

    def test_parity_and_stop_bits_widen_each_character(self) -> None:
        eight_n_one = minimum_scan_interval_seconds(9600, [(_HOLDING, 1)])
        eight_e_two = minimum_scan_interval_seconds(
            9600, [(_HOLDING, 1)], parity="even", stop_bits=2
        )
        assert eight_e_two > eight_n_one
        assert eight_e_two == eight_n_one * 12 / 10

    def test_coils_pack_eight_per_byte(self) -> None:
        """16 coils are 2 data bytes, far cheaper than 16 registers' 32."""
        coils = minimum_scan_interval_seconds(9600, [(_COILS, 16)])
        registers = minimum_scan_interval_seconds(9600, [(_HOLDING, 16)])
        assert coils < registers

    def test_multiple_blocks_accumulate(self) -> None:
        one = minimum_scan_interval_seconds(9600, [(_HOLDING, 5)])
        two = minimum_scan_interval_seconds(9600, [(_HOLDING, 5), (_COILS, 5)])
        assert two > one
