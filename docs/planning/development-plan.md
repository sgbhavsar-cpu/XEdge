# xEdge — Development Plan

**Document ID:** XEDGE-PLAN-001  
**Version:** 1.0  
**Status:** Draft  
**Date:** 2026-07-03  

---

## 1. Overview

This document defines the phased development plan for xEdge across an 18-month delivery timeline with an enterprise team of 10+ engineers. It covers team structure, phase objectives, milestone definitions, and the overall strategy for reaching production-ready software with IEC 62443 / NERC CIP compliance.

---

## 2. Team Structure

### 2.1 Core Team (Permanent)

| Role | Count | Responsibilities |
|---|---|---|
| Engineering Manager | 1 | Delivery, planning, stakeholder communication, risk management |
| Lead Architect | 1 | Architecture decisions, ADRs, cross-team design reviews |
| Core Engine Engineers | 2 | Pipeline engine, store-and-forward, config engine, plugin framework |
| Protocol Engineers | 3 | Driver development (southbound protocols); C extension wrappers |
| Platform / DevOps Engineer | 1 | CI/CD, Docker builds, RAUC OTA, hardware test lab |
| Security Engineer | 1 | PKI, RBAC, IEC 62443 controls, pen test, compliance tooling |
| QA / Test Engineer | 1 | Test framework, HIL tests, conformance test suites, coverage |
| UX / Frontend Engineer | 1 | Local device web UI (day one, ADR-007): auth, config editor, monitoring dashboard; later the fleet manager dashboard |

**Total: 11 engineers**

> **Revision note (2026-07-04):** the local device Web UI moved from a Phase-3-only,
> post-GA-adjacent "configuration UI" line item to a **day-one core deliverable**
> (ADR-007, HLR §4.9). The UX role is promoted from the Phase-3-only extended team
> to the permanent core team as a result — see Sprint 3.5 in sprint-planning.md.

### 2.2 Extended Team (Phase-specific)

| Role | Phases | Responsibilities |
|---|---|---|
| Technical Writer | 4–6 | User docs, API docs, compliance documentation |
| Compliance Consultant | 5–6 | IEC 62443 gap analysis, NERC CIP evidence package, SOC 2 prep |
| Hardware Integration Specialist | 1, 4, 6 | Vendor HW bring-up, driver conformance testing |

---

## 3. Phases & Milestones

| Phase | Duration | Sprints | Key Deliverable |
|---|---|---|---|
| **Phase 1: Foundation** | Months 1–3 | S1–S6 (incl. S3.5) | Working Modbus TCP → MQTT Sparkplug B pipeline on real hardware; local browser-based configuration + monitoring UI operational from Sprint 3.5 onward (ADR-007) |
| **Phase 2: Tier-1 Complete** | Months 4–6 | S7–S12 | Full Modbus suite + OPC UA client/server; SD card store-forward; CI green; UI extended to cover every new driver type and store-and-forward status |
| **Phase 3: Security & Observability** | Months 7–9 | S13–S18 | mTLS everywhere; RBAC; OTel integration; IEC 62443 SL-1 baseline |
| **Phase 4: Tier-2 Protocols** | Months 10–13 | S19–S26 | DNP3, IEC 104, BACnet, EtherNet/IP, PROFINET drivers |
| **Phase 5: Fleet & Advanced** | Months 14–16 | S27–S32 | IEC 61850, DLMS, fleet manager, OTA, remote diagnostics, multi-cloud |
| **Phase 6: Hardening & GA** | Months 17–18 | S33–S36 | IEC 62443 SL-2 audit, pen test, hardware matrix, GA release |

### 3.1 Milestone Definitions

| Milestone | Definition | Date (Target) |
|---|---|---|
| **M1 — First Data** | Modbus TCP tags read and published to MQTT broker via Sparkplug B | End of Sprint 4 |
| **M1.5 — Local Web UI Operational** | Operator can open a browser at the device's IP, complete first-login password setup, view live driver/tag/northbound status, and edit + apply configuration — no separate tooling required (ADR-007) | End of Sprint 3.5 |
| **M2 — MVP Alpha** | Tier-1 protocols (Modbus + OPC UA), RAM store-forward, basic config, Docker | End of Sprint 10 |
| **M3 — MVP Beta** | SD card persistence, REST API, structured logging, OPC UA server | End of Sprint 14 |
| **M4 — Security Baseline** | mTLS, RBAC, audit log, IEC 62443 SL-1 gap analysis complete | End of Sprint 18 |
| **M5 — Protocol Complete** | All Tier-2 protocol drivers integrated and tested | End of Sprint 26 |
| **M6 — Fleet Ready** | Fleet manager, OTA, remote diagnostics, multi-cloud connectors | End of Sprint 32 |
| **M7 — GA** | IEC 62443 SL-2, NERC CIP evidence package, pen test passed, docs complete | End of Sprint 36 |

---

## 4. Definition of Done

### 4.1 Story Level
- [ ] Acceptance criteria verified by developer + QA
- [ ] Unit tests written and passing (≥ 80% coverage on new code)
- [ ] No new Ruff / mypy / bandit violations introduced
- [ ] Reviewed and approved by ≥ 1 other engineer (≥ 2 for security changes)
- [ ] Relevant documentation updated (API spec, config schema, docstring)

### 4.2 Sprint Level
- [ ] All sprint stories at Done
- [ ] CI pipeline green (lint + type check + unit tests + integration tests)
- [ ] Integration test suite passes on target hardware (Raspberry Pi 4 + industrial x86)
- [ ] No critical or high CVEs in pip-audit / Grype scan
- [ ] Sprint demo recorded or presented

### 4.3 Phase Level
- [ ] All phase milestones achieved
- [ ] Architecture Decision Records (ADRs) written for all significant decisions
- [ ] Security threat model updated for new attack surface
- [ ] Performance benchmarks run against NFR targets
- [ ] Hardware compatibility matrix updated

---

## 5. Risk Register

| ID | Risk | Probability | Impact | Mitigation |
|---|---|---|---|---|
| R-01 | Protocol library GPL licensing conflict with commercial edition | Low | High | **Mitigated by ADR-006:** in-house stacks for Modbus, Sparkplug B, IEC 104 (clean-room from official specs; GPL code used only as black-box test oracle); libiec61850 commercial license procured before Sprint 27; legal review of remaining deps in Phase 1 |
| R-02 | libiec61850 C binding instability / upstream changes | Medium | High | Pin version; maintain internal fork if needed; allocate 1 sprint buffer |
| R-03 | Hardware test lab procurement delays | Medium | Medium | Cloud-hosted PLC simulators (Modbus, OPC UA) from Sprint 1; real HW procured by S7 |
| R-04 | IEC 62443 SL-2 compliance gap discovered late | Low | High | SL-1 gap analysis at M4 (Sprint 18); SL-2 gap analysis at Sprint 28 with buffer |
| R-05 | PROFINET driver complexity (no mature Python lib) | High | Medium | PROFINET allocated 3 sprints (double others); C extension path planned from start |
| R-06 | SD card write endurance in field | Medium | Medium | WAL sync tuning; configurable sync policy; endurance testing in Phase 4 |
| R-07 | Team scaling/onboarding bottleneck in Phase 4 | Medium | Medium | Strong documentation from Phase 1; driver template reduces onboarding time |
| R-08 | Sparkplug B v3.0 specification edge cases | Low | Low | Compliance test suite against Ignition and HiveMQ in Phase 2 |
| R-09 | In-house protocol stacks (Modbus, IEC 104, DNP3) fail interop with real field devices or miss spec edge cases | Medium | High | Black-box testing against reference implementations (pymodbus, lib60870, opendnp3 binaries) and simulators; HIL tests with real devices; DNP3 go/no-go gate after IEC 104 with commercial `dnp3` crate fallback (ADR-006) |
| R-10 | open62541 asyncio binding effort underestimated (no off-the-shelf async Python binding exists) | Medium | Medium | Binding layer scoped at 3–5 eng-weeks in Sprint 8; shared client/server design; asyncua available as CI oracle to validate behavior |
| R-11 | Web UI, moved to day-one scope (ADR-007) after Phase 1 planning was already underway, causes the UI to permanently lag backend capability if not actively tracked | Medium | Medium | Explicit per-sprint UI stories added to every remaining sprint touching driver/security/fleet capability (sprint-planning.md); Phase-level Definition of Done extended to require the UI reflect that phase's new capability before phase close |
| R-12 | Single-user, no-RBAC auth model (Sprint 3.5) is mistaken for a finished security posture rather than an explicitly interim one, leading to inappropriate production exposure before Sprint 14 RBAC lands | Low | High | Loopback-only default bind (matching the read-only REST API's existing posture); UI login banner states "single-user mode, full RBAC arrives in a later release"; ADR-007 and HLR §4.9 both document the interim nature explicitly |

---

## 6. Engineering Standards

### 6.1 Code Standards
- Python: `ruff` (format + lint) + `mypy --strict` type checking
- C extensions: `cppcheck` + `clang-analyzer` static analysis; Valgrind memcheck clean
- All code reviewed before merge (no self-merge on main)
- Branch strategy: `main` (protected) ← `develop` ← `feature/*` / `fix/*`

### 6.2 Testing Strategy

| Level | Tooling | Scope |
|---|---|---|
| Unit tests | pytest + pytest-asyncio | All core logic; pure functions; state machines |
| Integration tests | pytest + Docker Compose | Driver ↔ simulated device; pipeline end-to-end |
| HIL (Hardware-in-Loop) | pytest + physical devices | Real PLC/IED in lab; automated via CI on HW runners |
| Performance tests | locust + custom benchmarks | Pipeline throughput; store-forward write rate |
| Security tests | OWASP ZAP (REST API); custom | Auth bypass, injection, TLS downgrade |
| Conformance tests | Protocol simulators | Sparkplug B conformance; OPC UA CTT |

### 6.3 CI/CD Pipeline

```
PR opened
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Lint + type check (ruff, mypy)                         │
│  Unit tests (pytest) — amd64                            │
│  Security scan (pip-audit, bandit)                      │
│  Docker build (amd64, arm64, armv7)                     │
└──────────────────────┬──────────────────────────────────┘
                       │ PR merged to develop
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Integration tests (Docker Compose, simulators)         │
│  Container vulnerability scan (Grype)                   │
│  SBOM generation (syft)                                 │
└──────────────────────┬──────────────────────────────────┘
                       │ Develop → main (release)
                       ▼
┌─────────────────────────────────────────────────────────┐
│  HIL tests on physical hardware runners                 │
│  Performance benchmarks vs. NFR targets                 │
│  Signed Docker image push to GHCR                       │
│  RAUC bundle generation + signing                       │
│  Release notes auto-generated from changelog            │
└─────────────────────────────────────────────────────────┘
```

---

## 7. Phase Details

### Phase 1: Foundation (Months 1–3)

**Objective:** Establish project scaffolding, developer tooling, and a working end-to-end data path for the simplest protocol (Modbus TCP).

**Phase outcomes:**
- Working Docker image: Modbus TCP (in-house stack, ADR-006) → Pipeline → RAM store-forward → MQTT Sparkplug B (in-house encoder)
- Config engine with YAML + JSON Schema validation
- Structured logging via structlog
- CI pipeline green (lint + unit + integration)
- Hardware test lab set up (simulated Modbus server in CI)
- Protocol license audit complete; IEC 60870-5-104 and IEEE 1815 specification documents procured
- **Local browser-based configuration + monitoring UI (Sprint 3.5, ADR-007):** first-login
  password setup, live dashboard, full config editor — running on the device itself,
  no cloud/fleet-manager dependency; Milestone M1.5

---

### Phase 2: Tier-1 Protocols Complete (Months 4–6)

**Objective:** Complete the Tier-1 protocol suite and all core data infrastructure components.

**Phase outcomes:**
- Modbus RTU (serial) and Modbus RTU-over-TCP drivers
- OPC UA client driver (subscription + polling)
- OPC UA server (northbound) — basic information model
- SD card / eMMC store-and-forward with WAL (SQLite)
- Configurable per-tag retention, deadband, scan rate
- Sparkplug B birth/death certificates correct
- REST management API (v1) — config read, driver status
- Performance benchmarks: ≥ 50k tags/s on Raspberry Pi 4
- Milestone M2 (MVP Alpha) demo
- **Web UI kept in lockstep:** every Tier-1 capability added this phase (RTU drivers,
  OPC UA client/server, SD store-forward) gets a corresponding config/monitoring
  screen in the same or immediately following sprint — the UI is never allowed to
  drift more than one sprint behind backend capability (see per-sprint stories in
  sprint-planning.md)

---

### Phase 3: Security & Observability (Months 7–9)

**Objective:** Build production-grade security controls and observability infrastructure.

**Phase outcomes:**
- mTLS on all northbound connections and REST API
- RBAC implementation (4 built-in roles + custom)
- PKI management: cert generation, rotation, CA trust store
- TPM 2.0 integration for key storage
- Audit log: hash-chained JSON, syslog forwarding
- OpenTelemetry integration: traces, metrics, structured logs
- OTLP exporter (Grafana/Datadog compatible)
- Remote diagnostic CLI (WebSocket, RBAC-gated)
- IEC 62443 SL-1 gap analysis and remediation
- Milestone M4 (Security Baseline)

---

### Phase 4: Tier-2 Protocols (Months 10–13)

**Objective:** Implement all five Tier-2 protocol drivers and validate against real equipment.

**Phase outcomes:**
- IEC 60870-5-104 driver (in-house stack per ADR-006; master, spontaneous + GI)
- DNP3 driver (in-house lean master per ADR-006 go/no-go gate; unsolicited, serial + TCP)
- BACnet IP and MS/TP driver
- EtherNet/IP (Rockwell ControlLogix/CompactLogix) driver
- PROFINET IO driver (C extension, GSDML parsing)
- Per-driver HIL tests with real or certified-sim hardware
- Driver framework v2: hot-reload without pipeline interruption
- Milestone M5 (Protocol Complete)

---

### Phase 5: Fleet, Advanced Features & Multi-Cloud (Months 14–16)

**Objective:** Production deployment and management capabilities.

**Phase outcomes:**
- IEC 61850 MMS client + GOOSE subscriber + SV subscriber
- DLMS/COSEM client driver
- Fleet management agent + self-hosted fleet manager
- OTA via RAUC: A/B partition, signed bundles, staged rollout
- Multi-cloud connectors: AWS IoT Core, Azure IoT Hub
- Northbound write-back (NCMD/DCMD → southbound driver)
- Virtual tag engine (expression-based computed tags)
- Alarm/event detection engine with independent retention
- Configurable per-tag/per-stream store-and-forward retention
- IEC 61850 GOOSE stale-data detection
- Milestone M6 (Fleet Ready)

---

### Phase 6: Hardening, Compliance & GA (Months 17–18)

**Objective:** Certify quality, security, and compliance; release GA.

**Phase outcomes:**
- IEC 62443 SL-2 compliance review and gap closure
- NERC CIP evidence package (CIP-002, CIP-005, CIP-007, CIP-010)
- External penetration test; all findings remediated
- Hardware compatibility matrix (≥ 6 hardware platforms)
- Sparkplug B conformance test (HiveMQ / Ignition)
- OPC UA CTT (Compliance Test Tool) pass
- Full documentation: user guide, API reference, hardening guide, operator runbook
- SBOM published, vulnerability policy published
- Reproducible build verification
- Milestone M7 (GA Release)
