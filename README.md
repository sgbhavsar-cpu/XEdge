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

**Sprint 2 (Driver Framework + Modbus TCP) complete.** Milestone M1 (First
Data) is reached: xEdge reads real tags from a live Modbus TCP device at a
configured scan rate and pushes them through the pipeline, end to end.

- Sprint 1: repo scaffolding, config engine, structured logging, systemd
  watchdog integration, the `BaseDriver`/`DriverSupervisor` skeleton, CI
  pipeline, multi-arch Docker build.
- Sprint 2: in-house Modbus MBAP/PDU codec (ADR-006, clean-room from the
  public spec), the Modbus TCP driver (FC01–04, per-tag Bad-quality handling
  on protocol exceptions, supervisor-driven reconnect on transport failure),
  per-driver-type config schema (FR-DF-004), and pipeline v1
  (`TagUpdate` → `UnifiedTag`). Cross-validated against pymodbus as an
  independent black-box oracle, per ADR-006.

Verified via unit + integration tests (including the pymodbus oracle
cross-check), ruff, mypy --strict, bandit, and a live `docker build && docker
run` smoke test reading a real device. Northbound publishing (MQTT/Sparkplug
B) and store-and-forward are Sprint 3+ (see
[Sprint Planning](docs/planning/sprint-planning.md)).

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
for a fully configured driver — point `config.host` at a reachable Modbus TCP
device (or a `pymodbus` simulator) and enable it.

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
