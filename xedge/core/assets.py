"""Asset Management (Sprint C6, XEDGE-460/461/462/463/464; ADR-010): a
metadata and grouping layer over the existing driver-first config model,
not a new primary configuration entity — see ADR-010's Decision section
for the full reasoning behind that choice. An asset's parameters
*reference* existing tags (`instance_id/tag_id`); nothing in the
pipeline, store, northbound, or alarm path changes, and a deployment
with no `assets:` section behaves exactly as it did before this module
existed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from xedge.core.connectivity import ConnectivityState
from xedge.core.pipeline import UnifiedTag
from xedge.store.ring_buffer import OnEvictWithKeyCallback


@dataclass(frozen=True, slots=True)
class AssetParameter:
    tag_ref: str
    """`instance_id/tag_id` of an existing tag — a reference, never a new
    tag definition (ADR-010 §1)."""
    alias: str | None = None
    unit: str | None = None
    store: bool = True


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    name: str
    enabled: bool = True
    serial_number: str | None = None
    asset_type: str | None = None
    make: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    description: str | None = None
    gateway_id: str | None = None
    """Optional, soft reference to a Fleet Manager DeviceRecord (ADR-010
    §4) — never validated locally: the device registry that reference
    points at is a separate system this device may have no connection to
    at all (fleet.enabled: false is fully supported), so there is nothing
    to check it against here."""
    parameters: tuple[AssetParameter, ...] = ()


def parse_assets(assets_config: list[dict[str, Any]]) -> list[Asset]:
    """Build typed Assets from the `assets` config section — a flat list,
    the same shape convention `xedge.core.alarms.build_alarm_rules` uses:
    cross-cutting, not scoped to any one driver instance."""
    return [
        Asset(
            id=entry["id"],
            name=entry["name"],
            enabled=entry.get("enabled", True),
            serial_number=entry.get("serial_number"),
            asset_type=entry.get("asset_type"),
            make=entry.get("make"),
            model=entry.get("model"),
            firmware_version=entry.get("firmware_version"),
            description=entry.get("description"),
            gateway_id=entry.get("gateway_id"),
            parameters=tuple(
                AssetParameter(
                    tag_ref=parameter["tag_ref"],
                    alias=parameter.get("alias"),
                    unit=parameter.get("unit"),
                    store=parameter.get("store", True),
                )
                for parameter in entry.get("parameters", [])
            ),
        )
        for entry in assets_config
    ]


def all_tag_refs(drivers_config: list[dict[str, Any]]) -> set[str]:
    """Every resolvable `instance_id/tag_id` across every configured
    driver instance's tag_groups — the universe a `tag_ref` must be a
    member of. Deliberately independent of whether the driver instance
    or tag_group is `enabled`: a *disabled* driver still has real tags an
    asset can legitimately reference ahead of turning it on, since
    referential integrity is about "does this identifier exist," not "is
    it currently live" (that question belongs to the derived connection
    state below, not to validation)."""
    refs: set[str] = set()
    for driver in drivers_config:
        instance_id = driver.get("id")
        if not instance_id:
            continue
        for group in driver.get("tag_groups", []):
            for tag in group.get("tags", []):
                tag_id = tag.get("id")
                if tag_id:
                    refs.add(f"{instance_id}/{tag_id}")
    return refs


def validate_asset_references(config: Mapping[str, Any]) -> list[str]:
    """XEDGE-461/ADR-010 consequences: every asset parameter's `tag_ref`
    must resolve to a real `instance_id/tag_id` somewhere in `drivers`.
    Returns human-readable error strings (empty means valid) instead of
    raising directly, so `ConfigValidator` can fold them into the same
    error-reporting shape a JSON Schema violation produces — this runs on
    every apply path that already calls `ConfigValidator.validate()`
    (`ConfigEngine.load/reload/rollback`, the fleet agent's pushed-config
    path, the Web UI's schema-driven and raw-YAML save paths), not only
    one of them, per ADR-010's "on every apply" requirement."""
    assets_config = config.get("assets")
    if not assets_config:
        return []
    valid_refs = all_tag_refs(config.get("drivers", []))
    errors: list[str] = []
    for asset_index, entry in enumerate(assets_config):
        asset_id = entry.get("id", f"<index {asset_index}>")
        for param_index, parameter in enumerate(entry.get("parameters", [])):
            tag_ref = parameter.get("tag_ref")
            if tag_ref and tag_ref not in valid_refs:
                errors.append(
                    f"assets/{asset_index}/parameters/{param_index}: "
                    f"asset {asset_id!r} references tag_ref {tag_ref!r}, which "
                    "does not match any configured driver's tag"
                )
    return errors


def compute_suppressed_tag_ids(assets: list[Asset]) -> set[str]:
    """ADR-010 §3: a `tag_ref` referenced by at least one asset parameter
    with `store: false` is suppressed from cold-store spill — *unless*
    some other parameter (the same or a different asset) also references
    it with `store: true`, which wins.

    Not specified by ADR-010 itself (no example there has conflicting
    `store` flags on the same tag_ref) — this project's own
    interpretation, recorded here rather than silently picked: losing
    data because of a conflicting *second* reference is a worse failure
    mode than retaining slightly more than one reference strictly asked
    for."""
    suppress_candidates: set[str] = set()
    explicitly_wanted: set[str] = set()
    for asset in assets:
        for parameter in asset.parameters:
            if parameter.store:
                explicitly_wanted.add(parameter.tag_ref)
            else:
                suppress_candidates.add(parameter.tag_ref)
    return suppress_candidates - explicitly_wanted


class AssetStorageFilter:
    """Live view of which tag_ids the current `assets` config suppresses
    from cold-store spill (ADR-010 §3's "enforced at the spill boundary").
    `refresh()` is meant to be called from a `ConfigStore.subscribe()`
    listener so a hot-reloaded `assets` section takes effect without
    restarting `RingBufferManager`, which owns this filter's wrapped
    callback for the whole process lifetime."""

    def __init__(self, assets_config: list[dict[str, Any]]) -> None:
        self._suppressed: set[str] = compute_suppressed_tag_ids(parse_assets(assets_config))

    def refresh(self, assets_config: list[dict[str, Any]]) -> None:
        self._suppressed = compute_suppressed_tag_ids(parse_assets(assets_config))

    def wrap(self, append: OnEvictWithKeyCallback) -> OnEvictWithKeyCallback:
        def filtered_append(stream_key: str, tag: UnifiedTag) -> None:
            if tag.tag_id in self._suppressed:
                return
            append(stream_key, tag)

        return filtered_append


def compute_asset_connection_state(
    instance_states: list[ConnectivityState],
) -> ConnectivityState:
    """ADR-010 §4: derived, never stored. Connected when every backing
    driver instance is Connected, Not Connected when none are, Degraded
    otherwise — ADR-011's shared state machine, this module's own
    adapter (the third consumer `xedge.core.connectivity`'s own docstring
    already anticipated: Modbus device health, gateway state, and this).

    **This project's own interpretation, not a customer confirmation**
    (the same caveat `xedge.fleet.registry.GatewayConnectionState`'s
    docstring already applies to gateway state): an asset whose backing
    instances are all UNKNOWN — an empty parameter list, or every backing
    driver type not yet implementing `get_connectivity_state()` (only the
    Modbus family does today; see
    `DriverSupervisor._with_live_metrics`) — is UNKNOWN, not
    NOT_CONNECTED. "No information yet" and "confirmed down" are
    different claims; collapsing them would make every asset backed
    purely by non-Modbus drivers look like a failure at a glance, for a
    reason that has nothing to do with that asset's actual health."""
    if not instance_states or all(s is ConnectivityState.UNKNOWN for s in instance_states):
        return ConnectivityState.UNKNOWN
    if all(s is ConnectivityState.CONNECTED for s in instance_states):
        return ConnectivityState.CONNECTED
    if all(s is ConnectivityState.NOT_CONNECTED for s in instance_states):
        return ConnectivityState.NOT_CONNECTED
    return ConnectivityState.DEGRADED


def asset_backing_instance_ids(asset: Asset) -> set[str]:
    """The distinct driver instance_ids backing at least one of `asset`'s
    parameters — derived from `parameter.tag_ref`, never stored."""
    return {parameter.tag_ref.split("/", 1)[0] for parameter in asset.parameters}


def derive_asset_connection_state(
    asset: Asset, driver_connectivity: Mapping[str, ConnectivityState]
) -> ConnectivityState:
    """`driver_connectivity` is typically `{instance_id: status.
    connectivity_state for instance_id, status in supervisor.all_status().
    items()}` — a plain mapping rather than `DriverSupervisor` itself, so
    this stays testable with a literal dict, no supervisor/driver
    machinery required. An instance_id backing this asset but missing
    from `driver_connectivity` entirely (a disabled driver, which
    `DriverSupervisor` never starts and so never reports *any* status
    for) is simply excluded from the combination below, not treated as
    any particular state."""
    instance_ids = asset_backing_instance_ids(asset)
    states = [driver_connectivity[i] for i in instance_ids if i in driver_connectivity]
    return compute_asset_connection_state(states)
