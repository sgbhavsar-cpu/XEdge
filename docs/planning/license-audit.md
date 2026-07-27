# xEdge — License Audit (Sprint 1, XEDGE-010)

**Document ID:** XEDGE-PLAN-003
**Version:** 1.0
**Status:** Living document — update whenever a new dependency is added
**Date:** 2026-07-03

Cross-references: [ADR-006](../architecture/adr-006-protocol-stack-build-vs-buy.md), HLR CON-003/CON-004/CON-006.

---

## 1. Purpose

Enumerate every candidate library referenced in the architecture and sprint
plans, flag GPL/AGPL exposure for the commercial edition, and record the
clean-room rule that applies to in-house stacks. This is the artifact
XEDGE-010 (Sprint 1) commits to producing; it must be updated whenever a new
dependency is proposed.

## 2. Clean-room rule (binding — see HLR CON-006)

For every protocol built **in-house** because the best available library is
GPL-encumbered (Modbus, Sparkplug B, IEC 60870-5-104, and DNP3 if the ADR-006
gate is passed):

- Implementation MUST be derived only from the official specification /
  public standard, never from reading GPL source.
- The corresponding GPL library MAY be used as a **black-box test oracle**
  only (run its binary, compare outputs) — never opened in an editor by an
  engineer who is also writing the in-house implementation.
- Each in-house driver ships a provenance record (template: §5) naming the
  spec version used and confirming no GPL source was consulted.

## 3. Dependency license table

| Library | Used by | License | Commercial-edition status | Notes |
|---|---|---|---|---|
| pyyaml | config engine | MIT | ✅ Clear | |
| jsonschema | config engine | MIT | ✅ Clear | |
| structlog | logging | MIT / Apache-2.0 | ✅ Clear | |
| pyserial-asyncio | Modbus RTU transport | BSD-3-Clause | ✅ Clear | Transport only; framing/CRC is in-house (ADR-006) |
| paho-mqtt | MQTT transport | EPL-2.0 / EDL-1.0 | ✅ Clear | Permissive; dual-licensed by Eclipse |
| pymodbus | **test oracle only** (ADR-006) | BSD-3-Clause | ✅ Clear | Not shipped in the runtime driver; used in CI to validate the in-house Modbus codec |
| pysparkplug | **test oracle only** (ADR-006) | Apache-2.0 | ✅ Clear | Bundles the officially-generated Sparkplug B protobuf classes; used in tests to decode our in-house encoder's output. Installed with `--no-deps` (its own paho-mqtt<2 pin conflicts with our real paho-mqtt 2.x dependency; only its payload-decode classes are used, never its MQTT client) |
| amqtt | **Embedded MQTT broker, runtime** (ADR-012 §3; promoted from test-only Sprint C5, XEDGE-453) | MIT | ✅ Clear — re-verified 2026-07-27, see §3.1 | Also served as the test-only broker fixture since Sprint 3 (ADR-006); that role is unaffected |
| tahu (Sparkplug B ref impl) | **not used** | Apache-2.0 | N/A | Not installed; the in-house encoder is built from the Eclipse spec + public field-number tables, not this codebase |
| asyncua | **MVP OPC UA runtime** (ADR-006 §7 amendment) | LGPL-3.0 | ⚠ GPL edition only until legal review; commercial edition MUST NOT ship it | Used directly as the OPC UA client/server implementation for the MVP (interim — open62541 binding is still the ADR-006 §3 target for the commercial edition) |
| fastapi | REST API v1 | MIT | ✅ Clear | |
| uvicorn | REST API v1 ASGI server | BSD-3-Clause | ✅ Clear | |
| httpx | **test-only** (FastAPI TestClient transport) | BSD-3-Clause | ✅ Clear | Not a runtime dependency; only exercised via `fastapi.testclient.TestClient` and directly in the live REST API smoke test |
| open62541 | OPC UA client + server (ADR-006) | MPL-2.0 | ✅ Clear (file-level copyleft; compatible with proprietary linking) | In-house asyncio C-extension binding required (no off-the-shelf async Python binding exists) |
| lib60870-C | **black-box oracle only** (ADR-006) | GPL-2.0 (OSS ed.) / Commercial | ⚠ GPL — do not link into commercial edition | IEC 104 stack is built in-house from the purchased IEC 60870-5-104 spec |
| OpenDNP3 / pydnp3 | **archived — do not use** | Apache-2.0 (dead upstream) | N/A | Upstream unmaintained; in-house lean-build master planned (ADR-006 gate) or commercial Rust `dnp3` crate fallback |
| bacpypes3 | BACnet IP/MSTP | MIT | ✅ Clear | Selected over BAC0 per ADR-006 update (both MIT; bacpypes3 is the actively maintained async-native option) |
| pycomm3 | EtherNet/IP | MIT | ✅ Clear | |
| libiec61850 | IEC 61850 MMS | GPL-3.0 (OSS ed.) / Commercial (MZ Automation) | ⚠ Commercial license required for commercial edition | Budget approved (ADR-006 §6); procure before Sprint 27 |
| gurux-dlms-python | DLMS/COSEM — decision deferred to Phase 4 | GPL-2.0 / Commercial | ⚠ GPL — do not link into commercial edition without the commercial license | Build-vs-buy decision point at Phase 4 close (ADR-006) |
| RAUC | OTA (OS-level, not linked into xedge process) | LGPL-2.1 | ✅ Clear — used as an external OS tool, not a Python dependency | |
| cryptography (pyca) | PKI/TLS | Apache-2.0 / BSD | ✅ Clear | |
| bcrypt | password hashing | Apache-2.0 | ✅ Clear | |
| opentelemetry-python | observability | Apache-2.0 | ✅ Clear | |
| ruff, mypy, bandit, pytest | dev/test tooling only | MIT / Apache-2.0 | N/A — not shipped | |
| hatchling | build backend | MIT | N/A — not shipped | |

### 3.1 Candidates introduced by XEDGE-CRD-001 (ADR-012)

**None of these may be merged until its license is verified from the
distributed package metadata and this table is updated** (ADR-012 §0,
XEDGE-DR-001 D-12). Entries below record the *candidate* and the
*verification owed*, not a cleared decision.

| Library | Proposed use | License (claimed) | Verification status | Notes |
|---|---|---|---|---|
| amqtt | **Promotion from test-only to runtime** — embedded MQTT broker (ADR-012 §3) | MIT | ✅ Verified 2026-07-27 (prerequisite P-5, done in Sprint C5 rather than C4 as originally scheduled — recorded here rather than silently reassigned) | See §4 item 6 for the full P-5 write-up: license (own + full transitive closure), maintenance, and ARM-build finding |
| pycomm3 | EtherNet/IP CIP originator (ADR-012 §1) | MIT | ⏳ Verify in sprint C6 (prerequisite P-1) | **Explicit messaging only — does not implement CIP Class 1 implicit (cyclic) I/O.** See ADR-012 §1 and open item Q-7 before committing to sprint C7 |
| cpppo | EtherNet/IP fallback | ⚠ Unverified — understood to be copyleft with a commercial option | ⏳ Verify before considering | Not currently cleared for the commercial edition. Do not adopt without a license purchase decision |
| pysnmp (lineage) | SNMP manager + agent + notification originator/receiver (ADR-012 §2) | ⚠ Unverified | ⏳ Verify in sprint C7 (prerequisites P-3, P-4) | Project changed stewardship and PyPI naming is fragmented across forks — verify the exact package identity, license and maintenance status, **and confirm it supports the agent role**, not just manager |
| psycopg (or equivalent) | Fleet Manager Postgres driver (ADR-013 §5) | LGPL-3.0 (psycopg2) / Apache-2.0 (psycopg3) | ⏳ Verify at Delivery 2 planning | Central server only — never shipped on a gateway. Prefer psycopg3 for the permissive license |
| React, Vite, TypeScript + transitive npm tree | Fleet Manager SPA dashboard (ADR-013 §6) | MIT (direct deps) | ⏳ Full tree audit required at Delivery 2 | **Central server only — not shipped on a gateway.** The device UI keeps ADR-007's no-npm posture. An npm SBOM and CI dependency audit are in scope for that sprint (risk R-11) |

## 4. Open items

1. **asyncua legal review** — the MVP ships asyncua directly as the OPC UA
   runtime (ADR-006 §7 amendment, 2026-07-04), not merely as a CI test
   oracle. Confirm LGPL-3.0 obligations (dynamic linking / relinking rights)
   before this reaches the commercial edition; until reviewed, treat asyncua
   as GPL-edition-only. Building the open62541 binding removes this
   dependency entirely.
2. **IEC 60870-5-104 spec purchase** — required before Sprint 19 (in-house
   IEC 104 stack); budget approved per ADR-006 §6.
3. **IEEE 1815 (DNP3) spec purchase** — required before Sprint 20, contingent
   on the ADR-006 go/no-go gate.
4. **libiec61850 commercial license** — procure before Sprint 27.
5. **DLMS decision** — gurux vs. in-house build decided at Phase 4 close
   (now Delivery 2, P10).
6. **amqtt runtime promotion** (ADR-012 P-5) — ✅ **resolved 2026-07-27**
   (Sprint C5, not C4 as originally scheduled — the prerequisite table in
   ADR-012 assigned this to "Sprint C4" as the owner, but it wasn't acted
   on until the sprint that actually needed it; recorded rather than
   quietly absorbed).
   - **License** — `amqtt==0.11.3`: MIT, confirmed from installed package
     metadata (`pip show`), not assumed from this document's prior entry.
     Full transitive closure also verified individually from package
     metadata, not just amqtt's own declared dependencies: `dacite` (MIT),
     `passlib` (BSD), `psutil` (BSD-3-Clause), `pyyaml` (MIT, already a
     core dependency), `transitions` (MIT), `typer` (MIT), `websockets`
     (BSD-3-Clause, already a core dependency), and typer's own
     dependencies `rich` (MIT), `click` (BSD), `shellingham` (ISC),
     `typing-extensions` (PSF-2.0, confirmed via its dist-info
     `License-Expression` field after `pip show` returned a blank
     `License:` field for it — a newer PEP 639 metadata style, not a
     missing license), and `six` (MIT). All permissive; no commercial-
     edition exposure.
   - **Maintenance** — actively maintained: 0.11.3 (the version already
     installed) is the latest release, Python 3.10-3.13 support matrix,
     "Production/Stable" classifier.
   - **ARM footprint finding** — `psutil` (an amqtt dependency) ships
     pre-built wheels for `manylinux2014_aarch64` (64-bit ARM) but **none
     for armv7** (32-bit) as of 7.2.2, meaning it compiles from source on
     that target. Not currently expected to be a blocker: the Dockerfile's
     builder stage already carries a C toolchain (`build-essential` +
     `libffi-dev`, added Sprint 0/XEDGE-403 for `cffi`), which should cover
     psutil's own build needs too — but this is reasoned from the existing
     fix's scope, not confirmed by actually building the armv7 image with
     amqtt in the dependency tree. Verify that build explicitly before
     relying on this.
   - **RAM footprint against the ADR-007 1 GB target** — **not measured**.
     No ARM hardware or emulated target was available to test against from
     this development environment. Carried forward as part of open item
     Q-6 (HIL pass) rather than invented a number for — stated plainly
     rather than assumed fine.
   - **TLS/auth/ACL scope** — scoped as XEDGE-454, same sprint, per
     ADR-012 §3's requirement that this not be optional. Verification
     recorded here precedes the implementation work in this sprint's
     commit history — check XEDGE-454's own commit for delivered-vs-
     planned status if reading this after the fact.
7. **pycomm3 verification** (ADR-012 P-1) — license and maintenance from
   package metadata. Due sprint C6. **Blocked behind open item Q-7**
   (whether CIP implicit I/O is required), which decides whether a library
   is sufficient at all.
8. **SNMP library verification** (ADR-012 P-3, P-4) — package identity,
   license, maintenance, and **agent-role support**. Due sprint C7.
   Fallback if none clears: v1/v2c in-house, v3 escalated to the customer
   as a scope question.

## 5. Provenance record template (per in-house driver)

Copy this block into each in-house driver's module docstring or an adjacent
`PROVENANCE.md` once implementation begins:

```
Provenance record — <protocol name> driver
Specification(s) used: <name, version, section(s) referenced>
Reference/oracle implementations used for black-box testing only: <name, version>
Engineers who authored this driver: <names>
Confirmation: no GPL-licensed source of a reference implementation was read
or consulted by the authoring engineer(s) during development.
Date: <date>
```
