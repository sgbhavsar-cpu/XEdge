
# xEdge — IIoT Edge Software Stack

xEdge is a multi-protocol IIoT edge software stack that runs on Linux. It collects data from industrial field devices using standard OT protocols, normalizes it into a unified data model, buffers it reliably on the edge with per-stream store-and-forward, and publishes it to cloud platforms or exposes it northbound via OPC UA and Sparkplug B over MQTT.

> **Status: pre-alpha, under active development.** The table below distinguishes what is implemented today from what is planned. Anything marked *Planned* does not exist in this repository yet.

## Implemented Today

| Capability | Details |
|---|---|
| **Southbound protocols** | Modbus TCP, Modbus RTU (RS-485 serial), Modbus RTU-over-TCP, OPC UA client, BACnet/IP client |
| **Northbound** | MQTT + Sparkplug B (in-house encoder), OPC UA server |
| **Store & Forward** | RAM ring buffer with spill to a SQLite cold tier; replay-on-reconnect; configurable retention |
| **Data pipeline** | Unified tag model, engineering-unit scaling, deadband suppression, quality mapping, alarm engine with acknowledge/shelve |
| **Write-back** | Sparkplug B NCMD and REST → `WriteRouter` → driver, RBAC-checked and audit-logged |
| **Web UI** | Local, on-device browser UI (ADR-007) — schema-driven configuration forms, live monitoring, first-login password setup |
| **Security** | RBAC (4 roles), bcrypt local accounts, hash-chained tamper-evident audit log, per-IP rate limiting, self-signed HTTPS for the Web UI/REST API |
| **Observability** | OpenTelemetry traces, Prometheus metrics, structured JSON logs, diagnostic WebSocket + CLI |
| **Device management** | Fleet agent + self-hosted Fleet Manager v1 (registration, heartbeat, pull-based config delivery) |
| **Deployment** | Docker / Podman, cross-platform amd64 + arm64 + armv7 |
| **License** | Dual license — GPL v3 (community) / Commercial (enterprise) |

## Implementation Status Matrix

| Capability | Status | Notes |
|---|---|---|
| Modbus TCP / RTU / RTU-over-TCP | 🟡 Partial | Working. Register batching, multi-register data types, write priority and shared-bus multi-slave are in progress — see [crd-delivery-plan.md](docs/planning/crd-delivery-plan.md) sprints C1–C3 |
| OPC UA client | ✅ Shipped | |
| OPC UA server (northbound) | ✅ Shipped | Basic information model |
| BACnet/IP client | ✅ Shipped | BACnet MS/TP is *Planned* |
| MQTT + Sparkplug B publisher | 🟡 Partial | Publisher is hard-wired to Sparkplug B; configurable payloads, subscriber and embedded broker are in progress (sprint C5). **No TLS yet** — sprint C4 |
| Store-and-forward | ✅ Shipped | |
| Alarm engine | ✅ Shipped | Threshold + rate-of-change, ack/shelve, independent retention |
| Web UI (device-local) | ✅ Shipped | |
| RBAC, audit log, rate limiting | ✅ Shipped | |
| OpenTelemetry / Prometheus | ✅ Shipped | |
| Fleet agent + Fleet Manager | 🟡 Partial | v1: registration, heartbeat, config delivery. Certificate onboarding, dashboard and multi-tenancy are in progress — see [ADR-013](docs/architecture/adr-013-central-management-platform.md) |
| PKI / certificate management | ❌ Planned | Sprint C4 |
| mTLS everywhere | ❌ Planned | Web UI/REST API has server-side TLS only |
| EtherNet/IP | ❌ Planned | Sprint C7 |
| SNMP client + agent | ❌ Planned | Sprint C8 |
| SMTP notifications | ❌ Planned | Sprint C6 |
| SNTP | ❌ Planned | Sprint C3 |
| Asset management | ❌ Planned | Sprint C6 — see [ADR-010](docs/architecture/adr-010-asset-management-model.md) |
| IEC 60870-5-104 | ❌ Planned | Delivery 2 |
| DNP3 | ❌ Planned | Delivery 2 |
| IEC 61850 (MMS/GOOSE/SV) | ❌ Planned | Delivery 2 |
| DLMS/COSEM | ❌ Planned | Delivery 2 |
| PROFINET | ❌ Planned | Delivery 2 |
| BACnet MS/TP | ❌ Planned | Delivery 2 |
| OTA updates | ❌ Planned | Delivery 2 — container image based, not host OS |
| AWS IoT Core / Azure IoT Hub connectors | ❌ Planned | Delivery 2 |
| IEC 62443 SL-1 baseline | 🟡 Partial | See the [gap analysis](docs/security/iec62443-sl1-gap-analysis.md) for the honest control-by-control status |
| IEC 62443 SL-2 | ❌ Planned | Delivery 2 |

**Legend:** ✅ Shipped · 🟡 Partial — works, with named gaps · ❌ Planned — not implemented

## Documentation

| Document | Description |
|---|---|
| [Development Plan](docs/planning/development-plan.md) | Current delivery structure, capacity, standards, risk register |
| [Delivery 1 Sprint Plan](docs/planning/crd-delivery-plan.md) | Sprint-by-sprint backlog and status for the active delivery |
| [Delivery Decision Record](docs/planning/XEDGE-DR-001-delivery-decisions.md) | Every delivery decision, with rationale |
| [High-Level Requirements](docs/requirements/HLR.md) | Functional, non-functional, security and compliance requirements, with implementation status |
| [Customer Compliance Report](docs/requirements/XEDGE-CRD-001-gateway-compliance-report.md) | Gap analysis against the customer requirement document |
| [System Architecture](docs/architecture/system-architecture.md) | Component design, data flows, deployment model |
| [Security Architecture](docs/architecture/security-architecture.md) | Threat model, security controls, compliance mapping |
| [IEC 62443 SL-1 Gap Analysis](docs/security/iec62443-sl1-gap-analysis.md) | Control-by-control status |
| **ADRs** | [006 build-vs-buy](docs/architecture/adr-006-protocol-stack-build-vs-buy.md) · [007 Web UI](docs/architecture/adr-007-web-ui-architecture.md) · [008 driver isolation](docs/architecture/adr-008-driver-isolation-model.md) · [009 fleet management](docs/architecture/adr-009-fleet-management.md) · [010 asset model](docs/architecture/adr-010-asset-management-model.md) · [011 serial bus + connectivity](docs/architecture/adr-011-serial-bus-and-connectivity.md) · [012 CRD protocol build-vs-buy](docs/architecture/adr-012-crd-protocol-build-vs-buy.md) · [013 central platform](docs/architecture/adr-013-central-management-platform.md) |

## Quick Start (Development)

```bash
git clone https://github.com/sgbhavsar-cpu/XEdge
cd XEdge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,test]"
# pysparkplug pins paho-mqtt<2, conflicting with our paho-mqtt 2.x dependency;
# only its payload-decode classes are used, as a black-box test oracle.
pip install --no-deps "pysparkplug>=0.6"
cp config/examples/modbus-minimal.yaml config/local.yaml
xedge --config config/local.yaml
```

## Repository Structure

```
XEdge/
├── xedge/                  # Core Python package
│   ├── core/               # Pipeline engine, config, supervisor, hot-reload
│   ├── drivers/            # Protocol driver plugins
│   ├── northbound/         # MQTT/Sparkplug B, OPC UA server
│   ├── store/              # Ring buffer + SQLite cold tier
│   ├── api/                # REST API + server-rendered Web UI
│   ├── cli/                # Diagnostic CLI client
│   ├── fleet/              # Fleet agent + Fleet Manager
│   ├── security/           # PKI, certificate management (in progress, sprint C4)
│   └── observability/      # OTel, logging, audit log
├── c_extensions/           # Performance-critical C extensions
├── config/                 # Configuration schemas and examples
├── docs/                   # All project documentation
├── tests/                  # Unit and integration tests
├── deploy/                 # Docker, systemd
└── tools/                  # Mock servers, simulators
```

## License

Community edition: [GPL v3](LICENSE-GPL)
Commercial edition: Contact [xedge@example.com](mailto:xedge@example.com) for a commercial license.
