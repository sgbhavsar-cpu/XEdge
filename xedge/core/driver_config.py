"""Resolves and validates per-driver-type configuration (FR-DF-004).

Each driver `type` (e.g. `modbus_tcp`) has its own JSON Schema, bundled and
resolved the same way as the core schema (see xedge.core.main._default_schema_path):
packaged copy first (real install), repo-relative fallback (editable/dev).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from xedge.core.config import ConfigValidationError, ConfigValidator
from xedge.drivers.base import DriverConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def driver_type_schema_path(driver_type: str) -> Path:
    packaged = resources.files("xedge") / "schema" / "drivers" / f"{driver_type}.schema.json"
    if packaged.is_file():
        return Path(str(packaged))
    return _REPO_ROOT / "config" / "schema" / "drivers" / f"{driver_type}.schema.json"


def build_driver_config(entry: dict[str, Any]) -> DriverConfig:
    """Validate one `drivers[]` entry from xedge.yaml against its driver
    type's schema and return a ready-to-use DriverConfig.

    Raises ConfigValidationError if the driver type has no known schema, or
    if `config`/`tag_groups` fail validation against it.
    """
    driver_type = entry["type"]
    schema_path = driver_type_schema_path(driver_type)
    if not schema_path.is_file():
        raise ConfigValidationError(
            f"No configuration schema found for driver type {driver_type!r} "
            f"(expected at {schema_path}); is the driver type registered?"
        )

    driver_section = {
        "config": entry.get("config", {}),
        "tag_groups": entry.get("tag_groups", []),
    }
    ConfigValidator(_load_schema(schema_path)).validate(driver_section)

    return DriverConfig(
        instance_id=entry["id"],
        driver_type=driver_type,
        config=driver_section["config"],
        tag_groups=driver_section["tag_groups"],
    )


def _load_schema(schema_path: Path) -> dict[str, Any]:
    import json

    with schema_path.open("r", encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
    return result


# Driver types that share a physical port across instances (currently just
# RTU serial — TCP-based transports each own their own socket, so two
# instances can never collide the way two RTU-serial instances on the same
# /dev/ttyUSBx would).
_SHARED_PORT_DRIVER_TYPES = frozenset({"modbus_rtu_serial"})


def find_duplicate_serial_slave_ids(
    drivers: list[dict[str, Any]],
) -> dict[tuple[str, int], list[str]]:
    """Group enabled `modbus_rtu_serial` instance ids by (port, unit_id),
    returning only the groups with more than one instance (XEDGE-433).

    Different unit_ids sharing one port is exactly the multi-drop topology
    `SerialBusManager` (ADR-011 Part 1) exists to support — this only flags
    the case that's still wrong even with the bus manager in place: two
    slaves claiming the *same* address on the *same* wire, which no amount
    of correct serialization can make into two distinct devices.

    An entry with a missing `port`/`unit_id` is skipped here rather than
    raising — that's a shape error the per-instance driver-type schema
    (`build_driver_config`) already catches on its own, and this function
    only needs to reason about entries specific enough to compare.
    """
    by_slave: dict[tuple[str, int], list[str]] = {}
    for entry in drivers:
        if entry.get("type") not in _SHARED_PORT_DRIVER_TYPES:
            continue
        if not entry.get("enabled", True):
            continue
        config = entry.get("config", {})
        port = config.get("port")
        unit_id = config.get("unit_id")
        if port is None or unit_id is None:
            continue
        by_slave.setdefault((port, unit_id), []).append(entry["id"])
    return {key: ids for key, ids in by_slave.items() if len(ids) > 1}


def conflicting_serial_instance_ids(drivers: list[dict[str, Any]]) -> frozenset[str]:
    """Every instance id involved in *any* slave-ID collision — the set a
    caller should refuse to start, alongside whichever ones it starts
    normally. Both sides of a collision are included: given two instances
    both claiming (port, unit_id), neither is more "correct" than the
    other, so there is no safe way to prefer one over the other."""
    duplicates = find_duplicate_serial_slave_ids(drivers)
    return frozenset(instance_id for ids in duplicates.values() for instance_id in ids)
