# Protocol Quick Starts

XEDGE-493 (Sprint H1). One section per protocol xEdge speaks today.
Each points at a real, schema-validated example under `config/examples/`
— copy it, edit the connection details, drop it into `drivers:` (or the
matching core section), see [Configuration Guide](configuration-guide.md)
for how the file/hot-reload/Web UI fit together.

Every protocol here is verified against a real server/agent test double in
this project's own test suite (`tests/fixtures/`), **except EtherNet/IP**
— see that section for why, stated plainly rather than glossed over.

## Modbus TCP

Example: [modbus-tcp-example.yaml](../../config/examples/modbus-tcp-example.yaml)
· Driver type: `modbus_tcp`

```yaml
config: { host: 192.168.1.50, port: 502, unit_id: 1 }
tag_groups:
  - id: analog
    scan_rate_ms: 100
    tags:
      - { id: temperature_01, function_code: read_holding_registers, address: 0 }
```

Contiguous addresses within one tag group batch into a single request
(up to the protocol's own 125-register / 2000-coil limit) — put related
tags in the same group rather than splitting them for no reason, and
throughput scales with tag count instead of degrading per tag (verified
under load in Sprint H1, XEDGE-492). `scan_rate_ms` floor: 50ms.

## Modbus RTU (RS-485 serial) / RTU-over-TCP

Same driver family, different transport: `modbus_rtu_serial` (a real
serial port, shared-bus multi-drop with write priority) or
`modbus_rtu_tcp` (RTU framing over a TCP socket — a serial-to-Ethernet
gateway). **RS-485 cannot meet the same 50ms floor other transports can**
— its actual floor is derived from baud rate and frame size (Decision
D-10), and a fast-but-unrealistic `scan_rate_ms` will not achieve the
requested rate on real hardware regardless of what the file accepts. This
is a stated, customer-accepted limitation, not an oversight — see the
[Handover Package](../planning/XEDGE-CRD-001-handover.md) for the exact
wording.

## OPC UA Client

Example: [opcua-example.yaml](../../config/examples/opcua-example.yaml)
· Driver type: `opcua_client`

```yaml
config: { endpoint_url: "opc.tcp://192.168.1.60:4840/server/" }
tag_groups:
  - id: group1
    scan_rate_ms: 100
    tags:
      - { id: pump_speed, node_id: "ns=2;s=Pump1.Speed" }
```

`node_id` is whatever your OPC UA server's own address space uses — get
it from the server's own node browser/exported information model, not
guessed from a naming convention.

## BACnet/IP Client

Example: [bacnet-example.yaml](../../config/examples/bacnet-example.yaml)
· Driver type: `bacnet_ip`

```yaml
config: { local_address: "0.0.0.0:47808", device_instance: 1001 }
tag_groups:
  - id: group1
    scan_rate_ms: 100
    tags:
      - { id: room_temp, device_address: "192.168.1.70:47808", object_identifier: "analog-value,1" }
```

`device_instance` must be unique on the BACnet network — it identifies
*this xEdge instance* as a BACnet device, separate from whichever remote
device you're reading. BACnet MS/TP (the serial variant) is not
implemented; only BACnet/IP.

## EtherNet/IP Scanner

Example: [ethernet-ip-example.yaml](../../config/examples/ethernet-ip-example.yaml)
· Driver type: `ethernet_ip`

```yaml
config: { host: 192.168.1.80, port: 44818, slot: 0 }
tag_groups:
  - id: group1
    scan_rate_ms: 50
    tags:
      - { id: setpoint, tag_name: "Program:MainProgram.Setpoint" }
```

`slot` is the CPU slot number in the rack (0 for most CompactLogix; check
your ControlLogix rack layout). `tag_name` is the Logix symbolic tag path,
resolved by the controller's own symbol table at connect time — there is
no offline tag browser; a typo in `tag_name` surfaces as that tag reading
Bad quality, not a config error.

**No real-server integration test exists for this driver** — every other
protocol in this list is tested against a real (if minimal) server/agent;
EtherNet/IP's Python library (`pycomm3`) requires Logix's symbol-table
object, which no permissively-licensed open-source CIP simulator
implements. It is tested against a mocked library boundary instead
(`tests/unit/test_ethernet_ip_client.py`). Verify against your actual
controller model before relying on it in production — this is the one
protocol area this delivery could not verify with real (even simulated)
wire traffic.

## SNMP Client (polling a device)

Example: [snmp-client-example.yaml](../../config/examples/snmp-client-example.yaml)
· Driver type: `snmp_client`

```yaml
config: { host: 192.168.1.90, port: 161, version: v2c, community: "${SECRET:snmp_community}" }
tag_groups:
  - id: group1
    scan_rate_ms: 200
    tags:
      - { id: sys_uptime, oid: "1.3.6.1.2.1.1.3.0", operation: get }
```

`operation: get_next` walks to the *next* OID after the one configured —
use it for a table whose exact instance index you don't know in advance,
never as a way to "batch" plain reads of known OIDs (GETBULK returns the N
*next* values after each OID, not the values at each OID — a real bug
caught during this delivery, see the Sprint C8 notes in
[crd-delivery-plan.md](../planning/crd-delivery-plan.md)). `use_bulk` is
rejected under SNMP v1, which has no GETBULK PDU.

## SNMP Agent (xEdge as a managed device)

Example: [snmp-agent-example.yaml](../../config/examples/snmp-agent-example.yaml)
· Core section: `snmp_agent`

Exposes standard MIB-II (`sysDescr`/`sysUpTime`/`sysName`) plus a live
driver-status table and alarm counters, all under a placeholder Private
Enterprise Number (`1.3.6.1.4.1.999999`) — swap that one constant for a
real, IANA-registered PEN before shipping to a customer's own NMS.
**Started once at process startup, like the embedded MQTT broker** —
enabling it via hot-reload requires a restart to actually bind the
listening socket (see [Configuration Guide](configuration-guide.md#hot-reload)).

## SNMP TRAP/INFORM (alarm notifications out)

Example: [snmp-notify-example.yaml](../../config/examples/snmp-notify-example.yaml)
· Core section: `snmp_notify`

One notification per alarm raise/clear, same ack/shelve suppression rules
as SMTP notifications below. **`notify_type: trap` is fire-and-forget UDP
(RFC 1905): a successful send only ever confirms the packet left this
device, never that the destination received it** — this is a genuine
protocol property, not a gap in xEdge. Use `notify_type: inform` (a
confirmed round trip) for any destination where missing a delivery would
matter.

## SNMP TRAP/INFORM Receiver (inbound notifications from a device)

Example: [snmp-trap-receiver-example.yaml](../../config/examples/snmp-trap-receiver-example.yaml)
· Driver type: `snmp_trap_receiver`

Event-driven — no `scan_rate_ms`; a tag only updates when a notification
whose `trap_oid` matches actually arrives. Get the vendor-specific
`trap_oid` values from the source device's own MIB.

## MQTT Subscriber (pulling data from an external broker)

Example: [mqtt-subscriber-example.yaml](../../config/examples/mqtt-subscriber-example.yaml)
· Driver type: `mqtt_subscriber`

Maps an arbitrary topic's payload (JSON, raw, or templated) into tags —
the one driver here that isn't polling a field device, it's subscribing
to whatever another system already publishes.

## Embedded MQTT Broker (xEdge as the broker)

Example: [mqtt-broker-example.yaml](../../config/examples/mqtt-broker-example.yaml)
· Core section: `mqtt_broker`

For a site with no existing broker. TLS, username/password auth
(`allow_anonymous` must be explicitly set — off by default), and
publish/subscribe ACLs are all real, not optional bolt-ons (ADR-012 §3).
**The publish and subscribe ACL plugins are asymmetric**: an empty
`publish_acl` falls back to "permitted" for backward compatibility, but
once either ACL dict is non-empty, an empty `subscribe_acl` means *zero*
subscribe access for everyone, not unrestricted — configure both
deliberately, never assume one mirrors the other. ACL editing is raw-YAML
only today (see [Configuration Guide](configuration-guide.md#web-ui-vs-raw-yaml)).
Started once at process startup — see the SNMP Agent note above; the same
restart caveat applies here.

## Northbound MQTT + Sparkplug B / generic JSON (publishing data out)

Core section: `northbound`. Sparkplug B (birth/death/sequence-numbered
protobuf) by default; set `mqtt.publisher_type: generic_json` for a
configurable plain-JSON topic/payload instead. Works against either an
external broker or xEdge's own embedded one above.

## SMTP (email alarm notifications + scheduled reports)

Example: [smtp-example.yaml](../../config/examples/smtp-example.yaml)
· Core section: `smtp`

Same ack/shelve suppression as SNMP notifications: an acknowledged alarm
does not re-notify; a shelved tag's alarms are suppressed until unshelved.
`alarm_notifications`/`scheduled_reports` are raw-YAML only (see
[Configuration Guide](configuration-guide.md#web-ui-vs-raw-yaml)).
