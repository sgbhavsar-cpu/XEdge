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
