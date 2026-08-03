# ADR-010: Asset Management Data Model

**Status:** ACCEPTED
**Date:** 2026-07-26
**Deciders:** Lead Architect, Project Owner
**Implements:** XEDGE-DR-001 decision D-11
**Delivers:** XEDGE-CRD-001 §4.11 (Basic Asset Management)

## Context

The customer requirement document asks for "Basic Asset Management": an
Asset with Name, Serial Number, Type, Make, Firmware Version, Description,
Alias and Units; enable/disable; assignment to a gateway; an Asset
Connection State; a parameter list with a per-parameter storage toggle; and
a centralized cross-protocol configuration interface.

xEdge today has no Asset concept at any layer. Its configuration model is
strictly three-level:

```
driver instance  (id, type, transport config)
  └── tag_group  (scan_rate_ms, deadband, retention)
        └── tag  (id, address, function_code, scaling, engineering_unit)
```

A tag belongs to a driver, not to a physical device. The `tag_id` that
flows through the entire system — pipeline, ring buffer, cold store,
Sparkplug B metric names, OPC UA node names, alarms, write-back routing —
is `f"{instance_id}/{tag['id']}"`. That identifier is load-bearing in at
least eight modules and is persisted in the SQLite cold store, so changing
its shape is a data migration, not a refactor.

The compliance report (XEDGE-CRD-001 §8.4) flagged this as "an architecture
decision, not a form to add," and the customer's own open question §9 Q5
asks whether Asset should become the primary configuration entity or an
additional metadata layer.

## Decision

**Asset is a metadata and grouping layer over the existing driver-first
model. It is not the primary configuration entity, and it does not own
driver instances or tags.**

### 1. Data model

A new top-level `assets` config section, parallel to `drivers`:

```yaml
assets:
  - id: pump-101
    name: "Feedwater Pump 101"
    enabled: true
    serial_number: "SN-88213"
    asset_type: "centrifugal_pump"
    make: "Grundfos"
    model: "NK 80-250"
    firmware_version: "4.2.1"
    description: "Primary feedwater pump, boiler house"
    gateway_id: "gw-eastwing"          # optional; see §4
    parameters:
      - tag_ref: "modbus-1/discharge_pressure"
        alias: "Discharge Pressure"
        unit: "bar"
        store: true
      - tag_ref: "modbus-1/motor_current"
        alias: "Motor Current"
        unit: "A"
        store: false
```

The key property: **`tag_ref` points at an existing `instance_id/tag_id`.**
Assets reference tags; tags do not know they belong to an asset. Nothing in
the pipeline, store, northbound, alarm or write-back path changes. No
existing config needs migrating — a deployment with no `assets:` section
behaves exactly as today.

### 2. Why not Asset-as-primary

Making Asset the primary entity (operator configures an Asset, which owns
driver instances and tag mappings underneath) would require:

- Reshaping the config schema and every driver-type sub-schema
- Rewriting the config-editor navigation tree, driver forms and tag forms
  (`xedge/api/config_ui.py`, ~854 lines, plus 11 templates)
- Changing `tag_id` composition, or introducing an asset→tag indirection in
  the pipeline hot path
- Migrating persisted cold-store rows and config history snapshots
- Reworking hot-reload's diff logic, which is driver-instance-scoped

That is a multi-sprint refactor of working, tested code, on a committed
schedule, to satisfy a requirement whose functional content — asset
metadata, grouping, per-parameter storage, connection state — is fully
satisfiable by a reference layer. The refactor buys conceptual purity, not
capability.

### 3. Per-parameter storage toggle

`parameters[].store` is the CRD's "Storage Requirement" per parameter.
Today, retention is a tag-group-level store-and-forward setting. The
asset-level toggle is resolved at config load into the existing
per-tag-group machinery: a tag referenced by an asset parameter with
`store: false` is excluded from cold-store spill for that stream.

**Known constraint:** because ring buffers are currently keyed by
`source_driver` (see `xedge/store/ring_buffer.py`'s module docstring — a
documented interim simplification), a per-parameter toggle cannot be
enforced at buffer granularity until stream keys become per-tag-group or
per-tag. For this delivery the toggle is enforced at the **spill boundary**
(the `on_evict` → cold-store callback), which achieves the requirement's
observable behaviour — the parameter's history is not persisted — without
restructuring the buffer keying. Finer granularity is a Delivery 2 item.

### 4. Asset ↔ gateway mapping and connection state

`gateway_id` is an optional reference to a Fleet Manager `DeviceRecord`.
It is deliberately a soft reference: a device configures its own assets
locally and works standalone with no fleet registration at all, which
preserves the ADR-007 property that the device is fully usable offline.

**Asset Connection State is derived, never stored.** It is computed from
the connectivity state of the driver instances backing the asset's
parameters, using the shared state machine from ADR-011 — an asset is
Connected when all backing instances are Connected, Degraded when some
are, Not Connected when none are. This is the third consumer of that one
state machine (device health, gateway state, asset state), which is
precisely why it is built once.

`enabled: false` on an asset is presentational and reporting-level: it
suppresses the asset from dashboards and asset-scoped reports. It does
**not** stop the underlying driver instances, because those instances may
back parameters on other assets. Stopping data collection remains a
driver-level operation (`POST /api/v1/drivers/{id}/disable`), which already
exists. This distinction must be made explicit in the UI, or operators will
reasonably expect disabling an asset to stop its polling.

### 5. Centralized cross-protocol configuration interface

The CRD's "centralized protocol configuration interface" is satisfied by an
asset-scoped view in the existing Web UI: an asset page listing its
parameters with their backing driver instance and protocol, with inline
links into the existing per-driver config forms. The existing schema-driven
form generator (`xedge/api/schema_forms.py`) already renders every driver
type from its JSON Schema, so no new per-protocol form work is needed.

## Consequences

**Positive**

- No migration of existing configuration, persisted data, or `tag_id` shape
- The pipeline hot path is untouched — zero runtime cost for deployments
  not using assets
- Delivers in one sprint (C6) rather than three
- Assets compose over *any* driver type, including EtherNet/IP and SNMP
  arriving later in the delivery, with no per-driver work

**Negative**

- A tag can be referenced by more than one asset, and nothing prevents a
  tag being referenced by none. Both are arguably correct, but they mean
  "the set of tags" and "the set of asset parameters" are not the same set,
  and reports must be clear about which they are counting.
- Deleting or renaming a tag can leave a dangling `tag_ref`. Config
  validation must check referential integrity on every apply — this is new
  validation work, not free.
- The advanced YAML editor exposes the driver-first model directly, so an
  operator who thinks asset-first will see the underlying structure there.
  The abstraction is honest rather than hidden, but it is visible.

**Risk / escape hatch**

If the customer's answer to their own §9 Q5 turns out to be "Asset must be
the primary entity" (open item Q-3 in XEDGE-DR-001 §4), the data model
above survives unchanged — what changes is the UI, which would present an
asset-first navigation and creation flow that writes both the asset entry
and its backing driver/tag entries in one transaction. That is a UI-layer
project of roughly one sprint, not a re-architecture. **This is the main
reason for choosing the reference model: it keeps both outcomes reachable.**

## Alternatives considered

**Asset as primary configuration entity.** Rejected on cost and schedule
grounds — see §2. Reachable later via the UI-layer path above.

**Asset as an annotation on `tag_groups`.** Rejected: a physical asset's
parameters routinely span multiple tag groups (different scan rates) and
sometimes multiple driver instances (a pump read over Modbus with a
temperature from a BACnet controller). Binding assets to tag groups would
make the common case unrepresentable.

**Asset inferred from tag naming convention.** Rejected: implicit, fragile,
and provides nowhere to put serial number, make, or firmware version.

## References

- XEDGE-CRD-001 §4.11, §5 item 8, §8.4, §9 Q5
- XEDGE-DR-001 D-11, open item Q-3
- ADR-011 (connectivity state machine — supplies Asset Connection State)
- ADR-007 (device works standalone, offline — preserved by the soft
  `gateway_id` reference)
