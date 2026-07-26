from __future__ import annotations

import pytest

from xedge.core.config import ConfigValidationError
from xedge.core.driver_config import build_driver_config


def _valid_entry() -> dict:
    return {
        "id": "modbus_tcp_01",
        "type": "modbus_tcp",
        "config": {"host": "192.168.1.100", "port": 502, "unit_id": 1},
        "tag_groups": [
            {
                "id": "analog_inputs",
                "scan_rate_ms": 1000,
                "tags": [
                    {
                        "id": "temperature_01",
                        "function_code": "read_holding_registers",
                        "address": 0,
                    }
                ],
            }
        ],
    }


def test_build_driver_config_valid_modbus_tcp() -> None:
    config = build_driver_config(_valid_entry())
    assert config.instance_id == "modbus_tcp_01"
    assert config.driver_type == "modbus_tcp"
    assert config.config == {"host": "192.168.1.100", "port": 502, "unit_id": 1}
    assert config.tag_groups[0]["id"] == "analog_inputs"


def test_build_driver_config_unknown_type_raises() -> None:
    entry = _valid_entry()
    entry["type"] = "nonexistent_protocol"
    with pytest.raises(ConfigValidationError, match="No configuration schema found"):
        build_driver_config(entry)


def test_build_driver_config_missing_host_raises() -> None:
    entry = _valid_entry()
    del entry["config"]["host"]
    with pytest.raises(ConfigValidationError):
        build_driver_config(entry)


def test_build_driver_config_bad_scan_rate_raises() -> None:
    entry = _valid_entry()
    # XEDGE-413 lowered the floor from 50ms to 1ms (open item Q-2), so 0 is
    # now the boundary rather than 10.
    entry["tag_groups"][0]["scan_rate_ms"] = 0
    with pytest.raises(ConfigValidationError):
        build_driver_config(entry)


def test_build_driver_config_accepts_sub_50ms_scan_rate() -> None:
    """XEDGE-413: the 50ms floor is gone. Whether 1ms is *achievable* depends
    on transport and block count — the serial driver warns when it is not
    (xedge.drivers.modbus.serial) — but the schema no longer forbids it."""
    entry = _valid_entry()
    entry["tag_groups"][0]["scan_rate_ms"] = 1
    assert build_driver_config(entry).tag_groups[0]["scan_rate_ms"] == 1


def test_build_driver_config_bad_function_code_raises() -> None:
    entry = _valid_entry()
    entry["tag_groups"][0]["tags"][0]["function_code"] = "write_coil"
    with pytest.raises(ConfigValidationError):
        build_driver_config(entry)


class TestFindDuplicateSerialSlaveIds:
    """XEDGE-433. Two slaves on the same physical port claiming the same
    unit_id is a real bus conflict SerialBusManager cannot fix by
    serializing traffic correctly — they're still the same address."""

    @staticmethod
    def _serial_entry(instance_id: str, port: str, unit_id: int, enabled: bool = True) -> dict:
        return {
            "id": instance_id,
            "type": "modbus_rtu_serial",
            "enabled": enabled,
            "config": {"port": port, "unit_id": unit_id},
            "tag_groups": [],
        }

    def test_no_conflict_with_different_unit_ids_on_one_port(self) -> None:
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 1),
            self._serial_entry("b", "/dev/ttyUSB0", 2),
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {}

    def test_no_conflict_with_the_same_unit_id_on_different_ports(self) -> None:
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 1),
            self._serial_entry("b", "/dev/ttyUSB1", 1),
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {}

    def test_conflict_detected_for_same_port_and_unit_id(self) -> None:
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 5),
            self._serial_entry("b", "/dev/ttyUSB0", 5),
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {("/dev/ttyUSB0", 5): ["a", "b"]}

    def test_three_way_conflict_lists_all_three(self) -> None:
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 5),
            self._serial_entry("b", "/dev/ttyUSB0", 5),
            self._serial_entry("c", "/dev/ttyUSB0", 5),
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {("/dev/ttyUSB0", 5): ["a", "b", "c"]}

    def test_a_disabled_conflicting_instance_does_not_count(self) -> None:
        """An instance that will never actually open the port cannot
        collide with anything."""
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 5),
            self._serial_entry("b", "/dev/ttyUSB0", 5, enabled=False),
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {}

    def test_other_driver_types_are_ignored(self) -> None:
        """A modbus_tcp and a modbus_rtu_serial entry sharing an
        (incidentally) identical-looking config field can never actually
        collide on a physical wire."""
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 5),
            {
                "id": "b",
                "type": "modbus_tcp",
                "config": {"host": "/dev/ttyUSB0", "unit_id": 5},
                "tag_groups": [],
            },
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {}

    def test_malformed_entry_is_skipped_not_raised(self) -> None:
        from xedge.core.driver_config import find_duplicate_serial_slave_ids

        drivers = [
            self._serial_entry("a", "/dev/ttyUSB0", 5),
            {"id": "b", "type": "modbus_rtu_serial", "config": {}, "tag_groups": []},
        ]
        assert find_duplicate_serial_slave_ids(drivers) == {}


class TestConflictingSerialInstanceIds:
    def test_flattens_every_side_of_every_collision(self) -> None:
        from xedge.core.driver_config import conflicting_serial_instance_ids

        drivers = [
            TestFindDuplicateSerialSlaveIds._serial_entry("a", "/dev/ttyUSB0", 1),
            TestFindDuplicateSerialSlaveIds._serial_entry("b", "/dev/ttyUSB0", 1),
            TestFindDuplicateSerialSlaveIds._serial_entry("c", "/dev/ttyUSB1", 2),
            TestFindDuplicateSerialSlaveIds._serial_entry("d", "/dev/ttyUSB1", 2),
        ]
        assert conflicting_serial_instance_ids(drivers) == frozenset({"a", "b", "c", "d"})

    def test_empty_when_nothing_conflicts(self) -> None:
        from xedge.core.driver_config import conflicting_serial_instance_ids

        drivers = [TestFindDuplicateSerialSlaveIds._serial_entry("a", "/dev/ttyUSB0", 1)]
        assert conflicting_serial_instance_ids(drivers) == frozenset()
