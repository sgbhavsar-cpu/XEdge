# xEdge Fleet Manager dashboard

React + Vite + TypeScript single-page app, served by `xedge-fleet-manager`
itself (ADR-013 §6). Departs from the device-local Web UI's no-npm posture
(ADR-007) deliberately — that ADR still governs the device-local UI
unchanged; this is a server-side operator console only, never shipped on
a gateway.

## Development

Requires a real `xedge-fleet-manager` running against a real Postgres
database (see `deploy/docker/docker-compose.fleet.yml`) — this dashboard
has no mock backend; every test that exercises it (Vitest component
tests included) hits the real REST API, matching this project's
established testing philosophy.

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api/v1/fleet/* to
                    # https://localhost:8090 (see vite.config.ts)
```

## Build

```bash
npm run build       # writes to ../xedge/fleet/static/dashboard/
```

Builds directly into the `xedge` package tree so a non-editable
`pip install .` picks the result up as ordinary package data — no
`pyproject.toml` change needed (unlike `config/schema/`, which lives
outside `xedge/` and does need one). Gitignored like any other build
artifact; `deploy/docker/Dockerfile`'s `frontend-builder` stage runs this
before the Python build stage's `pip install`.

## Tests

```bash
npm run typecheck   # tsc -b --noEmit
npm run lint        # oxlint
npm run test         # Vitest + React Testing Library (jsdom)
npm run test:e2e     # Playwright, against a real running backend --
                     # set FLEET_TEST_PASSWORD (and optionally
                     # FLEET_TEST_TENANT/FLEET_TEST_USERNAME) first
```
