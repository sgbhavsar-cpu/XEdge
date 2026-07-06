# IEC 62443 SL-1 Gap Analysis

Sprint 18, XEDGE-141 — also closes the never-written Sprint 15 story
XEDGE-125 (this is that gap analysis, written once rather than as two
separate documents). Status is against `security-architecture.md` §3.1's
IEC 62443-3-3 SR table, assessed honestly against what's actually been
built and verified across this session's work (TLS → RBAC → Audit Log →
OpenTelemetry → Diagnostic CLI → this sprint), not against the target
design's full aspirational scope (that table describes SL-**2** intent;
this document is scoped to what's needed for an SL-**1** baseline —
gaps toward SL-2 are noted but not all blocking).

Legend: **Done** (built and verified this session or earlier) · **Partial**
(something real exists but doesn't fully meet the control) · **Not Done**
(no code addresses this yet) · **N/A** (out of xEdge's own scope — an
OS/network/deployment-layer control).

| SR | Requirement | Status | Notes |
|---|---|---|---|
| SR 1.1 | Human user identification and authentication | Partial | Local accounts + bcrypt + signed session cookies (`xedge/api/auth.py`). No JWT bearer tokens or X.509 client-cert auth (XEDGE-113, deferred — no non-browser API client exists yet to need them). |
| SR 1.2 | Software process and device identification | Not Done | No device X.509 identity, no TPM-backed key. |
| SR 1.3 | Account management | Done | RBAC + user CRUD (`/api/v1/users`, `/ui/users`), 4 roles × 10 permissions (`xedge/api/permissions.py`). |
| SR 1.4 | Identifier management | Not Done | Depends on SR 1.2's device identity cert, which doesn't exist. |
| SR 1.5 | Authenticator management | Partial | `UserStore.change_password()` exists; no certificate rotation API. |
| SR 1.6 | Wireless access management | N/A | Host-OS/NetworkManager concern, not xEdge's. |
| SR 1.7 | Strength of password-based authentication | Partial | bcrypt cost 12 ✓ (matches the target exactly). Minimum length is **8** characters (`xedge/api/ui.py::_MIN_PASSWORD_LENGTH`), not the 12 the target design names — no complexity policy beyond length. Concrete follow-up: raise to 12 and add a complexity check. |
| SR 1.8 | PKI certificates | Partial | Self-signed cert auto-generation (`xedge/api/tls.py`) — no ACME, no full PKI/CA integration. |
| SR 1.9 | Strength of public key authentication | Partial | RSA 2048 (confirmed in `tls.py`'s cert generation) — meets the ≥2048 target; ECDSA option not offered. |
| SR 1.10 | Authenticator feedback | Done | Generic "Invalid username or password" (doesn't confirm account existence); no password echo anywhere. |
| SR 1.11 | Unsuccessful login attempts | Done | `LoginAttemptTracker` — 5 failures / 15 min system-wide lockout. |
| SR 1.12 | System use notification | Not Done | No configurable login banner. |
| SR 1.13 | Access via untrusted networks | Partial | TLS ✓; mutual TLS not implemented (XEDGE-104's full scope, explicitly deferred in the TLS sprint). |
| SR 2.1 | Authorization enforcement | Done | `require_permission`/`require_permission_redirect` on every REST + Web UI route; every diagnostic command individually gated. |
| SR 2.2 | Wireless use control | N/A | Host-OS concern. |
| SR 2.3 | Portable/mobile devices | Not Done | No USB policy enforcement (this is a hardening-guide/OS-level item, not xEdge code). |
| SR 2.4 | Mobile code | Done | ADR-007's own decision: vanilla JS only, no npm/React build pipeline, no third-party script sources. |
| SR 2.5 | Session lock | Partial | Sessions have a sliding idle-timeout refresh (`require_permission`'s cookie refresh); no separate hard 15-minute diagnostic-session lock. |
| SR 2.6 | Remote session termination | Partial | Logout clears the cookie; no server-side revocation list — `SessionManager` is deliberately stateless (survives restart by design), so an issued token can't be remotely invalidated before its own expiry. A real fix needs a revocation store, trading away the current restart-survival property. |
| SR 2.7 | Concurrent session control | Not Done | No max-sessions-per-user enforcement. |
| SR 3.1 | Communication integrity | Partial | TLS for Web UI/REST API ✓ (Sprint 13). MQTT northbound TLS not implemented (XEDGE-105, deferred). |
| SR 3.2 | Malicious code protection | Not Done | No container image signing (XEDGE-145 — deferred this sprint; see the concrete follow-up below). |
| SR 3.3 | Security functionality verification | Done | `self-test` diagnostic command (XEDGE-137) — loopback driver round-trip, store round-trip, northbound connectivity check. |
| SR 3.4 | Software and information integrity | Not Done | No signed OTA bundles (no OTA system exists at all yet — Sprint 30 scope); config hash verification beyond JSON Schema validation doesn't exist. |
| SR 3.5 | Input validation | Done | JSON Schema validation on every config write and driver-type config (`ConfigValidator`, `build_driver_config`), enforced on every mutating REST/Web UI/diagnostic path. |
| SR 3.6 | Deterministic output | Partial | Watchdog restart + audit logging on driver failures exist; not "deterministic output" in the strict formal sense the SR describes. |
| SR 3.7 | Error handling | Partial | Structured JSON error responses everywhere (`HTTPException`, diagnostic `{"status": "error", ...}`); error messages do include some raw validation-error text (e.g. `ConfigValidationError`'s formatted message) — never a stack trace, but not fully sanitized either. |
| SR 3.8 | Session integrity | Partial | HMAC-SHA256-signed session tokens with sliding-window refresh; no JWT, no explicit rotation beyond that refresh. |
| SR 4.1 | Information confidentiality | Partial | TLS in transit ✓. No at-rest encryption for the audit log, config history, or cold store — all plain files on disk today. |
| SR 4.2 | Information persistence | Partial | Configurable retention (`store.retention_duration_seconds`) purges old tag data; no "decommission wipe" feature for a full data-bearing-device retirement. |
| SR 4.3 | Use of cryptography | Partial | bcrypt/HMAC-SHA256/RSA-2048 (all approved algorithms) — no FIPS 140-2 mode. |
| SR 5.1 | Network segmentation | N/A / Not Done | Zone/conduit enforcement is an OS/network-layer control outside xEdge's own code — documented in the new hardening guide, not implemented in xEdge. |
| SR 5.2 | Zone boundary protection | Done | `api.host` defaults to `127.0.0.1` (loopback-only) — verified this sprint via a real Docker container test that this default genuinely blocks non-loopback traffic (confirmed the hard way: a published Docker port couldn't reach the app until `api.host: 0.0.0.0` was explicitly set). |
| SR 5.3 | General purpose person-to-person comms | Done | No chat/email functionality exists. |
| SR 5.4 | Application partitioning | Done | Each driver instance runs as its own supervised asyncio task (`DriverSupervisor`), isolated from the pipeline and other instances; northbound is a fully separate subsystem (`NorthboundDispatcher`). |
| SR 6.1 | Audit log accessibility | Partial | Hash-chained, tamper-evident audit log, readable by the `auditor` role (`GET /api/v1/audit`, `/ui/audit`) — done. SIEM forwarding: not implemented (XEDGE-120, deferred; documented as a manual-log-shipping workaround in the hardening guide). |
| SR 6.2 | Continuous monitoring | Partial | OpenTelemetry tracing + Prometheus `/metrics` ✓ (Sprint 16). No built-in alerting or fleet-manager dashboard (no fleet manager exists — Sprint 29 scope). |
| SR 7.1 | Denial of service protection | Done | Per-IP rate limiting added this sprint (`xedge/api/rate_limit.py`, covers REST API + Web UI identically), plus pre-existing bounded queues (tag queue, ring buffers). |
| SR 7.2 | Resource management | Partial | Bounded queues/ring buffers prevent unbounded memory growth from tag data; no explicit process-level memory limit enforcement; watchdog is a liveness kick, not resource-exhaustion-triggered. |
| SR 7.3 | Control system backup | Done | `ConfigVersionHistory` (automatic versioned snapshots + rollback) and `GET`/`PUT /api/v1/config` serve as export/import. |
| SR 7.4 | Control system recovery | Not Done | No OTA/A-B rollback system exists (Sprint 30 scope); process-level restart is a deployment-layer (systemd/Docker `--restart`) concern, not xEdge code. |
| SR 7.5 | Emergency power | Done | Graceful shutdown on SIGTERM (`_wait_for_shutdown_signal`), SQLite WAL mode for the cold store. |
| SR 7.6 | Network and security configuration settings | Partial | This sprint's hardening guide (`docs/security/hardening-guide.md`) now exists; the diagnostic `config validate` command serves as an informal config-linting tool, but not a dedicated standalone one. |
| SR 7.7 | Least functionality | Partial | Minimal container runtime image (`python:3.11-slim`, no build tools); no formal unused-service audit performed. |
| SR 7.8 | Control system component inventory | Not Done | No SBOM published with releases. |

## Concrete, named follow-ups (in rough priority order)

1. **Container image signing (XEDGE-145)** — deferred this sprint because
   signing requires pushing to a real container registry with real
   credentials, neither of which exist in this environment; writing CI
   YAML that can't be exercised would be unverified infrastructure, not a
   tested feature. When a registry is available: add
   `sigstore/cosign-installer` + keyless OIDC signing
   (`cosign sign --yes <image>@<digest>`) as a step in
   `.github/workflows/ci.yml`'s `docker-build` job, gated on `push: true`
   with real registry credentials, then a policy-enforcement step
   (`cosign verify`) in the deployment pipeline.
2. **Raise minimum password length to 12 + add a complexity check** (SR 1.7)
   — a one-line change in `xedge/api/ui.py`'s `_MIN_PASSWORD_LENGTH` plus a
   small complexity check, small effort for a named, specific gap.
3. **SBOM publishing** (SR 7.8) — `pip install cyclonedx-bom` +
   `cyclonedx-py` in CI, publish alongside release artifacts.
4. **FIPS 140-2 mode** (SR 4.3) — would need auditing every crypto call
   site (`bcrypt`, `cryptography`, `hmac`) against a FIPS-validated
   provider; not attempted this pass.
5. **At-rest encryption for the audit log / config history / cold store**
   (SR 4.1) — currently plain files; would need a design decision on key
   management (where does the encryption key live?) before implementation.
6. **mTLS for the REST API/Web UI** (SR 1.13) and **MQTT TLS** (SR 3.1) —
   both explicitly deferred in the Sprint 13 TLS work; this pass didn't
   revisit them.
7. **SIEM forwarding for the audit log** (SR 6.1) — the log is already
   append-only JSONL, straightforward to tail with Fluentd/Vector/syslog-ng
   in the meantime; a built-in forwarder is still unbuilt.

## OWASP ZAP baseline scan (XEDGE-146)

A real scan — not a synthetic exercise — run this sprint:
`zaproxy/zap-stable`'s `zap-baseline.py -t http://host.docker.internal:18080`
against the live, real scratch xEdge instance (three configured drivers,
TLS/audit-log/OTel/rate-limiting all active), from a container reaching
the host via Docker Desktop's `host.docker.internal`.

**Result: 0 High, 0 Medium, 0 Low, 1 Informational.** 66 checks passed
outright, including the ones most likely to catch a real gap:
anti-clickjacking header, `X-Content-Type-Options`, `Strict-Transport-Security`,
cookie `Secure`/`HttpOnly`/`SameSite` flags, CSP, absence of the classic
information-disclosure/XSS/injection patterns ZAP's passive rules check
for.

**The one finding**: "Storable and Cacheable Content" (informational) on
three URLs — `/`, `/robots.txt`, `/sitemap.xml` — none of which exist
(`create_app()` has no route at `/`; ZAP's spider tried it as a starting
point and got a 404 with no explicit `Cache-Control` header). **Triaged as
accepted, not fixed**: a 404 response to a nonexistent path carries no
sensitive data, so its cacheability poses negligible real risk — IEC 62443
SL-1 is a baseline, not zero-informational-findings perfection, and adding
cache-control handling to every response (or every 404 specifically) for
this alone isn't proportionate. Worth revisiting if a future scan surfaces
it alongside something that actually returns sensitive data uncached.

**Scope caveat, stated honestly**: the baseline scan's spider only found 3
URLs — it has no session cookie, so it can't crawl past `/ui/login` into
the authenticated Web UI/REST surface (dashboard, config editor, users,
audit log, diagnostics). This scan verifies the *unauthenticated* attack
surface is clean; it does **not** cover the authenticated routes, which
are exactly where XEDGE-285 said the real risk lives (write-capable
endpoints). **Concrete follow-up**: a ZAP Automation Framework job with an
authenticated context (a login script driving `POST /api/v1/auth/setup`
+`/login`, then crawling with the resulting session cookie attached) would
close this gap — not attempted this pass, since it's meaningfully more
setup than a baseline scan and this sprint's time was better spent
verifying the parts that were actually reachable.
