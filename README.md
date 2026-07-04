# xEdge — IIoT Edge Software Stack

xEdge is an enterprise-grade, multi-protocol IIoT edge software stack that runs on Linux. It collects data from industrial field devices using standard OT protocols, normalizes it into a unified data model, buffers it reliably on the edge with per-stream store-and-forward, and publishes it to cloud platforms or exposes it northbound via OPC UA and Sparkplug B over MQTT.

## Key Capabilities

| Capability | Details |
|---|---|
| **Southbound protocols** | Modbus RTU/TCP/RTU-over-TCP, OPC UA client, IEC 60870-5-104, DNP3, IEC 61850 (MMS/GOOSE/SV), BACnet IP/MSTP, EtherNet/IP, PROFINET, DLMS/COSEM |
| **Northbound** | MQTT + Sparkplug B, OPC UA server, pluggable cloud connectors (AWS IoT Core, Azure IoT Hub, custom) |
| **Store & Forward** | RAM ring buffer backed by SD card / eMMC; configurable per-tag retention policy |
| **Security** | mTLS everywhere, RBAC, PKI / HSM support, tamper-evident audit log, IEC 62443 SL-2 target |
| **Observability** | OpenTelemetry traces/logs, structured JSON logs (SIEM-ready), remote diagnostic CLI |
| **Device management** | Self-hosted fleet manager, OTA via RAUC, Git-ops config management |
| **Deployment** | Docker / Podman, systemd, cross-platform ARM + x86 |
| **License** | Dual license — GPL v3 (community) / Commercial (enterprise) |

## Documentation

| Document | Description |
|---|---|
| [High-Level Requirements](docs/requirements/HLR.md) | Functional, non-functional, security, and compliance requirements |
| [System Architecture](docs/architecture/system-architecture.md) | Component design, data flows, deployment model |
| [Security Architecture](docs/architecture/security-architecture.md) | Threat model, security controls, compliance mapping |
| [ADR-006: Protocol Stack Build vs. Buy](docs/architecture/adr-006-protocol-stack-build-vs-buy.md) | Per-protocol decision: in-house stack vs. third-party library (ACCEPTED) |
| [Development Plan](docs/planning/development-plan.md) | Phases, milestones, team structure |
| [Sprint Planning](docs/planning/sprint-planning.md) | Detailed sprint backlog across 18 months |
| [License Audit](docs/planning/license-audit.md) | Per-dependency license table, clean-room rule, provenance record template |

## Status

**Software MVP (Phase 1–2) complete: a real, runnable edge stack** — reads
real field devices over Modbus (TCP, RTU serial, RTU-over-TCP) and OPC UA,
normalizes and hardens the data (deadband, engineering-unit scaling, quality
mapping), buffers it reliably through a RAM-ring + SQLite-WAL
store-and-forward tier that survives northbound outages, publishes it
northbound as Sparkplug B over MQTT and via its own OPC UA server, hot
-reloads its config without a restart, and exposes a read-only REST API for
status/driver/config introspection.

**Phase 1 (Sprints 1–3) — foundation, Modbus TCP, MQTT/Sparkplug B:**
- Repo scaffolding, config engine, structured logging, systemd watchdog
  integration, the `BaseDriver`/`DriverSupervisor` skeleton, CI pipeline,
  multi-arch Docker build.
- In-house Modbus MBAP/PDU codec (ADR-006, clean-room from the public
  spec), the Modbus TCP driver (FC01–04, per-tag Bad-quality handling on
  protocol exceptions, supervisor-driven reconnect), per-driver-type config
  schema (FR-DF-004), and pipeline v1 (`TagUpdate` → `UnifiedTag`).
- In-house Sparkplug B protobuf encoder (hand-rolled wire format from the
  public field-number spec, ADR-002/ADR-006), the bdSeq/seq session state
  machine, the MQTT connector (NBIRTH/NDEATH-as-LWT/NDATA, asyncio bridge
  over paho-mqtt 2.x), and the northbound dispatcher (connect/reconnect
  backoff, FR-NB-010).

**Phase 2 (this MVP round) — OPC UA, RTU, store-and-forward, hardening,
hot-reload, REST API:**
- OPC UA client driver and OPC UA server (`asyncua`-backed per the ADR-006
  §7 interim amendment), both cross-validated against a real `asyncua`
  runtime.
- Modbus RTU serial and RTU-over-TCP drivers sharing a common polling base
  with the TCP driver; RTU CRC16 cross-validated against `pymodbus`.
- Pipeline v2: engineering-unit scaling, per-tag-group deadband
  suppression (absolute/percentage), and source-vs-ingestion timestamp
  resolution (FR-DP-002/003/006).
- Store-and-forward: RAM ring buffer spills to a per-stream SQLite WAL
  cold tier on overflow; the northbound dispatcher replays the backlog
  (oldest-first, peek-then-confirm-delete so a failed publish can't lose
  data) immediately after every reconnect, plus a retention purge sweep
  (FR-SF-001..005).
- Config hot-reload: an mtime-polling watcher validates and applies
  changes live, restarting only the driver instances whose config actually
  changed; version history (last 10 by default) is persisted *before*
  secrets substitution so `${SECRET:...}` placeholders — never resolved
  plaintext — are what ever touches disk; rollback to any prior version is
  supported (FR-CM-002/005/006).
- REST API v1 (FastAPI): `/health`, `/api/v1/status`, `/api/v1/drivers`
  (live per-instance metrics), `/api/v1/config` (secrets-safe, reads from
  version history). Read-only, no auth yet, so it binds loopback-only by
  default (FR-CM-003).

Cross-validated at every layer against independent black-box oracles
(pymodbus, pysparkplug, amqtt as a real MQTT broker, and a real `asyncua`
runtime — never read as reference implementations, per ADR-006), plus live
runs against real device/broker/server processes end to end. Verified via
215+ unit + integration tests (90% line coverage), ruff, mypy --strict,
bandit, and a `docker build && docker run` smoke test.

Not yet done: hardware-in-the-loop validation on real (non-simulated) field
devices and across the 6 target platforms, mTLS/RBAC and the other
IEC 62443 security controls, the remaining southbound protocols (IEC
60870-5-104, DNP3, IEC 61850, BACnet, EtherNet/IP, PROFINET, DLMS/COSEM),
fleet management/OTA, and OpenTelemetry tracing — see
[Sprint Planning](docs/planning/sprint-planning.md) for the full remaining
backlog.

## Quick Start (Development)

```bash
git clone https://github.com/sgbhavsar-cpu/xedge
cd xedge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,test]"
xedge --config config/examples/modbus-minimal.yaml
```

`config/examples/modbus-minimal.yaml` ships with its Modbus driver disabled
(no device to reach by default). See `config/examples/modbus-tcp-example.yaml`
for a fully configured driver (plus store-and-forward, hot-reload, and REST
API settings) — point `config.host` at a reachable Modbus TCP device (or a
`pymodbus` simulator) and enable it. `config/examples/opcua-example.yaml`
shows the OPC UA client + server.

Once running, the read-only REST API (loopback-only by default) is at
`http://127.0.0.1:8080`: try `/health`, `/api/v1/status`, `/api/v1/drivers`,
and `/api/v1/config`.

Run the test suite and static checks:

```bash
pytest tests/ --cov=xedge --cov-report=term-missing
ruff check xedge tests
mypy xedge
bandit -r xedge -q
```

Build and run the Docker image:

```bash
docker build -f deploy/docker/Dockerfile -t xedge:dev .
docker run --rm -v "$(pwd)/config/examples/modbus-minimal.yaml:/etc/xedge/xedge.yaml:ro" xedge:dev
```

## Repository Structure

```
xedge/
├── xedge/                  # Core Python package
│   ├── core/               # Pipeline engine, config, scheduler
│   ├── drivers/            # Protocol driver plugins
│   ├── northbound/         # MQTT, OPC UA server, cloud connectors
│   ├── store/              # Store-and-forward engine
│   ├── fleet/              # Fleet management agent
│   ├── security/           # PKI, RBAC, audit
│   └── observability/      # OTel, logging, diagnostic CLI
├── c_extensions/           # Performance-critical C extensions
├── config/                 # Configuration schemas and examples
├── docs/                   # All project documentation
├── tests/                  # Unit, integration, hardware-in-loop tests
├── deploy/                 # Docker, systemd, Helm
└── tools/                  # CLI tools, config validators, simulators
```

## License

Community edition: [GPL v3](LICENSE-GPL)  
Commercial edition: Contact [xedge@example.com](mailto:xedge@example.com) for a commercial license.
