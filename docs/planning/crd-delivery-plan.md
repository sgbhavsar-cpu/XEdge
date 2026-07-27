# xEdge — Delivery 1 Sprint Plan (XEDGE-CRD-001)

**Document ID:** XEDGE-PLAN-004
**Version:** 1.0
**Status:** Active
**Date:** 2026-07-26
**Window:** 2026-07-27 → 2026-12-06
**Decision record:** [XEDGE-DR-001](XEDGE-DR-001-delivery-decisions.md)
**Parent plan:** [development-plan.md](development-plan.md)

Story IDs use the **XEDGE-4xx** range to keep this backlog distinct from
the v1.0 backlog in [sprint-planning.md](sprint-planning.md) (XEDGE-001..300).

Sprints are two calendar weeks. Estimates are **scope-size person-days**
carried from XEDGE-CRD-001 §6 — they are not a resource plan
(XEDGE-DR-001 D-03).

---

## 1. Delivery summary

| Sprint | Dates | Focus | CRD area | Est. (d) |
|---|---|---|---|---:|
| **0** | Jul 27 – Aug 02 | Stabilization | — | 6 |
| **C1** | Aug 03 – Aug 16 | Modbus core: scheduler, batching, data types | §4.1–4.3 | 22 |
| **C2** | Aug 17 – Aug 30 | Modbus writes, connectivity state machine, health | §4.1, §4.4 | 24 |
| **C3** | Aug 31 – Sep 13 | RS-485 bus manager, serial/TCP hardening, SNTP | §4.1, §4.2, §5 | 29 |
| **C4** | Sep 14 – Sep 27 | Certificates, MQTT TLS, gateway provisioning | §4.9, §4.10 | 41 |
| **C5** | Sep 28 – Oct 11 | MQTT subscriber, payload templating, broker | §4.10 | 40 |
| **C6** | Oct 12 – Oct 25 | Asset Management, SMTP | §4.11, §4.7 | 48 |
| **C7** | Oct 26 – Nov 08 | EtherNet/IP Scanner | §4.5 | 36 |
| **C8** | Nov 09 – Nov 22 | SNMP client, agent, traps, MIB | §4.6 | 48 |
| **H1** | Nov 23 – Dec 06 | Integration, HIL, docs, handover | all | 25 |
| | | | **Total** | **319** |

Against XEDGE-CRD-001's ~342-day estimate. The reduction is the shared
connectivity state machine and shared certificate subsystem being built
once rather than three and two times respectively (ADR-011, ADR-013 §4) —
the discount §8.6 of that report anticipated but did not apply.

---

## 2. Sprint 0 — Stabilization (Jul 27 – Aug 02, 1 week)

**Goal:** green CI on all four jobs. Nothing else merges until this does.
**Exit criterion:** `gh run list` shows a fully green run on `main`.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-400 | 0.5 | Fix 29 ruff violations (27× E501, 1× F401, 1× I001). Finding F-2 |
| XEDGE-401 | 0.5 | Pin dev toolchain to exact versions in `pyproject.toml` — `ruff`, `mypy`, `bandit`. Finding F-5 |
| XEDGE-402 | 1 | Introduce a single `data_dir` config root; derive `store.directory`, `config_management.history_directory`, webui and fleet paths from it. Fixes the 4 `/data` test failures at the cause rather than the symptom. Findings F-3, F-9 |
| XEDGE-403 | 0.5 | Add `build-essential` + `libffi-dev` to the Dockerfile builder stage so `cffi` compiles on armv7. **First armv7 image ever produced.** Finding F-4, decision D-18 |
| XEDGE-404 | 1 | Fix cold-store replay orphan: add `SqliteColdStore.stream_keys()` and enumerate from the store, not from ring-buffer keys. Finding F-7 |
| XEDGE-405 | 1 | Correct `README.md` — shipped protocols only, plus an implementation-status matrix. Decision D-29 |
| XEDGE-406 | 0.5 | Annotate `HLR.md` with per-requirement implementation status. Decision D-30 |
| XEDGE-407 | 0.5 | Mark `sprint-planning.md` superseded with a mapping to this plan. Decision D-28 |
| XEDGE-408 | 0.5 | Enable branch protection on `main`; require green CI on PR. Decision D-19 |

**Not in scope:** the session-token clock-granularity test failure is
Windows-only and does not affect CI (Linux). Logged, not fixed.

---

## 3. Delivery 1 sprints

### Sprint C1 — Modbus core (Aug 03 – Aug 16)

**Goal:** the scan rate means what it says, and a tag group is one
transaction rather than N.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-410 | 5 | Transport-neutral **fixed-period scheduler**: schedule next-due-time rather than sleeping after work. Fixes scan-rate drift (F-6). Foundation for ADR-011 Part 2 |
| XEDGE-411 | 6 | **Block-read batching**: coalesce contiguous addresses within a tag group into single FC01/02/03/04 requests; configurable max batch size; correct splitting at non-contiguous boundaries and at the protocol's 125-register / 2000-coil limits |
| XEDGE-412 | 5 | **Multi-register data types**: int32/uint32/int64/uint64/float32/float64 with configurable word order and byte order. CRD's "combine multiple registers into a single value" |
| XEDGE-413 | 2 | Lower poll-interval floor to 1 ms with **per-transport minimums** documented; RS-485 floor derived from baud rate and frame size. Decision D-10. **Q-2 resolved — customer accepted the RS-485 limitation (2026-07-26)** |
| XEDGE-414 | 2 | Configurable retry count and retry-on-exception per instance |
| XEDGE-415 | 2 | Web UI + schema: batching, data type, word/byte order, retry fields on the Modbus driver forms |

**Risk:** XEDGE-411 changes the read path every Modbus test depends on.
Batching correctness at boundaries is where this sprint can silently go
wrong — property-based tests over address layouts are worth the time here.

**Customer input needed by sprint end:** ~~open item Q-2 (poll-floor
acceptance, and that RS-485 cannot reach 1 ms)~~ — **resolved 2026-07-26,
accepted.**

---

### Sprint C2 — Modbus write path and device health (Aug 17 – Aug 30)

**Goal:** writes are first-class and prioritised; device availability is a
modelled state, not an inference.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-420 | 5 | **Shared connectivity state machine** (`xedge/core/connectivity.py`) — `UNKNOWN`/`CONNECTED`/`DEGRADED`/`NOT_CONNECTED` with asymmetric failure and recovery thresholds. ADR-011 Part 3. **Built once; three consumers** |
| XEDGE-421 | 3 | Modbus device-health adapter over XEDGE-420: consecutive-failure threshold → Not Connected, auto-recovery. CRD §4.4 |
| XEDGE-422 | 4 | **Dedicated write-tag configuration** — write-only tags, independent of a read function code's implied write |
| XEDGE-423 | 3 | **FC15** (write multiple coils) implementation; wire up the existing-but-uncalled **FC16** (write multiple registers) |
| XEDGE-424 | 4 | **Write-priority scheduling**: two-band priority queue, writes drain ahead of pending reads; per-instance configurable. ADR-011 Part 2 |
| XEDGE-425 | 2 | Human-readable Modbus exception names surfaced to the UI (currently raw numeric code only) |
| XEDGE-426 | 3 | **Extend the pipeline config builder beyond Modbus** so OPC UA and BACnet tags receive scaling, deadband and engineering units. Finding F-12 |

---

### Sprint C3 — RS-485 bus manager, hardening, SNTP (Aug 31 – Sep 13)

**Goal:** multi-drop RS-485 works. This is the largest functional gap in
the customer's primary protocol.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-430 | 3 | **`pty`-based fake-serial test fixture.** Prerequisite, not an afterthought — the bus manager is untestable without it, and it retroactively fixes the RTU driver's 23% coverage. Finding F-8 |
| XEDGE-431 | 8 | **`SerialBusManager`**: port-level arbiter owning the serial handle; instances stay one-per-slave and queue through it; global T3.5 inter-frame timing; hard per-transaction timeout. ADR-011 Part 1 |
| XEDGE-432 | 4 | **RS-485 RTS pre/post-transmit delay** (µs) — custom serial transport; `pyserial-asyncio` does not expose this |
| XEDGE-433 | 2 | Slave-ID uniqueness-on-bus validation at config apply |
| XEDGE-434 | 2 | Serial port auto-detection; dropdown in the Web UI driver form |
| XEDGE-435 | 4 | **On-Demand and On-Connect polling modes** as scheduler submission policies. CRD §4.1, §4.2 |
| XEDGE-436 | 3 | Modbus TCP: persistent vs on-demand connection mode, keepalive interval, connection-retry count |
| XEDGE-437 | 3 | **SNTP client** — multi-server, configurable sync interval and timezone, sync-status reporting in the UI. CRD §4.8 |

**Customer input needed:** open item Q-4 (target hardware) genuinely
affects XEDGE-432 — RTS timing requirements are converter-specific and
this ships unverified without it.

---

### Sprint C4 — Certificates, MQTT TLS, gateway provisioning (Sep 14 – Sep 27)

**Goal:** close the two most serious security gaps in shipped code, and
deliver CRD §4.9, using one certificate subsystem rather than two.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-440 | 10 | **Certificate management subsystem** in `xedge/security/` (currently empty, F-11): fleet CA, CSR signing, trust store, upload/manage root and CA certificates, rotation. ADR-013 §4. **Two consumers from day one** |
| XEDGE-441 | 6 | **MQTT northbound TLS/mTLS.** Closes finding F-10 — the connector currently has no transport security at all and ships credentials in clear. Also SR 3.1 in the SL-1 gap analysis |
| XEDGE-442 | 6 | **Join-token → certificate onboarding**: single-use, time-limited join tokens; device-side keypair generation and CSR; manager-side signing; mTLS thereafter. ADR-013 §3. Closes SR 1.2 and SR 1.4 |
| XEDGE-443 | 4 | Certificate rotation over the existing mTLS session, before expiry |
| XEDGE-444 | 5 | **Gateway metadata fields** on `DeviceRecord`: serial number, make, protocol, hardware firmware version (distinct from `agent_version`). CRD §4.9 |
| XEDGE-445 | 3 | **Four-state gateway connection model** (Connected/Disconnected/Active/Inactive) via the XEDGE-420 adapter. CRD §4.9 |
| XEDGE-446 | 4 | Remote configuration management: extend the existing pull-based delivery path with config authoring and per-device targeting on the manager |
| XEDGE-447 | 3 | Web UI: certificate management screens; fleet status showing the four-state model |

**Prerequisite:** ADR-012 P-5 — `amqtt` license, maintenance and ARM
footprint re-verified this sprint, ahead of C5.

---

### Sprint C5 — MQTT buildout (Sep 28 – Oct 11)

**Goal:** MQTT becomes a general-purpose capability rather than a
Sparkplug-B-only publisher.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-450 | 10 | **Generic MQTT Subscriber**: external broker connection, arbitrary topic subscription, payload parsing (JSON/raw/templated) mapped into tags. CRD §4.10 — entirely absent today |
| XEDGE-451 | 8 | **Publisher payload templating** beyond fixed Sparkplug B — configurable payload structure, configurable topics, event-driven and interval triggers |
| XEDGE-452 | 3 | Manual re-publish trigger |
| XEDGE-453 | 8 | **Embedded MQTT Broker** — `amqtt` promoted to a runtime dependency (ADR-012 §3), with connection diagnostics and configuration |
| XEDGE-454 | 6 | **Broker security**: TLS, authentication, ACL model. Not optional — the broker adds a listening service to the device's attack surface (risk R-09) |
| XEDGE-455 | 5 | Web UI: subscriber/publisher/broker config forms, payload structure preview, topic list, QoS |

---

### Sprint C6 — Asset Management and SMTP (Oct 12 – Oct 25)

| Story | Est. | Description |
|---|---:|---|
| XEDGE-460 | 8 | **Asset entity** as a metadata layer: `assets` config section with full CRD metadata, parameters referencing `instance_id/tag_id`. ADR-010 |
| XEDGE-461 | 4 | Referential-integrity validation on config apply — dangling `tag_ref` detection. ADR-010 consequences |
| XEDGE-462 | 4 | Per-parameter storage toggle enforced at the cold-store spill boundary. ADR-010 §3 |
| XEDGE-463 | 4 | Asset ↔ gateway mapping; **derived** asset connection state via the XEDGE-420 state machine. ADR-010 §4 |
| XEDGE-464 | 3 | Asset enable/disable, with the UI making clear it is presentational and does **not** stop the backing drivers |
| XEDGE-465 | 10 | Web UI: asset list, asset detail, parameter management, centralized cross-protocol configuration view |
| XEDGE-466 | 8 | **SMTP client** — SSL/TLS, authentication, wired into Alarm Engine v2 as a notification channel; scheduled-report triggering. CRD §4.7 |
| XEDGE-467 | 4 | Web UI: SMTP server configuration and notification rules |

**Prerequisites this sprint:** ADR-012 P-1 (`pycomm3` license verified) and
resolution of open item Q-7 (EtherNet/IP implicit I/O) ahead of C7
planning.

**Customer input needed:** open item Q-3 (asset-first UI vs metadata
layer). The data model survives either answer; only XEDGE-465 changes.

---

### Sprint C7 — EtherNet/IP Scanner (Oct 26 – Nov 08)

> **⚠ This sprint's estimate assumes explicit messaging at a scan interval
> satisfies the CRD's "cyclic exchange" requirement.** No mainstream Python
> CIP library implements Class 1 implicit I/O (ADR-012 §1). If true
> implicit I/O is required, this becomes a multi-sprint protocol build and
> the delivery date is at risk. **Resolve open item Q-7 before this sprint
> starts.** Risk R-02.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-470 | 3 | Finalise library selection against ADR-012 §1; record license in `license-audit.md`; write the provenance record |
| XEDGE-471 | 8 | CIP originator: connection establishment to ControlLogix/CompactLogix; connection monitoring and fault handling |
| XEDGE-472 | 8 | Symbolic tag access: runtime tag discovery or L5X import; arrays and UDTs |
| XEDGE-473 | 5 | Acyclic (explicit) read/write with confirmation |
| XEDGE-474 | 5 | Cyclic data exchange at configured scan interval; I/O mapping into tags |
| XEDGE-475 | 3 | Device/connection configuration schema; integration with the C1 scheduler and C2 connectivity state machine |
| XEDGE-476 | 4 | Web UI: EtherNet/IP driver config form + tag-list import widget |

---

### Sprint C8 — SNMP (Nov 09 – Nov 22)

**Note the two directions:** xEdge polls devices *and* is itself pollable.

| Story | Est. | Description |
|---|---:|---|
| XEDGE-480 | 3 | Finalise library selection (ADR-012 P-3, P-4 — must support the **agent** role); record license; provenance record |
| XEDGE-481 | 10 | **SNMP manager**: GET / GETNEXT / GETBULK / SET; v1, v2c, v3 with USM auth and privacy |
| XEDGE-482 | 8 | **SNMP agent** — xEdge itself pollable by the customer's NMS; expose driver, tag and system status via MIB |
| XEDGE-483 | 6 | **TRAP / INFORM originator**, wired to Alarm Engine v2 |
| XEDGE-484 | 5 | **TRAP / INFORM receiver**, mapping inbound notifications to tags/alarms |
| XEDGE-485 | 6 | MIB upload, parse and browse |
| XEDGE-486 | 6 | Web UI: SNMP client/agent config, MIB browser, trap destination management |
| XEDGE-487 | 4 | Integration with the C1 scheduler and C2 connectivity state machine |

---

### Sprint H1 — Integration, HIL, handover (Nov 23 – Dec 06)

| Story | Est. | Description |
|---|---:|---|
| XEDGE-490 | 6 | Full cross-protocol integration test: Modbus + EtherNet/IP + SNMP + BACnet + OPC UA concurrently, with assets spanning protocols |
| XEDGE-491 | 8 | **HIL pass against representative hardware.** Decision D-20. Requires hardware — chase open item Q-6 by Oct 25 |
| XEDGE-492 | 4 | Performance validation: batched Modbus throughput, scan-rate accuracy under load, broker footprint on the ARM target |
| XEDGE-493 | 4 | Customer documentation: configuration guide, protocol-by-protocol quick starts, onboarding walkthrough |
| XEDGE-494 | 3 | Handover package: compliance matrix against XEDGE-CRD-001, known limitations, deferred-item register |

**Explicit handover statements** — these must be in the handover package,
not discovered later:

- Central management is **single-tenant, API-only**, no dashboard (ADR-013 §2)
- OTA is **not** delivered in Delivery 1; when it arrives it updates the
  application container, **not the host OS or kernel** (ADR-013 §7)
- RS-485 cannot meet a 1 ms poll floor; per-transport minimums apply (D-10)
- Any protocol area verified by simulator only, if the HIL pass could not
  cover it (D-20)

---

## 4. Milestones

| ID | Definition | Target |
|---|---|---|
| **M-C0** | CI green on all four jobs; first armv7 image produced | 2026-08-02 |
| **M-C1** | Modbus reads at spec: batched, fixed-period, multi-register | 2026-08-30 |
| **M-C2** | Multi-drop RS-485 operational with write priority | 2026-09-13 |
| **M-C3** | MQTT TLS closed; certificate-based device onboarding working | 2026-09-27 |
| **M-C4** | MQTT complete: subscriber, publisher templating, broker | 2026-10-11 |
| **M-C5** | Assets and SMTP notifications delivered | 2026-10-25 |
| **M-C6** | All eight CRD areas implemented | 2026-11-22 |
| **M-C7** | **CRD-001 handover** — HIL passed, documented, compliance matrix signed | **2026-12-06** |

---

## 5. Scope-cut candidates

Identified in advance so a date decision can be made quickly rather than
under pressure. In order of preference — least customer impact first.

| Rank | Candidate | Saves | Cost of cutting |
|---:|---|---:|---|
| 1 | SNMP v3 (ship v1/v2c) | ~8d | Weaker SNMP security; needs customer agreement |
| 2 | MIB browse UI (accept uploaded MIBs without a browser) | ~6d | Operator convenience only |
| 3 | Embedded MQTT broker | ~14d | A named CRD requirement — requires explicit customer agreement |
| 4 | EtherNet/IP L5X import (runtime discovery only) | ~4d | Commissioning convenience |
| 5 | Asset centralized-config view (keep per-driver forms) | ~5d | UX regression against CRD §4.11 |

**Not cut candidates under any circumstance:** MQTT TLS (XEDGE-441) and the
certificate subsystem (XEDGE-440). They are security-load-bearing and
already 18 sprints overdue against their original plan.

---

## 6. Risk register — Delivery 1

Programme-level risks are in
[development-plan.md §5](development-plan.md). These are sprint-specific.

| ID | Risk | Sprint | Mitigation |
|---|---|---|---|
| **R-CRD-01** | The "protocols in the image" exclusion list is still unknown; all eight areas planned as in scope | all | Q-1. Re-cut immediately if it arrives |
| **R-CRD-02** | No hardware for the HIL pass; field interop unverified at handover | H1 | Q-6, chased by Oct 25. If unavailable, state it explicitly in the handover package |
| **R-CRD-03** | Batching (XEDGE-411) changes the read path all Modbus tests depend on | C1 | Property-based tests over address layouts; oracle comparison against `pymodbus` |
| **R-CRD-04** | Bus manager becomes a single point of failure per port | C3 | Hard per-transaction timeout. Note this is physically true of RS-485 regardless |
| **R-CRD-05** | **EtherNet/IP implicit I/O unavailable in any Python CIP library** | C7 | Q-7 before C7 planning. Highest-impact estimate risk in the delivery |
| **R-CRD-06** | SNMP library may not support the agent role, or may fail license verification | C8 | ADR-012 P-3/P-4 verified during C7, one sprint ahead |
| **R-CRD-07** | Customer expects an asset-first UI | C6 | Q-3. ADR-010's escape hatch: data model survives, ~1 sprint of UI |
| **R-CRD-08** | Customer expects a fleet dashboard at handover | H1 | ADR-013 §2. Correct the expectation **now** |
| **R-CRD-09** | Embedded broker footprint exceeds the 1 GB ARM target | C5 | Verified in C4 as ADR-012 prerequisite P-5, one sprint ahead |

---

## 7. Sprint status log

Updated at every sprint boundary (decision D-32). Each entry records
completed stories, carry-over, and a re-forecast against 2026-12-06.

| Sprint | Status | Completed | Carried | Forecast vs M-C7 |
|---|---|---|---|---|
| 0 | ✅ Complete ([PR #2](https://github.com/sgbhavsar-cpu/XEdge/pull/2)) | XEDGE-400/401/402/403/404/405/406/407/408 | — | On plan |
| C1 | ✅ Complete ([PR #3](https://github.com/sgbhavsar-cpu/XEdge/pull/3)) | XEDGE-410/411/412/413/414/415 | — | On plan |
| C2 | ✅ Complete ([PR #4](https://github.com/sgbhavsar-cpu/XEdge/pull/4)) | XEDGE-420/421/422/423/424/425/426 | — | On plan |
| C3 | ✅ Complete ([PR #5](https://github.com/sgbhavsar-cpu/XEdge/pull/5)) | XEDGE-430/431/432/433/434/435/436/437 | — | On plan |
| C4 | ✅ Complete ([PR #6](https://github.com/sgbhavsar-cpu/XEdge/pull/6), [PR #7](https://github.com/sgbhavsar-cpu/XEdge/pull/7)) | XEDGE-440/441/442/443/444/445/446/447 | Agent-side proactive cert rotation (see notes) | On plan |
| C5 | ✅ Complete ([PR #8](https://github.com/sgbhavsar-cpu/XEdge/pull/8), [PR #9](https://github.com/sgbhavsar-cpu/XEdge/pull/9), [PR #10](https://github.com/sgbhavsar-cpu/XEdge/pull/10)) | XEDGE-450/451/452/453/454/455; XEDGE-443's agent-side rotation (carried from C4, resolved in PR #10 — see addendum) | mqtt_broker ACL editing in the Web UI (deferred to the raw-YAML editor by design — see notes) | On plan |

### Sprint 0 notes (2026-07-27 → 08-02)

**Exit criterion met** — first fully green CI run in the project's history,
including the armv7 image, which had never once built.

CI turned out to have **four** independent failures, not one: each job masked
the next. Beyond the three known ones, `bandit -r xedge` was also exiting 1 on
a B101 finding nobody had seen, because the lint job failed before reaching
it. The suppression there targeted ruff's `S101`, which does nothing for
bandit.

XEDGE-402 was a design smell rather than a test bug. `/data` was hardcoded at
four independent call sites; the failing test proved it by carefully
overriding `store.directory` and still hitting `/data`, because
`config_management.history_directory` had its own separate default.

Two pre-existing Windows-only test flakes were also fixed. They did not block
CI (Linux has a nanosecond clock) but made the suite unrunnable on the
development machine, which matters across eight remaining sprints.

### Sprint C1 notes (2026-08-03 → 08-16)

All six stories delivered. Coverage 88%; planner 100%, datatypes 98%,
polling 91%.

**Open item Q-2 resolved** — the customer accepted that RS-485 cannot reach a
1 ms poll floor. XEDGE-413 lowered the schema floor to 1 ms and added a
computed per-transport achievable-floor warning, so the acceptance is backed
by xEdge stating what *is* achievable rather than failing silently.

**A scheduler bug was caught by its own test.** The first XEDGE-410
implementation advanced the deadline by whole intervals on overrun, so a 3 ms
overrun on a 60 ms group pushed the next read 57 ms further out — halving the
effective rate because a cycle ran 5% late. Corrected to reset the deadline to
now. Worth noting as evidence that the timing tests earn their keep.

**Batching's cost was paid back explicitly.** A device rejects a whole block
for one unmapped register, which would have marked every tag in that block
Bad. A rejected multi-tag block is re-read tag by tag, preserving the per-tag
error attribution the unbatched loop had.

`serial.py` remains at 34% coverage — the `pty`-based fake-serial fixture is
XEDGE-430, already scheduled first in C3 as a prerequisite for the bus
manager. Its new floor-calculation logic is unit-tested.

**Carried into C2:** nothing. XEDGE-423 (FC15/FC16) was already C2 scope;
multi-register *writes* are refused rather than truncated until it lands.

### Sprint C2 notes (2026-08-17 → 08-30)

All seven stories delivered. Coverage 88%; `connectivity.py`, `planner.py`
and `scheduler.py` at 100%, `polling.py` at 94%.

**A hang bug was caught before it shipped, the same way C1's scheduler bug
was — by the tests that were already there.** The first XEDGE-424
implementation tied the write-priority scheduler's start/stop to `run()`
(the poll loop), on the reasoning that reads and writes both need it
running. That reasoning missed a real case: `write()` is a valid call the
moment a driver is *connected*, and the write-back path — plus several of
this package's own pre-existing tests — call `configure()` → `connect()` →
`write()` directly without ever starting the poll loop. Every one of those
hung indefinitely, awaiting a consumer that was never started. Fixed by
making `connect()`/`disconnect()` concrete methods on
`BaseModbusPollingDriver` that own the scheduler's lifecycle around a new
`_connect_transport()`/`_disconnect_transport()` hook, rather than tying it
to `run()`. Worth flagging as a pattern: both sprints' most serious bugs
were caught by pre-existing or newly-written tests, not by inspection.

**XEDGE-426 turned out not to be a bug.** The original compliance review
flagged `build_tag_pipeline_configs` as "Modbus-shaped only," quoting its
own docstring. The function itself branches on nothing driver-type-specific,
and the OPC UA/BACnet schemas already declare
`scaling`/`deadband`/`engineering_unit` identically to Modbus — confirmed
by calling the function directly against a hand-built OPC UA and BACnet
config before changing anything. The docstring was stale; the code was
already correct. Fixed the docstring and added regression tests (including
a mixed three-driver-type fleet in one call) so the claim is now backed by
a test, not just a docstring. **Lesson for the remaining sprints:** a
finding from the original compliance report is a hypothesis to verify
against current code, not a confirmed defect to fix on sight — this is the
second time in two sprints (after C1's FC01–04/FC05/FC06 status) that
direct verification changed the diagnosis.

**FC15 (write multiple coils) is implemented and tested at the codec
level, with no runtime caller** — stated in the PR rather than papered
over. No bulk-write concept exists yet in `write()`'s single-tag API to
call it from; manufacturing one to give FC15 a caller would be scope
invented to satisfy an estimate line, not the CRD. This mirrors the
project's own precedent: FC16 sat uncalled from Sprint 2 until this sprint
gave it one, documented plainly the whole time.

**`serial.py` remains untested by the RTU fixture** (34%, unchanged from
C1) — XEDGE-430 (C3, first story) is still the fix.

**Carried into C3:** nothing.

### Sprint C3 notes (2026-08-31 → 09-13)

All eight stories delivered (XEDGE-430–437, 29 points).

**Multi-drop RS-485 works end to end.** `SerialBusManager` (XEDGE-431) is a
port-level arbiter: several `ModbusRtuSerialDriver` instances (several
slave IDs) sharing one physical port now acquire/release one shared
connection and one shared `RequestScheduler`, instead of racing each other
for the handle. What made this fit the existing per-transport driver shape
without a parallel class hierarchy: `_scheduler` became an overridable
property (a private per-instance scheduler by default; the serial driver
overrides it to return the bus manager's shared one), on top of the
`connect()`/`disconnect()` → `_connect_transport()`/`_disconnect_transport()`
restructuring C2's write-priority scheduler already needed — reused rather
than duplicated.

**RTS timing ships, but genuinely unverified against real hardware.**
XEDGE-432's pre/post-transmit delay (µs) is a custom serial transport
(`pyserial-asyncio` doesn't expose RTS toggle timing) that degrades to a
logged warning, not a crash, on a platform/adapter that doesn't support
RTS toggling at all. Open item Q-4 (target hardware) is still unresolved —
RTS timing requirements are converter-specific, so this is correct against
the spec but not yet validated against the customer's actual RS-485
transceivers.

**pty-based serial testing (XEDGE-430) is Linux-only by necessity, not by
choice.** `pty.openpty()` needs `termios`, unavailable on Windows. The
fixture and its 8 dependent tests (`test_serial_bus_manager.py`) skip
cleanly on a Windows dev machine and are validated by Linux CI only — the
gap C1's notes flagged for `serial.py`'s 34% coverage is now closed there.

**XEDGE-435 and XEDGE-436 both add an "on_demand" knob — deliberately
independent ones.** XEDGE-435's `polling_mode: on_demand` controls when a
*tag group* reads (never automatically; only via
`POST .../tag-groups/{id}/poll`, which calls the driver's own `poll_now()`).
XEDGE-436's `connection_mode: on_demand` controls when a *TCP socket* is
open (never held between transactions; every read and write dials and
closes its own). They compose freely — an on_demand tag group over an
on_demand connection is a legitimate combination — but the shared word is a
real risk for anyone skimming the schema without reading both descriptions.
No renaming done: each name is the obviously correct word for its own
concern in isolation. Flagging it here so nobody "fixes" it into something
worse by looking at only one side.

**A second hang bug, this time in test infrastructure rather than product
code — found while investigating a stuck CI job, not part of any planned
story.** PR #4's (Sprint C2) Python 3.11 CI job ran 90+ minutes (vs. 13 for
the identical 3.12 job) with zero test output past `test_diagnostics_ws.py`.
In-progress logs aren't fetchable from GitHub's API; cancelling the run and
re-fetching its logs afterward showed it stalled immediately on entering
`test_e2e_modbus_to_sparkplug.py`, before that file's one test printed
anything at all. Root cause: `paho.mqtt.Client.connect()`'s third
positional argument is the MQTT keepalive interval, not a connect timeout —
it is a blocking socket call with no timeout of its own, so a broker that
accepts the TCP connection but never answers CONNECT hangs that call, and
the whole event loop thread, forever. Why this only manifested on 3.11
remains unconfirmed (most likely an `amqtt`-internal timing/scheduling
difference between minor versions); what's fixed is that it can no longer
hang silently — the connect now runs off-thread and bounded with
`asyncio.wait_for`, and `amqtt`'s own `broker.start()` is bounded the same
defensive way. Pushed directly to the C2 branch (PR #4) rather than carried
into C3, since every stacked branch inherits the same test file and would
hit the identical hang.

**Carried into C4:** open item Q-4 (RTS timing vs. real hardware) — same
as this sprint, unresolved.

### Sprint C4 notes (certificates, MQTT TLS, gateway provisioning)

All eight stories delivered, across two PRs ([#6](https://github.com/sgbhavsar-cpu/XEdge/pull/6):
XEDGE-440/442/443; [#7](https://github.com/sgbhavsar-cpu/XEdge/pull/7):
XEDGE-441/444/445/446/447).

**The Fleet Manager became two services, not one.** ADR-013 describes
post-enrollment device calls authenticating via mTLS, but TLS
client-certificate enforcement (`ssl_cert_reqs=CERT_REQUIRED`) applies to
the whole listening socket, before any request routing — it can't
coexist on one port with join-token enrollment or admin/CLI traffic,
neither of which has a client certificate yet. Confirmed uvicorn has no
way to hand a route handler the peer certificate that *was* presented
(grepped the installed package: no `peercert`/`getpeercert` anywhere), so
per-device identification on the mTLS port still goes through
`device_token` bearer auth, unchanged from Sprint 29 — defense in depth
across two independent layers, not one layer doing both jobs. This was a
real fork with a genuine cost tradeoff (a second port to firewall/compose/
document vs. weaker enforcement), so it was put to the project owner
rather than decided silently; the two-port split was the answer.

**A real bug in this project's pinned httpx/httpcore**, not just a
deprecation warning: `AsyncClient(cert=(...), verify=str(ca_path))`
connects at the TLS layer but then fails every request with a bare
`ReadError`. Root-caused with a from-scratch raw `asyncio`+`ssl` repro
(bypassing uvicorn and httpx entirely) proving the certificates and
handshake were fine independent of httpx. Fixed by building one
`ssl.SSLContext` and passing it as `verify=`, per httpx's own suggested
replacement for the deprecated string form — a shared helper now lives at
`xedge/security/tls_context.py` and is used by both the fleet agent and
the MQTT connector (XEDGE-441).

**amqtt (the test-only broker) cannot enforce mandatory client
certificates.** Reading `amqtt/broker.py` directly: a listener with
`cafile` configured always sets `ssl.CERT_OPTIONAL`, never
`CERT_REQUIRED` — it requests a client cert but accepts a connection
without one. A test asserting "connecting without a cert is rejected"
against this fixture was removed rather than left passing for the wrong
reason; the surviving test proves the connector *can* present a client
cert successfully, which is what it's actually responsible for. A real
broker enforcing this (Mosquitto's `require_certificate true`, EMQX,
HiveMQ) would reject at the same TLS layer `manager_device_app`'s
`ssl_cert_reqs=CERT_REQUIRED` already demonstrates working, in
`test_fleet_agent.py`.

**The CRD's four gateway connection states aren't fully specified.**
"Connected/Disconnected/Active/Inactive" (§4.9) is named but the
boundaries between them aren't — `GatewayConnectionState`'s docstring
records this project's own interpretation (a time-since-last-heartbeat
classification, not ADR-011's consecutive-failure hysteresis, which is
built for a poll model a pull-based heartbeat doesn't have) plainly as an
assumption, not a customer confirmation. Low cost to be wrong about
(a four-line enum mapping), flagged rather than silently guessed.

**Carried into C5:** agent-side proactive certificate rotation. The
rotation *endpoint* (XEDGE-443) works and is tested over real mTLS, but
nothing on the device side yet decides "my certificate expires soon,
rotate now" — doing so mid-run means tearing down and rebuilding the
heartbeat loop's mTLS client to pick up the new certificate, a real
piece of structural work in `fleet_heartbeat_loop` that was scoped out
of this sprint's check-in rather than rushed. At the default 90-day
`--cert-validity-days`, this matters before Delivery 1 handover
(2026-12-06) if any device enrolls early in the window — worth
sequencing before C5 closes, not deferred indefinitely.

**Post-merge addendum — a bug the test suite could not have caught, found
by manually running the Fleet Manager and a real device process side by
side.** Every automated heartbeat test uses a 0.05s interval so the suite
stays fast; the real default is 60s. Running it for real at a realistic
interval surfaced this immediately: the second heartbeat onward failed
with "Server disconnected without sending a response," because uvicorn's
default `timeout_keep_alive` (5s) routinely elapses before the next
heartbeat reuses the pooled httpx connection, and httpx correctly refuses
to blind-retry a POST across that race. Fixed by disabling connection
reuse on the agent's httpx clients (`httpx.Limits(max_keepalive_
connections=0)`) — a heartbeat is infrequent enough that a fresh TCP+TLS
handshake every time costs nothing worth optimizing for. A regression test
reproduces the race deterministically (short server-side keep-alive rather
than waiting out the real timeout) and was confirmed to fail without the
fix. Landed on the certificates branch (PR #6, commit `47d7395`) rather
than this one, since the bug originates in code that branch introduced —
same reasoning as the Sprint C3 paho-mqtt hang fix above — then merged
forward into PR #7.

**This is worth generalizing, not filing away as a one-off:** every
automated test in this delivery has used compressed intervals (heartbeats,
polling, retries) to keep the suite fast, which is the right call for CI
time but means none of them can catch a timing-dependent bug that only
appears at real-world intervals. A deliberate manual run at production-
realistic timing — not just a fast pytest pass — is worth doing again
before H1 handover, not assumed covered by the suite being green.

### Sprint C5 notes (MQTT subscriber, generic publisher, embedded broker)

All six stories delivered, across two PRs ([#8](https://github.com/sgbhavsar-cpu/XEdge/pull/8):
XEDGE-450/451/452; [#9](https://github.com/sgbhavsar-cpu/XEdge/pull/9): XEDGE-453/454/455).

**`amqtt` promoted from test-only to a runtime dependency** (ADR-012 §3,
prerequisite P-5) for XEDGE-453's embedded broker. License/maintenance/
ARM-footprint re-verification is recorded in
[license-audit.md](license-audit.md) §4 item 6, not assumed carried over
from when it was only a test fixture's dependency.

**Three more real `amqtt` behaviors found by reading its source directly,
not assumed from its docs** — the same discipline C4's notes applied to
the `cafile`/`CERT_OPTIONAL` finding:

1. The publish/subscribe ACL plugin is asymmetric. `TopicAccessControlListPlugin`
   has a backward-compat carve-out for PUBLISH ("no publish_acl configured →
   assume permitted") that SUBSCRIBE does not — an empty `subscribe_acl`
   dict, once the plugin loads at all (i.e. either dict is non-empty),
   means zero subscribe access for everyone, not unrestricted. Confirmed
   empirically before writing the module docstring or the schema's field
   descriptions around it, not inferred from reading the source alone.
2. A rejected authentication attempt gets no CONNACK at all — amqtt just
   closes the raw connection (`broker.py::_handle_client_session`). A
   paho-mqtt client surfaces that as `on_disconnect`, never `on_connect`;
   tests asserting rejection had to be written around that, not around a
   CONNACK failure reason code.
3. `Broker.shutdown()` can hang forever if any connection ever reached the
   listener without completing a handshake — reproduced directly with a
   throwaway `asyncio.open_connection()`-then-close against a bare broker.
   That is exactly what a bare TCP health-check/liveness probe against
   this port would do. Given the broker is a new listening service on the
   device (ADR-012 §3's own risk register R-09) and a stuck shutdown would
   block every future restart, this was treated as a real product bug, not
   just a test-fixture inconvenience: `MqttBrokerService.stop()` now bounds
   the call with its own 10s `asyncio.wait_for`, with a regression test that
   deliberately triggers the underlying hang and asserts `stop()` still
   returns.

**The Web UI's schema-driven form engine (`xedge.api.schema_forms`) has no
widget for array or dynamic-key-object schema types** — a pre-existing gap
(`alarms.rules`, an array, already rendered as an unusable plain-text
fallback before this sprint), not something XEDGE-453 introduced. It
mattered more here: `mqtt_broker.users`' naive fallback would have rendered
a Python list-of-dicts — *including plaintext broker passwords* — directly
into the page source, not just an unusable widget. Resolved by giving
`users` its own small dedicated list/add/delete page (`/ui/config/mqtt-broker/users`,
modeled on the existing Web UI accounts page) rather than teaching the
shared form engine a new general-purpose widget; `publish_acl`/`subscribe_acl`
(not secrets, just awkward to render generically) stay on the raw-YAML
"Advanced" editor, same as `alarms.rules` already does, with an explicit
note on the MQTT Broker settings page pointing there. XEDGE-455 was scoped
to config *usability* for the credential-exposure case specifically, not to
building a general keyed-collection editor.

**Adding the first genuinely push-based driver type to the Web UI surfaced
a driver-type-agnostic assumption in it.** The generic "add tag group" flow
(`config_ui.py`) stubs every new group with a hardcoded `scan_rate_ms`
default, correct for every prior driver type (all poll-based) but meaningless
for `mqtt_subscriber` — every other existing driver schema declares
`scan_rate_ms`, this was the first one that didn't, and schema validation
correctly rejected the resulting config. Fixed by accepting (but documenting
as unused) `scan_rate_ms` in `mqtt_subscriber.schema.json`'s tag-group
schema, rather than special-casing the generic UI route per driver type —
found by actually clicking through the add-driver → add-tag-group → add-tag
flow end-to-end against a live running instance, not just by unit-testing
the driver module in isolation.

**Carried into C6, now for the second time:** agent-side proactive
certificate rotation (XEDGE-443's endpoint works; nothing on the device
side decides to call it yet). C4's notes already flagged this as "worth
sequencing before C5 closes, not deferred indefinitely" — it was deferred
again in favor of the broker/UI work this sprint's stories actually
required. At the default 90-day `--cert-validity-days`, a device enrolled
in the first weeks of C4 (mid-to-late September) is now past or close to
half its certificate's validity window with still no rotation path — this
should not be deferred a third time.

**Also carried:** `mqtt_broker.publish_acl`/`subscribe_acl` Web UI editing
(intentionally deferred, see above — not a gap discovered late, a scope
line drawn deliberately this sprint).

**Addendum — the cert-rotation carry above was resolved immediately
after, rather than actually carried into C6.** `fleet_heartbeat_loop` now
checks, once per heartbeat, whether `device-cert.pem` has fewer than
`fleet.cert_rotation_threshold_days` (default 30) left before expiry, and
if so calls XEDGE-443's `/rotate-certificate` endpoint itself — a fresh
keypair each time (`generate_key_and_csr`, the same call enrollment
makes), not a re-signed copy of the existing key.

The structural cost flagged back in C4 was real: the mTLS `httpx.AsyncClient`
bakes its `ssl.SSLContext` in at construction, so nothing on a live client
can be told "trust this new cert/key instead." Fixed by rebuilding the
client fresh every heartbeat iteration rather than once outside the loop —
free to do since `max_keepalive_connections=0` (the Sprint C4 keep-alive
fix) already meant no connection was reused *within* the old long-lived
client either. Rotation attempts are gated on that same iteration's
heartbeat having actually succeeded, so an unreachable manager produces
one warning per cycle, not two.

Verified with a regression test that proves the part a rotation which only
*appeared* to work would fail: a device enrolled with a deliberately
1-day-validity certificate (so the rotation threshold is already crossed
on the first heartbeat, rather than waiting out real time) whose
heartbeats keep succeeding *after* the rotation, over a client presenting
the new certificate — not just that the file on disk changed.

---

## 8. Revision history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-26 | Initial Delivery 1 plan from XEDGE-DR-001 |
| 1.1 | 2026-07-27 | Sprint C3 status row backfilled (was missing from §7's table despite its notes existing); Sprint C4 complete — status row + notes |
| 1.2 | 2026-07-27 | Sprint C4 addendum: keep-alive connection-reuse bug found by manual verification, fixed, regression-tested |
| 1.3 | 2026-07-27 | Sprint C5 complete — status row + notes (embedded MQTT broker, three more amqtt findings, Web UI config gap and fix, cert-rotation carry flagged as overdue) |
| 1.4 | 2026-07-27 | Sprint C5 addendum: agent-side proactive certificate rotation (XEDGE-443, carried from C4) delivered in PR #10 — no longer carried into C6 |
