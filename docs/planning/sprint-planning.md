# xEdge — Detailed Sprint Planning

**Document ID:** XEDGE-PLAN-002  
**Version:** 1.0  
**Status:** Draft  
**Date:** 2026-07-03  

Sprint duration: **2 weeks**  
Team: **10 engineers** (core), expanding per phase  
Capacity per sprint: ~100 story points (10 engineers × 2 weeks × avg 5 SP/engineer/day × 1 day/sprint overhead)

---

## Phase 1: Foundation (Sprints 1–6, Months 1–3)

### Sprint 1 — Project Bootstrap & Scaffolding

**Goal:** Every engineer can build, test, and run xEdge locally. CI is green from day one.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-001 | Lead Arch | 5 | Define repository structure, pyproject.toml, hatchling build config |
| XEDGE-002 | DevOps | 8 | GitHub Actions CI: lint (ruff), type check (mypy), unit tests (pytest), coverage |
| XEDGE-003 | DevOps | 8 | Docker multi-arch build pipeline (amd64, arm64, armv7) via buildx |
| XEDGE-004 | Core Eng 1 | 5 | asyncio application skeleton: main loop, graceful SIGTERM shutdown, watchdog kick |
| XEDGE-005 | Core Eng 2 | 8 | Config engine: YAML load, JSON Schema validation, layered merge, `ConfigStore` |
| XEDGE-006 | Core Eng 1 | 5 | Structured logging setup: structlog, JSON formatter, log level config |
| XEDGE-007 | Lead Arch | 5 | `BaseDriver` ABC definition, `DriverSupervisor` skeleton, `TagUpdate` dataclass |
| XEDGE-008 | QA | 8 | Test framework: pytest fixtures, async test helpers, CI Docker-compose test env |
| XEDGE-009 | Protocol Eng 1 | 5 | Modbus simulator (Docker, configurable registers) for CI use |
| XEDGE-010 | Security | 5 | License audit: enumerate all candidate libraries, flag GPL dependencies; procure IEC 60870-5-104 + IEEE 1815 specs; document clean-room rules per ADR-006 |
| XEDGE-011 | DevOps | 5 | Hardware test lab setup: Raspberry Pi 4 + x86 IPC registered as self-hosted runners |
| XEDGE-012 | All | 3 | Team agreements: coding standards, PR review checklist, branch strategy, Definition of Done |

**Sprint capacity:** 70 SP (accounting for onboarding overhead in Sprint 1)  
**Sprint review:** CI pipeline running; skeleton app starts and logs JSON; all engineers shipping code

---

### Sprint 2 — Driver Framework + Modbus TCP (Core)

**Goal:** First real tag read from a Modbus TCP server via the driver framework.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-013 | Core Eng 1 | 8 | `DriverSupervisor`: load drivers from config, lifecycle management, restart with backoff |
| XEDGE-014 | Core Eng 2 | 5 | `TagUpdate` → `UnifiedTag` normalization stage (Phase 1: no deadband, pass-through) |
| XEDGE-015 | Protocol Eng 1 | 8 | In-house Modbus codec (ADR-006): MBAP framing, PDU encode/decode FC01–FC04, exception PDU parsing; validated against pymodbus as black-box oracle |
| XEDGE-015b | Protocol Eng 1 | 8 | Modbus TCP client: async connect, polling scheduler, scan rate, reconnect with backoff |
| XEDGE-016 | Protocol Eng 1 | 5 | Modbus TCP driver: error handling (timeout, exception codes → quality flags) |
| XEDGE-017 | Core Eng 2 | 8 | Internal async pipeline bus: driver output queue → pipeline → northbound input queue |
| XEDGE-018 | DevOps | 5 | Integration test: Modbus TCP driver vs. simulator; verify all FC reads return correct values |
| XEDGE-019 | QA | 8 | Unit tests for `DriverSupervisor` restart logic (fault injection; mock driver) |
| XEDGE-020 | Core Eng 1 | 5 | Config schema v0.1: driver section, tag group, tag definition |
| XEDGE-021 | Lead Arch | 5 | ADR-001: Python + C extensions language choice |
| XEDGE-022 | Security | 3 | Threat model v0.1 (attack surface enumeration) |

**Sprint capacity:** 68 SP  
**Sprint review:** Modbus TCP tags read at configured scan rate via the in-house stack, visible in logs

---

### Sprint 3 — MQTT Northbound + Sparkplug B Birth/Data

**Goal:** Tags published to MQTT broker using Sparkplug B. Milestone M1 achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-023 | Protocol Eng 2 | 13 | MQTT connector: paho-mqtt, TLS, connect/reconnect state machine, backoff |
| XEDGE-024 | Protocol Eng 2 | 13 | Sparkplug B encoder (in-house per ADR-006, from Eclipse spec v3.0 + official `.proto`): NBIRTH, NDEATH (LWT), NDATA with all Modbus data types; seq/bdSeq/alias state machine |
| XEDGE-025 | Protocol Eng 2 | 5 | Sparkplug B DBIRTH/DDATA: device-level messages per driver instance |
| XEDGE-026 | Core Eng 1 | 8 | Northbound connector plugin interface: `NorthboundConnector` ABC |
| XEDGE-027 | QA | 8 | Integration test: full path Modbus TCP → Sparkplug B → MQTT broker; verify payload |
| XEDGE-028 | DevOps | 5 | Add Mosquitto MQTT broker to CI Docker-compose stack |
| XEDGE-029 | Protocol Eng 2 | 5 | MQTT configuration: multiple broker support (≥ 2 simultaneous targets) |
| XEDGE-030 | Core Eng 2 | 5 | Basic RAM ring buffer per tag group (Phase 1: no SD persistence yet) |
| XEDGE-031 | Lead Arch | 3 | ADR-002: Sparkplug B as primary payload format |
| XEDGE-032 | All | 5 | **Milestone M1 demo:** end-to-end Modbus TCP → Sparkplug B pipeline on Raspberry Pi 4 |

**Sprint capacity:** 70 SP  
**Sprint review:** Tags visible in MQTT Explorer with correct Sparkplug B structure; demo recorded  
**🏁 MILESTONE M1 — First Data**

---

### Sprint 4 — Pipeline Engine v1 + Quality Model

**Goal:** Proper normalization, quality stamping, and OPC UA quality code mapping.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-033 | Core Eng 1 | 8 | Quality code mapper: Modbus exception codes → OPC UA StatusCode |
| XEDGE-034 | Core Eng 2 | 8 | Timestamp resolver: source vs ingestion timestamp logic; nanosecond precision |
| XEDGE-035 | Core Eng 1 | 8 | Engineering unit scaling: scale/offset per tag; typed value conversion |
| XEDGE-036 | Core Eng 2 | 8 | Deadband filter: absolute and percentage; bypass for first-value and quality-change |
| XEDGE-037 | Protocol Eng 1 | 8 | Modbus write support: FC05/FC06/FC15/FC16 codec + client, via REST API (Phase 1: unauthenticated, localhost only) |
| XEDGE-038 | QA | 8 | Unit tests: quality mapper, timestamp resolver, deadband filter edge cases |
| XEDGE-039 | Core Eng 2 | 5 | `UnifiedTag` metadata enrichment: source address, driver ID, request latency |
| XEDGE-040 | Protocol Eng 3 | 8 | Modbus driver: configurable FC and register maps from YAML config |
| XEDGE-041 | DevOps | 5 | Performance baseline: measure tag/s throughput on Raspberry Pi 4 with 1000 Modbus tags |

**Sprint capacity:** 66 SP

---

### Sprint 5 — Config Engine v1 + REST API Skeleton

**Goal:** Configuration fully schema-validated; basic REST API for status and config read.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-042 | Core Eng 1 | 8 | Config version history: store last 10 configs; rollback API |
| XEDGE-043 | Core Eng 2 | 8 | Config hot-reload: file watcher → validate → apply → restart affected drivers |
| XEDGE-044 | Protocol Eng 3 | 8 | REST API skeleton: FastAPI + uvicorn (HTTPS, port 8443); `/health`, `/api/v1/status` |
| XEDGE-045 | Protocol Eng 3 | 8 | REST API: `GET /api/v1/drivers` — list drivers with status, metrics, last-error |
| XEDGE-046 | Core Eng 1 | 5 | REST API: `GET /api/v1/config` — return current running config (secrets redacted) |
| XEDGE-047 | Security | 5 | Secrets resolver: `${SECRET:name}` substitution from env vars and files |
| XEDGE-048 | QA | 8 | Integration tests: config validation errors return human-readable messages; reload works |
| XEDGE-049 | Lead Arch | 5 | JSON Schema v0.1 published in `/config/schema/` |
| XEDGE-050 | DevOps | 5 | Dependabot config, pip-audit in CI, first SBOM generation with syft |

**Sprint capacity:** 60 SP

---

### Sprint 6 — Store & Forward v1 (RAM) + End-of-Phase Polish

**Goal:** RAM ring buffer with overflow handling; Phase 1 close-out and retrospective.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-051 | Core Eng 1 | 13 | RAM ring buffer: per-group, configurable depth, FIFO eviction (not alarm groups) |
| XEDGE-052 | Core Eng 2 | 8 | Backpressure: when RAM buffer full, slow down driver scan rate; log warning |
| XEDGE-053 | Core Eng 1 | 8 | Buffer metrics: queue depth, oldest pending timestamp exposed as OTel gauge |
| XEDGE-054 | Protocol Eng 2 | 8 | MQTT reconnect replay: send buffered data on reconnect (RAM only at this stage) |
| XEDGE-055 | DevOps | 8 | Docker image: non-root user, read-only rootfs, writable /data mount, health check |
| XEDGE-056 | QA | 8 | End-to-end test: simulate 5-minute MQTT outage; verify buffered data replayed correctly |
| XEDGE-057 | All | 5 | Phase 1 retrospective; backlog grooming for Phase 2 |
| XEDGE-058 | Lead Arch | 3 | ADR-003: SQLite WAL for cold storage (Phase 2 design decision) |

**Sprint capacity:** 61 SP  
**🏁 PHASE 1 COMPLETE**

---

## Phase 2: Tier-1 Protocols Complete (Sprints 7–12, Months 4–6)

### Sprint 7 — Modbus RTU Serial + RTU-over-TCP

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-059 | Protocol Eng 1 | 13 | Modbus RTU driver (in-house framing: CRC-16, T1.5/T3.5 inter-frame timing): RS-232/RS-485 via pyserial-asyncio; baud/parity/bits config |
| XEDGE-060 | Protocol Eng 1 | 8 | Modbus RTU multi-drop: bus address handling (1–247); inter-frame timing tuning per baud rate |
| XEDGE-061 | Protocol Eng 1 | 5 | Modbus RTU-over-TCP: framing distinction from Modbus TCP |
| XEDGE-062 | DevOps | 8 | Serial port HIL test: USB-to-RS485 adapter in CI runner; loopback test |
| XEDGE-063 | Core Eng 1 | 5 | udev rule template for serial port hotplug; driver auto-restart on device reconnect |
| XEDGE-064 | QA | 8 | Integration tests: RTU vs RTU-over-TCP framing; simulate bus collision |
| XEDGE-065 | Protocol Eng 3 | 8 | Modbus driver config: multi-device per driver instance (up to 256 unit IDs) |
| XEDGE-066 | Core Eng 2 | 8 | Driver framework: multiple instances of same driver type, isolated config/state |

---

### Sprint 8 — OPC UA Client Driver v1

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-067a | Protocol Eng 2 | 13 | open62541 asyncio C-extension binding layer (ADR-006): build integration, event-loop bridging, shared by client + server |
| XEDGE-067 | Protocol Eng 2 | 13 | OPC UA client (open62541): endpoint discovery (LDS + direct URL), security modes: None/Sign/SignAndEncrypt |
| XEDGE-068 | Protocol Eng 2 | 13 | OPC UA subscriptions: MonitoredItems, configurable sampling interval, reconnect + re-subscribe |
| XEDGE-069 | Protocol Eng 2 | 5 | OPC UA polling mode: single-read for nodes that don't support subscriptions |
| XEDGE-070 | Protocol Eng 2 | 5 | OPC UA quality: StatusCode → OPC UA quality model (direct mapping) |
| XEDGE-071 | Protocol Eng 3 | 5 | OPC UA client config: NodeId browsing, NodeSet import from XML |
| XEDGE-072 | DevOps | 8 | OPC UA simulator (Prosys OPC UA Simulation Server, open62541 demo server, or asyncua as oracle) in CI |
| XEDGE-073 | QA | 8 | Integration tests: OPC UA subscription CoV vs. polling; reconnect after server restart |

---

### Sprint 9 — OPC UA Server (Northbound) v1

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-074 | Protocol Eng 2 | 13 | OPC UA server: open62541 (shared binding from XEDGE-067a), information model auto-built from tag config hierarchy |
| XEDGE-075 | Protocol Eng 2 | 8 | OPC UA server: subscriptions, MonitoredItems, configurable publish interval |
| XEDGE-076 | Protocol Eng 2 | 5 | OPC UA server: security policies (None on loopback, Basic256Sha256 on LAN) |
| XEDGE-077 | Protocol Eng 2 | 5 | OPC UA server: user auth (anonymous off by default, username/password) |
| XEDGE-078 | Core Eng 1 | 5 | OPC UA server: diagnostic nodes (_Diagnostics subtree) with driver status, queue depths |
| XEDGE-079 | Protocol Eng 3 | 8 | OPC UA write routing: incoming write → `WriteRouter` → southbound driver |
| XEDGE-080 | QA | 8 | OPC UA CTT (Compliance Test Tool) run; document any failures |
| XEDGE-081 | Lead Arch | 5 | ADR: OPC UA information model versioning strategy |

---

### Sprint 10 — SD Card Store & Forward v1 (SQLite WAL)

**Goal:** Milestone M2 (MVP Alpha) achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-082 | Core Eng 1 | 13 | SQLite WAL store: per-group DB files, write UnifiedTag rows, WAL sync policy |
| XEDGE-083 | Core Eng 2 | 8 | RAM → SD spill: when RAM buffer full, spill oldest to cold tier |
| XEDGE-084 | Core Eng 1 | 8 | Replay task: read from cold tier on reconnect; time-ordered; configurable rate-limit |
| XEDGE-085 | Core Eng 2 | 5 | Checkpoint tracking: per-connector, advance after PUBACK batch |
| XEDGE-086 | Core Eng 1 | 8 | Storage pressure monitor: 80%/95% alerts; configurable eviction policy |
| XEDGE-087 | QA | 8 | Power-loss test: intentional power cut with 1000 in-flight tags; verify WAL recovery |
| XEDGE-088 | DevOps | 5 | Performance benchmark: store-forward write/read rate on SD card (Class A2) |
| XEDGE-089 | All | 3 | **Milestone M2 demo and stakeholder review** |

**🏁 MILESTONE M2 — MVP Alpha**

---

### Sprint 11 — Per-Tag Retention Policy + Virtual Tags

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-090 | Core Eng 2 | 8 | Per-tag retention: `retention_duration` and `retention_max_samples` enforcement |
| XEDGE-091 | Core Eng 2 | 5 | Automatic data purge: background task evicts expired data; no inline blocking |
| XEDGE-092 | Core Eng 1 | 13 | Virtual tag engine: safe expression evaluator (simpleeval); tag dependencies tracked |
| XEDGE-093 | Core Eng 1 | 8 | Virtual tag recalculation: triggered on any input tag update; result enters pipeline |
| XEDGE-094 | Protocol Eng 3 | 8 | Config schema v0.2: retention, virtual tags, deadband refinement |
| XEDGE-095 | QA | 8 | Unit tests: retention boundary conditions; virtual tag circular dependency detection |
| XEDGE-096 | Lead Arch | 5 | ADR: virtual tag expression evaluation security (why no eval(); simpleeval sandbox) |

---

### Sprint 12 — Performance Tuning + Phase 2 Close

**Goal:** Meet NFR performance targets; Phase 2 retrospective.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-097 | Core Eng 1 | 13 | Pipeline throughput optimization: profile and eliminate bottlenecks; target 50k tags/s |
| XEDGE-098 | Core Eng 2 | 8 | C extension for hot pipeline path: normalize + quality-stamp inner loop (Cython or cffi) |
| XEDGE-099 | Protocol Eng 1 | 5 | Modbus batch read optimization: coalesce adjacent registers into single request |
| XEDGE-100 | DevOps | 8 | Performance CI gate: benchmark runs on every merge; alert on >10% regression |
| XEDGE-101 | QA | 8 | Stress test: 50 simultaneous Modbus drivers × 1000 tags each; measure CPU/RAM |
| XEDGE-102 | Protocol Eng 3 | 5 | REST API: `GET /api/v1/metrics` (Prometheus-compatible endpoint) |
| XEDGE-103 | All | 5 | Phase 2 retrospective; Phase 3 planning |

**🏁 PHASE 2 COMPLETE**

---

## Phase 3: Security & Observability (Sprints 13–18, Months 7–9)

### Sprint 13 — TLS + Certificate Management

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-104 | Security | 13 | mTLS on REST API: self-signed CA bootstrap; client cert validation; trust store config |
| XEDGE-105 | Security | 13 | mTLS on MQTT northbound: client cert auth; broker cert verification |
| XEDGE-106 | Security | 8 | Certificate management API: `POST /api/v1/security/certificates` upload; rotation with overlap |
| XEDGE-107 | Security | 8 | Device identity certificate: auto-generate on first boot; store in OS keyring |
| XEDGE-108 | Security | 5 | ACME client: Let's Encrypt compatible for internet-reachable deployments |
| XEDGE-109 | QA | 8 | Security tests: TLS downgrade attempt; expired cert rejection; untrusted CA rejection |
| XEDGE-110 | DevOps | 5 | CI: TLS test infra (test CA, signed certs for integration tests) |

---

### Sprint 14 — RBAC + Authentication

**Goal:** Milestone M3 (MVP Beta) achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-111 | Security | 13 | RBAC engine: permission matrix, role assignment, per-endpoint enforcement decorator |
| XEDGE-112 | Security | 8 | User management API: CRUD users, assign roles, list active sessions |
| XEDGE-113 | Security | 8 | JWT token issuance and validation: configurable expiry, revocation list |
| XEDGE-114 | Security | 5 | bcrypt password storage; login lockout after 5 failures |
| XEDGE-115 | Security | 5 | Login banner (configurable); session idle timeout (default 15 min) |
| XEDGE-116 | Core Eng 1 | 5 | REST API: auth middleware applies to all non-health endpoints |
| XEDGE-117 | QA | 8 | Security tests: privilege escalation attempt; role-boundary enforcement |
| XEDGE-118 | All | 5 | **Milestone M3 demo** |

**🏁 MILESTONE M3 — MVP Beta**

---

### Sprint 15 — Audit Log + TPM Integration

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-119 | Security | 13 | Audit log: hash-chained JSON; append-only file; log all auth, config, write events |
| XEDGE-120 | Security | 8 | Audit log SIEM forwarding: syslog-ng / Fluentd config templates (CEF + JSON) |
| XEDGE-121 | Security | 8 | TPM 2.0 integration: device cert private key stored in TPM via tpm2-pytss |
| XEDGE-122 | Security | 5 | PKCS#11 interface for HSM alternative to TPM |
| XEDGE-123 | Security | 5 | Binary integrity verification: signed manifest checked on startup |
| XEDGE-124 | QA | 8 | Audit test: verify all required events appear in audit log; hash chain validated |
| XEDGE-125 | Security | 3 | IEC 62443 SL-1 gap analysis: map all SR 1.x–7.x to current implementation |

---

### Sprint 16 — OpenTelemetry Integration

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-126 | Core Eng 2 | 13 | OpenTelemetry SDK integration: OTLP/gRPC exporter, resource attributes (device ID, version) |
| XEDGE-127 | Core Eng 2 | 8 | OTel traces: spans for driver.read, pipeline.process, store.write, northbound.publish |
| XEDGE-128 | Core Eng 1 | 8 | OTel metrics: counters, gauges, histograms for all pipeline stages |
| XEDGE-129 | Core Eng 2 | 5 | OTel + structlog correlation: inject trace_id/span_id into all log entries |
| XEDGE-130 | Core Eng 1 | 5 | Prometheus endpoint: OTel Prometheus exporter on `/metrics` (port 9090) |
| XEDGE-131 | DevOps | 8 | OTel collector stack in dev Docker-compose: Grafana + Tempo + Loki |
| XEDGE-132 | QA | 5 | Verify: every alarm event produces a 100%-sampled trace end-to-end |

---

### Sprint 17 — Remote Diagnostic CLI

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-133 | Core Eng 1 | 13 | Diagnostic WebSocket server (wss://): authenticated session, RBAC-gated commands |
| XEDGE-134 | Core Eng 1 | 8 | CLI commands: `status`, `driver list`, `driver restart`, `driver logs`, `tag read` |
| XEDGE-135 | Core Eng 2 | 8 | CLI commands: `store-forward status`, `network check`, `config show`, `config validate` |
| XEDGE-136 | Core Eng 1 | 5 | Packet capture command: duration + size limit, AES-256 encrypted upload to fleet manager |
| XEDGE-137 | Core Eng 2 | 5 | Self-test command: loopback driver, store write/read, northbound ping; report pass/fail |
| XEDGE-138 | Security | 5 | Diagnostic CLI session audit logging: all commands recorded with session ID |
| XEDGE-139 | QA | 5 | Integration tests: CLI commands via WebSocket; unauthorized command rejection |
| XEDGE-140 | DevOps | 5 | `xedge-cli` thin client tool for developer workstations |

---

### Sprint 18 — IEC 62443 SL-1 Hardening + Phase 3 Close

**Goal:** Milestone M4 (Security Baseline) achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-141 | Security | 13 | IEC 62443 SL-1 gap closure: remediate all SR findings from Sprint 15 gap analysis |
| XEDGE-142 | Security | 8 | Hardening guide v1: OS hardening, xEdge config hardening, network segmentation |
| XEDGE-143 | Security | 8 | NERC CIP CIP-007 evidence: failed login reports, access logs, patch history export |
| XEDGE-144 | Security | 5 | Rate limiting on REST API: 100 req/min per IP; lockout on auth failure threshold |
| XEDGE-145 | DevOps | 5 | Container image signing with cosign; policy enforcement in CI |
| XEDGE-146 | QA | 5 | Security regression test suite (OWASP ZAP against REST API) |
| XEDGE-147 | All | 5 | **Milestone M4 demo; IEC 62443 SL-1 attestation** |

**🏁 MILESTONE M4 — Security Baseline**  
**🏁 PHASE 3 COMPLETE**

---

## Phase 4: Tier-2 Protocols (Sprints 19–26, Months 10–13)

### Sprint 19 — IEC 60870-5-104 Driver

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-148 | Protocol Eng 2 | 13 | In-house IEC 104 stack (ADR-006, clean-room from IEC 60870-5-104 spec): APCI framing, U/S/I frames, k/w flow control, TESTFR keepalive; lib60870 binaries as black-box oracle only |
| XEDGE-149 | Protocol Eng 2 | 13 | IEC 104 master: connect/STARTDT, spontaneous data (ASDU types 1–45), quality flags |
| XEDGE-150 | Protocol Eng 2 | 8 | IEC 104 general interrogation (type 100) and counter interrogation (type 101) |
| XEDGE-151 | Protocol Eng 2 | 5 | IEC 104 command issuance: types 45–51, 58–64; command result feedback |
| XEDGE-152 | Protocol Eng 2 | 5 | IEC 104 quality → OPC UA quality mapping (IV, NT, SB, BL, OV flags) |
| XEDGE-153 | QA | 8 | HIL test: IEC 104 against IEC 104 simulator (e.g., 61850 Playground or QTester104) |

---

### Sprint 20 — DNP3 Driver

**Gate (ADR-006):** proceed in-house only if the IEC 104 in-house stack (Sprint 19) landed on budget; otherwise license the commercial Rust `dnp3` crate and convert these stories to integration work.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-154 | Protocol Eng 3 | 13 | In-house DNP3 link layer + transport function (clean-room from IEEE 1815): frame CRC, transport segmentation/reassembly |
| XEDGE-155 | Protocol Eng 3 | 13 | DNP3 master: TCP + serial transport; application layer; unsolicited responses |
| XEDGE-156 | Protocol Eng 3 | 8 | DNP3 data objects: Binary Input, Analog Input, Counter, Binary Output, Analog Output |
| XEDGE-157 | Protocol Eng 3 | 5 | DNP3 integrity poll scheduling; event data classes |
| XEDGE-158 | Protocol Eng 3 | 5 | DNP3 quality flag → OPC UA quality mapping |
| XEDGE-159 | QA | 8 | HIL test: DNP3 against TMW DNP3 Test Harness or FreyrSCADA DNP3 simulator |

---

### Sprint 21 — BACnet IP Driver

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-160 | Protocol Eng 1 | 13 | bacpypes3 integration (MIT, per ADR-006): BACnet/IP device discovery, object enumeration, property read |
| XEDGE-161 | Protocol Eng 1 | 8 | BACnet COV subscriptions: Analog Input/Value, Binary Input/Output/Value |
| XEDGE-162 | Protocol Eng 1 | 8 | BACnet polling fallback: objects without COV support |
| XEDGE-163 | Protocol Eng 1 | 5 | BACnet Write Present Value (commandable objects) |
| XEDGE-164 | Protocol Eng 1 | 5 | BACnet quality: reliability property → OPC UA quality mapping |
| XEDGE-165 | QA | 8 | HIL test: BACnet against YABE (BACnet Explorer) + Sedona VM simulator |

---

### Sprint 22 — BACnet MS/TP Driver

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-166 | Protocol Eng 1 | 13 | BACnet MS/TP: RS-485 serial, token-passing master, MAC address config (0–127) |
| XEDGE-167 | Protocol Eng 1 | 8 | MS/TP tuning: baud rate, max-info-frames, max-master |
| XEDGE-168 | Protocol Eng 1 | 5 | Shared config schema for BACnet IP + MS/TP (same data object model, different transport) |
| XEDGE-169 | DevOps | 5 | RS-485 HIL test rig: USB-RS485 adapter + BACnet MSTP test device in lab |
| XEDGE-170 | QA | 8 | Integration test: MS/TP device read + COV; confirm timing meets spec |
| XEDGE-171 | Protocol Eng 2 | 8 | Refactor: BACnet driver framework allows IP + MSTP instances simultaneously |

---

### Sprint 23 — EtherNet/IP (Rockwell) Driver

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-172 | Protocol Eng 2 | 13 | pycomm3 integration: Ethernet/IP CIP originator; ControlLogix / CompactLogix connection |
| XEDGE-173 | Protocol Eng 2 | 13 | Tag-name based access: symbolic PLC tags from L5X export or runtime discovery |
| XEDGE-174 | Protocol Eng 2 | 8 | Array and UDT (User-Defined Type) tag handling |
| XEDGE-175 | Protocol Eng 2 | 5 | EtherNet/IP write: tag write with confirmation |
| XEDGE-176 | QA | 8 | HIL test: real CompactLogix (or Studio 5000 Logix Designer emulator) |
| XEDGE-177 | Protocol Eng 3 | 5 | Config schema: EtherNet/IP driver section with tag-list import from L5X CSV |

---

### Sprint 24 — PROFINET Driver (Phase 1: C Extension)

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-178 | Protocol Eng 3 | 13 | PROFINET IO C extension: RT frame parsing, I/O data cyclic exchange |
| XEDGE-179 | Protocol Eng 3 | 13 | GSDML parser: extract data types and module layout from device description files |
| XEDGE-180 | Protocol Eng 3 | 8 | PROFINET IO-Controller: AR establishment, CR negotiation |
| XEDGE-181 | Protocol Eng 3 | 5 | PROFINET quality: IOPS/IOCS → OPC UA quality mapping |
| XEDGE-182 | DevOps | 5 | PROFINET test rig: Siemens PLCSIM Advanced or ET 200SP simulator |
| XEDGE-183 | QA | 8 | HIL test: PROFINET IO device cyclic data read; topology change handling |

---

### Sprint 25 — Driver Framework v2: Hot-Reload + Driver Health API

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-184 | Core Eng 1 | 13 | Driver hot-reload: config change triggers graceful restart (disconnect → new config → reconnect) without pipeline gap |
| XEDGE-185 | Core Eng 2 | 8 | Driver health API: `GET /api/v1/drivers/{id}/health` — last read time, error count, consecutive errors |
| XEDGE-186 | Core Eng 1 | 8 | Driver enable/disable at runtime: `POST /api/v1/drivers/{id}/disable` — clean shutdown without removing config |
| XEDGE-187 | Core Eng 2 | 5 | Driver config dry-run: `POST /api/v1/drivers/{id}/validate` — validate new config without applying |
| XEDGE-188 | QA | 8 | Integration tests: hot-reload of 10 drivers simultaneously; no tag gap > 2 scan cycles |
| XEDGE-189 | Protocol Eng 1 | 5 | Cross-driver tag reference: allow virtual tag to reference tags from different driver instances |
| XEDGE-190 | Lead Arch | 5 | ADR: driver isolation model and thread executor design |

---

### Sprint 26 — Phase 4 Integration Testing + Milestone M5

**Goal:** All Tier-2 drivers tested end-to-end; Milestone M5 achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-191 | QA | 13 | Full protocol compatibility matrix: all drivers vs. simulator + real hardware |
| XEDGE-192 | QA | 8 | Multi-protocol test: 5 simultaneous drivers, mixed protocols, 10k tags total |
| XEDGE-193 | DevOps | 8 | Hardware compatibility: test on 4 hardware platforms (RPi 4, RPi 5, Advantech, Beckhoff) |
| XEDGE-194 | Protocol Eng 2 | 5 | IEC 104 + DNP3: confirm command write-back via Sparkplug B NCMD round-trip |
| XEDGE-195 | Core Eng 1 | 5 | Alarm detection engine v1: threshold hi/lo/hi-hi/lo-lo; state change detection |
| XEDGE-196 | Protocol Eng 3 | 5 | PROFINET: add alarm and diagnostic telegram handling |
| XEDGE-197 | All | 5 | **Milestone M5 demo** |

**🏁 MILESTONE M5 — Protocol Complete**  
**🏁 PHASE 4 COMPLETE**

---

## Phase 5: Fleet, Advanced Features & Multi-Cloud (Sprints 27–32, Months 14–16)

### Sprint 27 — IEC 61850 MMS Client

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-198 | Protocol Eng 1 | 13 | libiec61850 Python bindings: IED connect, server model discovery |
| XEDGE-199 | Protocol Eng 1 | 13 | IEC 61850 MMS: report control blocks (RCB) — buffered (BRCB) + unbuffered (URCB) subscriptions |
| XEDGE-200 | Protocol Eng 1 | 8 | IEC 61850 MMS: XCBR, MMXU, MMTR, XSWI logical node mapping to UnifiedTag |
| XEDGE-201 | Protocol Eng 1 | 5 | IEC 61850 control: SBO (Select Before Operate) and direct control |
| XEDGE-202 | Protocol Eng 1 | 5 | IEC 61850 quality: q attribute → OPC UA quality mapping |
| XEDGE-203 | QA | 8 | HIL test: libiec61850 server simulator; BRCB integrity vs. live report |

---

### Sprint 28 — IEC 61850 GOOSE + SV + DLMS

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-204 | Protocol Eng 2 | 13 | IEC 61850 GOOSE subscriber: raw Ethernet multicast; stale-data timeout detection |
| XEDGE-205 | Protocol Eng 2 | 8 | IEC 61850 SV (Sampled Values): 80/256 samples/cycle; power quality data types |
| XEDGE-206 | Protocol Eng 3 | 13 | DLMS/COSEM: HDLC + TCP wrapper; OBIS code access; push notifications — build-vs-buy per ADR-006 decision at Phase 4 close (in-house clean-room vs. gurux commercial license; gurux is GPL v2/commercial, NOT usable freely in commercial ed.) |
| XEDGE-207 | Protocol Eng 3 | 5 | DLMS authentication: none, low (PAP), high (GMAC-256) |
| XEDGE-208 | QA | 8 | HIL test: GOOSE stale-data detection; DLMS meter read |
| XEDGE-209 | Core Eng 1 | 5 | IEC 62443 SL-2 gap analysis: identify remaining gaps for Phase 6 |

---

### Sprint 29 — Fleet Management Agent + Fleet Manager v1

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-210 | Core Eng 1 | 13 | Fleet agent: device identity registration, heartbeat (60s), health status report |
| XEDGE-211 | Core Eng 2 | 13 | Fleet manager server (separate service): device registry, REST API, heartbeat receiver |
| XEDGE-212 | Core Eng 1 | 8 | Fleet management channel: MQTT management namespace + gRPC (configurable) |
| XEDGE-213 | Core Eng 2 | 8 | Config push: fleet manager → device; device validates, applies, reports result |
| XEDGE-214 | Security | 5 | Fleet agent mTLS: device cert used for fleet manager authentication |
| XEDGE-215 | DevOps | 5 | Fleet manager Docker deployment: Compose stack with DB backend |
| XEDGE-216 | QA | 8 | Integration test: 10-device simulated fleet; config push to all; verify all apply |

---

### Sprint 30 — OTA via RAUC + Multi-Cloud Connectors

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-217 | DevOps | 13 | RAUC integration: A/B partition scheme, update slot config, rollback on failure |
| XEDGE-218 | DevOps | 8 | RAUC bundle: xEdge Docker image → RAUC bundle; signing pipeline |
| XEDGE-219 | Core Eng 1 | 8 | Fleet agent: OTA trigger (immediate / maintenance window / staged rollout) |
| XEDGE-220 | Core Eng 2 | 8 | AWS IoT Core connector: X.509 auth, IoT Core MQTT endpoint, Thing shadow integration |
| XEDGE-221 | Core Eng 2 | 8 | Azure IoT Hub connector: SAS + X.509 auth, DPS provisioning support |
| XEDGE-222 | QA | 8 | OTA test: update with forced failure at 50%; verify rollback to prior version |

---

### Sprint 31 — Write-back, Alarm Engine v2, Config Import/Export

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-223 | Core Eng 1 | 8 | Write-back routing: NCMD/DCMD → `WriteRouter` → driver; RBAC check; audit log |
| XEDGE-224 | Core Eng 2 | 8 | Alarm engine v2: rate-of-change detection; alarm suppression (shelving); alarm state machine |
| XEDGE-225 | Core Eng 1 | 5 | Alarm independent retention: alarm tier always retained longer than telemetry tier |
| XEDGE-226 | Protocol Eng 3 | 5 | Config import: CSV and JSON bulk tag import for mass commissioning |
| XEDGE-227 | Protocol Eng 3 | 5 | Config export: export running tag list as CSV (OT engineer-friendly) |
| XEDGE-228 | Core Eng 2 | 5 | Configurable per-stream retention: different `retention_duration` for alarms vs. telemetry |
| XEDGE-229 | QA | 8 | Integration test: write-back round-trip (NCMD → Modbus FC16 → quality confirmation) |
| XEDGE-230 | DevOps | 5 | Remote command whitelist: `restart-driver`, `collect-diagnostics`, `run-self-test` |

---

### Sprint 32 — Fleet Dashboard + Phase 5 Close

**Goal:** Milestone M6 (Fleet Ready) achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-231 | UX / Core | 13 | Fleet manager dashboard: device health grid, OTA status, last-seen, alarm summary |
| XEDGE-232 | Core Eng 1 | 8 | Fleet manager: staged rollout (% of fleet per day); rollback abort for failed devices |
| XEDGE-233 | Core Eng 2 | 5 | Fleet agent: remote command execution (whitelist); result reporting |
| XEDGE-234 | DevOps | 5 | SBOM generation automated on every release; published to GitHub releases |
| XEDGE-235 | QA | 8 | Full fleet test: 20-device simulated fleet; OTA rollout; config push; heartbeat monitoring |
| XEDGE-236 | All | 5 | **Milestone M6 demo** |

**🏁 MILESTONE M6 — Fleet Ready**  
**🏁 PHASE 5 COMPLETE**

---

## Phase 6: Hardening, Compliance & GA (Sprints 33–36, Months 17–18)

### Sprint 33 — IEC 62443 SL-2 Closure

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-237 | Security | 13 | IEC 62443 SL-2 gap closure: implement all remaining SR requirements identified in Sprint 28 |
| XEDGE-238 | Security | 8 | NERC CIP evidence package: CIP-002, CIP-005, CIP-007, CIP-010 documentation and export tools |
| XEDGE-239 | Security | 8 | SOC 2 control mapping: CC6, CC8, A1 controls evidence collection |
| XEDGE-240 | Security | 5 | FIPS 140-2 mode: configure cryptography library to use FIPS-approved algorithms only |
| XEDGE-241 | Security | 5 | CIS benchmark hardening guide: OS + xEdge config checklist |
| XEDGE-242 | QA | 8 | Security regression test suite (OWASP ZAP, manual pen test checklist) |
| XEDGE-243 | DevOps | 5 | Vulnerability policy published: CVE SLAs, responsible disclosure contact |

---

### Sprint 34 — External Penetration Test + Remediation

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-244 | Security | 5 | Pen test scope definition and engagement with external firm |
| XEDGE-245 | All | 20 | Pen test finding remediation (budget; actual depends on findings) |
| XEDGE-246 | Security | 8 | Pen test report integration into security documentation |
| XEDGE-247 | QA | 8 | Hardware compatibility matrix: test on 6 platforms; document results |
| XEDGE-248 | QA | 5 | OPC UA CTT (Compliance Test Tool) full run; all mandatory tests pass |
| XEDGE-249 | QA | 5 | Sparkplug B conformance: HiveMQ Sparkplug Validator + Ignition demo project |

---

### Sprint 35 — Documentation + Reproducible Builds

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-250 | Tech Writer | 13 | User guide: installation, first-run, Modbus quick-start, OPC UA quick-start |
| XEDGE-251 | Tech Writer | 13 | Operator runbook: troubleshooting, log analysis, common error codes |
| XEDGE-252 | Tech Writer | 8 | API reference: OpenAPI 3.1 spec published; Swagger UI embedded in fleet manager |
| XEDGE-253 | Tech Writer | 5 | Compliance guide: IEC 62443 controls summary; NERC CIP evidence walkthrough |
| XEDGE-254 | DevOps | 8 | Reproducible builds: verify identical binary from same commit + lockfile on 3 build hosts |
| XEDGE-255 | DevOps | 5 | Release pipeline: semantic versioning, auto changelog from conventional commits |
| XEDGE-256 | Core Eng 1 | 5 | Migration tool: upgrade config from any prior version to current schema |

---

### Sprint 36 — GA Release + Milestone M7

**Goal:** General Availability release. Milestone M7 achieved.

| Story | Owner | Points | Description |
|---|---|---|---|
| XEDGE-257 | DevOps | 8 | GA release: signed Docker images (amd64, arm64, armv7) pushed to GHCR |
| XEDGE-258 | DevOps | 5 | RAUC GA bundle: signed, published to release page |
| XEDGE-259 | QA | 8 | Final regression test: full protocol suite on 2 hardware platforms |
| XEDGE-260 | Security | 5 | Final SBOM published (CycloneDX + SPDX); vulnerability scan clean |
| XEDGE-261 | All | 5 | GA release notes: features, known limitations, upgrade guide from beta |
| XEDGE-262 | All | 5 | **Milestone M7 demo and stakeholder sign-off** |
| XEDGE-263 | All | 3 | Project retrospective; post-GA backlog seeding (Phase 7 planning) |

**🏁 MILESTONE M7 — General Availability**  
**🏁 PHASE 6 COMPLETE**

---

## Summary Timeline

```
Month:  1    2    3    4    5    6    7    8    9    10   11   12   13   14   15   16   17   18
        ├────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┼────┤
Phase:  ╠══PHASE 1══╣╠══PHASE 2══╣╠══PHASE 3══╣╠════PHASE 4════╣╠══PHASE 5══╣╠═PHASE 6═╣

M1      ────────────●
M2           ───────────────────●
M3                       ───────────────●
M4                              ────────────────●
M5                                              ──────────────────●
M6                                                           ──────────────●
M7                                                                          ──────────────●
```

---

## Post-GA Roadmap (Phase 7+)

Planned for 6-month post-GA release cycle:

| Feature | Priority |
|---|---|
| Edge analytics: configurable aggregation (min/max/avg over window) | High |
| MQTT 5.0 shared subscriptions + topic aliases | High |
| Kafka northbound connector | Medium |
| OPC UA Historical Access server (serves store-forward data) | Medium |
| IEC 62443 SL-3 capability uplift | Medium |
| Web-based configuration UI (React + REST API) | Medium |
| Kubernetes / k3s Helm chart + operator | Low |
| Edge ML inference pipeline (ONNX runtime) | Low |
