# xEdge Hardening Guide

Full guide referenced from `docs/architecture/security-architecture.md` §4
(the summary there stays as a summary; this is the actionable version).
Sprint 18, XEDGE-142. Every recommendation below either references a
config section or code path that actually exists in this codebase today —
nothing here is aspirational unless explicitly marked **(planned)**.

## 1. Host OS hardening

- Minimal base image/distro: remove compilers, package managers, and any
  service not required to run `xedge`. The reference container
  (`deploy/docker/Dockerfile`) uses `python:3.11-slim` for exactly this
  reason — no build toolchain in the runtime stage.
- Dedicated, unprivileged service account. The reference container already
  does this (`xedge:xedge`, UID/GID `10001`, `--no-create-home --shell /usr/sbin/nologin`)
  — mirror the same posture for a bare-metal/systemd install: create a
  system user with no login shell, don't run as root.
- Firewall: allow only the ports xEdge actually needs — REST API/Web UI
  (`api.port`, default `8080`), OPC UA server if enabled
  (`opcua_server.endpoint_url`'s port, default `4840`), and metrics
  (`9090` is listed in the container's `EXPOSE` as a placeholder for a
  future dedicated metrics port, but today `/metrics` is served on the
  *same* port as the REST API — see §2.4 below). Deny everything else,
  especially anything southbound-facing from the management network.
- AppArmor/SELinux profiles: **(planned)** — no xEdge-specific profile
  ships today; this is a real gap, not implemented.

## 2. xEdge configuration hardening

Every one of these is a real, working `xedge.yaml` section — set them
explicitly rather than relying on defaults for a production deployment,
even where the default already leans secure.

### 2.1 TLS (`tls`)
Defaults `enabled: true` with an auto-generated self-signed certificate.
For anything beyond a single-operator LAN, replace the self-signed cert
with one from a real internal CA: set `tls.cert_path`/`tls.key_path` to a
properly-issued cert/key pair. `tls.validity_days` controls self-signed
cert lifetime (default 825 days) if you do stay with auto-generation.

### 2.2 Authentication + RBAC
First-login setup (`/ui/setup` or `POST /api/v1/auth/setup`) always creates
one `admin` account — create named accounts per operator immediately after
first boot (`/ui/users` or `POST /api/v1/users`) and assign the
least-privileged role that covers their job: `readonly` for pure
monitoring, `operator` for day-to-day tag/config work, `auditor` for
compliance review, `admin` only for account/security administration. See
`xedge/api/permissions.py`'s `ROLE_PERMISSIONS` for the exact matrix.

### 2.3 Rate limiting (`rate_limit`, new this sprint)
Defaults `enabled: true`, `requests_per_minute: 100` per client IP, across
both the REST API and every Web UI route (same app, same middleware —
XEDGE-285's requirement, since the Web UI is exactly as write-capable as
the REST API it wraps). Lower `requests_per_minute` for a deployment with
a known-small number of legitimate clients.

### 2.4 Metrics exposure (`metrics`)
`/metrics` is deliberately unauthenticated (a real Prometheus scraper
can't do cookie-based session auth) and is **not** currently on a separate
port — it shares the REST API's port and bind address. If your network
segmentation model needs metrics scraping from a zone that shouldn't also
reach the write-capable REST endpoints, put a reverse proxy in front that
only forwards `/metrics` from the monitoring zone, or set
`metrics.enabled: false` and scrape via the diagnostic CLI's `status`/
`store-forward status` commands instead.

### 2.5 Diagnostic access (`xedge/api/diagnostics.py`)
Every diagnostic command is individually RBAC-gated and audit-logged —
`self-test`/`driver restart` require `diagnostics:run`/`driver:restart`
respectively (only `admin`/`operator` roles have these by default). Don't
grant `operator` to accounts that only need read access; use `readonly`.

### 2.6 Audit log
`webui_dir/audit.jsonl` is append-only and hash-chained
(`xedge/observability/audit_log.py`'s `verify_chain()`) — back it up as
part of your regular backup routine and periodically verify the chain
(`/ui/audit`'s "chain integrity" indicator, or `xedge-cli compliance cip-007`
for a structured export) rather than only checking it after an incident.
SIEM forwarding of this file: **(planned)** — no built-in forwarder ships
today; forward the JSONL file with your own log-shipping agent
(Fluentd/Vector/syslog-ng) in the meantime.

## 3. Network hardening

- Bind address: `api.host` **defaults to `127.0.0.1`** (loopback-only) —
  deliberately, since this API is write-capable
  (`xedge/api/server.py`'s own module docstring). Only change this to
  `0.0.0.0` (or a specific non-loopback interface) if you actually need
  remote access, and put it behind the firewall rules in §1 when you do.
- **Container networking gotcha, found and verified while writing this
  guide**: if you run xEdge in the reference Docker container and publish
  its port with `-p host:8080`, the `api.host: 127.0.0.1` default means
  Docker's NAT/port-publishing traffic (arriving on the container's
  `eth0`, not `lo`) **cannot reach the app** — the TLS handshake fails
  with a bare connection reset, not a clean "connection refused." You must
  set `api.host: 0.0.0.0` in the mounted config for a published-port
  container deployment to work at all. This is expected/correct behavior
  given the security default, not a bug — but it's a real, easy-to-hit
  trap worth calling out explicitly.
- Southbound/northbound separation: run xEdge's southbound (Modbus/OPC
  UA/BACnet) network interface on a segment with no route to the
  management/northbound interface. xEdge itself doesn't enforce this —
  it's an OS/network-layer control, same as `security-architecture.md`
  §4 already says.

## 4. systemd hardening (bare-metal/VM deployment)

xEdge ships as a console script (`xedge`/`xedge-cli`, `pyproject.toml`'s
`[project.scripts]`), not yet a systemd unit file — **(planned, no unit
file ships today)**. A hardened unit for a manual install should include
at minimum:

```ini
[Service]
ExecStart=/usr/local/bin/xedge --config /etc/xedge/xedge.yaml
User=xedge
Group=xedge
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/data
PrivateTmp=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
Restart=on-failure
```

## 5. Container hardening

The reference Dockerfile (`deploy/docker/Dockerfile`) already does:
non-root user (UID/GID `10001`), no shell, minimal `python:3.11-slim`
runtime stage, a real `HEALTHCHECK` (fixed this sprint — see below).
Recommended additions for production use:
- `--read-only` root filesystem at `docker run` time, with `/data` as the
  only writable mount (already a declared `VOLUME`).
- `--cap-drop=ALL` — xEdge needs no Linux capabilities beyond default
  networking.
- Image signing with cosign: **(planned, not implemented this pass)** —
  see the gap analysis for why (needs a real registry + credentials this
  environment doesn't have) and the concrete follow-up steps named there.

**Fixed this sprint**: the shipped `HEALTHCHECK` probed plain HTTP, but
`tls.enabled` has defaulted to `true` since Sprint 13 — every container
run with a stock config was failing its own health check silently (never
actually verified until this pass). `deploy/docker/healthcheck.py` now
tries HTTPS first (self-signed-cert tolerant) and falls back to HTTP,
verified with a real `docker build`/`docker run` against a mounted
`config/examples/modbus-minimal.yaml`.
