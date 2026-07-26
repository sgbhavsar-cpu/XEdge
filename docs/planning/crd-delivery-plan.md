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

---

## 8. Revision history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-26 | Initial Delivery 1 plan from XEDGE-DR-001 |
