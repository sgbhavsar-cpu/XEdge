"""Pipeline engine v1: TagUpdate -> UnifiedTag (system-architecture.md §3.3).

Phase 1 scope only (XEDGE-014): reshape a driver's raw TagUpdate into the
UnifiedTag representation northbound/store code will consume. Scaling,
deadband filtering, virtual tags, and alarm detection are later-sprint
stages (XEDGE-033..XEDGE-036, Sprint 4) that will extend this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xedge.drivers.base import Quality, TagUpdate, TagValue

_DATA_TYPE_NAMES: dict[type, str] = {
    bool: "BOOL",
    int: "INT64",
    float: "FLOAT64",
    str: "STRING",
    bytes: "BYTES",
}


@dataclass(frozen=True, slots=True)
class UnifiedTag:
    """Protocol-agnostic tag representation (FR-DP-001).

    Mirrors the JSON shape documented in system-architecture.md §4.2.
    """

    tag_id: str
    timestamp: datetime
    value: TagValue
    data_type: str
    quality: Quality
    source_driver: str
    source_address: str
    engineering_unit: str | None = None
    is_alarm: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize(update: TagUpdate) -> UnifiedTag:
    """Reshape a TagUpdate into a UnifiedTag.

    Phase 1 pass-through: no engineering-unit scaling (FR-DP-003), deadband
    filtering (FR-DP-006), or alarm detection (FR-DP-007) yet — those land
    in Sprint 4 once tag-group configuration carries that metadata.
    """
    return UnifiedTag(
        tag_id=update.tag_id,
        timestamp=update.timestamp,
        value=update.value,
        data_type=_DATA_TYPE_NAMES.get(type(update.value), "UNKNOWN"),
        quality=update.quality,
        source_driver=update.source_driver,
        source_address=update.source_address,
        metadata=dict(update.metadata),
    )


__all__ = ["UnifiedTag", "normalize", "Quality"]
