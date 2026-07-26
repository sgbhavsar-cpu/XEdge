# ADR-013: Central Management Platform

**Status:** ACCEPTED
**Date:** 2026-07-26
**Deciders:** Lead Architect, Project Owner
**Implements:** XEDGE-DR-001 decisions D-22, D-23, D-24, D-25, D-26, D-27
**Extends / partially supersedes:** [ADR-009](adr-009-fleet-management.md)
**Delivers:** XEDGE-CRD-001 §4.9 (Gateway Provisioning and Configuration),
plus the Phase 5 fleet scope from `sprint-planning.md` Sprints 29/30/32

## Context

The Fleet Manager built in Sprint 29 is a genuine working foundation:
device registration, heartbeat, pull-based config delivery, and a
device-token auth model, all covered by ADR-009. It is also, honestly
described:

- **SQLite-backed**, one file, no separate database service (ADR-009 §4)
- **No user accounts at all** — a single manager-wide `join_token` for
  enrolment and a single `admin_token` for every operator and CLI action
- **No tenancy** — one flat device registry
- **No UI** — REST API only; the Sprint 32 dashboard was never built
- **Device metadata limited to** `device_id`, `display_name`,
  `agent_version` and heartbeat-derived status

The project owner has directed that centralized device onboarding,
management and configuration from a central server is a first-class
deliverable, and that the platform should be **self-hosted and
multi-tenant capable**. XEDGE-CRD-001 §4.9 independently requires gateway
provisioning with specific metadata fields, a four-state connection model,
and certificate upload/management "via user account" — which the current
manager cannot express, because it has no concept of a user account.

The gap between what exists and what is being asked for is therefore not an
extension. It is a new product surface built on a working core.

## Decision

### 1. Evolve the existing Fleet Manager; do not restart

`xedge/fleet/manager_app.py` and `registry.py` remain the core. Their two
best properties are preserved unchanged:

- **Pull-based config delivery** (ADR-009 §1). Devices behind NAT with no
  inbound reachability are the normal case, not the exception. Every
  capability added below is delivered through the existing heartbeat
  response, never by the server calling into the device.
- **Hash-only secret storage** (ADR-009 §2). Tokens are verified, never
  stored.

### 2. Delivery split — what lands when

This is the load-bearing scheduling decision. The full platform does not
fit the committed CRD-001 window (XEDGE-DR-001 D-08), so it is split:

| Capability | Delivery 1 (by 2026-12-06) | Delivery 2 |
|---|---|---|
| Gateway metadata fields (serial, make, protocol, firmware) | ✅ | |
| Four-state connection model | ✅ (via ADR-011 state machine) | |
| Join-token → certificate onboarding | ✅ | |
| Certificate management and distribution | ✅ | |
| Remote configuration management | ✅ (extends existing pull path) | |
| Postgres migration + multi-tenancy | | ✅ |
| User accounts and RBAC on the manager | | ✅ |
| React SPA fleet dashboard | | ✅ |
| OTA orchestration | | ✅ |

**At CRD-001 handover the platform is single-tenant, `admin_token`-authed,
and API-only.** That is a deliberate, recorded interim state, not an
oversight — it satisfies every CRD §4.9 requirement while deferring the
scale-out work that no CRD requirement asks for.

### 3. Onboarding: join token now, certificate identity after

The enrolment flow, extending the existing `join_token` mechanism rather
than replacing it:

```
1. Operator provisions a one-time join token on the manager (bound to a
   device_id, single-use, time-limited).
2. Device boots with minimal config: manager URL + join token.
3. Device generates a keypair locally, submits a CSR with the join token.
4. Manager verifies the token, signs the CSR against the fleet CA, returns
   the device certificate and the CA chain.
5. Device discards the join token. All subsequent calls authenticate with
   the certificate (mTLS).
6. Full configuration arrives through the existing heartbeat pull path.
```

**The private key never leaves the device.** This is why it is a CSR flow
rather than the manager generating and shipping a keypair.

This closes ADR-009's explicitly deferred XEDGE-214 (mTLS), and closes
SR 1.2 (device identification) and SR 1.4 (identifier management) in the
IEC 62443 SL-1 gap analysis — both currently **Not Done** — while
delivering the CRD's "upload/manage root/CA certificates" and "secure
certificate deployment to gateways" requirements.

**Single-use, time-limited join tokens are a change from today's
manager-wide shared token.** The current model means one leaked token
enrols an unlimited number of impostor devices; that is acceptable for a
Sprint 29 spike and not acceptable for a delivered product.

**Certificate rotation** is in scope: a device may re-key over its existing
mTLS session before expiry. Without this, every deployed device breaks
simultaneously on certificate expiry — a failure mode worth designing out
rather than discovering.

### 4. Certificate management is shared, built once

The certificate subsystem lives in `xedge/security/` — currently an empty
package despite PKI being claimed as a Phase 3 outcome (finding F-11).

It has **two consumers from day one**, and this is why it is scheduled
early (Sprint C4) rather than alongside the fleet work:

- The fleet onboarding flow above
- The **MQTT northbound connector**, which today has no TLS at all
  (finding F-10) and ships credentials in clear

Building these separately would mean two CA abstractions, two trust stores
and two rotation stories. Sprint C4 builds one.

### 5. Multi-tenancy: Postgres with row-level tenant scoping

Delivery 2. Migrate the registry off SQLite to Postgres; every table
carries a `tenant_id` enforced at the query layer, with users, devices,
certificates and configuration all tenant-scoped.

Row-level scoping rather than schema-per-tenant or database-per-tenant:
it is the standard approach, keeps migrations single-path, and supports
cross-tenant operational reporting for the platform operator. The cost is
that tenant isolation is enforced by application code rather than by the
database — which means **every query path needs a test proving it cannot
leak across tenants**, and that test burden is part of the estimate.

Postgres rather than staying on SQLite: ADR-009 §4's reasoning (one row
update per device per 60s heartbeat) holds for a single small fleet and
stops holding for a multi-tenant platform with concurrent operator writes,
certificate issuance, and dashboard queries against the same file.

**This supersedes ADR-009 §4 for the central server only.** The *device's*
own cold store and config history stay SQLite — the 1 GB-RAM ARM target
reasoning is unchanged there, and nothing about this decision applies to
software running on a gateway.

### 6. Fleet dashboard: React + Vite + TypeScript, central server only

Delivery 2. The dashboard is a single-page application, served by the
Fleet Manager, consuming its REST API.

**This deliberately departs from ADR-007's decision** to avoid npm and
build pipelines entirely (vanilla JS, no third-party script sources — which
also satisfies IEC 62443 SR 2.4, mobile code). The departure is bounded and
the boundary is the important part:

| | Device-local Web UI | Fleet Manager dashboard |
|---|---|---|
| Stack | Jinja2 + vanilla JS | React + Vite + TypeScript |
| ADR | ADR-007, **unchanged** | This ADR |
| Runs on | the gateway (1 GB RAM ARM) | operator's server |
| SR 2.4 posture | preserved | new npm surface, needs SBOM |

The device — the constrained, field-deployed, security-sensitive component
— keeps ADR-007's posture exactly. The change applies only to a server-side
operator console where fleet-scale interactions (live grids over hundreds
of devices, filtering, bulk selection) genuinely do get awkward
server-rendered.

**Consequence that must be accepted, not assumed away:** an npm dependency
tree is a supply-chain surface. SBOM generation for the frontend, and
dependency audit in CI, are in scope for the sprint that introduces it —
not deferred to the security-debt backlog.

### 7. OTA by container image update, not RAUC

Delivery 2. The manager orchestrates a staged rollout by instructing
devices, through the heartbeat response, to pull and run a signed container
image; rollback re-pins the previous digest.

Chosen over the originally-planned RAUC A/B partition scheme (Sprint 30,
XEDGE-217) because RAUC requires a Yocto/Buildroot system image, bootloader
integration, and target hardware to test against — and the target gateway
hardware is currently unknown (XEDGE-DR-001 D-21). Container updates work
on any Docker/Podman host and are testable in CI today.

> **Limitation that must be stated to the customer, not buried:** container
> image update replaces the xEdge application. **It does not update the
> host OS or kernel.** A deployment needing OS-level patching in the field
> still needs RAUC or an equivalent. This is open item Q-5 in
> XEDGE-DR-001 §4.

RAUC A/B remains the planned upgrade once target hardware exists.

## Consequences

**Positive**

- The working core (pull-based delivery, hash-only secrets) is preserved;
  none of it is rewritten
- Certificate management is built once and serves both the fleet and the
  MQTT security gap, closing two SL-1 controls as a side effect
- The Delivery 1 / Delivery 2 split protects the committed customer date
  while keeping the full platform on a dated plan rather than a wish list
- ADR-007's security posture is preserved exactly where it matters — on the
  device

**Negative**

- Two UI stacks to maintain. Justified by the device/server split, but it
  is genuinely two stacks, and the fleet dashboard cannot reuse the
  device's existing templates and CSS.
- Application-enforced tenant isolation needs per-query-path testing; a
  missed `tenant_id` filter is a cross-tenant data leak, which is the worst
  failure mode this platform has.
- Migrating off SQLite means the Fleet Manager gains an external
  dependency, and `docker-compose.fleet.yml` gains a database container —
  a real increase in deployment complexity for the self-hosting customer.
- At CRD-001 handover the platform is single-tenant and API-only. If the
  customer expects a dashboard at handover, that expectation must be
  corrected **now**, not in November.

**Superseded from ADR-009**

- §2 (shared bearer tokens) — superseded by §3 above for device identity;
  `admin_token` remains until Delivery 2 introduces user accounts
- §4 (SQLite storage) — superseded by §5, **for the central server only**

## References

- XEDGE-CRD-001 §4.9, §5 item 6, §8.6
- XEDGE-DR-001 D-22..D-27, D-08, open item Q-5
- [ADR-009](adr-009-fleet-management.md) — the foundation this extends
- [ADR-007](adr-007-web-ui-architecture.md) — device UI posture, preserved
- [ADR-011](adr-011-serial-bus-and-connectivity.md) — supplies the
  four-state gateway connection model
- [iec62443-sl1-gap-analysis.md](../security/iec62443-sl1-gap-analysis.md)
  — SR 1.2, SR 1.4, SR 1.13, SR 3.1 all move on delivery of §3 and §4
