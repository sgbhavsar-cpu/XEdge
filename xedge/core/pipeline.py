"""Pipeline engine v2: TagUpdate -> UnifiedTag (system-architecture.md §3.3).

Stages, in order (matching the architecture doc's pipeline diagram):
  1. Normalize    — type mapping, XEDGE-014 (this module, always applied)
  2. Quality      — protocol-specific quality mapping happens in each driver
                     (e.g. Modbus exception -> Quality.BAD) before a TagUpdate
                     ever reaches here; this module passes Quality through.
  3. Timestamp    — source vs. ingestion timestamp (FR-DP-002, XEDGE-034)
  4. Scaling      — engineering-unit scale/offset (FR-DP-003, XEDGE-035)
  5. Deadband     — suppress redundant publishes (FR-DP-006, XEDGE-036),
                     stateful, applied by the caller via DeadbandFilter
Virtual tags and alarm detection (FR-DP-004/007) are later-sprint stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from xedge.drivers.base import Quality, TagUpdate, TagValue

_DATA_TYPE_NAMES: dict[type, str] = {
    bool: "BOOL",
    int: "INT64",
    float: "FLOAT64",
    str: "STRING",
    bytes: "BYTES",
}

DeadbandKind = Literal["absolute", "percentage"]


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


@dataclass(frozen=True, slots=True)
class ScalingConfig:
    """Linear engineering-unit conversion (FR-DP-003):
    `engineering_value = raw * scale + offset`."""

    scale: float = 1.0
    offset: float = 0.0


@dataclass(frozen=True, slots=True)
class DeadbandConfig:
    """FR-DP-006: suppress a publish unless the value moved by at least
    `value` (absolute) or `value` percent of the prior value (percentage)."""

    kind: DeadbandKind
    value: float


@dataclass(frozen=True, slots=True)
class TagPipelineConfig:
    """Per-tag pipeline parameters resolved from driver/tag-group config."""

    scaling: ScalingConfig | None = None
    deadband: DeadbandConfig | None = None
    engineering_unit: str | None = None


def normalize(update: TagUpdate, config: TagPipelineConfig | None = None) -> UnifiedTag:
    """Reshape a TagUpdate into a UnifiedTag: type mapping, timestamp
    resolution (FR-DP-002), and engineering-unit scaling (FR-DP-003).

    Deadband filtering (FR-DP-006) is NOT applied here — it needs
    cross-call state (the previous published value), which lives in
    DeadbandFilter; callers apply it to this function's output.
    """
    metadata = dict(update.metadata)

    if update.source_timestamp is not None:
        timestamp = update.source_timestamp
    else:
        timestamp = update.timestamp
        metadata["timestamp_estimated"] = True

    value: TagValue = update.value
    engineering_unit = config.engineering_unit if config else None
    if config is not None and config.scaling is not None and _is_scalable(update.value):
        value = float(update.value) * config.scaling.scale + config.scaling.offset

    return UnifiedTag(
        tag_id=update.tag_id,
        timestamp=timestamp,
        value=value,
        data_type=_DATA_TYPE_NAMES.get(type(value), "UNKNOWN"),
        quality=update.quality,
        source_driver=update.source_driver,
        source_address=update.source_address,
        engineering_unit=engineering_unit,
        metadata=metadata,
    )


def _is_scalable(value: TagValue) -> bool:
    # bool is an int subclass in Python but isn't a meaningful scaling target.
    return isinstance(value, int | float) and not isinstance(value, bool)


class DeadbandFilter:
    """Stateful per-tag last-published-value tracker (FR-DP-006).

    Bypasses suppression for a tag's first-ever value, any quality change,
    and any `is_alarm` transition (Sprint 31, XEDGE-224 — an alarm going
    active/clearing must never be silently swallowed just because the
    underlying value moved less than its deadband) — all three are always
    considered significant regardless of deadband configuration.
    """

    def __init__(self) -> None:
        self._last_published: dict[str, tuple[TagValue, Quality, bool]] = {}

    def should_publish(self, tag: UnifiedTag, deadband: DeadbandConfig | None) -> bool:
        prior = self._last_published.get(tag.tag_id)
        publish = self._should_publish(tag, prior, deadband)
        if publish:
            self._last_published[tag.tag_id] = (tag.value, tag.quality, tag.is_alarm)
        return publish

    @staticmethod
    def _should_publish(
        tag: UnifiedTag,
        prior: tuple[TagValue, Quality, bool] | None,
        deadband: DeadbandConfig | None,
    ) -> bool:
        if prior is None:
            return True
        prior_value, prior_quality, prior_is_alarm = prior
        if prior_quality != tag.quality or prior_is_alarm != tag.is_alarm:
            return True
        if deadband is None or not _is_scalable(tag.value) or not _is_scalable(prior_value):
            return tag.value != prior_value

        diff = abs(float(tag.value) - float(prior_value))
        if deadband.kind == "absolute":
            return diff >= deadband.value
        # percentage: relative to the magnitude of the prior value; a prior
        # value of exactly 0 falls back to treating any nonzero diff as
        # significant, since a percentage of zero is undefined.
        span = abs(float(prior_value))
        if span == 0:
            return diff != 0
        return (diff / span) * 100.0 >= deadband.value


def build_tag_pipeline_configs(drivers: list[dict[str, Any]]) -> dict[str, TagPipelineConfig]:
    """Build a `tag_id -> TagPipelineConfig` lookup from the raw `drivers`
    config section (as loaded from xedge.yaml).

    Driver-type-agnostic by construction: it reads `tag_groups[].deadband`
    and `tags[].scaling`/`engineering_unit` off whatever's in `drivers`,
    with no branch on `driver.get("type")` anywhere in this function. Every
    driver type that follows that config shape gets scaling/deadband for
    free — which, verified against the actual OPC UA and BACnet schemas
    (both declare `scaling`/`deadband`/`engineering_unit` identically to
    Modbus), is already all three shipped driver types, not "Modbus
    currently" as an earlier version of this docstring claimed. See
    `tests/unit/test_pipeline.py::TestBuildTagPipelineConfigsIsDriverTypeAgnostic`
    for the regression coverage backing that claim, since a docstring
    alone can drift out of sync with the code again exactly the way the
    previous one did.

    `tag_id` matches how drivers construct TagUpdate.tag_id: `f"{instance_id}/{tag['id']}"`.
    """
    configs: dict[str, TagPipelineConfig] = {}
    for driver in drivers:
        instance_id = driver.get("id")
        for group in driver.get("tag_groups", []):
            deadband_raw = group.get("deadband")
            deadband = (
                DeadbandConfig(deadband_raw["type"], deadband_raw["value"])
                if deadband_raw
                else None
            )
            for tag in group.get("tags", []):
                scaling_raw = tag.get("scaling")
                scaling = (
                    ScalingConfig(scaling_raw.get("scale", 1.0), scaling_raw.get("offset", 0.0))
                    if scaling_raw
                    else None
                )
                tag_id = f"{instance_id}/{tag['id']}"
                configs[tag_id] = TagPipelineConfig(
                    scaling=scaling, deadband=deadband, engineering_unit=tag.get("engineering_unit")
                )
    return configs


__all__ = [
    "DeadbandConfig",
    "DeadbandFilter",
    "ScalingConfig",
    "TagPipelineConfig",
    "UnifiedTag",
    "build_tag_pipeline_configs",
    "normalize",
    "Quality",
]
