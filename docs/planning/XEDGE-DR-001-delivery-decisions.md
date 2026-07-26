# xEdge — Delivery Decision Record

**Document ID:** XEDGE-DR-001
**Version:** 1.0
**Status:** Accepted
**Date:** 2026-07-26
**Baseline:** commit `be41cd7` (XEDGE-CRD-001 compliance report, 2026-07-10)
**Supersedes:** the delivery assumptions in `development-plan.md` v1.0 and
`sprint-planning.md` v1.0

---

## Purpose

This document records every decision taken in the 2026-07-26 delivery
planning session, with the rationale and the consequence of each. It is the
single source of truth for *why* the plan looks the way it does. The plan
itself lives in [`development-plan.md`](development-plan.md) (structure,
phases, capacity) and [`crd-delivery-plan.md`](crd-delivery-plan.md)
(sprint-by-sprint backlog).

Where a decision needs design detail rather than just a choice, it is
carried into an ADR — those are named inline below.

---

## 1. Findings that prompted the session

A review of the repository at `be41cd7` established the following, all
verified against the code and the CI history rather than the documents:

| # | Finding | Evidence |
|---|---|---|
| F-1 | CI has failed on **every job** for at least the last three pushes (2026-07-08, 07-08, 07-10) | `gh run list` — Lint, both test matrix legs, and Docker Build all red |
| F-2 | Lint fails on 29 real violations (27× E501, 1× F401, 1× I001) | reproduced locally with ruff 0.16 |
| F-3 | 4 integration tests fail with `PermissionError: '/data'` | `test_app_lifecycle.py` overrides `store.directory` but `config_management.history_directory` defaults to `/data/config-history` independently (`core/main.py:405`) |
| F-4 | The **armv7 Docker image has never been produced** | builder stage is bare `python:3.11-slim`; `cffi` has no armv7 wheel and `gcc` is absent |
| F-5 | The dev toolchain is floored, not pinned (`ruff>=0.6`, `mypy>=1.11`) | CI can go red with zero code changes; it already has |
| F-6 | The Modbus poll loop does not hold its scan rate and issues one round-trip per tag | `drivers/modbus/polling.py:145-152` — reads all tags sequentially *then* sleeps; `_read_one` always reads `quantity=1` |
| F-7 | Cold-store backlog can be orphaned across a restart | `northbound/dispatcher.py:107` enumerates ring-buffer stream keys, which are empty after restart; `SqliteColdStore` has no `stream_keys()` |
| F-8 | Modbus RTU serial is 23% covered (61 of 79 lines untested) | CI coverage report; no fake-serial fixture exists |
| F-9 | `core/main.py` — the composition root — is 38% covered | CI coverage report |
| F-10 | MQTT northbound has no TLS at all | `northbound/mqtt.py:10` still cites "Sprint 13 scope"; credentials cross the wire in clear |
| F-11 | `xedge/security/` is an empty package | despite PKI/mTLS being claimed as Phase 3 outcomes |
| F-12 | `build_tag_pipeline_configs` is Modbus-shaped only | `core/pipeline.py:165` — OPC UA and BACnet tags silently receive no scaling, deadband, or engineering unit |
| F-13 | README advertises six protocols that do not exist | IEC 104, DNP3, IEC 61850, DLMS, PROFINET, EtherNet/IP |
| F-14 | The documented branch strategy is not in use | no `develop` branch, no tags, all 14 commits direct to `main` |
| F-15 | The 11-engineer team in `development-plan.md` §2 does not correspond to reality | entire codebase produced 2026-07-03 → 07-10 |

---

## 2. Decisions

### 2.1 Direction and commercial context

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-01** | **XEDGE-CRD-001 is the single active roadmap.** | It is concrete, has a paying counterparty, and its scoping work is already done. Running two roadmaps in parallel means neither ships. | `sprint-planning.md` v1.0 is superseded, not deleted — see D-24. |
| **D-02** | The engagement is **won and committed**; dates are contractual. | Stated by the project owner. | Scope changes now carry commercial consequence. Schedule risk must be surfaced early and in writing (D-27). |
| **D-03** | Delivery capacity is **the project owner plus Claude Code**, as for the existing history. | Stated by the project owner. The 11-engineer structure was aspirational. | `development-plan.md` §2 is rewritten. The report's 342 person-days is retained as a **scope-size and commercial** figure, **not** as a resource plan — see D-26. |
| **D-04** | The committed window is the report's **7–9 sprints (~4 months)**. | Commercial commitment already made. | Plan targets **2026-12-06** for CRD-001 completion — the 9-sprint end of the committed range. |

### 2.2 Sequencing

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-05** | **Green CI before any feature work.** A dedicated Sprint 0. | Nothing should merge onto a red pipeline, least of all on a committed engagement. Addresses F-1..F-5. | One week, 2026-07-27 → 08-02. |
| **D-06** | **Phase 4/5/6 remains in the plan as a scheduled follow-on delivery**, not archived. | Project owner's explicit direction. The Tier-2 protocol suite and GA compliance remain product goals. | The plan carries **two deliveries**: Delivery 1 (CRD-001) and Delivery 2 (central platform + Tier-2 + GA). |
| **D-07** | **Interleave** the Phase 4/5/6 items that share foundations with CRD-001; defer the genuinely independent ones. | Building the same foundation twice is the most expensive failure mode available. | Certificate management, the connectivity state machine, and the RS-485 bus manager are built once in Delivery 1 and consumed by Delivery 2. BACnet MS/TP in particular becomes substantially cheaper after Sprint C3. |
| **D-08** | **Hold the committed date; phase the additions.** | Additions since the report (OTA orchestration, multi-tenancy, SPA dashboard) take the total from ~342 to ~430–450 equivalent person-days. The committed eight CRD areas fit; the additions do not. | CRD-001's eight areas hit 2026-12-06. OTA, multi-tenancy and the SPA dashboard land in Delivery 2 immediately after, with **single-tenant + server-rendered** as the interim state at handover. |

### 2.3 Customer-facing scope questions

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-09** | The CRD's "protocols in the image" exclusion is **unresolved**; plan **all eight areas as in scope**. | Safe against a scope dispute; matches the report's own assumption. | Carried as standing risk **R-CRD-01**. If an exclusion list arrives, scope and schedule are re-cut immediately. |
| **D-10** | Lower the poll-interval floor to the **1–10 ms** range, documented per transport. | The CRD's literal "1 nanosecond" is not achievable on any real transport. 1 ms is a good-faith move toward the spec and is achievable for small batched Modbus TCP reads. | Schema minimum drops from 50 ms. **RS-485 cannot meet 1 ms** — the achievable floor there is a function of baud rate and frame size and must be documented, not silently accepted. Enforced per driver type in Sprint C1. |
| **D-11** | **Asset is a metadata layer** over the existing driver-first model, not the primary configuration entity. | Far cheaper, requires no migration of existing config, and preserves the working driver-first UI. | See **ADR-010**. Risk: if the customer genuinely expects asset-centric operation, the UI may need an asset-first presentation over the same model — ADR-010 records that as the escape hatch. |

### 2.4 Build-vs-buy and licensing

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-12** | Dependency licensing is decided **case by case**, per library, with the license verified and recorded before adoption. | No blanket policy; the dual GPL/commercial edition model means each dep needs its own answer. | Every new runtime dependency requires a license entry in `license-audit.md` before it is merged. Non-negotiable — see D-13..D-15. |
| **D-13** | **EtherNet/IP: integrate a CIP library**, not clean-room. | Follows the BACnet/`bacpypes3` "buy" precedent (ADR-006). Clean-room CIP roughly doubles the estimate and CIP is materially more complex than Modbus. | See **ADR-012**. Candidate evaluation (`pycomm3`, `cpppo`) against license, maintenance, async fit, and **cyclic implicit-I/O support** is a prerequisite — several CIP libraries are explicit-messaging only, which would not meet the CRD. |
| **D-14** | **SNMP: integrate a library** for client, agent, and traps. | v3/USM auth+priv is genuinely hard to get right from spec, and getting it wrong is a security defect, not a bug. | See **ADR-012**. The CRD needs **both directions** — xEdge polls devices *and* is itself pollable — plus TRAP/INFORM send *and* receive. Library must cover the agent side, not just the manager side. |
| **D-15** | **MQTT broker: promote `amqtt` from test-only to a runtime dependency.** | It is already a proven dependency in the test suite. Explicitly revisits the prior "never imported by xedge itself" decision in `pyproject.toml`. | See **ADR-012**. Conditional on its license clearing D-12 for the commercial edition. If it does not, fall back to evaluating alternatives — a clean-room broker is not justified. |

### 2.5 Engineering architecture

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-16** | **Shared serial bus manager**: a port-level arbiter owns the serial handle and serializes requests; driver instances stay one-per-slave and queue through it. | Preserves the existing per-slave config and UI model. Fixes the real defect that two instances on one `/dev/ttyUSB0` collide. | See **ADR-011**. The arbiter is also the natural home for **write-priority scheduling** (a separate CRD requirement) and for RS-485 RTS timing — three requirements, one component. |
| **D-17** | **One connectivity state machine**, reused by Modbus device health, gateway connection state, and asset connection state. | The CRD describes near-identical models in three places. Three implementations guarantee drift. | See **ADR-011**. Reduces the report's estimate below its stated figure. Per-context adapters map to each context's state names (the gateway's 4-state enum vs. the device's Connected/Not Connected). |
| **D-18** | **Keep armv7**; fix the Docker builder stage. | Keeps the advertised platform matrix honest. | Add `build-essential` and `libffi-dev` to the builder stage. Multi-stage build already keeps the runtime image slim, so the cost is CI build time only. |
| **D-19** | **Feature branches + PRs into `main`**, squash-merged, gated on green CI. | Gives reviewable checkpoints and a clean audit trail appropriate to a paying customer, without the ceremony of a separate `develop` branch. | Supersedes `development-plan.md` §6.1's three-tier strategy (which was documented but never used — F-14). |

### 2.6 Verification

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-20** | **Simulators and black-box oracles during development; HIL before customer handover.** | Continues the existing, working pattern. Physical devices do not currently exist. | A hardware-procurement item with a date is added to the plan, scheduled ahead of the handover block. Until that pass runs, **field interop is unverified** — stated plainly rather than papered over. This is risk **R-CRD-02**. |
| **D-21** | Target gateway hardware is **unknown**; plan defensively. | No target device identified. | Assume 64-bit ARM or x86 running stock Linux with Docker. Keep armv7 building (D-18). Treat RS-485 RTS timing as configurable-but-unverified. All hardware-dependent assumptions are listed as risks in the plan. |

### 2.7 Central management platform

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-22** | **Evolve the existing Fleet Manager** into the central platform. | `xedge/fleet/manager_app.py` already does registration, heartbeat and config push — a real foundation, and the CRD's provisioning requirements land on it directly. | See **ADR-013**. Note the honest starting point: it is SQLite-backed, has a single shared join token, and **no user accounts at all** — only an admin token. |
| **D-23** | Central platform scope is **all four**: zero-touch onboarding + certificate provisioning; remote configuration management; fleet dashboard (health, inventory, alarms); OTA update orchestration. | Project owner's direction; centralized onboarding/management/configuration was called out as a first-class deliverable. | Only the **CRD-required subset** (onboarding, cert provisioning, gateway metadata, remote config) is in Delivery 1. Dashboard, multi-tenancy and OTA are Delivery 2 per D-08. |
| **D-24** | **Onboarding: join token now, certificate-based identity after.** | Extends what already exists (`FleetAgentConfig.join_token`). Operator provisions a one-time token; the device redeems it and receives its X.509 identity certificate for all subsequent authentication. | See **ADR-013**. Bridges directly to the CRD's certificate-management requirement and closes SR 1.2 / SR 1.4 in the SL-1 gap analysis, both currently Not Done. |
| **D-25** | **OTA by container image update**, not RAUC A/B, for this delivery. | Works on any Docker/Podman host, needs no OS or bootloader integration, and is testable in CI today. RAUC requires a Yocto/Buildroot image and target hardware that does not exist (D-21). | **Explicit limitation: this does not update the host OS or kernel.** RAUC A/B remains the planned upgrade in Delivery 2 once target hardware exists. Must be stated to the customer. |
| **D-26** | Central server is **self-hosted, multi-tenant capable**, on **Postgres with row-level tenant scoping**. | Standard, scales, and supports the concurrent writes the current SQLite registry will struggle with at fleet scale. | See **ADR-013**. Requires migrating the Fleet Manager off SQLite and introducing real user accounts — this is a new product surface, not an extension, and is the main reason it phases to Delivery 2. |
| **D-27** | Fleet dashboard is a **React + Vite + TypeScript SPA**, for the central server **only**. | Fleet-scale interactions — live grids, filtering, bulk operations across hundreds of devices — get awkward server-rendered. | **Deliberately departs from ADR-007's no-npm decision, for the central server only.** The device-local Web UI stays Jinja2 + vanilla JS, so ADR-007's mobile-code posture (SR 2.4) is preserved where it matters — on the device. The npm dependency surface needs an SBOM and audit story; recorded in ADR-013. |

### 2.8 Documentation and technical debt

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| **D-28** | Rewrite `development-plan.md`; add `crd-delivery-plan.md`; mark `sprint-planning.md` superseded with a mapping, not deleted. | The existing plan documents are actively misleading about team, velocity, and what is built. | This document set. |
| **D-29** | **Correct the README** to list only shipped protocols, and add an implementation-status matrix. | F-13. The repository is customer-visible on a committed engagement; advertising six unbuilt protocols is a live misrepresentation risk. | Roadmap items move to a clearly-labelled Roadmap section. |
| **D-30** | **Annotate `HLR.md`** with per-requirement implementation status. | Requirements currently read as delivered when many are not. | An implementation-status column is added. |
| **D-31** | **CRD-required security items now; the remainder to a tracked backlog.** | MQTT TLS and certificate management are CRD deliverables and are also the most serious gaps in shipped code (F-10, F-11). | SIEM forwarding, SBOM publishing, container signing, at-rest encryption and FIPS mode go to a named security-debt backlog with a re-review date — deferred deliberately and visibly, not dropped silently. |
| **D-32** | Two-week sprints; the delivery plan is **updated with status at every sprint boundary**. | Keeps a current forecast against a committed date at all times. | Slippage becomes visible within two weeks rather than at the end. |

---

## 3. Decisions carried into ADRs

| ADR | Covers | Decisions |
|---|---|---|
| **ADR-010** | Asset Management data model | D-11 |
| **ADR-011** | Modbus shared serial bus manager + unified connectivity state machine | D-16, D-17 |
| **ADR-012** | Protocol library build-vs-buy: EtherNet/IP, SNMP, MQTT broker | D-12, D-13, D-14, D-15 |
| **ADR-013** | Central management platform | D-22, D-23, D-24, D-25, D-26, D-27 |

---

## 4. Open items requiring customer input

These are **not** blocking the start of work, but each can force rework if
answered unfavourably late. All are carried as risks in
[`crd-delivery-plan.md`](crd-delivery-plan.md) §6.

| # | Question | Impact if answered late | Latest useful answer |
|---|---|---|---|
| **Q-1** | The exclusion list referenced as "protocols in the image" (D-09) | Could remove entire sprints from scope — or confirm all eight areas, which is what we are building to | Before Sprint C7 (first net-new protocol driver) |
| **Q-2** | Acceptance of the 1–10 ms poll floor and of per-transport floors, particularly that RS-485 cannot reach 1 ms (D-10) | Schema and scheduler rework in Modbus | Before Sprint C1 completes |
| **Q-3** | Whether "Asset" must be the primary configuration entity or may be a metadata layer (D-11) | UI rework in Sprint C6; the data model itself survives either way | Before Sprint C6 |
| **Q-4** | Target gateway hardware (D-21) | RS-485 RTS timing, armv7 necessity, OTA mechanism, and the HIL plan all depend on it | Before Sprint C3 |
| **Q-5** | Acceptance that OTA updates the application container, not the host OS/kernel (D-25) | Would force RAUC into scope, which needs hardware we do not have | Before Delivery 2 planning |
| **Q-6** | Availability of representative field devices for the pre-handover HIL pass (D-20) | Field interop stays unverified at handover | 4 weeks before the handover block |
| **Q-7** | **EtherNet/IP "cyclic exchange": is explicit messaging at a scan interval acceptable, or is true CIP Class 1 implicit I/O required?** No mainstream Python CIP library implements implicit I/O — see ADR-012 §1 | Decides whether Sprint C7 is a library integration or a multi-sprint protocol build. This is the single largest estimate risk in the delivery | **Before Sprint C7 planning** |

---

## 5. Revision history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-26 | Initial record — 32 decisions from the delivery planning session |
