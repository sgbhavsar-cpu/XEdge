# xEdge — Security Architecture

**Document ID:** XEDGE-ARCH-002  
**Version:** 1.0  
**Status:** Draft  
**Date:** 2026-07-03  

---

## 1. Threat Model

### 1.1 Trust Zones

```
┌─────────────────────────────────────────────────────────────────────┐
│  Zone 0: OT Field Network (untrusted data source)                   │
│  PLCs, IEDs, RTUs — no authentication assumed on southbound ports   │
│  xEdge is the security boundary between Zone 0 and Zone 1           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ Southbound protocol drivers
┌──────────────────────────────▼──────────────────────────────────────┐
│  Zone 1: xEdge Device (partially trusted, hardened Linux)           │
│  The edge process; all internal communication is process-local       │
│  Physical access = highest threat; assume physical hardening         │
└──────────┬──────────────────────────────────────────────────────────┘
           │ Northbound: mTLS to cloud/fleet
           │ Management: mTLS to operators
┌──────────▼──────────────────────────────────────────────────────────┐
│  Zone 2: IT / Cloud (trusted with verification)                      │
│  MQTT broker, fleet manager, SIEM — authenticated via certificates   │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 Threat Actors

| Actor | Capability | Primary Concern |
|---|---|---|
| **Insider / rogue operator** | Physical + local network access | Config tampering, data exfiltration |
| **Remote attacker (internet)** | Exploiting exposed services | RCE, credential theft, OTA hijacking |
| **Supply chain attacker** | Compromised dependency or OTA bundle | Malicious code execution |
| **Man-in-the-middle** | Network between device and cloud | Data integrity, command injection |
| **Physical attacker** | Device theft, hardware implant | Private key extraction, firmware replacement |

### 1.3 Attack Surface

| Surface | Exposure | Control |
|---|---|---|
| OPC UA server (4840/TCP) | LAN only | Security policy ≥ Basic256Sha256; certificate auth |
| REST / gRPC API (8443/TCP) | LAN only | mTLS + RBAC; IP allowlist optional |
| Web UI (same port as REST API, §3.9) | Loopback by default | Session-cookie auth, CSRF protection, lockout; **write-capable, so treated as at least as sensitive as the REST API, not less** — single-user/no-RBAC until Sprint 14 (ADR-007) |
| MQTT northbound | Internet (outbound) | mTLS client certificate; MQTT ACLs |
| Fleet management | Internet (outbound) | mTLS + signed payloads |
| Serial ports (southbound) | Physical | No software control; physical security + driver input validation |
| SSH (OS-level) | Configurable | Recommend key-only, disabled in production or behind jump host |
| Diagnostic WebSocket | LAN only | Authenticated session; audit logged |

---

## 2. Security Controls

### 2.1 Identity & Authentication

**Device Identity:**
- Each xEdge device has a unique X.509 identity certificate issued by the organization's CA
- Certificate stored in TPM 2.0 (where available) or encrypted file keystore
- Subject: `CN=xedge-<device-serial>, OU=xEdge, O=<org>`
- Used for: fleet manager mTLS, MQTT broker mTLS, audit log signing

**User Authentication:**
- Local accounts: username + bcrypt-hashed password (Argon2id supported)
- Token-based API auth: JWT (HS256 → HS512 or RS256) with configurable expiry
- X.509 client certificate authentication supported for service accounts
- MFA: TOTP supported for admin role (optional, recommended for NERC CIP)

**Session management:**
- REST API tokens expire after configurable TTL (default: 24 hours)
- Token revocation list maintained in memory; flushed on restart (or persisted to disk)
- Idle session timeout for diagnostic CLI: default 15 minutes

**Interim exception — Web UI, day one through Sprint 14 (ADR-007, HLR §4.9,
FR-WU-008):** the local Web UI does not yet implement the JWT/RBAC model above.
Instead: one local account, created via a mandatory first-login password-setup flow
(no shipped default credential); bcrypt (cost ≥ 12, same algorithm/cost as the
target model above — no migration needed later); a signed, HttpOnly,
SameSite=Strict session cookie instead of a JWT (there is only one user/role, so a
JWT's claims and revocation-list machinery has nothing to add yet); the same
15-minute idle timeout as the diagnostic CLI; a 5-attempt lockout. This is a
**documented, temporary narrowing of scope**, not a divergent security model — when
Sprint 14 ships, the single account is promoted to the `admin` role and the UI
starts enforcing the full permission matrix below, with no password migration.

### 2.2 Authorization (RBAC)

**Permission matrix:**

| Permission | admin | operator | auditor | readonly |
|---|---|---|---|---|
| tag:read | ✓ | ✓ | ✓ | ✓ |
| tag:write | ✓ | ✓ | ✗ | ✗ |
| config:read | ✓ | ✓ | ✓ | ✗ |
| config:write | ✓ | ✓ | ✗ | ✗ |
| driver:restart | ✓ | ✓ | ✗ | ✗ |
| security:manage | ✓ | ✗ | ✗ | ✗ |
| user:manage | ✓ | ✗ | ✗ | ✗ |
| audit:read | ✓ | ✗ | ✓ | ✗ |
| ota:trigger | ✓ | ✗ | ✗ | ✗ |
| diagnostics:run | ✓ | ✓ | ✗ | ✗ |

Custom roles can be defined with any combination of the above permissions.

**RBAC enforcement:**
- Every API endpoint is annotated with required permissions
- RBAC check occurs after authentication, before any business logic
- Denied requests return HTTP 403 + structured error body
- All authorization decisions (allow AND deny) are audit-logged

### 2.3 Transport Security

**TLS configuration (default):**
```yaml
tls:
  min_version: "TLSv1.2"      # TLS 1.3 preferred; 1.2 for legacy device compat
  preferred_version: "TLSv1.3"
  ciphers_tls12:               # TLS 1.2 fallback ciphers only
    - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
    - TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256
    - TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
  # TLS 1.3 ciphers are determined by the TLS 1.3 spec; no config needed
  verify_peer: true
  check_hostname: true
```

**Certificate validation:**
- Full chain validation against configured CA bundle
- CRL check (if CA publishes CRL endpoint)
- OCSP stapling supported (optional)
- Certificate pinning configurable per endpoint

### 2.4 Data-at-Rest Encryption

**Sensitive config values:**
- Stored encrypted in `config.enc.yaml`
- Encryption key derived from device secret (TPM-sealed or OS keyring)
- AES-256-GCM with per-field nonces

**Store-and-forward database (optional):**
- Full database encryption via SQLCipher or LUKS volume
- Enabled via `store.encryption: true` in config
- Key stored in TPM / OS keyring

**Audit log:**
- Stored in plaintext (readable by `auditor` role)
- Integrity protected via hash chain (not encrypted; must be readable for compliance)

### 2.5 Secure Boot & Image Integrity

**Supported schemes:**
1. **UEFI Secure Boot** (x86 industrial PCs): kernel + bootloader signed with MOK key
2. **U-Boot Verified Boot** (ARM SBCs): FIT image with RSA signature verification
3. **Container image signing** (Docker deployments): cosign + GHCR attestations

**xEdge binary integrity:**
- Docker image: signed with cosign; SHA-256 digest pinned in systemd unit / compose file
- Bare-metal: signed manifest checked on service start via `xedge-verify-integrity` helper
- Failed integrity check → alert (configurable: warn / abort startup)

### 2.6 Vulnerability & Patch Management

**Supply chain:**
- All Python dependencies pinned via `requirements.lock` (pip-tools)
- SBOM generated at build time (syft → CycloneDX JSON)
- Dependabot / Renovate configured for automated PR on CVE

**Runtime scanning:**
- `pip-audit` runs in CI on every commit
- Grype (container vulnerability scanner) runs on every Docker image build
- Results published to GitHub Security Advisory tracker

**Patch SLA:**
- Critical (CVSS ≥ 9.0): patch release within 7 days
- High (CVSS 7.0–8.9): patch release within 30 days
- Medium/Low: bundled in next scheduled release

---

## 3. Compliance Mapping

### 3.1 IEC 62443-3-3 Security Level 2 Control Mapping

| SR | Requirement | xEdge Control |
|---|---|---|
| SR 1.1 | Human user identification and authentication | Local accounts + JWT + X.509 cert auth |
| SR 1.2 | Software process and device identification | Device X.509 identity cert (TPM-backed) |
| SR 1.3 | Account management | RBAC config + admin API for user CRUD |
| SR 1.4 | Identifier management | Unique device CN in cert subject |
| SR 1.5 | Authenticator management | Cert rotation API + password management |
| SR 1.6 | Wireless access management | Wi-Fi interface config via NetworkManager |
| SR 1.7 | Strength of password-based authentication | bcrypt cost ≥ 12; min 12-char password policy |
| SR 1.8 | PKI certificates | Full PKI support, ACME + manual rotation |
| SR 1.9 | Strength of public key authentication | RSA ≥ 2048 or ECDSA P-256/P-384 |
| SR 1.10 | Authenticator feedback | No plaintext password echo; masked in logs |
| SR 1.11 | Unsuccessful login attempts | Lockout after N failures (configurable, default 5) |
| SR 1.12 | System use notification | Configurable login banner |
| SR 1.13 | Access via untrusted networks | All remote access via mTLS; no plaintext |
| SR 2.1 | Authorization enforcement | RBAC on all endpoints |
| SR 2.2 | Wireless use control | Wi-Fi managed by host OS; xEdge does not bypass |
| SR 2.3 | Use of portable and mobile devices | USB policy documented in hardening guide |
| SR 2.4 | Mobile code | No JS/scripting from untrusted sources |
| SR 2.5 | Session lock | Diagnostic CLI idle timeout (15 min) |
| SR 2.6 | Remote session termination | Token revocation API |
| SR 2.7 | Concurrent session control | Configurable max sessions per user |
| SR 3.1 | Communication integrity | TLS for all external comms |
| SR 3.2 | Malicious code protection | Container image signing + integrity check |
| SR 3.3 | Security functionality verification | Self-test command (FR-RD-004) |
| SR 3.4 | Software and information integrity | Signed OTA bundles; config hash verification |
| SR 3.5 | Input validation | JSON Schema validation on all API inputs; driver input bounds-checking |
| SR 3.6 | Deterministic output | Watchdog-enforced restart; audit log on anomaly |
| SR 3.7 | Error handling | Structured error responses; no stack traces to external clients |
| SR 3.8 | Session integrity | JWT binding + session token rotation |
| SR 4.1 | Information confidentiality | TLS in transit; AES-256 at rest for sensitive data |
| SR 4.2 | Information persistence | Configurable data purge on decommission |
| SR 4.3 | Use of cryptography | Approved ciphers only; FIPS 140-2 mode configurable |
| SR 5.1 | Network segmentation | Zone/conduit model documented; firewall rules in hardening guide |
| SR 5.2 | Zone boundary protection | xEdge listens on specific interfaces only; default deny |
| SR 5.3 | General purpose person-to-person comms | No chat/email functionality |
| SR 5.4 | Application partitioning | Driver isolation; northbound/southbound separation |
| SR 6.1 | Audit log accessibility | Audit log readable by `auditor` role; SIEM forwarding |
| SR 6.2 | Continuous monitoring | OTel metrics + alerts; fleet manager health dashboard |
| SR 7.1 | Denial of service protection | Rate limiting on REST API; bounded queues |
| SR 7.2 | Resource management | Memory limits; watchdog restart on resource exhaustion |
| SR 7.3 | Control system backup | Config version history; export/import API |
| SR 7.4 | Control system recovery | A/B OTA rollback; systemd restart policy |
| SR 7.5 | Emergency power | Graceful shutdown on SIGTERM; WAL flush before exit |
| SR 7.6 | Network and security configuration settings | Hardening guide + config linting tool |
| SR 7.7 | Least functionality | Minimal installed packages; no unused services |
| SR 7.8 | Control system component inventory | SBOM published with each release |

### 3.2 NERC CIP Evidence Package

For each CIP standard, xEdge generates exportable evidence:

| CIP Standard | Evidence Generated by xEdge |
|---|---|
| CIP-002 (Asset ID) | Device inventory JSON export with hardware ID, software version, criticality flags |
| CIP-005 (ESP Access) | Network port configuration export; access log for all interactive sessions |
| CIP-007 (Systems Security) | Failed login reports; port scan results from self-test; patch history — implemented (Sprint 18, XEDGE-143) as the `compliance cip-007` diagnostic command (`xedge/api/diagnostics.py`, reachable via `xedge-cli` or `/ui/diagnostics`), returning the audit log's failed/successful login history plus current version + dependency versions in place of a real patch/upgrade history (no OTA/upgrade-tracking system exists yet to back that literally — see the SL-1 gap analysis) |
| CIP-010 (Config Mgmt) | Config change audit log; OTA update history with before/after hashes |
| CIP-011 (Info Protection) | Encryption status report; data classification tags |

---

## 4. Hardening Guide (Summary)

Full hardening guide published separately as `docs/security/hardening-guide.md`
(written Sprint 18, XEDGE-142 — the file referenced here now actually
exists). An honest SR-by-SR gap analysis against §3.1's table above,
including concrete named follow-ups, is at
`docs/security/iec62443-sl1-gap-analysis.md` (Sprint 18, XEDGE-141 — also
closes the never-written Sprint 15 gap-analysis story XEDGE-125). Key
points:

**OS hardening:**
- Minimal Linux image (Yocto kirkstone or Ubuntu Core)
- Remove unused packages, services, and compilers
- Enable AppArmor or SELinux with xEdge-specific profiles
- Disable root login; use dedicated `xedge` system user (no shell)
- Enable UFW/nftables: allow only ports 4840, 8443, 9090 on management interface

**xEdge hardening:**
- Run as non-root (UID 1000)
- `CAP_NET_RAW` only if GOOSE/SV is used
- Read-only rootfs; writable mounts: `/data`, `/var/log`, `/tmp` only
- Disable anonymous OPC UA and REST API access
- Set minimum TLS to 1.3 in production
- Enable audit log SIEM forwarding from day one

**Network hardening:**
- Separate southbound (OT) and northbound (IT) network interfaces
- No route between southbound and management interfaces (routing table rules)
- MQTT northbound: allow only outbound 8883/TCP (mTLS MQTT)
- Block all inbound from internet; only outbound connections initiated by xEdge
