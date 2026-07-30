# XEDGE-CRD-001 — Delivery 1 Handover Package

**Document ID:** XEDGE-CRD-001-H1
**Version:** 1.0
**Status:** Final
**Date:** 2026-07-30
**Prepared for:** Handover of Delivery 1 (Sprints 0, C1–C8, H1) against the
customer requirement document XEDGE-CRD-001.
**Story:** XEDGE-494 (Sprint H1)

This package is the single entry point for handover. It states what was
delivered, what remains genuinely open, and the specific statements the
delivery plan (`docs/planning/crd-delivery-plan.md`) required to be made
explicitly here rather than discovered later. It references, rather than
duplicates, two companion documents:

- [XEDGE-CRD-001-gateway-compliance-report.md](../requirements/XEDGE-CRD-001-gateway-compliance-report.md) — the full requirement-by-requirement compliance matrix (v2.0, revised from the pre-delivery gap analysis).
- [crd-delivery-plan.md](crd-delivery-plan.md) — the sprint-by-sprint delivery record, PR links, and per-sprint technical notes.

---

## 1. Delivery summary

All 8 requirement areas in the customer requirement document were built,
across 10 sprint-cycles (Sprint 0 + C1–C8 + H1, 2026-07-27 → 2026-07-30)
and 15 pull requests (`sgbhavsar-cpu/XEdge` PRs #2–#15, plus this
package's own PR closing Sprint H1). Headline capabilities delivered:

- **Modbus** RTU/TCP/RTU-over-TCP at full protocol-level spec (batching, multi-register types, write priority, on-demand/on-connect polling, per-transport scan-rate floors) — one open gap (Modbus TCP transport TLS) and one partial item (FC15 write-multiple-coils has no runtime caller yet).
- **EtherNet/IP Scanner** (`pycomm3`) — explicit messaging at a scan interval, customer-accepted as satisfying "cyclic exchange" (no mainstream Python CIP library implements true Class 1 I/O).
- **SNMP** (`pysnmp`) — client (v1/v2c/v3, GET/GETNEXT/GETBULK/SET), agent (v1/v2c, read-only), TRAP/INFORM originator wired to the alarm engine, TRAP/INFORM receiver. MIB upload/parse/browse deliberately descoped.
- **SMTP** — alarm notifications and scheduled reports, TLS/STARTTLS, wired to the same alarm engine as SNMP notifications.
- **SNTP** — multi-server time-sync status reporting (query-only; does not set the system clock).
- **Gateway Provisioning** — full metadata model, a 4-state connection model, and a real certificate-management subsystem (fleet CA, join-token enrollment, automatic rotation).
- **MQTT** — Subscriber (external broker → tags), configurable Publisher (Sparkplug B or generic JSON), embedded Broker (`amqtt`, TLS + auth + ACLs), manual republish trigger.
- **Basic Asset Management** — a metadata/grouping layer (ADR-010) over existing driver tags, spanning every protocol above.
- **Cross-protocol integration** (Sprint H1, XEDGE-490) — Modbus, EtherNet/IP, SNMP, BACnet, and OPC UA driver instances verified running concurrently, including an Asset whose parameters span all five.
- **Performance validation** (Sprint H1, XEDGE-492) — batched Modbus throughput confirmed to hold at 100 tags/1 request per cycle; scan-rate accuracy confirmed stable (no drift) under 5-instance concurrent load.

See the [compliance report](../requirements/XEDGE-CRD-001-gateway-compliance-report.md) §3–4 for the full requirement-by-requirement matrix.

---

## 2. Explicit handover statements

These four statements are required verbatim by the delivery plan
(`crd-delivery-plan.md`, Sprint H1 section: *"these must be in the
handover package, not discovered later"*). Each is stated here plainly,
with a pointer to where it is designed and enforced in the codebase.

> **Central management is single-tenant, API-only, no dashboard (ADR-013 §2).**
> The Fleet Manager (`xedge/fleet/manager_app.py`) exposes only a REST
> API — no HTML dashboard routes exist. A customer expecting a
> browser-based fleet console at handover should be corrected to this
> expectation now (delivery-plan risk R-CRD-08): what exists is scriptable
> device registration, heartbeat monitoring, and config push/pull, not a
> visual multi-tenant console.

> **OTA is not delivered in Delivery 1; when it arrives it updates the
> application container, not the host OS or kernel (ADR-013 §7).**
> No over-the-air update mechanism of any kind ships in this delivery.
> When built in a later delivery, its scope is the xEdge application
> container image, not host OS patching, kernel updates, or firmware —
> stated now so it is not assumed to cover more than it will.

> **RS-485 cannot meet a 1ms poll floor; per-transport minimums apply
> (Decision D-10).** The schema floor was lowered from 50ms to 1ms
> (Sprint C1, XEDGE-413) at the customer's request (Q-2, resolved
> 2026-07-26), but 1ms is not achievable on a real RS-485 link — the
> actual achievable floor depends on baud rate, frame size, and how many
> tags share a poll cycle. `xedge/drivers/modbus/serial.py`'s
> `minimum_scan_interval_seconds()` computes the real floor for a given
> configuration and logs a warning naming the achievable rate when a
> configured `scan_rate_ms` can't be met — it does not silently overrun.
> EtherNet/IP and SNMP schema floors were **not** lowered from 50ms, since
> sub-50ms rates were never verified achievable for either.

> **Any protocol area verified by simulator only, if the HIL pass could
> not cover it (Decision D-20).** Stated in full in §4 below — no
> physical field hardware (Modbus PLCs, a ControlLogix/CompactLogix,
> BACnet/OPC UA/SNMP equipment) was available in any development
> environment used across this delivery. Every protocol except
> EtherNet/IP was verified against a real (if minimal) open-source
> server/agent implementation of its own protocol — not a hand-rolled
> mock of xEdge's own expectations. This is real protocol-level
> verification, but it is not the same claim as verification against the
> customer's own field devices, and this package does not overstate it
> as one.

---

## 3. Known limitations

Organized by area. Every item below has a citation to the code or a
decision record — none of these are speculative; each was found and
recorded during implementation or verification, not surfaced for the
first time in this document.

### Protocol-level

| Area | Limitation | Reference |
|---|---|---|
| Modbus TCP | No transport TLS/mTLS; network-level isolation is the only mitigation today | Compliance report §4.2 |
| Modbus (RTU+TCP) | FC15 (write multiple coils): codec implemented and tested, no runtime caller — `write()`'s API is single-tag | Compliance report §4.1 |
| Modbus RTU | RTS pre/post-transmit delay is implemented but **never verified against real RS-485 hardware** — no such hardware was available | Compliance report §4.1, open item Q-4 |
| EtherNet/IP | "Cyclic exchange" is explicit messaging at a scan interval, not true CIP Class 1 implicit I/O — no mainstream Python CIP library implements it | Q-7, resolved 2026-07-28 |
| EtherNet/IP | No real-server integration test exists — the only protocol in this delivery tested against a mocked library boundary (`pycomm3.LogixDriver`) rather than real wire traffic, because no compatible open CIP simulator was found | `tests/unit/test_ethernet_ip_client.py` |
| EtherNet/IP | `pycomm3` is flagged by its own maintainers as no longer actively developed — a supportability risk, not a current functional gap | `license-audit.md` item 7 |
| SNMP (agent role) | xEdge itself, as a managed device, is pollable via v1/v2c only, never v3, and is **read-only** (no SET support) — the client/manager role fully supports v3 | Compliance report §4.6 |
| SNMP (agent role) | Every custom OID uses a placeholder Private Enterprise Number (`1.3.6.1.4.1.999999`) — **must be replaced with a real, IANA-assigned PEN before use against a customer's production NMS** | Q-8, resolved 2026-07-30 |
| SNMP | MIB upload/parse/browse was not built — tags use raw numeric OIDs only | §5 below (deferred item) |
| SNMP TRAP | `notify_type: trap` is fire-and-forget UDP (RFC 1905) by protocol definition — a successful send only confirms the packet left this device, never that it arrived. Use `notify_type: inform` where delivery confirmation matters | Confirmed against a real unreachable destination, Sprint C8 notes |
| MQTT (embedded broker) | Cannot enforce mandatory client certificates (mTLS) — `amqtt` always sets `ssl.CERT_OPTIONAL`, never `CERT_REQUIRED`, regardless of configuration. Client-side mTLS (publisher/subscriber connecting *out*) is unaffected and fully supported | Verified against amqtt's own source |
| MQTT (embedded broker) | Publish/subscribe ACLs are asymmetric: once either list is non-empty, an empty `subscribe_acl` means zero subscribe access for everyone, not unrestricted | `xedge/northbound/mqtt_broker.py` module docstring |
| MQTT (subscriber) | A wildcarded topic (`+`/`#`) maps every matching concrete topic to the same fixed `tag_id` — no dynamic per-topic tag fan-out | Driver docstring |
| Gateway Provisioning | The certificate subsystem is xEdge's own self-generated, self-managed fleet CA — there is no workflow for importing a customer-supplied external root/CA certificate | Compliance report §4.9 |

### Cross-cutting

| Area | Limitation | Reference |
|---|---|---|
| Connectivity state | `Connected`/`Degraded`/`Not Connected` is only computed by the Modbus and SNMP-client driver families. An Asset or Gateway backed solely by EtherNet/IP, OPC UA, BACnet, or MQTT-subscriber tags reports `Unknown` for this signal regardless of actual health — by documented design (`xedge/core/assets.py` docstring), not omission. | `xedge/core/connectivity.py` |
| Connectivity state (concrete case, found in Sprint H1) | **An Asset whose parameters span a connectivity-aware protocol (Modbus, SNMP) and a connectivity-unaware one (BACnet, OPC UA, EtherNet/IP) will show Degraded, never Connected** — confirmed by `tests/integration/test_cross_protocol_integration.py::test_asset_connection_state_across_five_protocols_is_degraded_not_connected` against a real multi-protocol `DriverSupervisor`, not a hand-built literal. This is a direct, previously-untested consequence of an existing, deliberate C6 design choice (`compute_asset_connection_state`'s own unit tests already covered the abstract two-value case) — not a new decision made in H1, but its first concrete demonstration. | XEDGE-490 |
| Asset storage toggle | `AssetParameter.store`'s granularity is per cold-store stream (`source_driver`), not per individual tag — ring buffers aren't yet keyed finer than that | ADR-010 §3 |
| Web UI | `alarms.rules`, `mqtt_broker.publish_acl`/`subscribe_acl`, `smtp.alarm_notifications`/`scheduled_reports`, and `snmp_notify.destinations` are all edited via the raw-YAML "Advanced" editor, not a dedicated form — `xedge.api.schema_forms` has no widget for array or dynamic-key-object schema types. **The `alarms.rules` case was found and fixed during Sprint H1 documentation work**: it previously rendered as a non-functional plain-text input (silently data-corrupting on save) rather than being properly routed to the Advanced editor like the other three — now consistent. | `xedge/api/config_ui.py::_SKIP_ALARMS_MANAGED_FIELDS` |
| Services requiring a restart | The embedded MQTT broker and the SNMP agent both bind a listening socket once, at process startup. Enabling either via hot-reload does not retroactively start it — a process restart is required. | Configuration Guide §Hot-reload |
| Performance: HIL pass (XEDGE-491) | **Not performed against physical field hardware — no such hardware was available in any development environment used on this delivery.** Every protocol except EtherNet/IP was instead verified against a real open-source server/agent implementation of its own protocol (see §4 for the full list); EtherNet/IP against a mocked library boundary. This is the D-20/R-CRD-02 statement from §2, restated here for completeness. | R-CRD-02, Q-6, resolved 2026-07-30: document as simulator-only, proceed |
| Performance: embedded broker RAM footprint (part of XEDGE-492) | **Not measured against the ADR-007 1GB ARM target** — no ARM hardware or emulated target was available in any development environment used on this delivery. `psutil` (an `amqtt` transitive dependency) does ship prebuilt ARM64 wheels but **not** armv7 ones (compiles from source there); this was reasoned as likely covered by the existing build toolchain, not confirmed by an actual armv7 build with `amqtt` in the tree. | `license-audit.md` §4 item 6, folded into the same open item (Q-6) as the HIL pass |
| Scope assumption | The customer requirement document's reference to protocols "excluded... in the image" was never resolved — the image was never received. Every one of the 8 CRD areas was built as fully in-scope, per the risk register's own stated fallback. This is a working assumption maintained for the entire delivery, not a customer sign-off. | Q-1, R-CRD-01 — **still open at handover** |

---

## 4. What "verified" means for each protocol

Every protocol in this delivery except EtherNet/IP is tested against a
real, if minimal, open-source implementation of its own protocol — a
genuine server/agent/broker exchanging real wire traffic with xEdge's own
driver code — not a hand-rolled stand-in for what xEdge itself expects to
see:

| Protocol | Test double | Real wire traffic? |
|---|---|---|
| Modbus TCP/RTU | In-house `FakeModbusServer` | Yes |
| OPC UA | `asyncua.Server` | Yes |
| BACnet/IP | `bacpypes3.app.Application` (the same library the driver itself uses, in device/server role) | Yes |
| MQTT (subscriber/publisher/broker) | `amqtt` (real broker) | Yes |
| SMTP | `aiosmtpd` | Yes |
| SNTP | A minimal in-house SNTP server fixture | Yes |
| SNMP (client, agent, TRAP originator, TRAP receiver) | `pysnmp`'s own agent/manager/notification-originator/notification-receiver API, used as a mutual test oracle | Yes |
| **EtherNet/IP** | Mocked `pycomm3.LogixDriver` boundary | **No** — no compatible open-source CIP simulator implements the Logix symbol-table object `pycomm3` requires |

Sprint H1 (XEDGE-490) additionally verified all of Modbus, EtherNet/IP,
SNMP, BACnet, and OPC UA running **concurrently** under one supervisor,
including an Asset whose parameters reference tags from all five —
proving the concurrent-multi-protocol scenario a single-protocol test
suite cannot, and surfacing the Degraded-not-Connected finding in §3.

None of the above is a substitute for interoperability testing against a
customer's actual field devices — see §2's D-20 statement and §3's HIL
row.

---

## 5. Deferred item register

Items deliberately not built in this delivery, each traceable to a
decision made in advance (the delivery plan's own pre-agreed scope-cut
list) rather than discovered as a shortfall at handover:

| Item | Why deferred | Cost to add later | Reference |
|---|---|---|---|
| SNMP MIB upload/parse/browse (XEDGE-485, part of XEDGE-486) | Scope-cut candidate 2 ("operator convenience only") — invoked at the start of Sprint C8 | ~6 days (v1.0 ROM estimate) | `crd-delivery-plan.md` §5, Sprint C8 notes |
| EtherNet/IP L5X import / runtime tag discovery | Scope-cut candidate 4 ("commissioning convenience," not required for symbolic-tag read/write once a tag name is known) | ~4 days (v1.0 ROM estimate) | `crd-delivery-plan.md` §5, Sprint C7 notes |
| FC15 (Modbus write multiple coils) runtime caller | Codec built and tested in Sprint C2; no bulk-write concept existed in `write()`'s single-tag API to call it from, and no story required it | Small — the codec is done; needs a bulk-write API surface | Compliance report §4.1 |
| Modbus TCP transport TLS | Never scoped as a story in the delivery plan; network-level isolation was the assumed mitigation throughout | Comparable to the Modbus RTU/TCP work already done, since the codec/pipeline layers are transport-agnostic | Compliance report §4.2 |
| SNMP agent v3 support / write (SET) access | Descoped implicitly — no story asked for it; v1/v2c-read was accepted as sufficient for a device to be *monitored* | Moderate — the client driver's own USM code (`AUTH_PROTOCOLS`/`PRIV_PROTOCOLS`) is directly reusable | Compliance report §4.6 |
| Gateway "import your own PKI" workflow | Not built; a self-managed fleet CA was built instead, satisfying "secure certificate deployment" without satisfying "bring your own root CA" if that was the literal intent | Moderate — the CA/CSR machinery in `xedge/security/` would need an "external CA" mode alongside the self-generated one | Compliance report §4.9 |
| Fleet Manager dashboard (HTML console) | Not scoped — ADR-013 §2 fixed this delivery as API-only, single-tenant, by design | Substantial — a new UI surface, not an extension of the existing one (the device-local Web UI's templates aren't reusable for a multi-tenant central console) | §2 above, R-CRD-08 |

Also not cut, and confirmed still delivered in full despite being on the
original scope-cut candidate list as an *option* to cut if the schedule
required it: the embedded MQTT broker (candidate 3) and the asset
centralized-config view being kept simple as a reference/grouping page
rather than a unified cross-protocol editor (candidate 5, and separately
customer-confirmed via Q-3) — the latter is a design choice, not an
unbuilt feature, so it is not listed as deferred above.

---

## 6. Compliance summary

Condensed from the full [compliance report](../requirements/XEDGE-CRD-001-gateway-compliance-report.md) §3:

| Requirement Area | Verdict |
|---|---|
| Modbus RTU (RS-485) | ✅ Compliant (RTS delay unverified against hardware) |
| Modbus TCP/IP | 🟡 Partial (no transport TLS) |
| Polling configuration | ✅ Compliant |
| Device health & availability | ✅ Compliant |
| EtherNet/IP Scanner | 🟡 Partial, customer-accepted (explicit messaging, not implicit I/O) |
| SNMP Client & Agent | 🟡 Partial, customer-accepted (agent: v1/v2c read-only; MIB browsing descoped) |
| SMTP | ✅ Compliant |
| SNTP | ✅ Compliant |
| Gateway Provisioning & Configuration | 🟡 Partial (self-managed CA only, not import-your-own-PKI) |
| MQTT (Subscriber/Publisher/Broker) | 🟡 Partial (broker can't enforce mandatory mTLS) |
| Basic Asset Management | ✅ Compliant, by documented design choice (ADR-010, customer-confirmed) |

**Legend:** ✅ Compliant · 🟡 Partial (a specific, named gap remains, listed in §3 above)

---

## 7. Items requiring customer acknowledgment at sign-off

1. The four **explicit handover statements** in §2.
2. **Q-1/R-CRD-01 remains open**: the "protocols in the image" exclusion list was never received; this delivery built all 8 CRD areas as in-scope. Recommend either explicit written confirmation that this is correct, or a scoped conversation if any area should not have been built.
3. **HIL / field-hardware interoperability is unverified** (§2, §3, §4) — recommend scheduling a hardware validation pass before this system is relied on for unattended production control, even though protocol-level correctness was verified against real (non-xEdge) implementations for every protocol except EtherNet/IP.
4. The **SNMP agent's placeholder Private Enterprise Number** (§3) must be replaced with a real, IANA-assigned PEN before this device is monitored by a customer's production NMS.
