# xEdge — Delivery 2 Sprint Plan

**Document ID:** XEDGE-PLAN-005
**Version:** 1.0 (draft — see §7 for what must happen before this becomes Active)
**Status:** Draft
**Date:** 2026-07-31
**Window:** 2026-12-07 → ~2027-06 (indicative; re-baseline once Delivery 1 actually closes)
**Parent plan:** [development-plan.md](development-plan.md) §3
**Predecessor:** [crd-delivery-plan.md](crd-delivery-plan.md) (Delivery 1, XEDGE-CRD-001)

Story IDs continue the v1.0 backlog ([sprint-planning.md](sprint-planning.md),
XEDGE-001..300) for stories carried from there unchanged, and use new
**XEDGE-5xx** IDs for stories that only exist because of how Delivery 1
actually landed (ADR-013's platform split, interleaving consequences).
This keeps a real story's identity stable across documents rather than
renumbering history.

This is **not** driven by a new customer requirement document. Delivery 2
is the continuation of the original v1.0 program (`sprint-planning.md`)
for the content phased out of Delivery 1 to protect the XEDGE-CRD-001
commitment date (ADR-013 §2 "Delivery split," decision D-08).

---

## 1. Delivery summary

Two kinds of content, sourced differently:

| Source | Content | Sprints |
|---|---|---|
| ADR-013 (written during Delivery 1, ahead of need) | Central platform maturity: Postgres/multi-tenancy, user accounts + RBAC, the fleet dashboard, OTA orchestration | P1–P4 |
| v1.0 `sprint-planning.md` (written before Delivery 1 existed, never executed) | Six Tier-2 protocol drivers, SL-2 security closure, GA | P5–P12 |

The v1.0-sourced sprints (P5–P12) are **not copied verbatim** — each is
checked below against what Delivery 1 actually built, since several of
v1.0's own assumptions no longer hold (its author didn't know EtherNet/IP
and BACnet/IP would land early, that a connectivity state machine and a
shared RS-485 bus manager would already exist, or that a certificate
subsystem would already be built and reusable).

**What this document is not yet**: a committed plan. §7 lists what has
to happen — most importantly, the P5→P6 gate and a real customer
conversation about scope and date — before this stops being a draft.

---

## 2. Sprint plan

### P1–P2 — Central platform: Postgres, multi-tenancy, RBAC

**Source:** [ADR-013](../architecture/adr-013-central-management-platform.md)
§5, decision D-26.

| Story | Points | Description |
|---|---:|---|
| XEDGE-500 | 13 | Postgres migration: Fleet Manager registry off SQLite. Every table gets a `tenant_id` column enforced at the query layer (ADR-013 §5 — row-level scoping, not schema-per-tenant) |
| XEDGE-501 | 8 | `docker-compose.fleet.yml` gains a Postgres service; migration tooling (schema versioning, one-shot SQLite→Postgres import for any Delivery-1 pilot deployments) |
| XEDGE-502 | 13 | User accounts + RBAC on the Fleet Manager itself (today: one shared `admin_token`). Roles at minimum mirror the device-local Web UI's four (ADR-007): readonly/operator/auditor/admin, tenant-scoped |
| XEDGE-503 | 13 | **Per-query-path tenant-isolation test suite** — ADR-013 §5's own stated consequence: "every query path needs a test proving it cannot leak across tenants." Not an addition to the estimate; the estimate already assumes this exists |
| XEDGE-504 | 5 | Join-token provisioning and device enrollment become tenant-scoped (a token belongs to one tenant; a device cannot enroll across tenants) |
| XEDGE-505 | 5 | Audit log for manager-side admin actions (user management, token issuance, config pushes) — the device-local Web UI already has this (ADR-007); the manager currently doesn't |

**Interleaving note (D-07):** none of Delivery 1's own work reduces this
estimate — it's genuinely new central-server scope, unlike P7 below.

### P3 — React SPA fleet dashboard

**Source:** ADR-013 §6, decision D-27.

| Story | Points | Description |
|---|---:|---|
| XEDGE-510 | 13 | Dashboard scaffold: React + Vite + TypeScript, served by the Fleet Manager, consuming its REST API. **Deliberately departs from ADR-007** (vanilla JS, no npm) — server-side only, justified in ADR-013 §6 by fleet-scale grid/filter/bulk-select interactions |
| XEDGE-511 | 8 | Device list: live grid over the tenant's fleet, connection state, cert expiry, filtering/search |
| XEDGE-512 | 8 | Device detail: metadata, config history, pending-config status, certificate rotation history |
| XEDGE-513 | 5 | Join-token management UI: issue, list, revoke |
| XEDGE-514 | 8 | **npm SBOM generation + CI dependency audit** — ADR-013 §6's own accepted consequence, in scope for this sprint, not deferred (risk R-11) |
| XEDGE-515 | 5 | User/role management UI (consumes P1–P2's RBAC) |

### P4 — OTA orchestration

**Source:** ADR-013 §7, decision D-25. **Explicitly not RAUC** — v1.0's
Sprint 30 (RAUC A/B) content is superseded, not carried forward; kept
here as a stated open question for when target hardware exists (D-21).

| Story | Points | Description |
|---|---:|---|
| XEDGE-520 | 13 | Manager-side OTA orchestration: staged rollout groups, canary-then-fleet sequencing, instructs devices via the existing heartbeat-response pull path (no new inbound channel) |
| XEDGE-521 | 8 | Device-side: pull and run a signed container image; health-check window before declaring success |
| XEDGE-522 | 8 | Rollback: re-pin the previous image digest on failed health check or manual trigger |
| XEDGE-523 | 5 | Image signing + verification (cosign or equivalent) — a device must refuse an unsigned or wrongly-signed image |
| XEDGE-524 | 3 | **Explicit customer-facing statement, restated at this sprint's close, not just at Delivery 1 handover**: this updates the xEdge application container, never the host OS or kernel (ADR-013 §7 limitation, open item Q-5) |

---

### P5 — IEC 60870-5-104 driver

**Source:** v1.0 Sprint 19, unchanged in substance — this is genuinely
new protocol scope with no Delivery 1 interleaving to draw on.

**Prerequisite (ADR-006 §6):** IEC 60870-5-104 spec purchase — tracked
open since Sprint 1 (`license-audit.md` §4 item 2), still outstanding.
Must close before this sprint starts, not during it.

| Story | Points | Description |
|---|---:|---|
| XEDGE-148 | 13 | In-house IEC 104 stack (clean-room from the purchased spec): APCI framing, U/S/I frames, k/w flow control, TESTFR keepalive. `lib60870-C` (GPL) as **black-box test oracle only** — ADR-006's clean-room rule applies in full (§2) |
| XEDGE-149 | 13 | IEC 104 master: connect/STARTDT, spontaneous data (ASDU types 1–45), quality flags |
| XEDGE-150 | 8 | General interrogation (type 100) and counter interrogation (type 101) |
| XEDGE-151 | 5 | Command issuance: types 45–51, 58–64; command result feedback — routes through the existing `WriteRouter` (unchanged since Sprint 13), not a new write path |
| XEDGE-152 | 5 | Quality flag → unified `Quality` mapping (IV, NT, SB, BL, OV) |
| XEDGE-153 | 8 | Test oracle: IEC 104 against a real simulator (QTester104 or equivalent) — same ADR-006 black-box-oracle pattern as every other in-house driver this project has built |
| XEDGE-286 | 5 | Web UI: IEC 104 driver config form — schema-driven, per the established pattern (every driver added since C7/C8 needed **zero** new template code, only a JSON Schema file) |

**This sprint's actual outcome is the P6 gate's decision input.** Track
person-days spent vs. this estimate explicitly — not just "done/not
done" — since Sprint 20's gate reads that number, not a completion date.

### P6 — DNP3 driver

**Source:** v1.0 Sprint 20. **Gate unchanged, refreshed 2026-07-31**
(`license-audit.md` §4 item 10): proceed in-house only if P5 landed on
budget; otherwise the fallback is a real commercial license negotiation
with Step Function I/O for `stepfunc/dnp3` — confirmed this sprint to be
a genuine paid-production-use license, not merely "commercial support
available." See §3 below for the full gate writeup — **this sprint's
scope cannot be finalized until P5 closes.**

| Story | Points | Description |
|---|---:|---|
| XEDGE-154 | 13 | In-house DNP3 link layer + transport function (clean-room from IEEE 1815): frame CRC, transport segmentation/reassembly |
| XEDGE-155 | 13 | DNP3 master: TCP + serial transport (the serial transport reuses C3's `SerialBusManager` — a real interleaving win not in v1.0's original estimate, since that component didn't exist yet); application layer; unsolicited responses |
| XEDGE-156 | 8 | Data objects: Binary Input, Analog Input, Counter, Binary Output, Analog Output |
| XEDGE-157 | 5 | Integrity poll scheduling; event data classes — reuses C1's transport-neutral fixed-period scheduler (another interleaving win) |
| XEDGE-158 | 5 | Quality flag → unified `Quality` mapping |
| XEDGE-159 | 8 | Test oracle: a real DNP3 simulator (FreyrSCADA or equivalent) — **only if the in-house path is taken**; if the commercial-crate fallback is taken instead, this becomes integration testing against the vendor library's own test tools, not a black-box oracle build |
| XEDGE-287 | 5 | Web UI: DNP3 driver config form |

**Prerequisite:** IEEE 1815 spec purchase (`license-audit.md` §4 item 3,
outstanding) — needed regardless of which side of the gate this lands on,
since the in-house path needs it to build from and the commercial-crate
path still needs it to write conformant config/mapping.

### P7 — BACnet MS/TP driver

**Source:** v1.0 Sprint 22, **substantially reduced** by Delivery 1
interleaving (development-plan.md §3 already flagged this as "the
largest single interleaving win" — confirmed here, not just asserted).

| Story | Points (v1.0) | Points (revised) | Why revised |
|---|---:|---:|---|
| XEDGE-166 | 13 | 8 | RS-485 serial + token-passing master, MAC address config — **C3's `SerialBusManager` (shared-bus scheduling, RTS timing, multi-drop) already exists**; this becomes "add an MS/TP protocol handler on top of an existing bus manager," not "build multi-drop serial handling from zero" |
| XEDGE-167 | 8 | 5 | MS/TP tuning (baud, max-info-frames, max-master) — same bus manager already has baud/timing config plumbing |
| XEDGE-168 | 5 | 5 | Shared config schema for BACnet IP + MS/TP (unchanged estimate — this is genuinely new schema work) |
| XEDGE-169 | 5 | 5 | RS-485 HIL test rig — unchanged; this is physical lab setup, not software |
| XEDGE-170 | 8 | 5 | Integration test: MS/TP device read + COV, timing vs. spec — can reuse `bacpypes3`'s own `Application` pattern already proven for BACnet/IP (C-era work), not a new test harness design |
| XEDGE-171 | 8 | 3 | Refactor: BACnet driver supports IP + MS/TP instances simultaneously — smaller than estimated because `DriverSupervisor` already runs arbitrarily many concurrent instances of different registered driver *types* with no shared state between them (proven directly, not assumed: Sprint H1's cross-protocol integration test runs five different protocol types under one supervisor at once — XEDGE-490). Adding `bacnet_mstp` as a second registered type alongside `bacnet_ip` is additive to that existing model, not a refactor of it |
| XEDGE-289 | 3 | 3 | Web UI: MS/TP transport fields — schema-driven, unchanged |
| **Total** | **50** | **34** | **~32% reduction from interleaving already built and paid for in Delivery 1** |

### P8 — PROFINET IO driver (Phase 1: C extension)

**Source:** v1.0 Sprint 24, unchanged — no Delivery 1 interleaving
applies (PROFINET's real-time cyclic I/O and GSDML parsing share nothing
with any protocol built so far).

| Story | Points | Description |
|---|---:|---|
| XEDGE-178 | 13 | PROFINET IO C extension: RT frame parsing, cyclic I/O data exchange — the one driver in this whole program requiring a C extension (real-time framing at the timing PROFINET demands isn't achievable in pure Python) |
| XEDGE-179 | 13 | GSDML parser: extract data types and module layout from device description files |
| XEDGE-180 | 8 | IO-Controller: AR establishment, CR negotiation |
| XEDGE-181 | 5 | Quality mapping: IOPS/IOCS → unified `Quality` |
| XEDGE-182 | 5 | Test rig: Siemens PLCSIM Advanced or ET 200SP simulator |
| XEDGE-183 | 8 | Integration test: cyclic data read, topology change handling |
| XEDGE-291 | 5 | Web UI: PROFINET config form + GSDML file upload widget — the first driver needing a file-upload config flow beyond the CSV tag-import pattern already built (XEDGE-CRD-001 §4.5 descoped this for EtherNet/IP; PROFINET can't skip it the same way since GSDML is mandatory, not optional, for module layout) |

### P9 — IEC 61850 MMS client

**Source:** v1.0 Sprint 27, unchanged.

**Prerequisite:** `libiec61850` commercial license procurement
(`license-audit.md` §4 item 4, outstanding since Sprint 1 — budget
already approved per ADR-006 §6, this is a procurement action, not an
open engineering decision).

| Story | Points | Description |
|---|---:|---|
| XEDGE-198 | 13 | `libiec61850` Python bindings: IED connect, server model discovery |
| XEDGE-199 | 13 | MMS report control blocks: buffered (BRCB) + unbuffered (URCB) subscriptions |
| XEDGE-200 | 8 | Logical node mapping (XCBR, MMXU, MMTR, XSWI) to `UnifiedTag` |
| XEDGE-201 | 5 | Control: SBO (Select Before Operate) and direct control — routes through `WriteRouter`, same as P5's IEC 104 command path |
| XEDGE-202 | 5 | Quality: `q` attribute → unified `Quality` mapping |
| XEDGE-203 | 8 | Test oracle: `libiec61850`'s own server simulator; BRCB integrity vs. live report |
| XEDGE-294 | 5 | Web UI: IED address, logical node/RCB selection config form |

### P10 — IEC 61850 GOOSE/SV + DLMS/COSEM

**Source:** v1.0 Sprint 28, unchanged except one open decision this
sprint itself was always scheduled to make.

| Story | Points | Description |
|---|---:|---|
| XEDGE-204 | 13 | GOOSE subscriber: raw Ethernet multicast, stale-data timeout detection |
| XEDGE-205 | 8 | Sampled Values (SV): 80/256 samples/cycle, power quality data types |
| XEDGE-206 | 13 | **DLMS/COSEM build-vs-buy decision point, at this sprint's start, not before** (per v1.0's own original design — this was never meant to be resolved early): in-house clean-room vs. `gurux-dlms-python` (GPL-2.0/commercial — `license-audit.md` §3 row, unusable freely in the commercial edition without its own paid license) |
| XEDGE-207 | 5 | DLMS authentication: none, low (PAP), high (GMAC-256) |
| XEDGE-208 | 8 | Test oracle: GOOSE stale-data detection, DLMS meter read |
| XEDGE-209 | 5 | IEC 62443 SL-2 gap analysis — identifies what P11 actually needs to close, produced here so P11 isn't scoped blind |
| XEDGE-295 | 5 | Web UI: GOOSE/SV and DLMS/COSEM config forms; GOOSE stale-data indicator on the driver health page |

### P11 — Security-debt closure, IEC 62443 SL-2

**Source:** v1.0 Sprint 33, decision D-31. Scoped by P10's own gap
analysis (XEDGE-209), not guessed at here.

| Story | Points | Description |
|---|---:|---|
| XEDGE-237 | 13 | SL-2 gap closure: remaining SR requirements per P10's analysis |
| XEDGE-238 | 8 | NERC CIP evidence package: CIP-002/005/007/010 documentation and export |
| XEDGE-239 | 8 | SOC 2 control mapping: CC6, CC8, A1 |
| XEDGE-240 | 5 | FIPS 140-2 mode: `cryptography` restricted to FIPS-approved algorithms only |
| XEDGE-241 | 5 | CIS benchmark hardening guide — extends the existing [hardening-guide.md](../security/hardening-guide.md), doesn't replace it |
| XEDGE-242 | 8 | Security regression suite: OWASP ZAP + manual pen test checklist |
| XEDGE-243 | 5 | Published vulnerability policy: CVE SLAs, responsible disclosure contact |
| XEDGE-299 | 5 | Web UI in scope for SL-2: CSRF review, session-fixation testing, WCAG 2.1 AA baseline |

### P12 — Pen test, hardware matrix, conformance, GA

**Source:** v1.0 Sprints 34–36, unchanged. Also where Delivery 1's own
carried limitation gets its real resolution:

| Story | Points | Description |
|---|---:|---|
| XEDGE-244 | 5 | Pen test scope + external firm engagement |
| XEDGE-245 | 20 | Pen test finding remediation (budget; actual depends on findings) |
| XEDGE-246 | 8 | Pen test report integration into security documentation |
| XEDGE-247 | 8 | **Hardware compatibility matrix — this is where Delivery 1's HIL gap (R-CRD-02, documented as simulator-only at handover) actually gets closed**, not carried forward again |
| XEDGE-248 | 5 | OPC UA CTT full conformance run |
| XEDGE-249 | 5 | Sparkplug B conformance: HiveMQ validator + Ignition demo project |
| XEDGE-300 | 3 | Pen test scope explicitly includes the Web UI auth flow and config-write path |
| XEDGE-257 | 8 | GA release: signed Docker images (amd64/arm64/armv7) to GHCR |
| XEDGE-258 | 5 | OTA bundle GA-published (P4's mechanism, not RAUC) |
| XEDGE-259 | 8 | Final regression: full protocol suite (now 14 drivers, not the ~5 v1.0 anticipated) on real hardware from XEDGE-247's matrix |
| XEDGE-260 | 5 | Final SBOM (CycloneDX + SPDX), clean vulnerability scan |
| XEDGE-261 | 5 | GA release notes: features, known limitations, upgrade guide |
| XEDGE-262 | 5 | Milestone M7 demo and stakeholder sign-off |

---

## 3. The P6 (DNP3) gate — full detail

Restated here rather than only in `license-audit.md`, since this is the
one decision in this entire plan that changes a downstream sprint's
actual scope, not just its risk profile.

**Original gate (v1.0 Sprint 20, unchanged):** proceed in-house only if
P5's in-house IEC 104 stack lands on budget (13+13+8+5+5 = 44 points,
excluding QA/UX); otherwise license the commercial Rust `dnp3` crate.

**Refreshed 2026-07-31** (full writeup: `license-audit.md` §4 item 10):
the fallback is confirmed to be a genuine paid-production-use commercial
license (Step Function I/O's `stepfunc/dnp3`, source-available for
eval/research/training only — production use requires a negotiated
agreement they have no obligation to grant), not merely "commercial
support is available" for an otherwise-free library. A third option
exists that v1.0's binary framing didn't name: wrap the free but
explicitly-abandoned OpenDNP3 (maintenance-only since 2020-12-20) via
`dnp3-python` or `pydnp3` — lowest short-term cost, open-ended
unpatched-upstream-bug exposure.

**This document does not resolve the gate.** It restates it accurately
so P5's close is the actual decision point, not a guess made now. Three
things must happen before P6 can be scoped for real:
1. P5 completes and its actual person-days are known.
2. If the fallback path is live, a commercial quote from Step Function
   I/O is obtained — "undisclosed" isn't a number that can be budgeted.
3. The customer is told which of the three paths was taken and why,
   the same way Q-7 (EtherNet/IP) and the SNMP agent's PEN/scope
   questions were surfaced during Delivery 1 rather than assumed.

---

## 4. Milestones

| ID | Definition | Target |
|---|---|---|
| **M-P1** | Fleet Manager on Postgres, multi-tenant, RBAC-authed | ~2027-01 |
| **M-P2** | Fleet dashboard GA on the central server | ~2027-02 |
| **M-P3** | OTA orchestration operational, container-image based | ~2027-02 |
| **M-P4** | P5 (IEC 104) closes; **DNP3 gate decision made and communicated** | ~2027-03 |
| **M-P5** | All six Tier-2 protocol drivers implemented | ~2027-05 |
| **M-P6** | SL-2 closed; pen test complete; HIL matrix run for real (closes Delivery 1's R-CRD-02) | ~2027-06 |
| **M7** | GA release, stakeholder sign-off | ~2027-06 |

---

## 5. Risk register — Delivery 2 additions

Carried programme-level risks are in [development-plan.md §5](development-plan.md).
New risks specific to this plan:

| ID | Risk | Prob. | Impact | Mitigation |
|---|---|---|---|---|
| **R2-01** | DNP3 gate resolved as "commercial" but Step Function I/O's quote exceeds budget | Medium | High | Get the quote during P5, not after P6 starts — a fourth option (wrap abandoned OpenDNP3 despite the risk) exists if the commercial path is unaffordable, per §3 |
| **R2-02** | Fleet Manager multi-tenant migration introduces a cross-tenant data leak | Low | Critical | XEDGE-503's per-query-path test suite is a named prerequisite of "done," not a follow-up |
| **R2-03** | PROFINET C extension introduces a memory-safety issue absent from the rest of the (pure-Python) codebase | Medium | High | Same discipline the driver-isolation model (ADR-008) already applies to third-party C bindings; fuzz the frame parser specifically |
| **R2-04** | P5/P6/P9/P10's in-house clean-room builds each independently reinvent already-solved problems (scheduling, quality mapping, config schema shape) | Medium | Medium | Reuse the transport-neutral scheduler (C1), connectivity state machine (C2), and `UnifiedTag`/`Quality` model explicitly — named per-sprint above, not left implicit |
| **R2-05** | Delivery 1's own stack (PRs #2-#16) is not yet merged/stable on `main` when Delivery 2 branches start | Low (closing out as of this document) | Medium | Confirm Delivery 1 fully green on `main` before cutting the first Delivery 2 branch — tracked in this session's own work, not assumed |

---

## 6. Scope-cut candidates

In order of preference, least customer impact first — mirroring Delivery
1's own §5 convention:

| Rank | Candidate | Saves | Cost of cutting |
|---:|---|---:|---|
| 1 | DLMS/COSEM (P10 half) | ~26pt | A named CRD-adjacent capability, but not in XEDGE-CRD-001 itself — lowest-regret cut if P6/P9 run over |
| 2 | PROFINET (P8, whole sprint) | ~57pt | The only driver needing a C extension; highest engineering risk per point, reasonable to defer to a Delivery 3 if capacity is tight |
| 3 | Fleet dashboard SPA (P3) | ~47pt | Fleet Manager stays API-only past Delivery 2 too — needs the same explicit customer statement Delivery 1's handover made, not a silent slip |
| 4 | Multi-tenancy (P1's tenant-scoping half, keep single-tenant Postgres) | ~15pt | Defers the highest-risk item (R2-02) entirely; single-tenant Postgres alone still fixes the SQLite concurrency ceiling |

**Not cut candidates under any circumstance:** P11 (SL-2) and P12's pen
test — same reasoning as Delivery 1's MQTT TLS/certificate subsystem:
security-load-bearing, not negotiable.

---

## 7. What has to happen before this plan is Active, not Draft

1. **Delivery 1 fully merged and green on `main`** — in progress as of
   this document's date; do not cut a Delivery 2 branch before this is
   confirmed (R2-05).
2. **A real customer conversation about Delivery 2's scope and date**,
   the same way XEDGE-CRD-001 itself started with XEDGE-DR-001. This plan
   inherits v1.0's story shape but has not been re-confirmed against
   what the customer actually wants next — do not assume the original
   v1.0 backlog order is still the customer's priority order.
3. **IEC 60870-5-104 and IEEE 1815 spec purchases** (`license-audit.md`
   §4 items 2 and 3) — both still open since Sprint 1, both block P5/P6
   from starting on day one if not closed first.
4. **`libiec61850` commercial license procurement** (`license-audit.md`
   §4 item 4) — budget approved, purchase not yet executed.
5. **Postgres vs. continued SQLite decision confirmed** with whoever
   owns the self-hosting customer's deployment complexity tradeoff
   (ADR-013 §5's own named negative consequence) — an engineering
   ADR already made the technical call; this is confirming the
   customer accepts the operational consequence (a new DB container in
   `docker-compose.fleet.yml`).
