# ADR-009: Fleet Management — Pull-Based Heartbeat, Not Push

**Status:** ACCEPTED
**Date:** 2026-07-06
**Deciders:** Lead Architect, Core Engineering

## Context

Sprint 29 ("Fleet Management Agent + Fleet Manager v1") calls for a
separate Fleet Manager service that devices register with, heartbeat to,
and receive config pushes from. This is the first feature in the codebase
where a *second* xEdge-adjacent service exists (`xedge-fleet-manager`,
`xedge/fleet/manager_app.py`) — everything before this was one process per
device. This ADR records the two load-bearing design deviations from the
sprint story's literal wording, and what's explicitly deferred.

## Decision

### 1. Config delivery is pull (heartbeat-response), not push

XEDGE-213 is titled "Config push: fleet manager → device," implying the
manager calls out to the device. This was **not** built that way.
Instead: `POST /api/v1/fleet/devices/{id}/config` on the manager only
queues the config (`DeviceRegistry.queue_config`); the device's own
`fleet_heartbeat_loop` (`xedge/fleet/agent.py`) picks it up as
`pending_config` in its *own* next heartbeat's response, then reports the
apply result (`last_config_apply`) on the heartbeat after that.

This was chosen because a device-initiated pull requires **zero inbound
network reachability to the device** — many real edge deployments sit
behind NAT/firewalls with no public address at all, the same reason
`xedge.core.hot_reload` already polls the config file's mtime rather than
being pushed to. A manager-initiated push would need the device's
loopback-only REST API (`api.host: 127.0.0.1` by default, per
`xedge.api.server`'s own module docstring) opened up on the network just
for the fleet manager to reach it — undermining that existing security
posture for a feature whose whole point is remote reachability, not local
convenience.

Consequence: config push is not synchronous. An operator calling `POST
.../config` gets `202 Accepted` ("queued for delivery"), not "applied" —
`GET .../config/status` is the way to observe eventual delivery. Actual
driver restart happens on `hot_reload.py`'s own poll cycle, asynchronously
relative to the heartbeat that wrote the file — `fleet_heartbeat_loop`
never touches `DriverSupervisor` directly, matching every other config
mutation path in this codebase (`xedge.api.server.put_config`'s docstring:
"no separate UI-only mutation path").

### 2. Auth is shared bearer tokens, not mTLS (XEDGE-214 deferred)

Three distinct secrets, never interchangeable:
- `join_token` — a manager-wide secret a device presents once, to enroll.
- `device_token` — issued per-device at registration (opaque,
  `secrets.token_urlsafe`, only its SHA-256 hash persisted — same "verify
  a hash, never store the secret" posture as `xedge.api.auth.UserStore`'s
  bcrypt hashes), authenticates that device's own heartbeat calls only.
- `admin_token` — authenticates operator/CLI calls (list/inspect devices,
  push config); auto-generated and persisted to disk on first run if not
  supplied, mirroring `xedge.api.auth.load_or_create_secret_key`.

XEDGE-214's device-certificate mTLS is real defense-in-depth this doesn't
provide (a leaked `device_token` lets an attacker impersonate exactly one
device; a leaked `admin_token` is full fleet control) — deferred because
building a CA/cert-issuance flow for hundreds of devices is a
substantially larger scope than this pass, the same category of deferral
as XEDGE-113's JWT bearer tokens in the Sprint 14 RBAC work. This is
today's real security posture, not a final one.

### 3. Management channel is REST/HTTP only (XEDGE-212's gRPC option deferred)

The sprint calls for "MQTT namespace + gRPC (configurable)." Only HTTP/
REST was built. Reasoning: the Fleet Manager already needs a REST API for
operator/CLI use (list devices, push config) — adding a *second* channel
(gRPC or MQTT) for the exact same registration/heartbeat/config-push
operations would be two implementations of one state machine for no
functional gain yet identified. If a future need emerges (e.g. lower
per-heartbeat overhead at very large fleet sizes), it's a protocol addon
to the same `DeviceRegistry`, not a rewrite.

### 4. Storage is SQLite, not a separate database service

`xedge/fleet/registry.py`'s `DeviceRegistry` is one SQLite file (WAL mode),
the same pattern as `xedge.store.sqlite_store.SqliteColdStore`'s cold
tier — consistent with ADR-007's 1GB-RAM ARM target philosophy, and
because a fleet manager's write volume (one row update per device per
heartbeat interval, default 60s) is nowhere near SQLite's practical
ceiling at any fleet size this sprint's own integration test scope (a
handful of simulated devices) needs to prove. `deploy/docker/docker-
compose.fleet.yml` therefore needs no separate database container.

## Consequences

- **Positive**: no new inbound port/firewall rule needed on any device —
  the fleet agent is exclusively an outbound HTTP client, same network
  posture as `xedge-cli` already assumes for its own outbound calls.
- **Positive**: `fleet_heartbeat_loop` reuses `ConfigValidator` and the
  existing config file + hot-reload path entirely — zero new mutation
  code, zero new restart logic.
- **Trade-off, accepted**: a config push takes up to two heartbeat
  intervals to fully resolve (one to deliver, one to report the apply
  result) rather than being synchronous — acceptable for a fleet
  management feature (minutes-scale operations), not for anything
  latency-sensitive.
- **Trade-off, accepted**: `last_config_apply.success = true` only means
  "validated and written," not "confirmed the driver actually restarted
  successfully" — hot-reload's own restart-only-affected-drivers happens
  on a separate poll cycle this module doesn't observe. A future
  enhancement could have the agent watch `DriverSupervisor` state after
  writing and report a richer result on a subsequent heartbeat; not built
  this pass.
- **Deferred, tracked**: XEDGE-214 (mTLS), the gRPC/MQTT channel option
  (XEDGE-212), per-device token rotation without a full re-register, and
  the staged-rollout/remote-command-execution features that build on this
  foundation in Sprints 30-32.
