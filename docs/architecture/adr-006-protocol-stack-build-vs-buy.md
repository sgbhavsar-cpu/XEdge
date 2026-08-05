# ADR-006: Protocol Stack Strategy — Build In-House vs. Use Libraries

**Document ID:** XEDGE-ADR-006
**Version:** 1.0
**Status:** ACCEPTED — 2026-07-03
**Date:** 2026-07-03

---

## 1. Context

xEdge ships under a dual license (GPL v3 community / commercial). Several protocol
libraries assumed in the current tech stack are GPL-only or dual GPL/commercial
(lib60870, libiec61850, gurux-dlms), one is dead upstream (OpenDNP3 / pydnp3 —
archived by Step Function I/O; successor is their **commercial** Rust `dnp3` crate),
and one was mischaracterized in earlier drafts (asyncua is pure-Python LGPL-3.0,
not an open62541 wrapper).

The question: should xEdge implement its own protocol stacks from the official
specifications rather than depend on third-party libraries?

This is a **per-protocol** decision, not a global one. The deciding factors:

1. **Protocol complexity** — engineering cost of a spec-compliant subset
2. **License of the best available library** — GPL contaminates the commercial edition
3. **Upstream maintenance health** — dead dependencies are a product risk
4. **Conformance certification demands** — some markets require certified stacks
5. **Differentiation value** — device-quirk handling and diagnostics depth are sellable

## 2. Legal constraint: clean-room discipline

Building "from scratch, taking ideas from open source" is safe for MIT/Apache/MPL
sources. For **GPL** sources (lib60870, libiec61850, gurux-dlms) it is NOT: an
engineer who reads GPL source and reimplements it risks creating a derivative work,
tainting the commercial edition.

**Rule:** in-house stacks for GPL-encumbered protocols MUST be developed from the
official specifications only. GPL implementations may be used solely as **black-box
test oracles** (run their binaries against our stack; never read their source).
Engineers assigned to an in-house stack must not have contributed to or studied the
corresponding GPL codebase. Document this in each driver's provenance record.

Consequence: the official specs must be purchased —
IEC 60870-5-104 (~USD 400), IEEE 1815 DNP3 (~USD 1,000), IEC 62056 DLMS series
(several documents). Modbus, Sparkplug B, and BACnet-relevant material are free or
low-cost.

## 3. Decision matrix

| Protocol | Decision | Rationale | Est. effort (in-house) |
|---|---|---|---|
| **Modbus RTU/TCP/RTU-over-TCP** | **BUILD** | Spec free & simple; MVP workhorse; owning RS-485 timing, byte-order variants, and quirk handling is differentiation. pymodbus (MIT) kept as test oracle only. | 2–4 eng-weeks |
| **Sparkplug B encoder/state machine** | **BUILD** | Protobuf schema + birth/death/seq/alias state machine; spec free (Eclipse). tahu reference impl not production-grade. | 2–3 eng-weeks |
| **IEC 60870-5-104 (controlling station)** | **BUILD** (lean) | lib60870 is GPL/commercial — fees due either way; APCI framing + k/w flow control + ~30 ASDU types is bounded scope. Requires purchased spec. | 6–10 eng-weeks |
| **DNP3 master** | **LEAN BUILD** — decision gate after IEC 104 ships | pydnp3 dead; alternatives are commercial-only. Harder than IEC 104 (transport segmentation, unsolicited). Gate: if IEC 104 in-house effort stays on budget, proceed; else license commercial `dnp3` crate. | 10–16 eng-weeks |
| **DLMS/COSEM client** | **BUILD or BUY — open** | Gurux is GPL v2/commercial (fees due either way); read-mostly client subset is bounded. Genuinely close call; decide in Phase 4 planning. | 8–12 eng-weeks |
| **OPC UA client + server** | **USE LIBRARY — open62541** (C, MPL 2.0) via in-house asyncio C-extension binding | Spec is thousands of pages; compliant stack (security policies, sessions, subscriptions, CTT) is a multi-year product. open62541 chosen over asyncua: MPL 2.0 is license-cleanest for the commercial edition and the C core fits the existing C-extension pattern. asyncua (LGPL-3.0) permitted only as a test oracle / CI simulator. | binding layer: 3–5 eng-weeks |
| **IEC 61850 MMS** | **USE LIBRARY** (libiec61850 commercial license) | MMS alone is enormous; industry-standard path is MZ Automation commercial license. | not viable |
| **IEC 61850 GOOSE/SV subscribers** | **BUILD later (carve-out)** | Pure raw-Ethernet frame decoding; separable from MMS; feasible in-house in Phase 5. | 3–5 eng-weeks |
| **BACnet IP** | **USE LIBRARY** (bacpypes3, MIT) | No licensing pressure; maintained; revisit only if it blocks. | — |
| **BACnet MS/TP** | **USE LIBRARY** (`bacnet-stack`, GPL-2.0-or-later WITH GCC-exception-2.0 on its core files, MIT on headers/glue) | **Corrected 2026-08-05 — bacpypes3 does not implement MS/TP at all; the row above previously conflated IP and MS/TP under one entry and one license.** `bacnet-stack`'s exception permits linking into the commercial edition; integrated as a separate daemon process per RS-485 port (Sprint P7), not a from-scratch clean-room build. Full detail: `license-audit.md` §3/§4 item 11. | daemon + IPC layer + Python driver, ~30 eng-days (Sprint P7 revised estimate) |
| **EtherNet/IP (CIP)** | **USE LIBRARY** (pycomm3, MIT) | No licensing pressure; monitor maintenance health. | — |
| **EtherNet/IP (CIP)** | **USE LIBRARY** (pycomm3, MIT) | No licensing pressure; monitor maintenance health. | — |
| **PROFINET IO** | **BUILD (forced)** | No mature open option exists; already planned as custom C extension. | 3 sprints (per plan R-05) |

## 4. Consequences

**Positive:**
- Commercial edition ships without GPL contamination for Modbus, Sparkplug B, IEC 104
  (and potentially DNP3/DLMS) — license fees limited to IEC 61850.
- Dead-upstream risk (pydnp3) eliminated for a Tier-2 protocol.
- Native asyncio drivers on the hot path — no thread-executor hops for the
  highest-volume protocols; helps the 50k tags/s NFR.
- Driver-framework template (BaseDriver ABC) gets exercised by in-house stacks
  first, hardening it before third-party integrations.

**Negative / accepted:**
- Spec purchase budget required (~USD 1,500–3,000 total for 104 + DNP3 + DLMS).
- In-house stacks bear their own conformance burden (DNP3 conformance test
  procedures, IEC 104 interop testing) — mitigated by black-box testing against
  reference implementations and simulators.
- Sprint plan impact: Sprint 2–4 Modbus stories change from "integrate pymodbus"
  to "implement Modbus stack" (+~1 sprint in Phase 1); IEC 104 in Phase 4 gains
  ~2 sprints; buffer already exists via R-02/R-05 allocations.

## 5. Cross-document updates (applied 2026-07-03)

- ✅ system-architecture.md: §3.2 driver table, §3.6 OPC UA server implementation,
  §7.2 protocol table, §7.4 northbound table, §8 ADR-006 summary entry
- ✅ development-plan.md: R-01 mitigation updated with clean-room rule; new risks
  R-09 (in-house stack interop) and R-10 (open62541 binding effort); Phase 1 and
  Phase 4 outcomes updated
- ✅ sprint-planning.md: XEDGE-010 (spec procurement), XEDGE-015/015b (in-house
  Modbus codec + client), XEDGE-024 (in-house Sparkplug encoder), XEDGE-059/060
  (RTU framing), XEDGE-067a (open62541 binding layer), XEDGE-074 (open62541
  server), XEDGE-148/149 (in-house IEC 104), Sprint 20 gate + XEDGE-154/155
  (in-house DNP3), XEDGE-160 (bacpypes3), XEDGE-206 (DLMS decision point)
- ✅ HLR: new constraint CON-006 (clean-room provenance requirement)

## 6. Resolutions (accepted 2026-07-03)

The decision matrix (§3) was accepted as recommended, with these defaults applied:

1. **Motivation** — commercial licensing freedom and upstream maintenance control
   are the primary drivers; performance and IP value are secondary benefits.
2. **Team capacity** — the documented 10-engineer / 18-month plan remains the
   baseline. **Standing review point:** if actual staffing falls short, the
   in-house scope contracts in this order: drop DLMS build → drop DNP3 build
   (license commercial `dnp3` crate) → keep Modbus + Sparkplug B + IEC 104.
3. **Spec & license budget** — assumed approved: IEC 60870-5-104 and IEEE 1815
   documents purchased in Phase 1 (added to Sprint 1 license-audit story);
   MZ Automation libiec61850 commercial license procured before Sprint 27.
4. **MVP protocol scope** — unchanged from HLR Tier-1: Modbus (in-house) +
   OPC UA client/server (open62541). Sparkplug B encoder in-house.
5. **Conformance** — in-house stacks validated by black-box testing against
   reference implementations and protocol simulators; formal DNP3 conformance
   certification deferred until a customer requires it (tracked as risk R-09
   in the development plan).

## 7. Amendment: asyncua as an interim OPC UA runtime for the MVP (2026-07-04)

**Context:** building the open62541 asyncio C-extension binding (§3, §6.4) is
itself a multi-week project with real technical risk (C build toolchain
across amd64/arm64/armv7, cffi/ctypes surface design, event-loop bridging) —
disproportionate to gate a software MVP on, when the goal is a working,
testable edge stack rather than the final production binding.

**Decision:** for the MVP, the OPC UA client and server are implemented
directly against `asyncua` (pure Python, LGPL-3.0) as the runtime library,
not merely as a test oracle. The open62541 C-extension binding remains the
ADR-006 §3 target for the commercial edition (MPL-2.0 is license-cleaner);
swapping it in later should not require changing `BaseDriver`/
`NorthboundConnector` call sites, only the OPC UA driver/server's internal
implementation.

**Consequences:**
- The GPL edition may ship asyncua as-is (LGPL-3.0 is compatible with GPL
  v3). The **commercial edition must not ship asyncua** until the LGPL-3.0
  obligations are reviewed by counsel (dynamic linking / relinking rights)
  — tracked as an open item (§4 update below) — or until the open62541
  binding replaces it, whichever comes first.
- Effort estimate for the eventual open62541 binding (§3 table) is
  unchanged; this amendment only sequences *when* it's built, not whether.
- Any OPC UA behavior specific to asyncua's implementation choices (error
  codes, session defaults) should be treated as incidental, not part of the
  driver's public contract, so the future swap stays low-risk.
