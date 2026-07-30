# xEdge — Customer Requirement Compliance & Gap Analysis Report

**Document ID:** XEDGE-CRD-001
**Version:** 2.0
**Status:** Final — Delivery 1 Handover
**Date:** 2026-07-30 (originally issued 2026-07-09, pre-delivery)
**Baseline:** xEdge codebase at the head of `feature/sprint-h1-integration-and-handover`, stacked on Sprints 0 and C1–C8 (PRs #2–#15) — see `docs/planning/crd-delivery-plan.md` §7 for the full sprint-by-sprint record.

**This is a revision of the original v1.0 gap analysis, not a new document.**
v1.0 (2026-07-09) scoped an 8-sprint engagement against a codebase where
seven of eight requirement areas were partially or fully unimplemented.
That engagement (Sprints 0, C1–C8) is now complete. Every row below has
been re-verified against the current codebase — file, class, or schema
field cited directly, not assumed carried over — and the original v1.0
wording is kept struck through inline where it changed, so a reader can
see the delta rather than only the endpoint. Sections 6 and 7 (the
original effort/cost estimate and delivery roadmap) are kept for the
historical record, marked accordingly, and compared against what actually
happened.

For the handover package this report is one input to (known limitations,
deferred-item register, explicit handover statements, and a condensed
top-level summary), see
[XEDGE-CRD-001-handover.md](../planning/XEDGE-CRD-001-handover.md).

---

## 1. Executive Summary

~~The customer requirement document asks for an OT-to-cloud edge gateway
module supporting Modbus (RTU/TCP), EtherNet/IP Scanner, SNMP, SMTP,
SNTP, cloud-driven Gateway Provisioning, full-featured MQTT
(Subscriber/Publisher/Broker), and Basic Asset Management... only Modbus
has a real implementation today, and even it is materially short of the
customer's spec... EtherNet/IP, SNMP, SMTP, SNTP, and MQTT
Subscriber/Broker are not implemented at all.~~

**All eight requirement areas are now implemented and verified against
real (or, for EtherNet/IP alone, mocked-boundary) infrastructure.**
Modbus RTU/TCP reached full spec compliance except Modbus TCP transport
TLS (still open, §4.2) and a write-multiple-coils runtime caller (FC15;
the codec exists, tested, unused — §4.1). EtherNet/IP Scanner, SNMP
(client/agent/TRAP-INFORM originator and receiver), SMTP, SNTP, Gateway
Provisioning with a real certificate-management subsystem, full MQTT
(Subscriber/Publisher/embedded Broker), and Basic Asset Management as a
metadata/grouping layer (ADR-010) were all built from zero across Sprints
C1–C8. Three items were deliberately descoped with the customer's
foreknowledge via the delivery plan's pre-agreed scope-cut list (MIB
upload/parse/browse, EtherNet/IP L5X import) or a decision record (SNMP
agent role read-only, community-string only). None of these are
discoveries made at handover — each has a decision ID and a date in
`docs/planning/XEDGE-DR-001-delivery-decisions.md`.

**What genuinely remains open, stated plainly rather than glossed over:**
Modbus TCP transport has no TLS; EtherNet/IP's "cyclic exchange" is
explicit messaging at a scan interval, not true CIP Class 1 implicit I/O
(no mainstream Python library implements it — customer-accepted, Q-7);
the SNMP agent role (xEdge as a managed device) supports v1/v2c and reads
only, not v3 or SET; connectivity state (Connected/Degraded/Not
Connected) is only computed by the Modbus and SNMP-client driver
families — an asset or gateway backed solely by EtherNet/IP, OPC UA,
BACnet, or MQTT-subscriber tags reports Unknown, by documented design,
not by omission; and the HIL pass (XEDGE-491) and the embedded broker's
RAM footprint against the 1GB ARM target (part of XEDGE-492) were both
verified only against simulators/test doubles — no physical field
hardware or ARM target was available in any development environment used
on this delivery (R-CRD-02/Q-6). See
[XEDGE-CRD-001-handover.md](../planning/XEDGE-CRD-001-handover.md) for
the complete known-limitations list and the required verbatim handover
statements.

**Original estimate vs. actual:** v1.0 estimated ~305–345 person-days,
~7–9 sprints. Actual: Sprints 0 + C1–C8 + H1 — 10 sprint-cycles by
calendar slot, several delivering multiple stories' worth of what v1.0
had split into separate backlog items (e.g., Sprint C4 combined
certificate management with MQTT TLS and gateway provisioning; Sprint C8
alone delivered four distinct SNMP protocol roles — client, agent, TRAP
originator, TRAP receiver — sharing one security model). See §6 for the
line-by-line comparison.

---

## 2. Scope, Baseline & Assumptions

- **Scope:** the 8 sections of the customer requirement document,
  delivered via `docs/planning/crd-delivery-plan.md` (XEDGE-CRD-001,
  window 2026-07-27 → 2026-12-06). **The line "protocols mentioned in the
  image not to be considered in Phase 1" was never resolved — the image
  was never received.** Per the original report's own recommendation,
  this delivery proceeded treating everything in the requirement
  document's text as in-scope (open item Q-1, risk R-CRD-01, mitigation
  "re-cut immediately if it arrives"). This is a **working assumption
  carried through the entire delivery, not a customer confirmation** —
  stated plainly rather than silently upgraded to "resolved" at handover.
  See §9.
- **Baseline:** the xEdge repository at the head of the H1 branch. Every
  driver named in the original report's "roadmap items, not shipped
  code" list (EtherNet/IP, SNMP) is now shipped; IEC 104, DNP3, IEC
  61850, DLMS, and PROFINET remain roadmap items for a later delivery,
  unchanged from v1.0's characterization.
- **Cost basis:** unchanged from v1.0 for the historical comparison in
  §6 (blended offshore rate ≈ $30/hr / $240 per 8h person-day) — this
  delivery's actual internal cost accounting is outside this report's
  scope.
- **Estimate class:** §6 remains a ROM estimate, retained for the
  historical record; it is not a claim about this delivery's actual
  tracked cost.

---

## 3. Compliance Summary

| # | Requirement Area | v1.0 Verdict | **Current Verdict** | Notes |
|---|---|---|---|---|
| 1a | Modbus RTU (RS-485) | 🟡 Partial (~45%) | ✅ **Compliant** | RTS delay unverified against real hardware (§4.1) |
| 1b | Modbus TCP/IP | 🟡 Partial (~50%) | 🟡 **Partial** | No transport TLS (§4.2) |
| 1c | Polling configuration | 🟡 Partial (~35%) | ✅ **Compliant** | Batching, retry, on-demand/on-connect all shipped |
| 1d | Device health & availability | ❌ Not implemented (~10%) | ✅ **Compliant** | Shared `ConnectivityState` machine (§4.4), reused by items 8 and 10 too |
| 2 | EtherNet/IP Scanner | ❌ Not implemented (0%) | 🟡 **Partial, customer-accepted** | Explicit messaging, not true Class 1 cyclic I/O (Q-7); no real-server test exists |
| 3 | SNMP Client & Agent | ❌ Not implemented (0%) | 🟡 **Partial, customer-accepted** | Client: full v1/v2c/v3. Agent: v1/v2c read-only. MIB browsing descoped |
| 4 | SMTP | ❌ Not implemented (0%) | ✅ **Compliant** | |
| 5 | SNTP | ❌ Not implemented (0%) | ✅ **Compliant** | Query-only; does not set the system clock (unchanged scope assumption) |
| 6 | Gateway Provisioning & Configuration | 🟡 Partial (~35%) | 🟡 **Partial** | Full metadata + 4-state model + real CA/rotation; no *import-your-own-PKI* workflow |
| 7 | MQTT (Subscriber/Publisher/Broker) | 🟡 Partial (~25%) | 🟡 **Partial** | All three roles shipped; broker cannot enforce mandatory mTLS (amqtt limitation) |
| 8 | Basic Asset Management | ❌ Not implemented (~10%) | ✅ **Compliant, by a documented design choice (ADR-010)** | Metadata/grouping layer, not a new primary entity — customer-confirmed (Q-3) |

**Legend:** ✅ Compliant · 🟡 Partial (a specific, named gap remains) · ❌ Not implemented

---

## 4. Detailed Compliance Matrix

### 4.1 Modbus RTU (RS-485)

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| RTU over RS-485/RS-232 serial interface | ✅ | ✅ | Unchanged: `xedge/drivers/modbus/serial.py`, `rtu_codec.py` |
| Configurable baud/parity/stop bits/slave ID | ✅ | ✅ | Unchanged |
| Poll multiple slave devices, configurable intervals | 🟡 | ✅ | `xedge/drivers/modbus/bus_manager.py::SerialBusManager` — reference-counted per `port_path`; `ModbusRtuSerialDriver` now delegates to it instead of opening the port itself |
| Prioritize write parameters vs read | ❌ | ✅ | `xedge/drivers/modbus/scheduler.py::RequestScheduler`/`RequestPriority` (`WRITE=0`, `READ=10`); the RTU driver's scheduler is the bus manager's *shared* one so every slave on one bus prioritizes writes together |
| On-Demand / On-Connect / Polling mechanisms | 🟡 | ✅ | `polling.py`: `POLLING_MODE_CONTINUOUS`/`ON_CONNECT`/`ON_DEMAND`; on-demand trigger via `POST /api/v1/drivers/{id}/tag-groups/{group_id}/poll` |
| Combine multiple registers into a single value | ❌ | ✅ | `xedge/drivers/modbus/datatypes.py` — uint16/int16/uint32/int32/float32/uint64/int64/float64, configurable word/byte order (all 4 permutations) |
| Multiple read & write parameters per device | 🟡 | ✅ | Multi-register write path now has a real caller (`polling.py::_write_register_tag`) for FC16, not just an unused codec |
| Map FC01/02/03/04 (read) and FC05/06/15/16 (write) | 🟡 | 🟡 **FC15 still open** | FC01–04, FC05, FC06, FC16 all have real callers now. **FC15 (write multiple coils): the codec (`codec.py::encode_write_multiple_coils`) is implemented and unit-tested, but has no runtime caller** — `write()`'s API is single-tag, with no bulk-write concept to invoke it from. Stated in the Sprint C2 PR itself, not discovered at handover. |
| Retry & error handling, meaningful error messages | 🟡 | ✅ | Schema: `retry_count`, `retry_on_exception`, `retry_backoff_seconds`, `consecutive_failure_threshold`, `recovery_threshold`. `polling.py::_exception_name()` surfaces a human-readable exception name in `TagUpdate.metadata` |
| Serial port selectable from detected ports | ❌ | ✅ | `GET /api/v1/serial-ports` (real pyserial enumeration) wired end-to-end into an HTML `<datalist>` autocomplete (`_form_macros.html`/`xedge-ui.js`) — still free-text underneath, so an unplugged port can still be typed manually |
| Baud rate list (1200–115200) | ✅ | ✅ | Unchanged |
| Parity None/Even/Odd | ✅ | ✅ | Unchanged |
| Stop bits 1/2 | ✅ | ✅ | Unchanged |
| Slave ID 1–247, unique-on-bus validation | 🟡 | ✅ | `xedge/core/driver_config.py::find_duplicate_serial_slave_ids()` — runs on every config apply (hot-reload and startup), not just once |
| RTS pre-transmit/post-transmit delay (µs) | ❌ | 🟡 **Implemented, unverified against real hardware** | `bus_manager.py`: `rts_pre_delay_us`/`rts_post_delay_us`, applied via `pyserial`'s `.rts` property; degrades to a logged no-op if the transport doesn't support modem-control lines. Stated explicitly (module docstring, open item Q-4) as never tested against a physical RS-485 converter — no such hardware was available in any development environment used on this delivery. |
| *(minor, carried from v1.0)* Modbus TCP `unit_id` range | 🟡 | 🟡 **Unchanged** | Schema still allows 0–255 rather than 1–255 with 0xFF special-cased |

### 4.2 Modbus TCP/IP

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Modbus TCP over Ethernet | ✅ | ✅ | Unchanged |
| Configure IP/host, port, unit ID | ✅ | ✅ | Unchanged |
| Concurrent communication with multiple devices | ✅ | ✅ | Unchanged, now verified running five *different protocols'* driver instances concurrently too (Sprint H1, XEDGE-490) |
| High-frequency polling | ✅ | ✅ | ~~Customer literally asks for "min 1 nanosecond" — physically unachievable~~ — resolved with the customer (D-10/Q-2, 2026-07-26): floor lowered to 1ms for Modbus specifically, per-transport achievable minimums documented and enforced (`minimum_scan_interval_seconds()` warns when a configured rate can't actually be met on a given serial link) |
| Map registers to system parameters | 🟡 | ✅ | Multi-register combining (`datatypes.py`) plus the Asset layer's named `tag_ref`/`alias` (§4.11) together cover this |
| Prioritize write vs read | ❌ | ✅ | Same shared scheduler as RTU (§4.1) |
| On-Demand / On-Connect / Polling | 🟡 | ✅ | Same as RTU |
| Combine registers into single value | ❌ | ✅ | Same as RTU |
| FC01/02/03/04/05/06/15/16 | 🟡 | 🟡 | Same FC15 gap as RTU (§4.1) — shared `polling.py` code path |
| Retry/error handling | 🟡 | ✅ | Same as RTU, plus `connection_retry_count`/`connection_retry_backoff_seconds` for a failed *connection* attempt specifically |
| TCP host/port (default 502) | ✅ | ✅ | Unchanged |
| Unit Identifier 1–255, 0xFF direct | 🟡 | 🟡 **Unchanged** | Still 0–255, not 1–255 + 0xFF special case |
| Persistent vs on-demand connection mode | ❌ | ✅ | `tcp.py`: `CONNECTION_MODE_PERSISTENT`/`CONNECTION_MODE_ON_DEMAND` |
| TCP keepalive interval & retry count | ❌ | ✅ | `_apply_keepalive()` — `SO_KEEPALIVE`+platform socket options, degrades gracefully where unsupported (e.g. some Windows configurations) |
| TLS enabled/disabled, certificate pinning | ❌ | ❌ **Still fully open** | No TLS/mTLS option anywhere in the Modbus TCP driver or its schema — confirmed by direct search, not assumed. The only real-world mitigation is network-level isolation (VLAN/firewall segmentation per the Hardening Guide), not transport encryption. |

### 4.3 Polling Configuration

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Configurable poll interval | 🟡 | ✅ | Resolved with the customer (D-10/Q-2) — see §4.2 |
| Configurable request timeout | ✅ | ✅ | Unchanged |
| Configurable connection retry count | ❌ | ✅ | §4.2 |
| Read batching / block read (fixed groups) | ❌ | ✅ | `xedge/drivers/modbus/planner.py::plan_read_blocks()` — greedy contiguous-address coalescing, respects the 125-register/2000-coil protocol ceilings and a configurable `max_block_size`/`max_block_gap`. Verified at scale under XEDGE-492 (100 batched tags, still 1 request/cycle). |
| Max batch/block size, configurable | ❌ | ✅ | `max_block_size`/`max_block_gap`, schema-exposed |
| Retry-on-exception, configurable | ❌ | ✅ | `retry_on_exception` |

### 4.4 Device Health & Availability

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Consecutive-failure threshold → offline/Not Connected | ❌ | ✅ | `xedge/core/connectivity.py::ConnectivityTracker` — asymmetric hysteresis, independently configurable `failure_threshold`/`recovery_threshold` |
| Auto-recovery to active state | 🟡 | ✅ | Same tracker; recovery requires `recovery_threshold` *consecutive* successes, deliberately harder than the failure path (a flapping link shouldn't oscillate the displayed state) |

Built as **shared infrastructure**, not a Modbus-only fix (ADR-011 Part
3): the same `ConnectivityState` enum
(`UNKNOWN`/`CONNECTED`/`DEGRADED`/`NOT_CONNECTED`) backs Modbus device
health (this section), Gateway connection state (§4.9), and Asset
connection state (§4.11). **Stated limitation, not a bug:** only the
Modbus driver family and the SNMP client implement
`get_connectivity_state()` today; EtherNet/IP, OPC UA, BACnet, and
MQTT-subscriber instances report `UNKNOWN` for this specific signal,
regardless of their actual health, until/unless extended the same way. A
Sprint H1 test (XEDGE-490) confirmed the concrete consequence: an asset
whose parameters span both a connectivity-aware and a connectivity-unaware
protocol always reports Degraded, never Connected — see the
[Known Limitations](../planning/XEDGE-CRD-001-handover.md) list.

### 4.5 EtherNet/IP Scanner

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Device configuration (IP, slot, path) | ❌ | ✅ | `xedge/drivers/ethernet_ip/client.py::EtherNetIpDriver`, built on `pycomm3` (MIT). `config/schema/drivers/ethernet_ip.schema.json`: host/port/slot/connect_timeout |
| Cyclic (implicit I/O) data exchange | ❌ | 🟡 **Customer-accepted substitute, not literal implicit I/O** | No mainstream Python CIP library — `pycomm3` included — implements true CIP Class 1 connected I/O (UDP 2222). What's delivered is explicit messaging repeated at a configured scan interval, put to the customer as open item **Q-7** and **resolved 2026-07-28**: accepted as satisfying "cyclic exchange." Schema floor: 50ms (not lowered the way Modbus's was — never verified achievable below that on real EtherNet/IP traffic). |
| Acyclic (explicit messaging) data exchange | ❌ | ✅ | This is the driver's actual native mode — `driver.read`/`driver.write` via `asyncio.to_thread` |
| Device/connection configuration | ❌ | ✅ | Connection-level `CommError` propagates to `DriverSupervisor` for restart-with-backoff; request-level errors (`DataError`/`ResponseError`/`RequestError`) mark individual tags Bad without dropping the session |
| I/O mapping | ❌ | 🟡 | Symbolic tags (BOOL/INT/DINT/REAL/STRING, program-scoped) map cleanly; array/UDT members work only as opaque tag-name strings (`'MyArray[3]'`, `'MyUdt.Member'`) — no structured decomposition UI. No runtime tag-discovery browser; **L5X import was cut** (delivery plan §5 scope-cut candidate 4 — commissioning convenience, not required for symbolic read/write) |
| Connection monitoring | ❌ | ✅ | Via the shared `DriverSupervisor` restart/backoff path, same mechanism every other driver uses |
| Read/write parameters | ❌ | ✅ | `access: read_only/write_only` tag-level guards; write-back via `write()` |

**Two caveats worth carrying into any customer conversation, stated
plainly:** (1) **no real EtherNet/IP simulator was found** — `cpppo`'s
CIP simulator doesn't implement the Logix symbol-table upload
`pycomm3.LogixDriver` requires, so this driver alone (of every protocol
in this delivery) is tested only against a mocked library boundary, not
real wire traffic. (2) `pycomm3` itself carries a **maintenance risk**:
`license-audit.md` item 7 quotes PyPI directly — *"pycomm3 is no longer
actively developed."* License (MIT) and current functionality are both
fine; ongoing support is a real, named risk, not a functional gap.

### 4.6 SNMP Client & Agent

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| v1/v2c/v3 client GET/GETNEXT/GETBULK/SET | ❌ | ✅ | `xedge/drivers/snmp/client.py::SnmpClientDriver`, built on `pysnmp>=7.1` (BSD-2-Clause). Full v3 USM auth (md5 through sha512) and privacy (des through aes256), RFC 3414's "privacy requires auth" enforced. GETBULK correctly restricted to `get_next`-operation tags only — a real semantics bug (GETBULK returns the *next N* values, not the values *at* each OID) was caught by a failing test before merge, not discovered later. |
| SNMP agent (xEdge itself pollable) | ❌ | 🟡 **v1/v2c read-only, not v3, not writable** | `xedge/northbound/snmp_agent.py::SnmpAgentService` — standard MIB-II plus a live driver-status table and alarm counters. **Only `add_v1_system` is configured — no SNMPv3 user is ever registered for the agent role**, even though the client role above fully supports v3. **No `SetCommandResponder` is registered** — external SNMP SET against xEdge itself is not supported, by design (module docstring). Every custom OID sits under a placeholder Private Enterprise Number (`1.3.6.1.4.1.999999`) that **must be replaced with a real, IANA-assigned PEN before production use against a customer's NMS** — customer-accepted for this delivery (Q-8, 2026-07-30), not silently shipped. |
| TRAP + INFORM sender, wired to the alarm engine | ❌ | ✅ | `xedge/core/snmp_notify.py::snmp_alarm_notification_loop` — fires only on a NORMAL↔alarming boundary crossing, same suppression rules as SMTP. **Protocol-level limitation, confirmed against a real unreachable destination:** `notify_type: trap` is fire-and-forget UDP (RFC 1905) — a successful send only ever confirms the packet left this device, never that the destination received it. Only `notify_type: inform` (a confirmed round trip) can detect a delivery failure. |
| TRAP + INFORM receiver | ❌ | ✅ | `xedge/drivers/snmp/receiver.py::SnmpTrapReceiverDriver` — generic, configurable OID→tag_id mapping (customer-accepted, Q-10), not tied to any vendor's MIB |
| MIB upload/parse/browse UI | ❌ | ❌ **Deliberately descoped, not built** | Pre-identified in `crd-delivery-plan.md` §5 as scope-cut candidate 2 before Sprint C8 started, invoked at the start of that sprint — a decision, not a discovery. Tags use raw numeric OIDs only; no symbolic MIB name resolution or browser. `pysmi` (the library that would parse MIBs) is license-cleared in `license-audit.md` §4 item 8 as due diligence, but was never actually added as a dependency. |

### 4.7 SMTP Protocol Support

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| SMTP send, server/auth/TLS config | ❌ | ✅ | `xedge/core/smtp.py::SmtpConfig` — `tls_mode: none/starttls/smtps` (two distinct stdlib code paths, not a single boolean), username/password auth, `tls_ca_certs_path` (added after a real trust-store gap was found against a live test server, not assumed fine) |
| Event-driven notifications (wired to alarms) | ❌ | ✅ | `alarm_notification_loop()` — polls the alarm engine, emails only on a NORMAL↔alarming boundary crossing (not on ack, not while shelved) |
| Scheduled email reports | ❌ | ✅ | `scheduled_report_loop()`, one task per configured report, can scope to specific tags and/or assets. Each report is a live snapshot at send time, not a historical digest — a stated scope choice. |

**Web UI note:** server config (host/port/TLS/auth) has a real
schema-driven form; the recipient-list arrays
(`alarm_notifications`/`scheduled_reports`) are edited via the raw-YAML
Advanced editor by design — the same pre-existing schema-forms gap named
in §4.11's Web UI note.

### 4.8 SNTP Protocol Support

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| SNTP sync, multi-server, interval config | ❌ | ✅ | `xedge/core/sntp.py::SntpConfig` — multiple servers tried in order per cycle; full 4-timestamp RFC 4330 offset/delay computation, not the 2-timestamp shortcut |
| Sync-status reporting | ❌ | ✅ | `SntpSyncStatus` (last sync time/server, offset, round-trip delay, consecutive failures, staleness) — a dedicated dashboard block and a status API |
| Uniform timestamping | (assumed OK) | ✅ | Unchanged from v1.0's assessment — xEdge's internal timestamping was already UTC/nanosecond-precision; SNTP adds sync *status visibility*, not a new timestamp model |

Query-only by design — does not set the system clock; the host OS is
still assumed NTP-synced (`ASM-001`), matching what v1.0 already
anticipated as the likely shape.

### 4.9 Gateway Provisioning and Configuration

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Gateway Name | 🟡 | 🟡 **Unchanged** | Still `DeviceRecord.display_name`, not renamed |
| Gateway Protocol | ❌ | ✅ | `DeviceRecord.protocol` column |
| Gateway Serial Number | ❌ | ✅ | `DeviceRecord.serial_number` |
| Gateway Make | ❌ | ✅ | `DeviceRecord.make` |
| Gateway Firmware Version | 🟡 | ✅ | `DeviceRecord.hardware_firmware_version`, deliberately distinct from `agent_version` (the xEdge software version) |
| Connection State (Connected/Disconnected/Active/Inactive) | 🟡 | ✅ | `GatewayConnectionState` enum, exactly these 4 names, computed from heartbeat age. **Stated caveat:** the Connected-vs-Active and Disconnected-vs-Inactive boundary definitions are this project's own interpretation, not a customer-confirmed spec — documented as such in the code itself. |
| Remote gateway config/updates from cloud | 🟡 | ✅ | Per-device config push + pull-and-report round trip, plus config-status polling |
| Initial onboarding, minimal device-side config | 🟡 | ✅ | Join-token enrollment: device generates its own keypair/CSR, redeems a single-use token, receives a CA-signed cert — device-side config before enrollment is just `fleet.enabled`/`manager_url`/`join_token` |
| Upload/manage root/CA certificates via user account | ❌ | 🟡 **A different, narrower thing was built** | A real certificate-management subsystem exists (`xedge/security/`: CA, CSR signing, rotation) — but it is **xEdge's own self-generated, self-managed fleet CA**, not a workflow for importing a *customer-supplied* external PKI. If the original requirement meant "bring your own root CA," that specific capability does not exist. |
| Secure certificate deployment to gateways | ❌ | ✅ | mTLS after enrollment; automatic rotation (`cert_rotation_threshold_days`, default 30 days before expiry) over the existing session, not a manual process |
| All subsequent config/management via cloud platform only | 🟡 | ✅ | The Fleet Manager is confirmed API-only — no HTML dashboard routes exist at all (ADR-013 §2, explicit scope for this delivery: single-tenant, API-only) |

### 4.10 MQTT

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Subscriber — external broker, arbitrary topic/payload → tags | ❌ | ✅ | `xedge/drivers/mqtt_subscriber/client.py::MqttSubscriberDriver` — genuinely general-purpose, unrelated to Sparkplug NCMD write-back. `payload_format: raw|json` with JSON-path extraction, scale/offset. **Stated limitation:** a wildcarded topic (`+`/`#`) maps every matching concrete topic to the same fixed `tag_id` — no dynamic per-topic tag fan-out. |
| Publisher — configurable payload, own structure | 🟡 | ✅ | `publisher_type: sparkplug_b` (default, unchanged fixed schema) or `generic_json` (configurable topic template, per-tag or batch payload mode, field-name mapping) — mutually exclusive per deployment |
| Manual republish trigger | ❌ | ✅ | `POST /api/v1/northbound/republish`, permission-gated and audit-logged, generic to whichever connector is configured |
| Broker — accept connections, diagnostics, config | ❌ | ✅ | `xedge/northbound/mqtt_broker.py::MqttBrokerService` wraps `amqtt` (MIT), promoted from test-only to a real runtime dependency (Sprint C5) |
| Broker Address/Port/Client ID/Keepalive/Topics/QoS | ❌ / 🟡 | ✅ | All present on both the broker and publisher/subscriber configs |
| Authentication (username/password) | 🟡 | ✅ | `FileAuthPlugin` (sha512_crypt) or explicit anonymous opt-in (off by default) |
| SSL/TLS, certificate upload/path | ❌ | 🟡 **Client-side mTLS: yes. Broker-enforced mTLS: no, a library limit** | Client side (publisher, subscriber, and the fleet agent) share one TLS/mTLS context builder (`xedge/security/tls_context.py`) — real client certificates supported. **The embedded broker cannot enforce mandatory client certificates**: `amqtt` always sets `ssl.CERT_OPTIONAL`, never `CERT_REQUIRED`, regardless of configuration — verified against amqtt's own source, documented in the schema and module docstring, not assumed. |
| Payload Type/Format selection | ❌ | ✅ | `generic_json` mode's `payload_mode: per_tag|batch`, configurable field names |

**ACL asymmetry, stated precisely:** once either `publish_acl` or
`subscribe_acl` is non-empty, the plugin loads for both directions — but
only PUBLISH has a "no config = permitted" backward-compatibility
carve-out. An empty `subscribe_acl` at that point means *zero* subscribe
access for everyone, not unrestricted. Configure both deliberately.

### 4.11 Basic Asset Management

| Requirement | v1.0 | **Current** | Evidence |
|---|---|---|---|
| Asset Name, Serial Number, Type, Make, Firmware Version, Description, Alias, Units | ❌ | ✅ | `xedge/core/assets.py::Asset` (name/serial_number/asset_type/make/model/firmware_version/description) + `AssetParameter` (alias, unit — per-parameter, matching how a physical asset's I/O points each carry their own unit) |
| Enable/Disable Asset | ❌ | 🟡 **Presentational only, by explicit design** | `Asset.enabled` does not stop backing drivers — stated in the schema and ADR-010 §4, not a partial implementation of a stop/start feature |
| Assign asset to a gateway, Asset Connection State | ❌ | ✅ | `gateway_id` soft reference (never locally validated — the Fleet Manager is a separate system this device may have no connection to). Connection state reuses the same `ConnectivityState` machine as §4.4/§4.9 — same "Modbus/SNMP-only report real state" caveat applies here too. |
| Parameter List per asset (multiple parameters) | 🟡 | ✅ | `AssetParameter.tag_ref` — a reference to an existing tag, never a new tag definition (ADR-010 §1) |
| Storage Requirement (store data or not) per parameter | 🟡 | ✅ | `AssetParameter.store`, enforced at the cold-store spill boundary. Interim constraint: granularity is per cold-store stream (`source_driver`), not per individual tag, since ring buffers aren't yet keyed finer than that (ADR-010 §3) |
| Centralized protocol configuration interface | 🟡 | 🟡 **A reference/grouping view, not a unified editor — customer-confirmed** | Dedicated asset pages exist (`/ui/config/assets/...`) with inline links into the existing per-driver forms; parameters are picked from a list of already-existing tags. There is no single flow that creates a driver/tag and an asset together. Put to the customer as open item **Q-3** and **resolved 2026-07-28**: this reference/grouping view was accepted over an asset-first creation flow. ADR-010 documents an escape hatch if this is revisited later. |
| Validate configuration before deployment | ✅ | ✅ | Extended, not just carried over: `validate_asset_references()` checks every `tag_ref` resolves to a real tag, on *every* config-apply path (hot-reload, fleet-pushed config, both Web UI save paths) — a real gap here (driver/tag deletion silently bypassing this check) was found and fixed during Sprint C6, not left latent |

---

## 5. Additional Development Required — Consolidated Backlog

**Status: this backlog is closed.** Retained verbatim below (struck
through) as the historical record of what v1.0 asked for, against what
was actually delivered — see §4 for the item-by-item current status, and
§9 for what remains genuinely open.

~~1. Modbus — bring to full spec compliance~~ — **done except Modbus TCP
TLS and FC15's runtime caller (§4.1/§4.2).**
~~2. EtherNet/IP Scanner (new driver)~~ — **done, `pycomm3`, explicit
messaging (Q-7), no real-server test (§4.5).**
~~3. SNMP Client & Agent (new module)~~ — **done, `pysnmp`; agent role is
v1/v2c read-only (§4.6); MIB browsing descoped.**
~~4. SMTP (new module)~~ — **done (§4.7).**
~~5. SNTP (new module)~~ — **done (§4.8).**
~~6. Gateway Provisioning~~ — **done; certificate subsystem is
xEdge's own CA, not an import-existing-PKI workflow (§4.9).**
~~7. MQTT full buildout~~ — **done; broker can't enforce mandatory mTLS,
a library limit (§4.10).**
~~8. Basic Asset Management (new data-model layer)~~ — **done as a
metadata/grouping layer per ADR-010, customer-confirmed (§4.11).**

---

## 6. Effort & Cost Estimate — Historical Record

**This section is retained as originally written (2026-07-09, pre-delivery)
for comparison. It is a ROM estimate that predates the work, not a report
of actual tracked cost or effort** — this repository's sprint records
(`docs/planning/crd-delivery-plan.md` §7) are the source of truth for
what actually happened.

ROM estimate in person-days (1 person-day = 8h). "Backend" = protocol/core engineering, "Frontend" = Web UI, "QA" = test + HIL + docs.

| Module | Backend (d) | Frontend (d) | QA (d) | **Total (d)** |
|---|---:|---:|---:|---:|
| Modbus — full-spec enhancement | 42 | 6 | 6 | **54** |
| EtherNet/IP Scanner (new) | 24 | 6 | 6 | **36** |
| SNMP Client & Agent (new) | 32 | 10 | 6 | **48** |
| SMTP (new) | 8 | 4 | 3 | **15** |
| SNTP (new) | 6 | 3 | 2 | **11** |
| Gateway Provisioning (extend) | 28 | 8 | 5 | **41** |
| MQTT full buildout | 32 | 12 | 8 | **52** |
| Basic Asset Management (new) | 28 | 14 | 6 | **48** |
| **Subtotal** | **200** | **63** | **42** | **305** |
| + Integration, ADRs, cross-module regression, docs (~12%) | | | | **+37** |
| **Total** | | | | **≈ 342 person-days** |

**Estimate vs. actual, qualitatively:** the original estimate's
7–9-sprint range (§ "Calendar time") is in the same order of magnitude as
the 10 sprint-slots actually used (Sprint 0 + C1–C8 + H1), though the two
aren't directly comparable line-by-line — several sprints combined work
v1.0 had estimated as separate line items (e.g., C4 combined certificate
management, MQTT TLS, and gateway metadata into one sprint), and the
actual delivery order differed from §7's suggested roadmap below (Modbus
RTU/RS-485 work was split across C1–C2 rather than one combined pass,
and Asset Management landed in C6, after MQTT buildout, rather than
before EtherNet/IP and SNMP as §7 recommended). No independent
person-day/cost tracking against this estimate was kept as part of this
delivery; this paragraph is a qualitative comparison, not a reconciled
actuals report.

---

## 7. Suggested Delivery Roadmap — Historical Record

**Retained as originally written.** The actual sprint sequence
(`docs/planning/crd-delivery-plan.md` §3) was:

| Actual sprint | Focus |
|---|---|
| Sprint 0 | CI stabilization (prerequisite, not in v1.0's roadmap) |
| C1 | Modbus TCP/RTU protocol-level completeness (batching, multi-register types, retry) |
| C2 | RS-485 multi-drop, write priority, on-demand/on-connect polling |
| C3 | (see delivery plan for exact scope) |
| C4 | Certificate management + MQTT TLS + gateway provisioning metadata — combined, not sequential as §7 suggested |
| C5 | MQTT buildout: subscriber, publisher templating, embedded broker |
| C6 | Asset Management + SMTP — **after** MQTT, not before EtherNet/IP/SNMP as §7's original rationale recommended |
| C7 | EtherNet/IP Scanner |
| C8 | SNMP (client, agent, TRAP originator, TRAP receiver) |
| H1 | Cross-protocol integration test, performance validation, customer documentation, handover package |

§7's original rationale for sequencing Asset Management before the new
protocol drivers ("avoids retrofitting asset-awareness onto 3 new
drivers") did not end up mattering in practice: Asset Management's
final design (ADR-010, a metadata layer referencing `instance_id/tag_id`
strings) is protocol-agnostic by construction, so EtherNet/IP and SNMP
tags became asset-referenceable automatically once built, with no
retrofit cost — confirmed directly in Sprint H1 (XEDGE-490), which built
an asset spanning tags from all five protocol families with no changes
to `xedge/core/assets.py`.

Original roadmap table, unchanged, for reference:

| Sprint(s) | Focus | Rationale |
|---|---|---|
| 1–2 | Modbus full-spec enhancement + shared cert-management subsystem | Highest customer-visible gap; cert subsystem unblocks two later items |
| 3–4 | Gateway Provisioning completion + MQTT TLS/mTLS + Publisher templating | Security-adjacent items grouped to avoid building PKI twice |
| 5–6 | Basic Asset Management, before EtherNet/IP and SNMP | Avoid retrofitting asset-awareness onto 3 new drivers |
| 6–7 | EtherNet/IP Scanner | New driver, reuses the existing plugin framework |
| 7–8 | SNMP Client & Agent, MQTT Subscriber + Broker | Parallelizable |
| 8 | SMTP + SNTP | Smallest items |

---

## 8. Key Risks & Recommendations — Outcomes

| # | Original risk/recommendation (2026-07-09) | Outcome |
|---|---|---|
| 1 | MQTT TLS is a pre-existing, undocumented gap | **Closed.** MQTT TLS/mTLS shipped Sprint C4; the embedded broker's own mTLS-enforcement limit (an `amqtt` library ceiling, not an xEdge gap) is now a documented, known limitation instead (§4.10). |
| 2 | EtherNet/IP/SNMP licensing needs an ADR before estimation firms up | **Done.** ADR-012 (buy, not clean-room) for both, following the BACnet/`bacpypes3` precedent (ADR-006) — materially reduced actual effort vs. a clean-room estimate, consistent with the recommendation's own prediction. |
| 3 | MQTT Broker build-vs-buy is an open decision | **Decided: buy.** `amqtt` promoted from test-only to a runtime dependency (ADR-012 §3, license-audit.md item 6). |
| 4 | Asset Management is an architecture decision, not a form — needs an ADR | **Done.** ADR-010, decided *before* coding started, not discovered mid-implementation. |
| 5 | The "1 nanosecond" minimum poll interval is not achievable — needs a clarifying conversation | **Resolved with the customer** (D-10/Q-2, 2026-07-26): per-transport achievable floors instead, 1ms for Modbus specifically. |
| 6 | Unify Device Health / Gateway Connection State / Asset Connection State into one shared component | **Done**, and it paid off as predicted: `xedge/core/connectivity.py`'s `ConnectivityState` machine backs all three (§4.4), including surfacing the same "only Modbus/SNMP report real state" caveat consistently across all three rather than three separately-behaving implementations. |

**New risks that materialized during delivery, not anticipated in v1.0**
(full detail in the [handover package](../planning/XEDGE-CRD-001-handover.md)):
no HIL pass against physical field hardware was possible in any
development environment used (R-CRD-02); the embedded broker's RAM
footprint against the 1GB ARM target was never measured for the same
reason; `pycomm3` (EtherNet/IP) is flagged by its own maintainers as no
longer actively developed.

---

## 9. Open Questions — Resolution Record

Of the two original open questions, one was formally resolved with the
customer and one remains genuinely open at handover — both tracked with
decision IDs in `docs/planning/XEDGE-DR-001-delivery-decisions.md`:

1. **Protocols excluded via "the image" — still open, not resolved
   (Q-1/R-CRD-01).** The referenced image was never received at any
   point across this delivery. Every one of the eight CRD areas was
   built as if fully in-scope, per the risk register's own stated
   mitigation ("re-cut immediately if it arrives") — a working
   assumption maintained for the entire delivery, not a customer
   sign-off that all eight areas were definitely wanted. If an exclusion
   list surfaces after handover naming any of the eight areas, that
   area's delivered scope should be treated as a bonus beyond the
   original ask, not a compliance failure to remediate.
2. ~~Practical minimum poll interval~~ — **Resolved 2026-07-26 (D-10/Q-2):** per-transport achievable floors; 1ms for Modbus, unchanged (50ms) for EtherNet/IP and SNMP since sub-50ms rates were never verified achievable for either.
3. ~~EtherNet/IP and SNMP: buy vs. clean-room~~ — **Resolved (ADR-012):** buy — `pycomm3` and `pysnmp`, both license-cleared (`license-audit.md` items 7–8).
4. ~~MQTT Broker: embed vs. from-scratch~~ — **Resolved (ADR-012 §3):** embed `amqtt`.
5. ~~Asset Management: primary entity vs. metadata layer~~ — **Resolved in two parts:** the data-model question was decided up front (ADR-010, before coding); the UI-presentation question was put to the customer separately and **resolved 2026-07-28 (Q-3):** reference/grouping view accepted over an asset-first flow.

**New open items that emerged during delivery** (decision IDs Q-4, Q-6
through Q-10 — see `XEDGE-DR-001-delivery-decisions.md` §4 for full text
of each): RTS delay hardware verification (Q-4, still open — no hardware
available); HIL pass hardware access (Q-6, resolved 2026-07-30 — document
as simulator-only, per R-CRD-02's pre-agreed fallback, rather than delay
handover chasing hardware); EtherNet/IP cyclic-exchange interpretation
(Q-7, resolved 2026-07-28); SNMP agent PEN/MIB-depth/trap-mapping scope
(Q-8/9/10, all resolved 2026-07-30).
