# ADR-012: Build-vs-Buy for CRD Protocols — EtherNet/IP, SNMP, MQTT Broker

**Status:** ACCEPTED (with named prerequisites — see §5)
**Date:** 2026-07-26
**Deciders:** Lead Architect, Project Owner
**Implements:** XEDGE-DR-001 decisions D-12, D-13, D-14, D-15
**Extends:** [ADR-006](adr-006-protocol-stack-build-vs-buy.md) to the three
protocol areas introduced by XEDGE-CRD-001

## Context

ADR-006 established xEdge's build-vs-buy posture: clean-room in-house
implementation for core protocols where the best library is GPL-encumbered
(Modbus, Sparkplug B, IEC 104), and "buy" where a permissive, maintained,
async-native library exists (BACnet via `bacpypes3`, MIT).

XEDGE-CRD-001 introduces three areas that ADR-006 never covered:
EtherNet/IP Scanner, SNMP Client + Agent, and an embedded MQTT Broker.
Together these are roughly 136 of the report's 200 backend person-days, so
the build-vs-buy answer here moves the estimate more than any other
decision in the delivery.

The compliance report (§8.2, §8.3) flagged all three as needing an ADR
before the estimate could firm up. This is that ADR.

## Decision

### 0. Licensing policy: case-by-case, verified before merge

No blanket licensing rule. Each new runtime dependency is assessed
individually against the dual GPL/commercial edition model, and **its
license is verified from the distributed package metadata and recorded in
[`license-audit.md`](../planning/license-audit.md) before the dependency is
merged** — not from memory, a README, or this document.

This matters because the existing audit table already carries two entries
(`asyncua`, LGPL-3.0; `libiec61850`, GPL/commercial) that constrain the
commercial edition. Adding a third by accident is a commercial problem, not
an engineering one.

### 1. EtherNet/IP Scanner — integrate a CIP library

Follow the `bacpypes3` precedent rather than the Modbus clean-room path.
Clean-room CIP roughly doubles the estimate, and CIP is materially more
complex than Modbus — an object model, connection manager, and two distinct
messaging classes rather than a register map.

**Candidate:** `pycomm3` (already listed MIT in `license-audit.md` from the
original Sprint 23 planning). `cpppo` is the fallback, but its licensing
must be verified before it is considered — it is not currently in the audit
table and is understood to be copyleft with a commercial option, which
would make it unusable in the commercial edition without a purchase.

> ### ⚠ Prerequisite finding: implicit (cyclic) I/O is not covered by the mainstream libraries
>
> The CRD asks for **both cyclic and acyclic** exchange. In CIP terms:
>
> - **Acyclic / explicit messaging** — request/response over TCP 44818.
>   This is what `pycomm3` does, and does well: symbolic tag read/write
>   against ControlLogix/CompactLogix, arrays, UDTs.
> - **Cyclic / implicit I/O** — Class 1 connected messaging over UDP 2222,
>   established via the connection manager, running at a negotiated RPI.
>   **No mainstream Python CIP library implements this.**
>
> This is not a detail that surfaces during implementation — it decides
> whether Sprint C7 is a 36-day integration or a multi-sprint protocol
> build. Three options, in order of preference:
>
> 1. **Explicit messaging at a scan interval** satisfies the customer's
>    functional need (periodic I/O data into tags) in the way most
>    commercial OT gateways actually implement "cyclic" polling. Confirm
>    acceptability with the customer — this is the cheapest correct answer
>    and most likely what they mean.
> 2. **Clean-room the implicit I/O layer** on top of the library's explicit
>    messaging. Real work, and it must be scoped before it is committed.
> 3. **Descope cyclic** for Phase 1.
>
> **This must be resolved before Sprint C7 planning.** It is carried as
> open item Q-7 in XEDGE-DR-001 §4 and as risk R-CRD-05 in the delivery
> plan. Sprint C7's estimate assumes option 1.
>
> **✅ Resolved 2026-07-28: option 1.** Explicit messaging at a scan
> interval, `pycomm3`'s own strength — Sprint C7 proceeds as the
> library-integration estimate above, not a multi-sprint protocol build.
> The driver and its docs must say "polled explicit messaging," not
> imply Class 1 implicit I/O it does not provide.

### 2. SNMP Client + Agent — integrate a library

The CRD needs **both directions**, which is easy to under-read:

| Requirement | Direction |
|---|---|
| GET / GETNEXT / GETBULK / SET against field devices | xEdge as **manager** |
| xEdge itself pollable by the customer's NMS | xEdge as **agent** |
| TRAP / INFORM **send** (wired to the Alarm Engine) | xEdge as **notification originator** |
| TRAP / INFORM **receive** | xEdge as **notification receiver** |
| MIB upload, parse, browse | tooling |

v3 with USM authentication and privacy is the deciding factor. Implementing
USM from RFC 3414 means implementing key localisation, engine discovery,
time-window checks and the auth/priv transforms correctly — and getting any
of that subtly wrong produces a *security* defect that tests will not
catch. This is precisely the case where "buy" is the responsible answer.

**Candidate:** the `pysnmp` lineage, which supports manager, agent, and
notification originator/receiver roles from one codebase. **Its license and
current maintenance status must be verified before adoption** — the project
changed stewardship and the package naming on PyPI is fragmented across
forks. This verification is a Sprint C8 prerequisite, not an assumption.

If no permissively-licensed, maintained option survives verification, the
fallback is v1/v2c in-house (straightforward — ASN.1 BER over UDP, no
crypto) with v3 escalated to the customer as a scope question. In-house v3
is not recommended at any price on this schedule.

### 3. MQTT Broker — promote `amqtt` from test-only to runtime

`amqtt` is already in the dependency tree, already MIT per
`license-audit.md`, and already proven — the integration test suite runs a
real broker against it today (`tests/fixtures/mqtt_broker.py`).

This **explicitly revisits and reverses** the prior decision recorded in
`pyproject.toml` ("never imported by xedge itself, never read as a
reference implementation, not shipped in any edition"). That decision was
correct when the broker was only a test fixture; the CRD makes an embedded
broker a product requirement, and re-deciding it in the open is better than
letting the comment quietly go stale.

A clean-room MQTT broker is not justified. It is a large, well-solved
problem with a permissive implementation already in the tree.

**Prerequisites before promotion:**

- Re-verify the license from package metadata (§0)
- Assess maintenance status and open security issues — a test fixture and a
  network-listening production service warrant different scrutiny
- Establish the security posture: the broker listens for inbound
  connections, which is new attack surface on the device. TLS, auth, and an
  ACL model are in scope for Sprint C5, not optional extras.
- Confirm resource footprint against the 1 GB-RAM ARM target from ADR-007

**Fallback if any prerequisite fails:** evaluate alternatives (including a
Mosquitto sidecar in the deployment rather than an in-process broker).
Clean-room remains rejected.

## Consequences

**Positive**

- Keeps roughly 136 backend person-days as integration rather than protocol
  engineering, which is what makes the committed date reachable at all
- Follows an established precedent (ADR-006's BACnet decision) rather than
  inventing a new posture
- The SNMP agent role, TRAP/INFORM, and MIB handling all come from one
  library rather than four separate builds

**Negative**

- Three new runtime dependencies, each with its own supply-chain,
  maintenance and CVE-tracking obligation. The security-debt backlog item
  for SBOM publishing (XEDGE-DR-001 D-31) becomes more valuable, not less.
- The commercial edition's dependency surface grows. If any of the three
  fails license verification, that sprint's plan changes materially.
- The embedded broker adds a listening service to the device's attack
  surface — a real security consequence of a convenience feature.

**Explicitly not decided here**

ADR-006's clean-room posture for Modbus, Sparkplug B, IEC 104 and DNP3 is
unchanged. This ADR extends the framework to three new areas; it does not
reopen the existing ones.

## Prerequisites checklist

None of these are optional, and each blocks its sprint:

| # | Prerequisite | Blocks | Owner action |
|---|---|---|---|
| P-1 | Verify `pycomm3` license and maintenance from package metadata; record in `license-audit.md` | C7 | Sprint C6 |
| P-2 | ~~Resolve the implicit-I/O question (§1 warning box) with the customer~~ ✅ **Resolved 2026-07-28** — option 1 (explicit messaging at a scan interval) accepted; see §1's warning box and XEDGE-DR-001 Q-7 | C7 | Before C7 planning |
| P-3 | Verify the SNMP library's license, package identity and maintenance; record in `license-audit.md` | C8 | Sprint C7 |
| P-4 | Confirm the SNMP library supports the **agent** role, not just manager | C8 | Sprint C7 |
| P-5 | Re-verify `amqtt` license, maintenance, and ARM footprint; define broker TLS/auth/ACL scope | C5 | Sprint C4 |

## References

- XEDGE-CRD-001 §4.5, §4.6, §4.10, §5 items 2/3/7, §8.2, §8.3, §9 Q3, §9 Q4
- XEDGE-DR-001 D-12, D-13, D-14, D-15, open item Q-7
- [ADR-006](adr-006-protocol-stack-build-vs-buy.md) — the framework this extends
- [license-audit.md](../planning/license-audit.md) — binding record; updated by every prerequisite above
