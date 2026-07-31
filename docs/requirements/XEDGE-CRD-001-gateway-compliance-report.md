# xEdge — Customer Requirement Compliance & Gap Analysis Report

**Document ID:** XEDGE-CRD-001
**Version:** 1.0
**Status:** Draft — for internal review
**Date:** 2026-07-09
**Baseline:** xEdge codebase as of commit `adbca33` (Sprint 31, 2026-07-08)

---

## 1. Executive Summary

The customer requirement document asks for an OT-to-cloud edge gateway module supporting Modbus (RTU/TCP), EtherNet/IP Scanner, SNMP, SMTP, SNTP, cloud-driven Gateway Provisioning, full-featured MQTT (Subscriber/Publisher/Broker), and Basic Asset Management — on a modular driver framework that can grow to more protocols later.

**Bottom line:** xEdge's driver framework, pipeline, and Web UI architecture are a strong, genuinely reusable foundation — the "modular enough to add protocols later" requirement is already satisfied by design. But of the eight requirement areas, only **Modbus** has a real implementation today, and even it is materially short of the customer's spec (no register batching, no multi-register data types, no write-priority, no On-Demand/On-Connect modes, no RS-485 RTS delay, single-slave-per-instance only). **EtherNet/IP, SNMP, SMTP, SNTP, and MQTT Subscriber/Broker are not implemented at all.** MQTT Publisher exists but is hard-wired to Sparkplug B, not the customer's "own payload structure" model. "Basic Asset Management" as described (asset as a first-class entity, separate from a tag/driver) does not exist as a data-model concept in xEdge today. "Gateway Provisioning" partially exists via the Fleet Management Agent but lacks the customer's specific metadata fields and certificate-management workflow.

**Estimated total additional effort: ~305–345 person-days (ROM, ±30%)**, roughly **7–9 sprints (14–18 weeks, ~3.5–4.5 months)** with a dedicated 5-person pod, **~$75,000–$95,000** at the assumed $30/hr blended offshore rate (Q6 below — adjust freely to your actual rate card).

---

## 2. Scope, Baseline & Assumptions

- **Scope:** strictly the 8 sections of the customer requirement document. The line "protocols mentioned in the image not to be considered in Phase 1" refers to an image that was not received with the request — per your direction, this report treats **everything in the text as in-scope for Phase 1** and flags this as an open item (§9).
- **Baseline:** the current xEdge repository (`E:\sac\Progs\XEdge`), not the aspirational README/HLR protocol list — several protocols named in `README.md` and `docs/requirements/HLR.md` (IEC 104, DNP3, IEC 61850, DLMS, PROFINET) are **roadmap items, not shipped code**; only Modbus (RTU/TCP/RTU-over-TCP), OPC UA client, and a BACnet/IP client actually exist under `xedge/drivers/`.
- **Cost basis:** blended offshore rate **≈ $30/hr ($240/8hr person-day)**, per your selection. This is illustrative — swap in your real rate card against the person-day figures in §6.
- **Estimate class:** Rough Order of Magnitude (ROM), sized against this project's own historical velocity (11-engineer team, 2-week sprints, per `docs/planning/development-plan.md`) and comparable driver efforts already completed (Modbus, BACnet). Not a committed sprint-level estimate — refine in backlog grooming before committing dates.

---

## 3. Compliance Summary

| # | Requirement Area | Verdict | % Complete (est.) |
|---|---|---|---|
| 1a | Modbus RTU (RS-485) | 🟡 Partial | ~45% |
| 1b | Modbus TCP/IP | 🟡 Partial | ~50% |
| 1c | Polling configuration | 🟡 Partial | ~35% |
| 1d | Device health & availability | ❌ Not implemented | ~10% |
| 2 | EtherNet/IP Scanner | ❌ Not implemented | 0% |
| 3 | SNMP Client & Agent | ❌ Not implemented | 0% |
| 4 | SMTP | ❌ Not implemented | 0% |
| 5 | SNTP | ❌ Not implemented | 0% |
| 6 | Gateway Provisioning & Configuration | 🟡 Partial | ~35% |
| 7 | MQTT (Subscriber/Publisher/Broker) | 🟡 Partial | ~25% |
| 8 | Basic Asset Management | ❌ Not implemented (no data-model concept) | ~10% |

**Legend:** ✅ Compliant  🟡 Partial  ❌ Not implemented

---

## 4. Detailed Compliance Matrix

### 4.1 Modbus RTU (RS-485)

| Requirement | Status | Evidence / Gap |
|---|---|---|
| RTU over RS-485/RS-232 serial interface | ✅ | `xedge/drivers/modbus/serial.py`, `rtu_codec.py` — clean-room codec, CRC-16, T3.5 timing |
| Configurable baud/parity/stop bits/slave ID | ✅ | `config/schema/drivers/modbus_rtu_serial.schema.json` — baud 1200–115200, parity none/even/odd, stop bits 1/2, unit_id 1–247 |
| Poll multiple slave devices, configurable intervals | 🟡 | Each driver **instance** is scoped to exactly one `unit_id`. Multiple slaves need multiple instances, but instances don't share a physical serial port — two instances configured against the same `/dev/ttyUSB0` will both try to open it independently (lock conflict / undefined behavior on a real multi-drop bus). No shared-bus multiplexer exists. |
| Prioritize write parameters vs read | ❌ | No scheduling priority anywhere in `polling.py`; writes execute ad hoc via write-back, competing with the poll loop for the same request lock with no priority ordering |
| On-Demand / On-Connect / Polling mechanisms | 🟡 | Only continuous Polling exists (`_poll_group` loops forever at `scan_rate_ms`). No On-Demand (read-on-request) or On-Connect (read-once-then-idle) mode |
| Combine multiple registers into a single value | ❌ | `_read_one` always reads `quantity = 1`; no 32/64-bit multi-register composition, no word/byte-order swap option |
| Multiple read & write parameters per device | 🟡 | Multiple read tags per group: yes. Write tags: not separately configurable — a tag becomes writable only if its read function code has a matching write function code (`_WRITE_FUNCTION_CODE_FOR_READ`); no dedicated write-only tag definition |
| Map FC01/02/03/04 (read) and FC05/06/15/16 (write) | 🟡 | FC01–04 read: ✅. FC05 (write single coil), FC06 (write single register): ✅. **FC16 (write multiple registers)**: codec exists but has no caller. **FC15 (write multiple coils)**: not implemented at all |
| Retry & error handling, meaningful error messages | 🟡 | Modbus exceptions map to `Quality.BAD` with the raw exception code in metadata — reasonable foundation. No configurable retry count, no consecutive-failure threshold, no human-readable exception name surfaced to the UI (raw numeric code only) |
| Serial port selectable from detected ports | ❌ | `config` schema takes a free-text `port` string; no port auto-detection/dropdown in the Web UI (`driver_new.html`) |
| Baud rate list (1200–115200) | ✅ | Schema enforces this exact range |
| Parity None/Even/Odd | ✅ | |
| Stop bits 1/2 | ✅ | |
| Slave ID 1–247, unique-on-bus validation | 🟡 | Range enforced; **uniqueness-on-bus validation not implemented** (nothing checks two instances on the same port don't share a slave ID) |
| RTS pre-transmit/post-transmit delay (µs) | ❌ | Not present anywhere; `pyserial-asyncio` doesn't expose RTS toggle timing out of the box — needs custom serial transport work for RS-485 converters that need it |

### 4.2 Modbus TCP/IP

| Requirement | Status | Evidence / Gap |
|---|---|---|
| Modbus TCP over Ethernet | ✅ | `xedge/drivers/modbus/tcp.py` |
| Configure IP/host, port, unit ID | ✅ | `modbus_tcp.schema.json` |
| Concurrent communication with multiple devices | ✅ | Multiple driver instances run independently under the supervisor |
| High-frequency polling | ✅ | `scan_rate_ms` floor is 50 ms in schema (customer literally asks for "min 1 nanosecond" — physically unachievable over TCP/IP; flagged in §9 for clarification) |
| Map registers to system parameters | 🟡 | Same gap as RTU — no multi-register combining, no named "parameter" abstraction, just raw tags |
| Prioritize write vs read | ❌ | Same gap as RTU |
| On-Demand / On-Connect / Polling | 🟡 | Same gap as RTU |
| Combine registers into single value | ❌ | Same gap as RTU |
| FC01/02/03/04/05/06/15/16 | 🟡 | Same gap as RTU (no FC15, FC16 unreachable) |
| Retry/error handling | 🟡 | Same as RTU; additionally no configurable connection-retry count |
| TCP host/port (default 502) | ✅ | |
| Unit Identifier 1–255, 0xFF direct | 🟡 | Schema allows 0–255 (should be 1–255 with 0xFF special-cased per spec — minor validation gap) |
| Persistent vs on-demand connection mode | ❌ | Driver always holds a persistent connection; no per-instance connection-mode choice |
| TCP keepalive interval & retry count | ❌ | Not implemented |
| TLS enabled/disabled, certificate pinning | ❌ | No TLS support in the Modbus TCP transport at all |

### 4.3 Polling Configuration

| Requirement | Status | Evidence / Gap |
|---|---|---|
| Configurable poll interval | 🟡 | 50 ms–24h enforced by schema; customer's "min 1 nanosecond" is not achievable on any real transport — needs clarification (§9) |
| Configurable request timeout | ✅ | `read_timeout_seconds` per instance |
| Configurable connection retry count | ❌ | Not present |
| Read batching / block read (fixed groups) | ❌ | Every tag is read individually; no request coalescing |
| Max batch/block size, configurable | ❌ | N/A — no batching exists to size |
| Retry-on-exception, configurable | ❌ | Not present |

### 4.4 Device Health & Availability

| Requirement | Status | Evidence / Gap |
|---|---|---|
| Consecutive-failure threshold → offline/Not Connected | ❌ | The generic supervisor (`xedge/core/supervisor.py`) restarts a crashed driver instance (NFR-R-006) but there is no per-slave "Not Connected" state model distinct from a hard restart, and no configurable failure-count threshold |
| Auto-recovery to active state | 🟡 | Implicit via supervisor restart-with-backoff, but not modeled as an explicit device availability state machine the UI can show |

### 4.5 EtherNet/IP Scanner

| Requirement | Status | Evidence / Gap |
|---|---|---|
| All items (cyclic/acyclic exchange, device config, I/O mapping, connection monitoring, read/write) | ❌ | **No EtherNet/IP driver exists in the codebase.** `xedge/drivers/` contains only `bacnet/`, `loopback/`, `modbus/`, `opcua/`. This is on the original HLR roadmap (FR-SB-005, Phase 4 / Sprints 19–26) but was never built — Phase 4 protocol work appears to have been superseded by the Fleet/Alarm/Write-back work actually delivered through Sprint 31. |

### 4.6 SNMP Client & Agent

| Requirement | Status | Evidence / Gap |
|---|---|---|
| All items (v1/v2/v3, TRAP/INFORM, MIB browsing, monitoring, read/write) | ❌ | **No SNMP support anywhere** — not in `xedge/drivers/`, not in the HLR, not a planned roadmap item. This is entirely net-new scope relative to xEdge's existing charter. |

### 4.7 SMTP Protocol Support

| Requirement | Status | Evidence / Gap |
|---|---|---|
| All items (SMTP send, server/auth/TLS config, event & scheduled email) | ❌ | No SMTP client anywhere. Good news: the Alarm Engine v2 (Sprint 31) already has the event/threshold detection and ack/shelve state machine this would hook into — the notification *trigger* logic exists, only the *email transport* is missing. |

### 4.8 SNTP Protocol Support

| Requirement | Status | Evidence / Gap |
|---|---|---|
| All items (SNTP sync, server/interval/timezone config, uniform timestamping) | ❌ | HLR only *assumes* (`ASM-001`) the host OS is NTP-synced — xEdge has no in-app SNTP client, server config, or sync-status reporting. Note: xEdge's internal timestamping (`FR-DP-001`) is already UTC/nanosecond-precision and sourced from the OS clock, so the underlying data model doesn't need to change — only an app-level SNTP client + config/status UI is missing. |

### 4.9 Gateway Provisioning and Configuration

| Requirement | Status | Evidence / Gap |
|---|---|---|
| Gateway Name | 🟡 | `DeviceRecord.display_name` exists (`xedge/fleet/registry.py`) — close enough conceptually, field name differs |
| Gateway Protocol | ❌ | No such field |
| Gateway Serial Number | ❌ | No such field |
| Gateway Make | ❌ | No such field |
| Gateway Firmware Version | 🟡 | `agent_version` exists but is the xEdge software version, not a hardware firmware version field |
| Connection State (Connected/Disconnected/Active/Inactive) | 🟡 | `DeviceRecord.status` computes `unknown/online/offline` from heartbeat age — a 2-state-ish model, not the customer's 4-state enum |
| Remote gateway config/updates from cloud, minimal initial setup | 🟡 | Fleet Manager already does this well: agent registers, manager pushes `pending_config`, device applies/reports — this is a strong existing foundation (`xedge/fleet/agent.py`, `manager_app.py`) |
| Initial onboarding with basic config only at device side | 🟡 | Same mechanism as above covers this reasonably |
| Upload/manage root/CA certificates via user account | ❌ | No certificate management UI or storage anywhere in `xedge/fleet/` or `xedge/security/` (the `security/` package is currently empty scaffolding) |
| Secure certificate deployment to gateways | ❌ | Same gap — no PKI distribution pipeline exists yet (this is also HLR `SR-AA-005`/`SR-TS-004`, itself not yet built despite being Phase-3-scoped) |
| All subsequent config/management via cloud platform only | 🟡 | Architecturally consistent with the existing Fleet Manager design intent |

### 4.10 MQTT

| Requirement | Status | Evidence / Gap |
|---|---|---|
| **Subscriber** — receive data from external sources, configurable broker/topic/port/QoS, TLS, auth, payload parsing/mapping | ❌ | `xedge/northbound/mqtt.py` only *subscribes* to its own Sparkplug B NCMD topic for write-back (Sprint 31) — there is no general-purpose subscriber that ingests external MQTT data into tags |
| **Publisher** — configurable topics/payload, real-time & event-driven, configurable interval/triggers, own payload structure, manual republish | 🟡 | Publisher exists but is **hard-wired to Sparkplug B** (`sparkplug/payload.py`) — fixed schema, not the customer's "configure own payload structure." No manual re-publish trigger. |
| **Broker** — accept client connections, diagnostics, config | ❌ | No embedded broker. `amqtt` is present only as a **test-only dependency** (`pyproject.toml`: "never imported by xedge itself... not shipped in any edition") — a deliberate prior decision that needs revisiting (build-vs-buy ADR, §9). |
| Broker Address, Port, Client ID, Keep Alive, Topic List, QoS | ❌ (broker) / 🟡 (publisher config) | Publisher has host/port/client_id/keepalive/QoS (`SparkplugConnectorConfig`); no topic-list concept since Sparkplug B derives topics from group/node/device IDs |
| Authentication (username/password) | 🟡 | Fields exist in `SparkplugConnectorConfig` but are unused/unwired in practice for the write-back path — needs verification and hardening |
| SSL/TLS, certificate upload/path | ❌ | **No TLS in the MQTT connector at all.** The module docstring explicitly defers this to "Sprint 13 scope" — Sprint 13 has passed (current: Sprint 31) and it was never delivered. This is a real, load-bearing security gap independent of the customer's ask. |
| Payload Type/Format selection, Payload Structure preview/schema view | ❌ | Sparkplug B is the only payload; no configurable payload type, no schema preview UI |

### 4.11 Basic Asset Management

| Requirement | Status | Evidence / Gap |
|---|---|---|
| Asset Name, Serial Number, Type, Make, Firmware Version, Description, Alias, Units | ❌ | **No "Asset" entity exists in the data model at all.** xEdge is organized as `Driver instance → tag_group → tag` — there is no entity above a tag/driver representing a physical field device with its own identity/metadata. |
| Enable/Disable Asset | ❌ | No such concept (drivers can be started/stopped, but that's not asset-level) |
| Assign asset to a gateway, Asset Connection State | ❌ | No mapping layer exists; "gateway" itself is only the Fleet Manager's `DeviceRecord`, not linked to any asset concept |
| Parameter List per asset (multiple parameters) | 🟡 | Conceptually covered by existing `tag_groups`/`tags`, but not asset-scoped — a tag belongs to a driver, not an asset |
| Storage Requirement (store data or not) per parameter | 🟡 | Retention/storage is configured at the tag-group level via store-and-forward, not per individual parameter with a simple on/off toggle |
| Centralized protocol configuration interface | 🟡 | The Web UI already has a decent driver-config pattern (`driver_new.html`/`driver_edit.html`) covering IP/port/baud/device-ID per protocol — reusable, but not "asset-aware" |
| Validate configuration before deployment | ✅ | JSON Schema validation on config apply is a strong existing pattern (`FR-CM-005`, `xedge/core/hot_reload.py`) — this generalizes well to new driver/asset schemas |

---

## 5. Additional Development Required — Consolidated Backlog

1. **Modbus — bring to full spec compliance**
   - Shared-serial-port multiplexer for multi-slave RTU buses
   - RS-485 RTS pre/post-transmit delay support (custom serial transport)
   - Read batching / block-read grouping with configurable max batch size
   - Multi-register data types (int32/uint32/float32/float64, byte/word-order swap) — "combine registers"
   - Dedicated write-tag configuration + FC15 (write multiple coils) + wire up existing-but-unused FC16
   - Write-priority scheduling ahead of read polling
   - On-Demand and On-Connect polling modes (today: Polling only)
   - TCP: persistent-vs-on-demand connection mode, keepalive interval/retry count, TLS + cert pinning
   - Device/slave health state machine: consecutive-failure threshold → Not Connected, auto-recovery, human-readable exception messages
   - Slave-ID-uniqueness-on-bus validation; serial port auto-detect dropdown in UI

2. **EtherNet/IP Scanner (new driver)** — CIP originator: cyclic (implicit I/O) + acyclic (explicit messaging), device/connection config, I/O mapping, fault handling, read/write. Recommend evaluating a permissively-licensed CIP library (e.g. `pycomm3`/`cpppo`) vs. the in-house clean-room approach used for Modbus (ADR-006 precedent) — a licensing/build-vs-buy ADR is a prerequisite.

3. **SNMP Client & Agent (new module)** — v1/v2c/v3 (incl. USM auth/priv) client GET/GETNEXT/GETBULK/SET; SNMP agent so xEdge itself is pollable; TRAP + INFORM sender (wired to the existing Alarm Engine) and receiver; MIB upload/parse/browse UI.

4. **SMTP (new module)** — SMTP client with SSL/TLS + auth, wired into the existing Alarm Engine v2 as a notification channel, plus scheduled-report triggering.

5. **SNTP (new module)** — multi-server SNTP client, sync interval/timezone config, sync-status reporting in the UI.

6. **Gateway Provisioning** — add Gateway Serial Number/Make/Protocol fields and a proper 4-state Connection State to `DeviceRecord`; build a certificate management subsystem (upload root/CA certs per account, secure distribution to gateways) — this should be built once and **shared** with MQTT's TLS need (item 7) rather than duplicated.

7. **MQTT full buildout** — generic Subscriber (external broker, arbitrary topic/payload → tag mapping); Publisher payload templating (beyond fixed Sparkplug B) with manual republish; embedded Broker with client diagnostics (build-vs-buy ADR — revisit the existing "amqtt is test-only" decision); TLS/mTLS across client and broker, sharing the cert-management subsystem from item 6.

8. **Basic Asset Management (new data-model layer)** — introduce "Asset" as a first-class entity above tag/driver; asset↔gateway mapping with connection state; parameter list with per-parameter storage toggle; centralized cross-protocol config screen. **Recommend an ADR** deciding whether Asset becomes a new top-level layer (asset owns one-or-more driver instances/tag groups) or an annotation on existing tag_groups — this is a real architecture decision, not just a form to add.

---

## 6. Effort & Cost Estimate

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
| + Integration, ADRs (EtherNet/IP library choice, MQTT broker build-vs-buy), cross-module regression, docs (~12%) | | | | **+37** |
| **Total** | | | | **≈ 342 person-days** |

**Calendar time:** with a dedicated pod of 3 backend/protocol engineers + 1 frontend engineer + 1 QA engineer (matching this project's existing role mix), backend is the critical path at 200d ÷ 3 ≈ 67 person-days per engineer ≈ **7 sprints (14 weeks, ~3.5 months)**. Adding the ~12% integration/ADR tail pushes this to **~8 sprints (16 weeks, ~4 months)**.

**Cost @ $30/hr blended ($240/person-day):**

| | Person-days | Cost |
|---|---:|---:|
| Point estimate | 342 | **≈ $82,000** |
| ROM range (±15–20% contingency) | 305–390 | **≈ $75,000 – $95,000** |

> Swap the $240/person-day figure for your actual blended rate to reprice — the person-day column is rate-independent.

---

## 7. Suggested Delivery Roadmap

| Sprint(s) | Focus | Rationale |
|---|---|---|
| 1–2 | Modbus full-spec enhancement (batching, multi-register types, write-priority, health state machine) + shared cert-management subsystem (foundation for #3, #6) | Highest customer-visible gap on the protocol they already use most; cert subsystem unblocks two later items |
| 3–4 | Gateway Provisioning completion + MQTT TLS/mTLS (reuses cert subsystem) + MQTT Publisher payload templating | Security-adjacent items grouped to avoid building PKI twice |
| 5–6 | Basic Asset Management (data model + UI) — do this **before** EtherNet/IP and SNMP so new drivers can be asset-aware from day one rather than retrofitted | Avoids rework: retrofitting asset-awareness onto 3 new drivers costs more than building it once, first |
| 6–7 | EtherNet/IP Scanner | New driver, reuses the existing plugin framework |
| 7–8 | SNMP Client & Agent, MQTT Subscriber + Broker | Parallelizable — independent of each other |
| 8 | SMTP + SNTP | Smallest items; slot in wherever a pod has slack |

---

## 8. Key Risks & Recommendations

1. **MQTT TLS is a pre-existing, undocumented gap**, not just a customer-ask — the connector has shipped without transport security since at least Sprint 13's original target. Recommend flagging this internally regardless of this engagement's outcome.
2. **EtherNet/IP and SNMP licensing/build-vs-buy needs an ADR before estimation firms up** — xEdge's precedent (ADR-006) is clean-room in-house builds for core protocols but "buy" for BACnet (`bacpypes3`, MIT). A similar buy-path for EtherNet/IP (e.g. `pycomm3`) and SNMP (`pysnmp`) would materially reduce the 200 backend-days above; a clean-room requirement would roughly double the EtherNet/IP and SNMP lines.
3. **MQTT Broker build-vs-buy is an open decision** — `amqtt` already exists in the dependency tree but was deliberately scoped test-only. Revisit that decision explicitly; embedding it (if its license is acceptable for the target edition) is materially cheaper than a clean-room broker.
4. **Asset Management is an architecture decision, not a form** — recommend an ADR before coding starts, and sequencing it ahead of the three new protocol drivers (§7) to avoid retrofitting.
5. **The "1 nanosecond" minimum poll interval is not achievable** on any real transport (TCP/serial round-trips are millisecond-scale at best) — needs a clarifying conversation with the customer; recommend a practical floor (e.g. 1–10 ms) instead.
6. **Device Health & Availability and Gateway Connection State should be unified** — the customer's document describes very similar 2–4-state connectivity models in three separate places (Modbus device health, Gateway connection state, Asset connection state). Recommend one shared "connectivity state machine" component reused across all three rather than three separate implementations — reduces the estimate above if adopted (not currently reflected as a discount).

---

## 9. Open Questions for the Customer

1. The requirement doc references protocols shown "in the image" as excluded from Phase 1 — no image was attached. Please confirm the exclusion list so Phase 1 scope (and this estimate) can be tightened.
2. Confirm the practical minimum poll interval (the literal "1 nanosecond" spec is not implementable).
3. For EtherNet/IP and SNMP: is a permissively-licensed third-party library acceptable (faster, cheaper), or does your licensing model require clean-room in-house implementation like xEdge's Modbus/Sparkplug B stacks (ADR-006)?
4. For MQTT Broker: is embedding an existing broker library acceptable, or is a from-scratch implementation required for licensing reasons?
5. For Basic Asset Management: should "Asset" become the primary configuration entity (operator configures an Asset, which owns one or more driver/tag mappings underneath), or an additional metadata layer on top of the existing driver-first model? This materially changes both cost and the resulting UX.
