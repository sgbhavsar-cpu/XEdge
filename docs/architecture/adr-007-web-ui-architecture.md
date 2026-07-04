# ADR-007: Local Web UI Architecture

**Status:** ACCEPTED
**Date:** 2026-07-04
**Deciders:** Lead Architect, Security Engineer, UX/Frontend Engineer

## Context

Through Phase 1–2 MVP delivery, xEdge exposed only a read-only, unauthenticated REST API
(`xedge/api/server.py`) for status/config/driver introspection, with the intent (per the
original `docs/planning/development-plan.md` Post-GA Roadmap) of adding a "web-based
configuration UI (React + REST API)" as a low-priority, post-GA feature.

This has been superseded: a browser-based configuration and monitoring UI is now a
**day-one requirement**, not a post-GA nicety. It must:

- Run **on the device itself** (no cloud/fleet-manager dependency to configure or
  monitor a single unit — consistent with system-architecture.md §1.2's non-goal of
  requiring cloud connectivity for core operation).
- Provide a **basic single-user login**, with the password **set at first login**
  (no default/shipped credential, ever — SR-AA-006 already prohibits plaintext
  storage; this extends the same care to "no factory password" for the UI specifically).
- Provide **full configuration capability** (not just viewing — editing and applying
  `xedge.yaml`-backed config through the browser) and **full monitoring capability**
  (live driver/tag/northbound/store-and-forward status).
- Be documented and planned for retroactively across the whole sprint plan, then
  implemented against the software already delivered (Phase 1–2 MVP), before continuing
  with new backend feature work.

This ADR is scoped to the **architecture and technology choice** for that UI. The
detailed requirements are captured in HLR.md §4.9 and the phased delivery plan in
`docs/planning/sprint-planning.md` Sprint 3.5 (this ADR is referenced from there).

## Decision

### 1. The UI is served by the xedge process itself, on-device, over HTTP(S) on the loopback interface by default

The UI is not a separate service or container. It is mounted onto the same FastAPI
`app` instance already created in `xedge/api/server.py` (which becomes read-write and
authenticated — see below), served by the same `uvicorn.Server` xEdge already runs.

**Why:** A separate service would need its own lifecycle management, its own port,
and its own story for talking to the driver/pipeline state — all of which the existing
API process already has direct in-memory access to. One process, one supervised
lifecycle, consistent with how the REST API itself was justified in the MVP.

Like the read-only REST API before it, the UI defaults to binding **127.0.0.1 only**.
Once full RBAC/mTLS lands (Sprint 14, per the existing plan), the operator can choose
to expose it more broadly; until then, defaulting to loopback is the safe posture for
an interface that can now *write* configuration, matching the reasoning already
established for the read-only API (and *more* important now that it's read-write).

### 2. No frontend build pipeline (no React, no npm, no Node.js in CI) — a server-rendered UI using Jinja2 + a small hand-written vanilla JS helper

Rejected: a React (or Vue/Svelte) single-page app, as the original Post-GA roadmap
line assumed.

**Why rejected:** xEdge's CI, build, and deployment pipeline is Python-only end to
end (ruff/mypy/bandit/pytest, hatchling wheel, Docker multi-arch build). A React SPA
would require a Node.js/npm toolchain in CI, a JS dependency tree (with its own
vulnerability-scanning story, separate from `pip-audit`), and a built-artifact step
that has to be kept in sync with the Python backend across every release. For a
device that must run on a 1 GB RAM ARM board (NFR-P-006) with a read-only rootfs
(NFR-C-004), shipping a full SPA framework's runtime and a separate build step is
disproportionate to what a config-editor-plus-dashboard actually needs.

**Why Jinja2 + a hand-written vanilla JS helper instead (revised 2026-07-04 from the
original htmx plan — see addendum at the end of this ADR):**
- FastAPI already supports Jinja2 templates natively (`fastapi.templating`); no new
  runtime dependency beyond `jinja2` itself (BSD-3-Clause, tiny, pure Python).
- A small (~50-line), hand-written, dependency-free JS file (`xedge/api/static/xedge-ui.js`)
  polls a handful of JSON endpoints and patches specific DOM elements — no
  third-party JS at all, so there's no vendoring/provenance question, no CDN
  fetch, and the device stays fully offline-capable by construction rather than
  by a "don't fetch it at runtime" policy on a third-party library.
- Config editing (structured YAML/JSON-Schema-backed forms) and live monitoring
  (polling-driven status tables) are simple enough that hand-rolled fetch/DOM-patch
  code is genuinely less code and less risk than adopting and vendoring a library
  for it.
- This keeps the entire UI Python-testable via FastAPI's `TestClient` (already the
  established pattern from `tests/unit/test_api.py`) and does not add any new
  language/toolchain to the project's CI matrix.

**Migration path preserved:** because the UI talks to the backend exclusively through
versioned, authenticated REST/JSON endpoints (see below), a richer SPA (or a
library like htmx, if ever wanted) can replace the server-rendered templates later
without changing the backend contract — the Post-GA roadmap's "React + REST API"
option remains available as a future frontend swap, not a backend rewrite, if a
richer UI is ever justified.

### 3. Authentication: single local user, password set at first login, session-cookie based — an intentionally simplified subset of the Sprint 14 RBAC/JWT design, not a divergent one

The full auth model (JWT tokens, 4 RBAC roles, X.509 client certs, TOTP MFA) remains
planned for Sprint 14 per HLR §6.1 and security-architecture.md §2.1 — that work is
not pulled forward in full. Instead:

- **First-login flow:** on first UI access, if no local user exists yet, the UI
  presents a "set up this device" form (new password, confirmed) instead of a login
  form. No default or hardcoded credential is ever shipped or written to disk.
- **Password storage:** bcrypt (cost factor ≥ 12), per SR-AA-006 — reusing the
  algorithm and cost factor already specified for the full auth system, not a
  separate/weaker scheme, so nothing needs to change when the full RBAC model lands.
- **Session model:** a signed, HttpOnly, SameSite=Strict session cookie (not a JWT
  bearer token yet — there's only one user and one role, so there is nothing for a
  JWT's claims/expiry-and-revocation machinery to buy beyond what a signed cookie
  already gives). Idle timeout defaults to 15 minutes, matching the diagnostic CLI's
  existing default (security-architecture.md §2.1), for consistency.
- **Single role:** whoever is logged in can do everything (config read/write, driver
  restart, etc.) — there is exactly one account. The 4-role RBAC matrix
  (security-architecture.md §2.2) is not implemented yet; this is a conscious,
  documented simplification, not an oversight, and the auth module is structured so
  the RBAC layer can be added around it later without a rewrite (permission checks
  are already routed through a single `require_auth()` dependency, ready to gain a
  `require_permission(...)` parameter later).
- **Login lockout:** a simple fixed-threshold lockout (5 failed attempts) is added
  now, pulling forward the *lockout* requirement from HLR/security docs' planned
  Sprint 14 scope (XEDGE-114) without the rest of that sprint's RBAC/JWT machinery,
  since brute-force protection is cheap to add and shouldn't wait.

### 4. Config writes go through the existing hot-reload path, not a new mutation mechanism

The UI's "save config" action writes the edited YAML to the same `xedge.yaml` file
the config engine already watches (`xedge.core.hot_reload.config_watch_loop`), then
lets the existing validate → apply → restart-affected-drivers pipeline take over —
it does not bypass validation or drivers' restart-on-change semantics via some
separate in-memory mutation path. This means every property already established for
hot-reload (secrets never resolved to disk, version history, restart-only-affected)
applies automatically to UI-driven changes too, with no new code path to keep in sync.

## Consequences

- **Positive:** No new build toolchain; small dependency footprint (`jinja2` only —
  see addendum below on dropping htmx); UI-driven config changes get hot-reload's
  existing safety properties for free; migration to a richer SPA later doesn't
  require a backend rewrite, since the API surface is already JSON/REST underneath
  the templates.
- **Positive:** bcrypt password storage and the 15-minute idle timeout are chosen to
  match the already-planned Sprint 14 values exactly, so upgrading from
  single-user-session-cookie to full JWT/RBAC later is additive, not a breaking change
  for existing device state (the stored password hash format doesn't change).
- **Negative / accepted trade-off:** no RBAC until Sprint 14 lands — anyone who can
  log in can do anything. This is acceptable for the MVP's single-operator,
  loopback-only-by-default deployment model, and is explicitly called out in
  HLR.md §4.9 and security-architecture.md as an interim posture, not a final one.
- **Negative / accepted trade-off:** the server-rendered UI is less interactive than
  a full SPA (e.g., no offline-first client state, no rich client-side validation
  beyond what HTML5 + a bit of JS gives). Acceptable given the UI's job here is
  CRUD-over-config plus live status, not a complex application.

## Addendum (2026-07-04): dropped htmx in favor of hand-written vanilla JS

This ADR originally specified vendoring htmx as the live-update mechanism. During
implementation, fetching a third-party JS file from an agent-selected CDN
(unpkg.com) to vendor into a device that serves it to browsers was flagged by the
development environment's safety controls as a supply-chain-relevant action
requiring explicit user confirmation of the source — the user didn't respond in
time to confirm, so the safer default was taken: **no third-party JS dependency at
all**. `xedge/api/static/xedge-ui.js` is a small (~50-line), hand-written,
dependency-free script that polls a few JSON endpoints and patches specific DOM
elements — it achieves the same practical goal (no CDN, no build step, offline
capable) without a vendoring/provenance question ever arising. All references to
"htmx" elsewhere in this ADR and in system-architecture.md/sprint-planning.md
describe the original intent; the as-built mechanism is plain JS. If a case for
htmx (or a richer library) comes up later, it should go through the user
explicitly, per the same safety control.
