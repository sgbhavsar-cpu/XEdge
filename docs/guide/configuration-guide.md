# Configuration Guide

XEDGE-493 (Sprint H1). How xEdge's configuration model actually works —
the file format, hot-reload, the Web UI's relationship to the file, and
secrets. For "what do I put in a driver's config for protocol X," see
[Protocol Quick Starts](protocol-quick-starts.md); for a first-boot
walkthrough, see [Onboarding Walkthrough](onboarding-walkthrough.md).

## The config file

One YAML file (`xedge.yaml`, or whatever path `--config` names), validated
against `config/schema/xedge-core.schema.json` — a real JSON Schema, not a
loose convention: every field the Web UI or the file format accepts is
declared there, with the type/range/enum the running process actually
enforces. `config/examples/*.yaml` are real, schema-valid starting points,
not illustrative snippets — each one is validated against its schema as
part of this project's own test suite.

Top-level sections: `data_dir`, `logging`, `watchdog`, `sntp`,
`system_tags`, `drivers`, `northbound`, `opcua_server`, `store`,
`config_management`, `api`, `tls`, `tracing`, `metrics`, `rate_limit`,
`alarms`, `assets`, `smtp`, `fleet`, `mqtt_broker`, `snmp_agent`,
`snmp_notify`. `drivers` is the one list nearly every deployment edits
first — see below.

## Anatomy of a driver

Every driver instance, regardless of protocol, has the same four-level
shape:

```yaml
drivers:
  - id: plc_01                # instance_id -- prefixes every tag from this driver
    type: modbus_tcp           # a registered driver type, see Protocol Quick Starts
    enabled: true
    config: { ... }            # protocol-specific: host/port, community, slot, etc.
    tag_groups:
      - id: analog              # a polling unit -- one scan_rate_ms for every tag in it
        scan_rate_ms: 100       # 50ms floor, shared across every polled protocol driver
        tags:
          - id: temperature_01  # this tag's id -- referenced elsewhere as plc_01/temperature_01
            address: 0          # protocol-specific: address/oid/node_id/object_identifier/tag_name
```

A tag's fully-qualified identifier is always `{instance_id}/{tag_id}` —
this is what you reference in `alarms.rules[].tag_id`, an asset
parameter's `tag_ref`, or a REST API tag lookup
(`GET /api/v1/drivers/{instance_id}/tags`). `scan_rate_ms` has a 50ms floor
enforced by every protocol driver's own schema (FR-SB-005/FR-SA-009) — a
group polls no faster than that regardless of what a config file requests.
Event-driven drivers (SNMP trap receiver) have no `scan_rate_ms` at all;
their tags only update when a matching notification arrives.

## Web UI vs. raw YAML

The Web UI's per-driver and per-core-section forms
(`/ui/config/drivers/{id}`, `/ui/config/core/{section}`) are generated
directly from the same JSON Schema that validates the file — every field
you see, its label, its help text, and what counts as a valid value all
come from `config/schema/`, not a separately maintained UI definition.
Saving through the form and hand-editing the YAML are the same operation
from the config engine's point of view.

**Gap, stated plainly:** the form generator (`xedge.api.schema_forms`)
does not have a widget for JSON Schema array types or dynamic-key objects
— a handful of fields are therefore edit-only through
`/ui/config/advanced` (the raw YAML editor, still schema-validated on
save):

| Section | Field(s) | Why |
|---|---|---|
| `alarms` | `rules` | array of objects |
| `mqtt_broker` | `publish_acl`, `subscribe_acl` | dynamic-key objects |
| `smtp` | `alarm_notifications`, `scheduled_reports` | nested object / array of objects |
| `snmp_notify` | `destinations` | array of objects |

Every core section's own page links to the Advanced editor and names the
relevant example file when this applies. `mqtt_broker.users` (also a
keyed collection) is the one exception with a dedicated page instead
(`/ui/config/mqtt-broker/users`) — it holds plaintext credentials, so the
generic form's plain-text fallback would have exposed them directly in
the page rather than merely being unusable.

`assets` is not on this list: an asset's `parameters` are a real
child-collection with their own dedicated add/edit/delete UI
(`/ui/config/assets/{asset_id}/parameters/...`), the same treatment a
driver's own `tag_groups`/`tags` get — arrays of a *complex enough* shape
get a dedicated page; everything above stays on raw YAML because nothing
has needed a dedicated page badly enough yet to justify one (ADR-010,
XEDGE-455/465 notes in the delivery plan).

## Hot-reload

`config_management.hot_reload_enabled` (default `true`) makes every save —
through the Web UI or an external edit of the file on disk — take effect
without restarting the process, polling the file every
`config_management.poll_interval_seconds` (default 2s). `max_versions`
bounds how many prior versions are kept for rollback. A driver whose
`config`/`tag_groups` changed is stopped and restarted with the new
config; other sections' consumers (the alarm engine, the pipeline) update
in place. Two exceptions, stated plainly rather than assumed: services
that bind a listening socket at startup — the embedded MQTT broker
(`mqtt_broker`) and the SNMP agent (`snmp_agent`) — are only ever started
once, during process startup; flipping `enabled: true` on either via
hot-reload does not retroactively start it. A process restart is required
for those two specifically.

## Secrets

Any string field can reference a secret instead of holding a literal value:

```yaml
password: "${SECRET:snmp_notify_nms_community}"
```

Resolved in this order: an environment variable named `SNMP_NOTIFY_NMS_
COMMUNITY` (the reference name, upper-cased) first, then a file
`<secrets_dir>/snmp_notify_nms_community` if a secrets directory is
configured. Neither source found raises at config-load time rather than
starting with a blank credential. The `${SECRET:...}` placeholder itself
(never the resolved plaintext) is what gets written back to disk on every
save — a config file, and the version history hot-reload keeps, never
contains a real secret value. Every credential-shaped field across every
protocol's example config (SNMP community strings, MQTT/SMTP passwords,
fleet join tokens) uses this mechanism; treat any field marked
`x-secret: true` in a schema the same way even if an example happens to
show a literal value for brevity.

## Validation

`/ui/config/drivers/{id}/validate` (and the equivalent check that runs
automatically on every save) checks two things together: JSON Schema
conformance for that driver's config and tags, and — for `assets` —
referential integrity (`assets[].parameters[].tag_ref` must resolve to a
real `instance_id/tag_id` somewhere in `drivers`, checked on every config
apply path, not only the Web UI's). A schema-invalid save is rejected
with the file left untouched; nothing partially applies.
