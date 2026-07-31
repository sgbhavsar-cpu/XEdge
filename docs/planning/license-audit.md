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
| pycomm3 | EtherNet/IP | MIT | ✅ Clear — re-verified 2026-07-28, see §3.1 | **Not actively maintained** (upstream's own notice — see §3.1); explicit messaging only, no CIP Class 1 implicit I/O (ADR-012 §1, XEDGE-DR-001 Q-7 resolved: accepted for Sprint C7) |
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
| pycomm3 | EtherNet/IP CIP originator (ADR-012 §1) | MIT | ✅ Verified 2026-07-28 (prerequisite P-1, due Sprint C6, done at the C6/C7 boundary once Q-7 below cleared it to matter). See §4 item 7 for the full write-up: license, zero-dependency footprint, and the "no longer actively developed" maintenance finding | **Explicit messaging only — does not implement CIP Class 1 implicit (cyclic) I/O.** ADR-012 §1/XEDGE-DR-001 Q-7 **resolved 2026-07-28**: explicit messaging at a scan interval accepted, Sprint C7 proceeds at its existing estimate |
| cpppo | EtherNet/IP fallback | ⚠ Unverified — understood to be copyleft with a commercial option | ⏳ Verify before considering | Not currently cleared for the commercial edition. Do not adopt without a license purchase decision |
| pysnmp | SNMP manager + agent + notification originator/receiver (ADR-012 §2) | BSD-2-Clause | ✅ Verified 2026-07-30 (prerequisites P-3, P-4, due Sprint C7, done at the C7/C8 boundary — same pattern as pycomm3's P-1). See §4 item 8 for the full write-up | Stewardship changed (Ilya Etingof → LeXtudio Inc.) but the PyPI naming fragmentation ADR-012 flagged has since resolved to one canonical package — see §4 item 8 |
| pyasn1 | pysnmp's BER encoding dependency | BSD-2-Clause | ✅ Verified 2026-07-30, see §4 item 8 | Actively maintained (0.6.4, released 2026-07-09) under a different small maintainer team than pysnmp/pysmi's LeXtudio lineage |
| pysmi | MIB upload/parse (XEDGE-485, ADR-012 §2) | BSD-2-Clause | ✅ Verified 2026-07-30, see §4 item 8 | Same LeXtudio lineage as pysnmp |
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
   - **TLS/auth/ACL scope** — delivered as XEDGE-454, same sprint, per
     ADR-012 §3's requirement that this not be optional: TLS listener,
     username/password auth (`FileAuthPlugin`, off by default —
     `allow_anonymous` must be explicitly set), and publish/subscribe
     topic ACLs (`TopicAccessControlListPlugin`), all against a real
     client in `tests/integration/test_mqtt_broker.py`, not mocked. Two
     more amqtt-specific findings surfaced verifying this (the ACL
     plugin's publish/subscribe asymmetry, and `Broker.shutdown()`
     hanging on an unhandshaked connection) — see
     `xedge/northbound/mqtt_broker.py`'s module docstring and
     crd-delivery-plan.md's Sprint C5 notes for the full write-up, not
     duplicated here.
7. **pycomm3 verification** (ADR-012 P-1) — ✅ **resolved 2026-07-28**
   (due Sprint C6; genuinely blocked behind Q-7 until that same date, not
   simply late — see XEDGE-DR-001 Q-7 and ADR-012 §1 for the resolution).
   - **License** — `pycomm3==1.2.16`: MIT, confirmed from installed
     package metadata (`pip show` and `importlib.metadata`'s
     `Classifier: License :: OSI Approved :: MIT License`), not assumed
     from this document's prior entry. **Zero runtime dependencies** —
     `Requires-Dist` lists only `pytest` under a `tests` extra — so there
     is no transitive closure to verify at all, unlike amqtt's.
   - **Maintenance — a real finding, not a clean bill of health.**
     1.2.16 (confirmed the latest release, uploaded 2025-12-22 per PyPI)
     is recent, but the project's own PyPI listing carries an explicit,
     prominent notice: **"⚠️ NOTE: pycomm3 is no longer actively
     developed."** Quoted verbatim, not paraphrased — verified by
     fetching the PyPI project page directly, not inferred from the
     release cadence alone. This does not reverse ADR-012 D-13's buy
     decision (the only licensed alternative, `cpppo`, carries a
     copyleft/commercial-license risk this document already flags as
     worse), but it is a real, ongoing risk for a customer deployment
     expected to outlive this delivery: no upstream fix is coming if
     Rockwell firmware changes break something this library depends on.
     Stated plainly here rather than left for whoever hits the eventual
     break to discover contextless.
   - **Trove classifiers list Python 3.6–3.10, not 3.11/3.12** (this
     project's own floor) — checked empirically rather than treated as a
     hard stop: it imports and runs cleanly on the Python 3.12
     environment this delivery develops against. Consistent with the
     "no longer actively developed" finding above (classifiers were
     never updated, not evidence of an actual incompatibility) rather
     than a second, separate problem.
   - **CIP scope** — confirms ADR-012 §1's own finding: explicit
     (acyclic) messaging only, no Class 1 implicit (cyclic) I/O. XEDGE-
     DR-001 Q-7 resolved 2026-07-28 (explicit messaging at a scan
     interval accepted), so this is no longer a blocking unknown, just
     the confirmed shape of what Sprint C7 integrates.
8. **SNMP library verification** (ADR-012 P-3, P-4) — ✅ **resolved
   2026-07-30** (due Sprint C7; done at the C7/C8 boundary, same pattern
   as item 7's P-1 — recorded rather than silently reassigned). **Both
   prerequisites cleared: no fallback to v1/v2c in-house is needed, and
   v3 does not need to be escalated to the customer as a scope cut.**
   - **Package identity — the fragmentation ADR-012 flagged has resolved
     itself.** The stewardship handoff (Ilya Etingof → LeXtudio Inc.,
     2022) briefly produced two live PyPI names, `pysnmp` (old, orphaned)
     and `pysnmp-lextudio` (the fork). That period is over: fetching
     `pysnmp-lextudio`'s own PyPI page today returns its current
     description verbatim — **"A deprecated package. Please use 'pysnmp'
     instead... LeXtudio Inc. has taken control of PySNMP PyPI packages,
     so please use pysnmp package instead."** LeXtudio now publishes
     under the plain `pysnmp` name (confirmed via `lextudio/pysnmp` on
     GitHub, not assumed from the PyPI blurb alone). There is one
     canonical package to depend on, not a fork to choose between.
   - **License** — `pysnmp==7.1.27` (latest, released 2026-05-16):
     BSD-2-Clause, confirmed both from PyPI metadata and the repo's own
     `pyproject.toml` (`license = "BSD-2-Clause"`), not the PyPI page's
     prose alone. Sole runtime dependency `pyasn1>=0.6.3`; the installed/
     current `pyasn1==0.6.4` (released 2026-07-09) is also BSD-2-Clause,
     under a distinct small maintainer team (Christian Heimes, Simon
     Pichugin — not LeXtudio), confirmed via its own PyPI page. No
     transitive dependency beyond that one.
   - **Maintenance — clean, unlike pycomm3's finding.** `lextudio/pysnmp`
     is not archived and was pushed as recently as 2026-05-19; PyPI's own
     classifier reads **"Development Status :: 5 - Production/Stable"**
     (quoted verbatim), and `pyproject.toml` declares Python 3.10–3.14
     support — this project's 3.12 floor sits comfortably inside that
     range, not at its edge the way pycomm3's stale 3.6–3.10 classifiers
     did. No deprecation notice of any kind on the package itself (only
     on the now-retired `pysnmp-lextudio` transitional name, above).
   - **Agent-role support (P-4) — confirmed from real example code, not
     marketing copy.** "Supports the agent role" is exactly the kind of
     claim a library's own description could overstate (SNMPv3's RFCs
     call every SNMP-speaking node an "entity" whether manager or agent,
     so generic language proves nothing on its own). Checked instead
     against `lextudio/pysnmp`'s own example tree
     (`examples/v3arch/asyncio/`), which carries a dedicated `agent/`
     directory alongside `manager/` and `proxy/` — not a single shared
     module with an agent flag bolted on:
     - `agent/cmdrsp/` (command *responder*, i.e. the agent side that
       answers GET/GETNEXT/GETBULK/SET): `implementing-scalar-mib-
       objects.py`, `implementing-snmp-table.py`,
       `detailed-vacm-configuration.py` (View-based Access Control),
       `multiple-usm-users.py`, `custom-mib-controller.py`,
       `listen-on-ipv4-and-ipv6-interfaces.py` — a genuinely-featured
       agent, covers XEDGE-482.
     - `agent/ntforg/` (notification originator) and
       `manager/ntfrcv/` (notification receiver) cover both TRAP/INFORM
       directions (XEDGE-483/484): `v1-trap.py`, `v2c-trap.py`,
       `v2c-inform.py`, `v3-trap.py`, `send-inform-to-multiple-
       managers.py`.
     - `manager/cmdgen/` (command generator, the classic manager role,
       XEDGE-481) has dedicated GETBULK examples
       (`getbulk-fetch-scalar-and-table-variables.py`,
       `getbulk-multiple-oids-to-eom.py`), GETNEXT, v1 GET, v2c SET, and
       — the specific thing ADR-012 §2 called "the deciding factor" —
       **SNMPv3 USM auth *and* privacy together**:
       `usm-sha-aes128.py` demonstrates SHA authentication with AES-128
       privacy in one example, not auth-only.
     - A second, higher-level API (`examples/hlapi/v3arch/asyncio/`) also
       ships separate `agent/` and `manager/` trees, so there is a choice
       of ergonomics for whichever role's implementation needs it.
   - **SNMPv3 privacy (AES/DES) needs no new crypto dependency.** Reading
     `pysnmp/proto/secmod/rfc3414/priv/des.py` and
     `pysnmp/proto/secmod/rfc3826/priv/aes.py` directly shows both import
     `cryptography.hazmat...` (inside a `try/except ImportError`, so it's
     a soft dependency at the code level) — the exact same `cryptography`
     package already in this project's own runtime dependencies since
     Sprint 13 (TLS/PKI). `pysnmp`'s own `pyproject.toml` pins
     `cryptography>=44.0.1` under its `dev` extra (a misleading name —
     that group actually gates several optional runtime features, privacy
     included, not just dev tooling); our own floor (`>=42.0`) needs
     bumping to match when SNMPv3 privacy is implemented, not a new
     dependency added.
   - **MIB upload/parse/browse (XEDGE-485) needs one new dependency,
     `pysmi`.** Same LeXtudio lineage as `pysnmp` (`pysmi==2.0.0`,
     released 2026-04-26; BSD-2-Clause, confirmed from PyPI metadata and
     `lextudio/pysmi`'s GitHub license detection independently) —
     "Production/Stable," not archived. Its GitHub repo's last push
     matches its release date exactly (2026-04-26), about three months
     stale relative to `pysnmp`'s own most recent push (2026-05-19) at
     the time of this check — worth naming plainly rather than glossing
     over, though not a red flag on its own for a MIB-compiler utility
     library, which has far less surface area to need fixing than a full
     protocol stack. Installing it pulls in one new transitive
     dependency, `lark` (its ASN.1/SMI grammar parser) — MIT, confirmed
     via `pip show lark` after installing, zero further transitive
     dependencies of its own (`requests`, its only other declared
     dependency, is already in this project's tree via `asyncua`).

9. **`click` CVE (PYSEC-2026-2132) — tracked, not fixed; fix is blocked
   upstream, not skipped.** Found by `pip-audit` during Sprint H1's
   Delivery 1 closeout (the first time `pip-audit` was run against this
   project — a gap in the quality-gate practice through Sprints 0–H1,
   not specific to this finding). A command-injection vulnerability in
   `click.edit()`, fixed upstream in `click==8.3.3`.
   - **Not reachable from this codebase.** `click` is not a direct
     dependency (absent from `pyproject.toml`) — it arrives transitively
     via `amqtt`'s `typer` dependency (amqtt's own CLI tooling) and via
     `uvicorn`. Nothing in `xedge/` imports `click` or `typer`, and
     `amqtt` is used here exclusively as a library (the embedded broker,
     item 6) — its `typer`-based CLI entry points are never invoked.
     There is no execution path in this product that reaches
     `click.edit()`.
   - **Attempted fix, found genuinely blocked, not just inconvenient.**
     Floor-pinning `click>=8.3.3` directly fails dependency resolution:
     `amqtt==0.11.3` *and* the latest `amqtt==0.11.4` both pin
     `typer==0.15.4` exactly (not a range), and `typer==0.15.4` requires
     `click<8.2,>=8.0.0` — confirmed from both packages' own installed/
     downloaded metadata, not assumed from changelog prose. No released
     `amqtt` version relaxes this. Forcing an override would install a
     `click` version against `typer`'s own declared incompatibility,
     risking a real breakage in `typer`'s code for zero benefit, since
     the vulnerable function is unreachable either way — rejected as a
     worse trade than the CVE itself.
   - **Actual resolution path**: upstream. Either `amqtt` relaxes its
     exact `typer` pin, or `typer` itself is bumped by an `amqtt`
     release, unblocking a compatible `click`. Re-check on the next
     `amqtt` version bump (already tracked whenever item 6 is revisited)
     rather than opening a separate recurring task.

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
