
# xEdge — IIoT Edge Software Stack

xEdge is an enterprise-grade, multi-protocol IIoT edge software stack that runs on Linux. It collects data from industrial field devices using standard OT protocols, normalizes it into a unified data model, buffers it reliably on the edge with per-stream store-and-forward, and publishes it to cloud platforms or exposes it northbound via OPC UA and Sparkplug B over MQTT.

## Key Capabilities

| Capability | Details |
|---|---|
| **Southbound protocols** | Modbus RTU/TCP/RTU-over-TCP, OPC UA client, IEC 60870-5-104, DNP3, IEC 61850 (MMS/GOOSE/SV), BACnet IP/MSTP, EtherNet/IP, PROFINET, DLMS/COSEM |
| **Northbound** | MQTT + Sparkplug B, OPC UA server, pluggable cloud connectors (AWS IoT Core, Azure IoT Hub, custom) |
| **Store & Forward** | RAM ring buffer backed by SD card / eMMC; configurable per-tag retention policy |
| **Web UI** | Local, on-device browser UI (day one, ADR-007) — full configuration + monitoring, single-user login with password set at first access |
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
| [ADR-007: Local Web UI Architecture](docs/architecture/adr-007-web-ui-architecture.md) | Day-one browser UI: tech stack, auth model, rationale |
| [Development Plan](docs/planning/development-plan.md) | Phases, milestones, team structure |
| [Sprint Planning](docs/planning/sprint-planning.md) | Detailed sprint backlog across 18 months |

## Quick Start (Development)

```bash
git clone https://github.com/sgbhavsar-cpu/xedge
cd xedge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config/examples/modbus-minimal.yaml config/local.yaml
xedge --config config/local.yaml
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
