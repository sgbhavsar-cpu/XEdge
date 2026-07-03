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
| amqtt | **test-only MQTT broker** (ADR-006) | MIT | ✅ Clear | Pure-Python asyncio broker; stands in for a real broker (Mosquitto) in integration tests so CI needs no external service |
| tahu (Sparkplug B ref impl) | **not used** | Apache-2.0 | N/A | Not installed; the in-house encoder is built from the Eclipse spec + public field-number tables, not this codebase |
| asyncua | **test oracle only** (ADR-006) | LGPL-3.0 | ⚠ Requires legal review before any packaging that links it | Used in CI as an OPC UA client/server simulator; not linked into the shipped runtime, which uses open62541 |
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

## 4. Open items

1. **asyncua legal review** — confirm CI-only usage (test oracle, not linked
   into any shipped artifact) is sufficient to avoid LGPL-3.0 obligations on
   the commercial edition. Track before Sprint 8 (OPC UA client work begins).
2. **IEC 60870-5-104 spec purchase** — required before Sprint 19 (in-house
   IEC 104 stack); budget approved per ADR-006 §6.
3. **IEEE 1815 (DNP3) spec purchase** — required before Sprint 20, contingent
   on the ADR-006 go/no-go gate.
4. **libiec61850 commercial license** — procure before Sprint 27.
5. **DLMS decision** — gurux vs. in-house build decided at Phase 4 close
   (before Sprint 28).

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
