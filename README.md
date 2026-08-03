
# xEdge — IIoT Edge Software Stack

xEdge is a multi-protocol IIoT edge software stack that runs on Linux. It collects data from industrial field devices using standard OT protocols, normalizes it into a unified data model, buffers it reliably on the edge with per-stream store-and-forward, and publishes it to cloud platforms or exposes it northbound via OPC UA and Sparkplug B over MQTT.

> **Status: Delivery 1 complete (Sprints 0, C1–C8, H1) against customer requirement document XEDGE-CRD-001.** The table below distinguishes what is implemented today from what is planned for a later delivery. Anything marked *Planned* does not exist in this repository yet. See the [Handover Package](docs/planning/XEDGE-CRD-001-handover.md) for known limitations and explicit scope statements before relying on anything below in production.

## Implemented Today

| Capability | Details |
|---|---|
| **Southbound protocols** | Modbus TCP, Modbus RTU (RS-485 serial), Modbus RTU-over-TCP, OPC UA client, BACnet/IP client, EtherNet/IP Scanner, SNMP client (v1/v2c/v3), SNMP TRAP/INFORM receiver, MQTT Subscriber |
| **Northbound** | MQTT + Sparkplug B or configurable generic-JSON publisher, OPC UA server, embedded MQTT broker, SNMP agent (device pollable), SNMP TRAP/INFORM originator, SMTP (alarm notifications + scheduled reports) |
| **Store & Forward** | RAM ring buffer with spill to a SQLite cold tier; replay-on-reconnect; configurable retention |
| **Data pipeline** | Unified tag model, engineering-unit scaling, deadband suppression, quality mapping, alarm engine with acknowledge/shelve |
| **Write-back** | Sparkplug B NCMD and REST → `WriteRouter` → driver, RBAC-checked and audit-logged |
| **Asset Management** | Metadata/grouping layer over existing driver tags, spanning every protocol above (ADR-010) |
| **Web UI** | Local, on-device browser UI (ADR-007) — schema-driven configuration forms, live monitoring, first-login password setup |
| **Security** | RBAC (4 roles), bcrypt local accounts, hash-chained tamper-evident audit log, per-IP rate limiting, self-signed HTTPS for the Web UI/REST API, fleet CA + mTLS device onboarding with automatic certificate rotation |
| **Observability** | OpenTelemetry traces, Prometheus metrics, structured JSON logs, diagnostic WebSocket + CLI |
| **Device management** | Fleet agent + self-hosted Fleet Manager (registration, heartbeat, pull-based config delivery, join-token/mTLS enrollment) — single-tenant, API-only (no dashboard) |
| **Deployment** | Docker / Podman, cross-platform amd64 + arm64 + armv7 |
| **License** | Dual license — GPL v3 (community) / Commercial (enterprise) |

## Documentation for operators

| Guide | Description |
|---|---|
| [Onboarding Walkthrough](docs/guide/onboarding-walkthrough.md) | First boot to live data: setup, first driver, alarms, northbound publishing, fleet enrollment |
| [Configuration Guide](docs/guide/configuration-guide.md) | The config file model, hot-reload, Web UI vs. raw YAML, secrets |
| [Protocol Quick Starts](docs/guide/protocol-quick-starts.md) | One section per protocol, each pointing at a real working example config |
| [Hardening Guide](docs/security/hardening-guide.md) | Production security checklist |

## Implementation Status Matrix

| Capability | Status | Notes |
|---|---|---|
| Modbus TCP / RTU / RTU-over-TCP | ✅ Shipped | Batching, multi-register types, write priority, shared-bus multi-slave all delivered. **No transport TLS.** FC15 (write multiple coils) has no runtime caller yet. See [compliance report](docs/requirements/XEDGE-CRD-001-gateway-compliance-report.md) §4.1–4.2 |
| OPC UA client | ✅ Shipped | |
| OPC UA server (northbound) | ✅ Shipped | Basic information model |
| BACnet/IP client | ✅ Shipped | BACnet MS/TP is *Planned* |
| EtherNet/IP Scanner | 🟡 Partial, customer-accepted | `pycomm3`-based explicit messaging at a scan interval, not true CIP Class 1 cyclic I/O (no mainstream Python library implements it). No real-server integration test exists for this driver alone — see [handover package](docs/planning/XEDGE-CRD-001-handover.md) §3–4 |
| SNMP client (v1/v2c/v3) | ✅ Shipped | GET/GETNEXT/GETBULK/SET, USM auth+privacy |
| SNMP agent (device pollable) | 🟡 Partial | v1/v2c only, read-only (no SNMPv3, no SET); placeholder Private Enterprise Number — must be replaced before production NMS use |
| SNMP TRAP/INFORM (send + receive) | ✅ Shipped | TRAP is fire-and-forget by protocol definition (RFC 1905); use INFORM where delivery confirmation matters |
| SMTP notifications + scheduled reports | ✅ Shipped | |
| SNTP | ✅ Shipped | Query-only; does not set the system clock |
| MQTT: Subscriber / configurable Publisher / embedded Broker | 🟡 Partial | All three roles shipped. Broker cannot enforce mandatory client certificates (mTLS) — an `amqtt` library limit, client-side mTLS is unaffected |
| Store-and-forward | ✅ Shipped | |
| Alarm engine | ✅ Shipped | Threshold + rate-of-change, ack/shelve, independent retention |
| Asset Management | ✅ Shipped | Metadata/grouping layer (ADR-010), not a new primary config entity — customer-confirmed. Connection state is Unknown unless every backing driver is Modbus/SNMP-family (only those report real connectivity today) |
| Web UI (device-local) | ✅ Shipped | A handful of array-shaped config fields (alarm rules, MQTT/SMTP/SNMP notification lists) are raw-YAML-only, not on a dedicated form |
| RBAC, audit log, rate limiting | ✅ Shipped | |
| OpenTelemetry / Prometheus | ✅ Shipped | |
| Fleet agent + Fleet Manager | ✅ Shipped | Registration, heartbeat, config delivery, join-token/mTLS enrollment, automatic certificate rotation. **Single-tenant, API-only — no dashboard** (ADR-013 §2, by design, not a gap) |
| PKI / certificate management | ✅ Shipped | A real fleet CA (self-generated, self-managed) with CSR signing and rotation — not a workflow for importing a customer-supplied external root CA |
| mTLS (fleet enrollment + heartbeat, MQTT client) | ✅ Shipped | Web UI/REST API still has server-side TLS only |
| IEC 60870-5-104 | ❌ Planned | Delivery 2 |
| DNP3 | ❌ Planned | Delivery 2 |
| IEC 61850 (MMS/GOOSE/SV) | ❌ Planned | Delivery 2 |
| DLMS/COSEM | ❌ Planned | Delivery 2 |
| PROFINET | ❌ Planned | Delivery 2 |
| BACnet MS/TP | ❌ Planned | Delivery 2 |
| OTA updates | ❌ Planned | Delivery 2 — container image based, not host OS or kernel |
| AWS IoT Core / Azure IoT Hub connectors | ❌ Planned | Delivery 2 |
| IEC 62443 SL-1 baseline | 🟡 Partial | See the [gap analysis](docs/security/iec62443-sl1-gap-analysis.md) for the honest control-by-control status |
| IEC 62443 SL-2 | ❌ Planned | Delivery 2 |
| HIL validation against physical field hardware | ❌ Not performed | No hardware was available in any development environment used on this delivery — see [handover package](docs/planning/XEDGE-CRD-001-handover.md) §2/§3 |

**Legend:** ✅ Shipped · 🟡 Partial — works, with named gaps · ❌ Planned — not implemented

## Documentation

| Document | Description |
|---|---|
| [Development Plan](docs/planning/development-plan.md) | Current delivery structure, capacity, standards, risk register |
| [Delivery 1 Sprint Plan](docs/planning/crd-delivery-plan.md) | Sprint-by-sprint backlog and status for the active delivery |
| [Delivery Decision Record](docs/planning/XEDGE-DR-001-delivery-decisions.md) | Every delivery decision, with rationale |
| [High-Level Requirements](docs/requirements/HLR.md) | Functional, non-functional, security and compliance requirements, with implementation status |
| [Handover Package](docs/planning/XEDGE-CRD-001-handover.md) | **Start here for Delivery 1 handover** — explicit scope statements, known limitations, deferred items |
| [Customer Compliance Report](docs/requirements/XEDGE-CRD-001-gateway-compliance-report.md) | Full requirement-by-requirement compliance matrix, revised from the original pre-delivery gap analysis |
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
