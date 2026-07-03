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
