# xEdge — Development Plan

**Document ID:** XEDGE-PLAN-001
**Version:** 2.0
**Status:** Active
**Date:** 2026-07-26
**Supersedes:** v1.0 (2026-07-03)
**Decision record:** [XEDGE-DR-001](XEDGE-DR-001-delivery-decisions.md)
**Sprint backlog:** [crd-delivery-plan.md](crd-delivery-plan.md)

---

## 0. What changed in v2.0, and why

Version 1.0 described an 18-month programme delivered by an 11-engineer
team across six phases. That plan was written before any code existed, and
the project it describes is not the project that happened.

What actually happened: the entire codebase — 14 commits, ~9,400 lines of
production Python and ~9,400 lines of tests — was produced between
2026-07-03 and 2026-07-10 by the project owner working with Claude Code.
There is no 11-engineer team. There is also, as of 2026-07-26, a signed
customer engagement (XEDGE-CRD-001) with committed dates, which v1.0 does
not account for at all.

v2.0 rewrites the plan against what is real:

| | v1.0 | v2.0 |
|---|---|---|
| Team | 11 engineers + extended team | Project owner + Claude Code |
| Structure | 6 phases, 36 sprints, 18 months | 2 deliveries: committed customer work, then platform + protocol build-out |
| Driver | HLR roadmap | XEDGE-CRD-001 customer requirements (D-01) |
| Branch strategy | `main` ← `develop` ← `feature/*` (documented, never used) | feature branches → PR → `main`, gated on green CI (D-19) |
| Verification | HIL against real PLCs from Sprint 1 | simulators during development; HIL before handover (D-20) |
| Estimates | person-days against a 5-person pod | retained as **scope size**, not as a resource plan (D-03) |

v1.0's sprint content is not discarded — Phase 4/5/6 remains a planned
delivery (D-06). It moves to Delivery 2 below, and
[`sprint-planning.md`](sprint-planning.md) is retained as its source
backlog with a superseded header.

---

## 1. Current state (verified 2026-07-26)

Assessed against the code and CI history, not against the documents.

### 1.1 What is built and working

Config engine with JSON Schema validation · driver framework and supervisor
· Modbus TCP / RTU-serial / RTU-over-TCP · OPC UA client and server ·
BACnet/IP client · in-house Sparkplug B encoder · RAM ring buffer + SQLite
cold store with replay-on-reconnect · hot reload · server-rendered Web UI
with schema-driven config forms · RBAC (4 roles × 11 permissions) ·
hash-chained audit log · rate limiting · self-signed TLS for the Web UI ·
OpenTelemetry tracing and Prometheus metrics · diagnostics WebSocket and
CLI · fleet agent and Fleet Manager v1 · alarm engine v2 with ack/shelve ·
write-back routing · CSV/JSON bulk tag import/export.

586 tests, 85% coverage.

### 1.2 What is claimed but not built

IEC 60870-5-104 · DNP3 · IEC 61850 (MMS/GOOSE/SV) · DLMS/COSEM · PROFINET ·
EtherNet/IP · OTA/RAUC · AWS IoT Core and Azure IoT Hub connectors ·
`xedge/security/` (empty package, despite PKI being claimed as a Phase 3
outcome).

Correcting these claims in `README.md` and `HLR.md` is Sprint 0 work
(D-29, D-30).

### 1.3 Known defects and debt

| ID | Issue | Where | Sprint |
|---|---|---|---|
| F-1..F-5 | CI red on all four jobs; lint violations; `/data` test failures; armv7 image never built; toolchain unpinned | CI, `pyproject.toml`, `Dockerfile` | 0 |
| F-6 | Modbus poll loop drifts and issues one round-trip per tag | `drivers/modbus/polling.py` | C1 |
| F-7 | Cold-store backlog orphaned across restart | `northbound/dispatcher.py:107` | 0 |
| F-8 | Modbus RTU serial 23% covered, no fake-serial fixture | `drivers/modbus/serial.py` | C3 |
| F-9 | `core/main.py` (composition root) 38% covered | `core/main.py` | 0 |
| F-10 | MQTT northbound has no TLS; credentials in clear | `northbound/mqtt.py` | C4 |
| F-11 | `xedge/security/` empty | — | C4 |
| F-12 | Pipeline config builder is Modbus-shaped only; OPC UA and BACnet tags get no scaling/deadband | `core/pipeline.py:165` | C2 |
| F-13 | README advertises six unbuilt protocols | `README.md` | 0 |
| F-14 | Documented branch strategy unused | — | 0 |

Full evidence in [XEDGE-DR-001 §1](XEDGE-DR-001-delivery-decisions.md).

---

## 2. Team and capacity

### 2.1 Actual structure

| Role | Who | Responsibilities |
|---|---|---|
| Project owner / engineer | 1 | All decisions, review, direction, customer interface |
| Implementation | Claude Code | Implementation, test authoring, documentation, under review |

### 2.2 How estimates are expressed

XEDGE-CRD-001 estimated ~342 person-days against a hypothetical 5-person
pod. That figure remains valid as a measure of **scope size** and as the
basis of the commercial agreement. It is **not** a resource plan, and
nothing in this document divides it by a headcount.

Scheduling in this plan is expressed in **calendar sprints of two weeks**,
sized against this project's own observed throughput. That throughput is
high, but two caveats govern how much confidence to place in it:

1. **It was measured on red CI.** The existing code was produced without a
   passing pipeline, and carries the defect set in §1.3. Lines per day is
   not the same as shippable-and-correct per day.
2. **Industrial protocol correctness is not verified by volume.** The risk
   in an OT gateway is field interop against real devices, which simulators
   do not fully derisk (D-20). This is why the HIL pass before handover is
   a scheduled block, not a nice-to-have.

Re-forecasting happens at every sprint boundary (D-32). Slippage becomes
visible within two weeks.

---

## 3. Delivery structure

### Delivery 1 — XEDGE-CRD-001 (committed)

**Window:** 2026-07-27 → 2026-12-06
**Content:** the eight requirement areas of the customer requirement
document, plus the CRD-required subset of central management.
**Detailed backlog:** [crd-delivery-plan.md](crd-delivery-plan.md)

| Sprint | Dates | Focus |
|---|---|---|
| **0** | Jul 27 – Aug 02 | Stabilization — green CI, doc corrections, PR workflow |
| **C1** | Aug 03 – Aug 16 | Modbus core: fixed-period scheduler, block-read batching, multi-register types, poll-interval floor |
| **C2** | Aug 17 – Aug 30 | Modbus write path, connectivity state machine, device health |
| **C3** | Aug 31 – Sep 13 | RS-485 bus manager, serial/TCP hardening, SNTP |
| **C4** | Sep 14 – Sep 27 | Certificate management, MQTT TLS, gateway provisioning, onboarding |
| **C5** | Sep 28 – Oct 11 | MQTT buildout: subscriber, payload templating, embedded broker |
| **C6** | Oct 12 – Oct 25 | Asset Management, SMTP notification channel |
| **C7** | Oct 26 – Nov 08 | EtherNet/IP Scanner |
| **C8** | Nov 09 – Nov 22 | SNMP client, agent, traps, MIB browser |
| **H1** | Nov 23 – Dec 06 | Integration, HIL pass, documentation, handover |

**State at handover:** all eight CRD areas delivered. Central management is
single-tenant, `admin_token`-authenticated and API-only — a deliberate
recorded interim state (ADR-013 §2), not an omission.

### Delivery 2 — Platform, Tier-2 protocols, GA

**Window:** 2026-12-07 → ~2027-06 (indicative; dated at Delivery 1 close)
**Content:** the additions phased out of Delivery 1 (D-08), plus the
Phase 4/5/6 content retained from v1.0 (D-06).

| Sprint | Focus | Source |
|---|---|---|
| **P1–P2** | Central platform: Postgres migration, multi-tenancy, user accounts + RBAC | ADR-013 §5, D-26 |
| **P3** | React SPA fleet dashboard | ADR-013 §6, D-27 |
| **P4** | OTA orchestration (container image, staged rollout, rollback) | ADR-013 §7, D-25 |
| **P5** | IEC 60870-5-104 driver | v1.0 Sprint 19 |
| **P6** | DNP3 driver (subject to the ADR-006 go/no-go gate) | v1.0 Sprint 20 |
| **P7** | BACnet MS/TP — **substantially cheaper after C3's RS-485 bus manager** | v1.0 Sprint 22 |
| **P8** | PROFINET IO (C extension, GSDML) | v1.0 Sprint 24 |
| **P9** | IEC 61850 MMS client | v1.0 Sprint 27 |
| **P10** | IEC 61850 GOOSE/SV + DLMS/COSEM | v1.0 Sprint 28 |
| **P11** | Security-debt closure, IEC 62443 SL-2 gap closure | D-31, v1.0 Sprint 33 |
| **P12** | Pen test, hardware matrix, conformance, GA | v1.0 Sprints 34–36 |

### Interleaving (D-07)

Foundations built once in Delivery 1 and consumed by Delivery 2:

| Built in | Component | Consumed by |
|---|---|---|
| C2 | Connectivity state machine (ADR-011) | gateway state, asset state, every future driver's health model |
| C3 | RS-485 shared bus manager (ADR-011) | **BACnet MS/TP (P7)** — the largest single interleaving win |
| C1 + C2 | Transport-neutral fixed-period scheduler (XEDGE-410, C1) with write priority added on top (C2) — corrected 2026-07-31: originally miscredited to C3 in this table, verified against `git log` on `xedge/drivers/modbus/scheduler.py` while planning Delivery 2's P6 (DNP3) | every future polled driver |
| C4 | Certificate management subsystem (ADR-013 §4) | fleet mTLS, MQTT TLS, SL-2 controls |
| C6 | Asset metadata layer (ADR-010) | composes over EtherNet/IP, SNMP, and all Tier-2 drivers with no per-driver work |

---

## 4. Definition of Done

### 4.1 Story level

- [ ] Acceptance criteria verified
- [ ] Tests written and passing; new code covered
- [ ] `ruff check`, `ruff format --check`, `mypy xedge`, `bandit -r xedge` all clean
- [ ] Any new runtime dependency has a verified license entry in [`license-audit.md`](license-audit.md) (D-12)
- [ ] Documentation updated — config schema, docstring, API surface
- [ ] Delivered on a feature branch via PR, **not** committed direct to `main` (D-19)

### 4.2 Sprint level

- [ ] All sprint stories Done
- [ ] CI green on `main` — all four jobs
- [ ] No new critical or high CVEs in `pip-audit`
- [ ] [`crd-delivery-plan.md`](crd-delivery-plan.md) updated with status and a re-forecast against the committed date (D-32)
- [ ] Written status note suitable for forwarding to the customer

### 4.3 Delivery level

- [ ] All delivery milestones achieved
- [ ] ADRs written for every significant decision
- [ ] Security posture reassessed for new attack surface
- [ ] HIL pass completed against representative hardware (D-20)
- [ ] Open items in [XEDGE-DR-001 §4](XEDGE-DR-001-delivery-decisions.md) resolved or explicitly re-deferred

---

## 5. Risk register

Risks carried from v1.0 that remain live, plus the risks this plan
introduces. Delivery-1-specific risks with sprint-level mitigation live in
[crd-delivery-plan.md §6](crd-delivery-plan.md).

| ID | Risk | Prob. | Impact | Mitigation |
|---|---|---|---|---|
| **R-01** | Protocol library licensing conflicts with the commercial edition | Medium | High | ADR-012 §0 — every new dependency license verified from package metadata and recorded before merge. Three verifications are named sprint prerequisites (ADR-012 §5) |
| **R-02** | **EtherNet/IP implicit (cyclic) I/O is not available in any Python CIP library** | High | High | ADR-012 §1. Resolve with the customer before C7 planning (open item Q-7). C7's estimate assumes explicit messaging at a scan interval — if true Class 1 implicit I/O is required, C7 becomes multi-sprint |
| **R-03** | Committed date missed | Medium | High | Additions phased to Delivery 2 (D-08); sprint-boundary re-forecast (D-32); scope-cut candidates identified in advance |
| **R-04** | Field interop failure discovered at HIL, late | Medium | High | HIL block scheduled before handover, not after (D-20). Hardware availability is open item Q-6 — chase 4 weeks ahead |
| **R-05** | Target hardware unknown; RS-485 RTS timing, armv7 need, and OTA mechanism all depend on it | High | Medium | Plan defensively (D-21): assume 64-bit Linux + Docker, keep armv7 building, treat RTS timing as configurable-but-unverified. Open item Q-4 |
| **R-06** | Customer expects an asset-first UI, not a metadata layer | Medium | Medium | ADR-010's escape hatch — the data model survives; only the UI changes, ~1 sprint. Open item Q-3 |
| **R-07** | Scope dispute over the unresolved "protocols in the image" exclusion | Medium | Medium | All eight areas planned as in scope (D-09). If an exclusion list arrives, re-cut immediately. Open item Q-1 |
| **R-08** | Customer expects a fleet dashboard at CRD handover | Medium | Medium | ADR-013 §2 records the interim state explicitly. **Correct the expectation now, not in November** |
| **R-09** | Embedded MQTT broker adds a listening service to the device's attack surface | Medium | Medium | TLS, auth and ACL are in C5 scope, not optional extras (ADR-012 §3) |
| **R-10** | Multi-tenant isolation is application-enforced; a missed `tenant_id` filter leaks across tenants | Medium | High | Per-query-path isolation tests are part of the P1–P2 estimate, not an addition to it (ADR-013 §5) |
| **R-11** | npm supply chain introduced by the SPA dashboard | Medium | Medium | SBOM generation and dependency audit in CI are in scope for P3 (ADR-013 §6) |
| **R-12** | Single-contributor bus factor; no second reviewer on security-sensitive changes | High | Medium | ADRs and this document set carry the reasoning, not just the code. Accepted consciously — it is a consequence of the capacity decision (D-03) |
| **R-13** | Velocity assumption based on throughput measured while CI was red | Medium | High | Sprint 0 establishes green CI as the baseline; every subsequent sprint re-forecasts against actuals (D-32) |
| **R-14** | OTA does not update host OS/kernel; customer may assume it does | Medium | Medium | ADR-013 §7 states the limitation. Confirm with customer — open item Q-5 |

---

## 6. Engineering standards

### 6.1 Code standards

- Python: `ruff` (format + lint), `mypy --strict`, `bandit`
- Dev toolchain **pinned to exact versions**, not floored — an unpinned
  toolchain turns CI red with no code change, and already has (F-5)
- Feature branch → PR → squash-merge to `main`, gated on green CI (D-19)
- No direct commits to `main`

### 6.2 Testing strategy

| Level | Tooling | Scope |
|---|---|---|
| Unit | pytest + pytest-asyncio | Core logic, codecs, state machines |
| Integration | pytest + in-process fakes | Driver ↔ simulated device; pipeline end-to-end |
| Black-box oracle | third-party libraries, never linked into the runtime (ADR-006) | Validating in-house codecs against reference implementations |
| HIL | pytest + physical devices | **Pre-handover pass only** — hardware does not yet exist (D-20) |
| Security | bandit, pip-audit, OWASP ZAP | Static analysis, dependency CVEs, authenticated API scan |

The v1.0 claim of HIL testing from Sprint 1 was never true and is removed.
Until the pre-handover pass runs, **field interop is unverified** — stated
plainly rather than implied otherwise.

### 6.3 CI/CD pipeline

```
PR opened
    │
    ▼
┌──────────────────────────────────────────────┐
│  ruff check + ruff format --check            │
│  mypy xedge                                  │
│  bandit -r xedge                             │
│  pytest (3.11, 3.12) + coverage              │
│  pip-audit                                   │
│  Docker build (amd64, arm64, armv7)          │
└───────────────────┬──────────────────────────┘
                    │ all green → squash-merge to main
                    ▼
┌──────────────────────────────────────────────┐
│  Tag + release artifacts (Delivery 2)        │
│  SBOM generation (security-debt backlog)     │
└──────────────────────────────────────────────┘
```

Container image signing, SBOM publishing and Grype scanning remain on the
security-debt backlog (D-31) with a re-review date, deferred visibly rather
than dropped.

---

## 7. Document map

| Document | Purpose |
|---|---|
| [XEDGE-DR-001](XEDGE-DR-001-delivery-decisions.md) | Every delivery decision, with rationale and consequence |
| **development-plan.md** (this) | Structure, capacity, standards, risk |
| [crd-delivery-plan.md](crd-delivery-plan.md) | Delivery 1 sprint-by-sprint backlog and status |
| [sprint-planning.md](sprint-planning.md) | v1.0 backlog — superseded; source for Delivery 2 |
| [license-audit.md](license-audit.md) | Binding dependency license record |
| [ADR-010](../architecture/adr-010-asset-management-model.md) | Asset data model |
| [ADR-011](../architecture/adr-011-serial-bus-and-connectivity.md) | Serial bus manager + connectivity state machine |
| [ADR-012](../architecture/adr-012-crd-protocol-build-vs-buy.md) | EtherNet/IP, SNMP, MQTT broker build-vs-buy |
| [ADR-013](../architecture/adr-013-central-management-platform.md) | Central management platform |

---

## 8. Revision history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-03 | Initial 18-month, 11-engineer, 6-phase plan |
| 1.1 | 2026-07-04 | Web UI promoted to day-one deliverable (ADR-007) |
| **2.0** | **2026-07-26** | **Rewritten against actual capacity and the committed XEDGE-CRD-001 engagement. Two-delivery structure. Team, verification approach, branch strategy and risk register corrected to reality.** |
