# xEdge — System Architecture

**Document ID:** XEDGE-ARCH-001  
**Version:** 1.0  
**Status:** Draft  
**Date:** 2026-07-03  

---

## Table of Contents

1. [Architecture Principles](#1-architecture-principles)
2. [System Overview](#2-system-overview)
3. [Component Design](#3-component-design)
4. [Data Model](#4-data-model)
5. [Data Flow](#5-data-flow)
6. [Deployment Architecture](#6-deployment-architecture)
7. [Technology Stack](#7-technology-stack)
8. [Key Design Decisions](#8-key-design-decisions)

---

## 1. Architecture Principles

| Principle | Application to xEdge |
|---|---|
| **Offline-first** | Designed for disconnected operation; cloud connectivity is an optional output path, not a dependency |
| **Plugin isolation** | Each driver and connector runs in its own async task/process; a crash is contained and recoverable |
| **Schema-first config** | Configuration is validated against versioned JSON Schema before use; no silent misconfiguration |
| **Zero-trust internals** | Every service-to-service call is authenticated; no implicit trust on the management network |
| **Least surprise** | Protocol quirks are absorbed inside drivers; the pipeline only sees Unified Tags |
| **Observable by default** | Every significant state transition emits a structured log and an OTel span; no silent failures |
| **Reproducible builds** | Given a Git commit + lock file, the build is deterministic and byte-identical |
| **Small attack surface** | Minimal OS dependencies, no root by default, read-only rootfs, capabilities-based privilege |

---

## 2. System Overview

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                          xEdge Process Tree                                      ║
║                                                                                  ║
║  ┌─────────────────────────────────────────────────────────────────────────────┐ ║
║  │                        xedge-core  (main process)                           │ ║
║  │                                                                             │ ║
║  │  ┌─────────────┐  ┌──────────────────┐  ┌──────────────────────────────┐   │ ║
║  │  │ Config       │  │ Supervision Tree │  │  REST / gRPC Management API  │   │ ║
║  │  │ Engine       │  │ (asyncio + tasks)│  │  (mTLS, RBAC-enforced)       │   │ ║
║  │  └──────┬──────┘  └────────┬─────────┘  └──────────────────────────────┘   │ ║
║  │         │                  │                                                 │ ║
║  │         ▼                  ▼                                                 │ ║
║  │  ┌──────────────────────────────────────────────────────────────────────┐   │ ║
║  │  │                    Driver Supervisor                                 │   │ ║
║  │  │  ┌─────────────┐ ┌──────────────┐ ┌──────────────┐ ┌─────────────┐  │   │ ║
║  │  │  │ Modbus TCP  │ │ Modbus RTU   │ │ OPC UA Client│ │ IEC 104 ...│  │   │ ║
║  │  │  │  Driver     │ │  Driver      │ │  Driver      │ │  Driver    │  │   │ ║
║  │  │  └──────┬──────┘ └──────┬───────┘ └──────┬───────┘ └──────┬─────┘  │   │ ║
║  │  └─────────┼───────────────┼────────────────┼────────────────┼─────────┘   │ ║
║  │            └───────────────┴────────────────┴────────────────┘             │ ║
║  │                                     │                                       │ ║
║  │                                     ▼ Unified Tag stream                    │ ║
║  │  ┌──────────────────────────────────────────────────────────────────────┐   │ ║
║  │  │                     Pipeline Engine                                  │   │ ║
║  │  │  normalize → quality-stamp → deadband → virtual-tags → alarm-detect  │   │ ║
║  │  └─────────────────────────────┬────────────────────────────────────────┘   │ ║
║  │                                │                                             │ ║
║  │                                ▼                                             │ ║
║  │  ┌──────────────────────────────────────────────────────────────────────┐   │ ║
║  │  │               Store-and-Forward Engine                               │   │ ║
║  │  │   RAM ring buffers (per group) ──► WAL on SD card / eMMC             │   │ ║
║  │  └───────────────┬──────────────────────────────────────────────────────┘   │ ║
║  │                  │                                                           │ ║
║  │         ┌────────▼────────┐                                                 │ ║
║  │         │ Northbound      │                                                  │ ║
║  │         │ Dispatcher      │                                                  │ ║
║  │         └───┬─────────┬───┘                                                 │ ║
║  │             │         │                                                      │ ║
║  │   ┌─────────▼──┐ ┌────▼──────────┐                                          │ ║
║  │   │ MQTT       │ │ OPC UA Server │                                           │ ║
║  │   │ Connector  │ │               │                                           │ ║
║  │   │ (Sparkplug)│ └───────────────┘                                           │ ║
║  │   └─────┬──────┘                                                             │ ║
║  │         │ pluggable connectors                                               │ ║
║  │   ┌─────▼────┐ ┌────────────┐ ┌──────────────┐                              │ ║
║  │   │ AWS IoT  │ │ Azure IoT  │ │ Custom MQTT  │                              │ ║
║  │   └──────────┘ └────────────┘ └──────────────┘                              │ ║
║  │                                                                             │ ║
║  │  ┌─────────────────┐  ┌────────────────────┐  ┌──────────────────────┐      │ ║
║  │  │ Fleet Agent     │  │ Observability      │  │ Security / RBAC      │      │ ║
║  │  │ (OTA, config,   │  │ (OTel, structlog,  │  │ (PKI, audit log,     │      │ ║
║  │  │  heartbeat)     │  │  diag CLI)         │  │  cert rotation)      │      │ ║
║  │  └─────────────────┘  └────────────────────┘  └──────────────────────┘      │ ║
║  └─────────────────────────────────────────────────────────────────────────────┘ ║
╚══════════════════════════════════════════════════════════════════════════════════╝
```

---

## 3. Component Design

### 3.1 Config Engine

**Responsibility:** Load, validate, merge, and distribute configuration to all components.

**Design:**
- Configuration stored in YAML files; versioned JSON Schema for validation
- Layered model: `base.yaml` ← `environment.yaml` ← `runtime-overrides.yaml`
- A `ConfigStore` object holds live configuration; components subscribe to change events
- On change: validate first, emit `ConfigChanged` event only if valid; log and reject if invalid
- Version history: last 10 snapshots stored; rollback API supported

```
base.yaml ──┐
env.yaml  ──┼──► Merger ──► Validator (jsonschema) ──► ConfigStore ──► ConfigChanged event
runtime   ──┘                                                 │
                                                      ┌───────▼──────────────────────┐
                                                      │  All components subscribe to │
                                                      │  their config section        │
                                                      └──────────────────────────────┘
```

**Key classes:**
- `ConfigEngine` — loads, merges, watches for file changes
- `ConfigStore` — typed access to config sections; emits change events
- `ConfigValidator` — wraps jsonschema with human-readable error formatting
- `SecretsResolver` — resolves `${SECRET:name}` from env vars, files, or Vault

### 3.2 Driver Framework (Protocol Abstraction Layer)

**Responsibility:** Provide a uniform lifecycle and data emission interface for all protocol drivers.

**Design:**
- Abstract base class `BaseDriver` with lifecycle hooks
- Each driver runs in its own `asyncio` task under a `DriverSupervisor`
- Supervisor handles restart with exponential backoff (1s → 2s → 4s … → 5 min)
- Drivers emit `TagUpdate` objects onto an internal `asyncio.Queue` (bounded, backpressure-aware)
- C extension drivers (libiec61850, OpenDNP3 Python bindings) run in a thread executor to avoid blocking the event loop

```python
class BaseDriver(ABC):
    @abstractmethod
    async def configure(self, config: DriverConfig) -> None: ...
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def run(self, output: asyncio.Queue[TagUpdate]) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def write(self, tag_id: str, value: Any) -> WriteResult: ...
    def get_metrics(self) -> DriverMetrics: ...
```

**Driver instance isolation:**
- Each `DriverInstance` has a unique ID (UUID)
- Crash in one driver does NOT affect pipeline or other drivers
- Config hot-reload triggers a controlled driver restart (disconnect → reconfigure → connect)

**Supported drivers (by phase):**

| Driver | Implementation (per ADR-006) | Execution model |
|---|---|---|
| Modbus TCP | **In-house stack** (MBAP framing + PDU codec; pymodbus as test oracle only) | Pure Python async |
| Modbus RTU/Serial | **In-house stack** (CRC-16, T3.5 inter-frame timing) + pyserial-asyncio | Pure Python async |
| OPC UA Client | open62541 (MPL 2.0) via in-house asyncio C-extension binding | C extension + executor |
| IEC 60870-5-104 | **In-house stack** (clean-room from IEC spec; lib60870 as black-box oracle) | Pure Python async |
| DNP3 | **In-house master (lean)** — go/no-go gate after IEC 104; fallback: commercial Rust `dnp3` crate | Pure Python async / TBD |
| BACnet IP | bacpypes3 (MIT) | Async / thread executor |
| BACnet MSTP | bacpypes3 + serial | Thread executor |
| EtherNet/IP | pycomm3 (MIT) | Thread executor |
| PROFINET | **In-house (forced)** — custom C extension | C extension |
| IEC 61850 MMS | libiec61850 Python bindings | C extension + thread executor |
| IEC 61850 GOOSE | libiec61850 Python bindings | Raw socket + C extension |
| IEC 61850 SV | libiec61850 Python bindings | Raw socket + C extension |
| DLMS/COSEM | gurux-dlms-python | Thread executor |

### 3.3 Pipeline Engine

**Responsibility:** Transform raw driver output into normalized, quality-stamped, filtered `UnifiedTag` objects.

**Pipeline stages (applied in order per tag update):**

```
[TagUpdate from driver]
        │
        ▼
┌─────────────────┐
│  1. Normalize   │  Apply scale/offset, map protocol type to Python type
└────────┬────────┘
         ▼
┌─────────────────┐
│  2. Quality     │  Map protocol quality → OPC UA StatusCode
│     Stamp       │
└────────┬────────┘
         ▼
┌─────────────────┐
│  3. Timestamp   │  Use source timestamp if valid; else ingestion time + flag
│     Resolve     │
└────────┬────────┘
         ▼
┌─────────────────┐
│  4. Deadband    │  Suppress if value change < abs/pct deadband (skip for alarms)
│     Filter      │
└────────┬────────┘
         ▼
┌─────────────────┐
│  5. Virtual     │  Evaluate configured expressions using recent tag values
│     Tags        │
└────────┬────────┘
         ▼
┌─────────────────┐
│  6. Alarm       │  Check threshold/state/RoC conditions; emit AlarmEvent if triggered
│     Detect      │
└────────┬────────┘
         ▼
   [UnifiedTag] → Store-and-Forward
```

**Key data types:**

```python
@dataclass
class UnifiedTag:
    tag_id: str                   # Globally unique tag identifier
    timestamp: datetime           # UTC, nanosecond precision
    value: TagValue               # Typed union: bool/int/float/str/bytes
    quality: OPCQuality           # Good / Uncertain / Bad + substatus
    source_driver: str            # Driver instance ID
    source_address: str           # Protocol-specific address (e.g. "40001" for Modbus)
    metadata: dict[str, Any]      # Protocol-specific extras
    is_alarm: bool                # Elevated retention/priority
```

### 3.4 Store-and-Forward Engine

**Responsibility:** Buffer `UnifiedTag` objects reliably across network outages and power loss.

**Two-tier design:**

```
    Hot tier (RAM)                    Cold tier (SD card / eMMC)
  ┌──────────────────────┐           ┌──────────────────────────────────────┐
  │  asyncio.Queue       │  spill    │  SQLite (WAL mode) / Append-log      │
  │  per tag group       │ ─────────►│  one DB file per tag group           │
  │  (max_depth=10000)   │           │  per-tag retention enforced on read  │
  └──────────────────────┘           └──────────────────────────────────────┘
         │  read                              │  read (replay on reconnect)
         └──────────────────┬────────────────┘
                            ▼
                    Northbound Dispatcher
```

**Storage layout (`/data/store/`):**
```
/data/store/
├── meta.db              ← tag group registry, retention policies
├── groups/
│   ├── critical/        ← alarm/event group (higher priority)
│   │   ├── 2026-07-01.db
│   │   └── 2026-07-02.db
│   └── telemetry/
│       ├── 2026-07-01.db
│       └── current.db
└── checkpoints/         ← last-published offset per northbound connector
```

**Replay policy on reconnect:**
1. Emit current live data (from RAM queue) immediately
2. Background replay task reads from cold tier in time order
3. Replay is rate-limited (configurable: default 10× live rate)
4. Checkpoint is advanced per-batch only after northbound ACK (QoS 1 PUBACK or equivalent)

**Power-loss safety:**
- SQLite WAL + synchronous=FULL for alarm tier
- SQLite WAL + synchronous=NORMAL for telemetry tier (tradeoff: up to 1 write-batch lost)
- Startup recovery: detect incomplete WAL transaction and roll back

### 3.5 Northbound Dispatcher & Connectors

**Responsibility:** Read from the store-and-forward engine and deliver to northbound targets.

**Connector interface:**

```python
class NorthboundConnector(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def publish(self, batch: list[UnifiedTag]) -> PublishResult: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    def get_metrics(self) -> ConnectorMetrics: ...
```

**Sparkplug B connector implementation:**
- Birth certificate generated on connect; includes all active tag definitions
- `NDATA` / `DDATA` messages contain only changed metrics (delta encoding)
- `NDEATH` sent via LWT on MQTT connect
- Inbound `NCMD` / `DCMD` routed to southbound driver via `WriteRouter`

**MQTT connection state machine:**
```
DISCONNECTED ──connect()──► CONNECTING ──success──► CONNECTED
                              │                        │
                           failure                 network loss
                              │                        │
                              ▼                        ▼
                         BACKOFF ◄──────── DETECTING LOSS
                              │
                        retry after backoff
                              │
                        CONNECTING (loop)
```

### 3.6 OPC UA Server

**Responsibility:** Expose all active tags as OPC UA nodes for local SCADA/HMI or northbound OPC UA consumers.

**Information model:**
```
Objects/
└── xEdge/
    ├── _Diagnostics/          ← system health, queue depths, driver status
    ├── Drivers/
    │   ├── modbus_tcp_01/
    │   │   └── Device_PLC01/
    │   │       ├── TagGroup_Analog/
    │   │       │   ├── Temperature_01   [AnalogItemType]
    │   │       │   └── Pressure_01      [AnalogItemType]
    │   │       └── TagGroup_Digital/
    │   │           └── PumpRunning_01   [TwoStateDiscreteType]
    │   └── opcua_client_01/
    │       └── ...
    └── VirtualTags/
        └── ...
```

**Implementation:** open62541 C library (MPL 2.0) via in-house asyncio C-extension binding, shared between OPC UA client and server (ADR-006). asyncua is used only as a CI test oracle/simulator, not at runtime.

**Security policy enforcement:**
- Endpoint without security (None): bound to loopback only (`127.0.0.1`)
- Signed / SignAndEncrypt endpoints: require certificate trust validation
- Certificate trust list managed via config; auto-reject untrusted certificates
- Session timeout: configurable (default: 30 min)

### 3.7 Security & RBAC

**Responsibility:** Authentication, authorization, PKI management, and audit logging.

**RBAC model:**

| Role | Capabilities |
|---|---|
| `admin` | Full access: config write, user management, driver control, OTA trigger |
| `operator` | Config read/write (non-security), driver restart, diagnostics |
| `auditor` | Audit log read, metrics read, config read (no secrets) |
| `readonly` | Tag value read only (OPC UA / REST) |

**PKI lifecycle:**
```
Device identity cert (HSM-backed)
    │
    ├──► mTLS with fleet manager
    ├──► mTLS with MQTT broker
    └──► OPC UA client auth

CA cert chain (stored in /data/pki/trust/)
    ├──► Validate incoming client certs (OPC UA, REST API)
    └──► Validate broker server cert
```

**Certificate rotation:**
- ACME protocol supported for internet-reachable deployments
- Manual rotation: `PUT /api/v1/security/certificates/device` with new cert + key
- Rotation is hot (no restart needed); old cert accepted for 1 TTL overlap period

**Audit log format:**
```json
{
  "ts": "2026-07-03T12:34:56.789Z",
  "seq": 1234,
  "level": "AUDIT",
  "actor": {"type": "user", "id": "ops-team\\jdoe", "ip": "10.0.1.5"},
  "action": "CONFIG_CHANGE",
  "resource": "driver/modbus_tcp_01/config",
  "result": "SUCCESS",
  "diff": {"scan_rate_ms": {"old": 1000, "new": 500}},
  "session_id": "sess_abc123",
  "hash_chain": "sha256:aabbcc..."
}
```

Hash chain: each audit entry's `hash_chain` field is `SHA-256(prev_hash + this_entry_json)` providing tamper evidence.

### 3.8 Observability

**Responsibility:** Emit structured telemetry consumable by SIEM, APM, and fleet management.

**Three pillars:**

1. **Structured Logging** (structlog)
   - JSON format, stdout (captured by Docker/systemd journal)
   - Fields: `ts`, `level`, `logger`, `msg`, `driver_id`, `tag_id`, `trace_id`, `span_id`
   - Forwarded via Fluentd / syslog-ng to SIEM (CEF or raw JSON)

2. **Distributed Tracing** (OpenTelemetry)
   - Trace spans: `driver.read` → `pipeline.normalize` → `store.write` → `northbound.publish`
   - OTLP/gRPC exporter to OpenTelemetry Collector
   - Sampling: 1% for nominal flow; 100% for errors and alarms

3. **Metrics** (OpenTelemetry metrics)
   - Counters: `xedge.tags.read.total`, `xedge.tags.published.total`, `xedge.errors.total`
   - Gauges: `xedge.store.queue_depth`, `xedge.store.bytes_pending`, `xedge.northbound.lag_seconds`
   - Histograms: `xedge.pipeline.latency_ms`, `xedge.northbound.publish_latency_ms`
   - Exposed as OTLP and optionally as Prometheus `/metrics` endpoint (via OTel Prometheus exporter)

4. **Remote Diagnostic CLI**
   - Served over authenticated WebSocket (`wss://device-ip:8443/diag`)
   - Line-oriented command interface
   - All commands RBAC-checked (minimum: `operator` role)
   - Sessions logged in audit trail

---

## 4. Data Model

### 4.1 Tag Configuration Model

```yaml
drivers:
  - id: modbus_tcp_01
    type: modbus_tcp
    config:
      host: 192.168.1.100
      port: 502
      unit_id: 1
    tag_groups:
      - id: analog_inputs
        scan_rate_ms: 1000
        retention_duration: "7d"
        retention_max_samples: 100000
        deadband:
          type: percentage
          value: 0.5
        tags:
          - id: temperature_01
            address: "40001"
            data_type: FLOAT32
            scaling:
              scale: 0.1
              offset: -273.15
            engineering_unit: "°C"
            description: "Reactor inlet temperature"
```

### 4.2 Unified Tag JSON Representation

```json
{
  "tag_id": "modbus_tcp_01/Device_001/temperature_01",
  "timestamp": "2026-07-03T12:34:56.789123456Z",
  "value": 85.3,
  "data_type": "FLOAT64",
  "quality": {
    "code": "Good",
    "substatus": null,
    "source_quality": "0x00"
  },
  "source_driver": "modbus_tcp_01",
  "source_address": "40001",
  "engineering_unit": "°C",
  "is_alarm": false,
  "metadata": {
    "modbus_exception": null,
    "request_latency_ms": 12
  }
}
```

### 4.3 Sparkplug B Mapping

| Unified Tag field | Sparkplug B field |
|---|---|
| `tag_id` | `name` (metric name) |
| `value` | `value` (typed per Sparkplug B datatype) |
| `timestamp` | `timestamp` (Unix ms) |
| `quality.code == "Bad"` | `is_null = true` |
| `is_alarm = true` | Custom property: `xedge_alarm = true` |
| `engineering_unit` | `properties["engUnit"]` |
| `source_address` | `properties["sourceAddress"]` |

---

## 5. Data Flow

### 5.1 Nominal Data Flow (Online)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ t=0ms: Driver reads tag from PLC (e.g., Modbus FC03 request/response)         │
│ t=1ms: TagUpdate emitted onto driver output queue                             │
│ t=2ms: Pipeline normalizes, quality-stamps, deadband-checks                  │
│ t=3ms: UnifiedTag written to RAM ring buffer                                  │
│ t=4ms: Async WAL flush to SD card (background, every 500ms)                   │
│ t=5ms: Northbound dispatcher reads from RAM buffer                            │
│ t=6ms: Sparkplug B encoder serializes to Protobuf                             │
│ t=7ms: MQTT PUBLISH sent (QoS 1)                                               │
│ t=<500ms: MQTT PUBACK received; checkpoint advanced                           │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Offline / Reconnect Flow

```
Network loss detected (MQTT keepalive timeout)
    │
    ▼
MQTT connector enters BACKOFF state
    │
    ▼
Pipeline continues writing to RAM buffer
    │
RAM buffer full → spill to SD card WAL
    │
    ▼ (network restored)
MQTT reconnect → send NBIRTH → confirm subscription
    │
    ▼
Replay task starts reading cold tier (oldest-first or alarm-first per config)
    │
Replay batches published at configured rate (10× live rate default)
    │
Checkpoint advanced after each PUBACK batch
    │
Replay complete → normal live operation
```

### 5.3 Write-back (Northbound → Southbound)

```
MQTT broker delivers NCMD message to xEdge
    │
    ▼
MQTT connector decodes Sparkplug B NCMD payload
    │
    ▼
WriteRouter resolves tag_id → driver_instance + address
    │
RBAC check: is the MQTT credential authorized for `tag:write`?
    │    No → reject, audit log
    │    Yes →
    ▼
Driver.write(address, value) called
    │
    ▼
Write result encoded as NDATA metric update (with quality)
    │
Published back to broker (confirms write succeeded/failed)
    │
Audit log entry created
```

---

## 6. Deployment Architecture

### 6.1 Single Device (Standard)

```
┌───────────────────────────────────────┐
│ Industrial Edge Device (Linux)        │
│                                       │
│  systemd                              │
│  └── xedge.service                    │
│      └── xedge-core (main process)    │
│                                       │
│  /data/          (SD card / eMMC)     │
│  /etc/xedge/     (config, certs)      │
│  /var/log/xedge/ (logs, symlink)      │
│                                       │
│  Ports:                               │
│    4840/TCP  — OPC UA                 │
│    8443/TCP  — REST + diag CLI (mTLS) │
│    9090/TCP  — Prometheus metrics     │
└───────────────────────────────────────┘
```

### 6.2 Docker Deployment

```yaml
# docker-compose.yml
services:
  xedge:
    image: xedge/core:1.0.0
    restart: unless-stopped
    volumes:
      - /etc/xedge:/etc/xedge:ro
      - /data/xedge:/data
    devices:
      - /dev/ttyS0:/dev/ttyS0     # Modbus serial
    network_mode: host             # required for Ethernet/IP, PROFINET, IEC 61850 GOOSE
    cap_add:
      - NET_RAW                   # required for GOOSE/SV raw Ethernet frames
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp
```

### 6.3 Fleet Deployment

```
┌─────────────────────────────────────────────────────────┐
│                     Fleet Manager                        │
│                  (self-hosted, cloud VM)                 │
│                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌────────────────┐   │
│  │ Device     │  │ OTA Update   │  │  Config Push   │   │
│  │ Registry   │  │ Server       │  │  API           │   │
│  └────────────┘  └──────────────┘  └────────────────┘   │
└───────────────────┬─────────────────────────────────────┘
                    │ MQTT (management namespace) / gRPC
        ┌───────────┴──────────────┐
        │                          │
┌───────▼──────────┐   ┌───────────▼────────────┐
│  xEdge Device 1  │   │  xEdge Device 2..N      │
│  (Site A)        │   │  (Site B, C, ...)        │
└──────────────────┘   └────────────────────────-┘
```

### 6.4 File System Layout

```
/
├── etc/xedge/
│   ├── xedge.yaml               # Main configuration
│   ├── drivers/                 # Per-driver config files
│   ├── pki/
│   │   ├── device.crt           # Device identity certificate
│   │   ├── device.key           # Private key (or TPM handle reference)
│   │   └── trust/               # CA certificates
│   └── rbac.yaml                # RBAC policy (signed)
│
├── data/
│   ├── store/                   # Store-and-forward database
│   ├── config-history/          # Last 10 config versions
│   └── diagnostics/             # Packet captures, self-test results
│
└── var/log/xedge/
    ├── xedge.log                # Structured JSON log (current)
    └── audit.log                # Audit trail (append-only, hash-chained)
```

---

## 7. Technology Stack

### 7.1 Core Runtime

| Component | Technology | Rationale |
|---|---|---|
| Primary language | Python 3.11+ | Rapid driver development, extensive OT library ecosystem |
| Async framework | asyncio (stdlib) | Single-threaded concurrency; drivers use thread executor for blocking I/O |
| C extensions | ctypes / cffi / Cython | Wrap libiec61850, OpenDNP3, lib60870; performance-critical path |
| Configuration | YAML + jsonschema | Human-readable, schema-validated |
| Secrets management | python-keyring / HashiCorp Vault SDK | Pluggable secrets backend |

### 7.2 Protocol Libraries

| Protocol | Implementation (per ADR-006) | License |
|---|---|---|
| Modbus | **In-house stack** (pymodbus as test oracle only) | xEdge dual license |
| OPC UA | open62541 via in-house asyncio binding (asyncua as test oracle) | MPL 2.0 |
| IEC 60870-5-104 | **In-house stack**, clean-room from IEC spec (lib60870 as black-box oracle) | xEdge dual license |
| DNP3 | **In-house master (lean build)**; go/no-go gate after IEC 104 — fallback: commercial Rust `dnp3` crate | xEdge dual license / TBD |
| Sparkplug B | **In-house encoder** from Eclipse spec + official `.proto` (tahu not used at runtime) | xEdge dual license |
| BACnet | bacpypes3 | MIT |
| EtherNet/IP | pycomm3 | MIT |
| IEC 61850 | libiec61850 Python bindings (commercial license for commercial ed.) | GPL (GPL ed.) / Commercial |
| DLMS/COSEM | Build-vs-buy decided at Phase 4 close (ADR-006); gurux is dual GPL v2 / Commercial | TBD |
| PROFINET | custom C extension | Proprietary / GPLv2 (kernel) |

### 7.3 Data & Storage

| Component | Technology | Rationale |
|---|---|---|
| Cold store | SQLite 3.x (WAL mode) | Zero-dependency, proven reliability, WAL for concurrent R/W |
| RAM buffer | asyncio.Queue (bounded) | Native async, backpressure built-in |
| Time-series access | Custom cursor + retention manager | Tailored to replay + eviction patterns |

### 7.4 Northbound

| Component | Technology | Rationale |
|---|---|---|
| MQTT client | paho-mqtt 2.x | Mature, MQTT 3.1.1 + 5.0, TLS 1.3 |
| Sparkplug B | In-house encoder + state machine (Eclipse spec v3.0 + official `.proto`) | tahu not production-grade; spec is free; owns seq/alias/birth-death state (ADR-006) |
| OPC UA server | open62541 via shared in-house binding | Same library as client; shares trust store (ADR-006) |
| Protobuf | protobuf 4.x | Sparkplug B payload encoding |

### 7.5 Security & PKI

| Component | Technology |
|---|---|
| TLS | Python ssl (OpenSSL backend) |
| Certificate management | cryptography (pyca) |
| HSM / TPM | tpm2-pytss (TPM 2.0 binding) / PKCS#11 via PyKCS11 |
| Password hashing | bcrypt / passlib |
| Audit log integrity | hashlib (SHA-256 chain) |

### 7.6 Observability

| Component | Technology |
|---|---|
| Structured logging | structlog 24.x |
| Metrics + Tracing | opentelemetry-python SDK 1.x |
| OTLP export | opentelemetry-exporter-otlp-proto-grpc |
| Prometheus endpoint | opentelemetry-exporter-prometheus |
| Log forwarding | Fluentd / syslog-ng (OS-level) |

### 7.7 Fleet & OTA

| Component | Technology |
|---|---|
| OTA update engine | RAUC (A/B partitions) |
| OTA bundle signing | OpenSSL / SWU signing |
| Fleet protocol | MQTT management plane + custom gRPC |
| Config delivery | Signed JSON diff over MQTT / gRPC |

### 7.8 Build & CI/CD

| Component | Technology |
|---|---|
| Build | pyproject.toml (PEP 621) + hatchling |
| Cross-compilation | Docker buildx (linux/amd64, linux/arm64, linux/arm/v7) |
| CI | GitHub Actions / self-hosted runners |
| Container registry | GHCR (GitHub Container Registry) |
| Dependency audit | pip-audit + Dependabot |
| SBOM | syft (CycloneDX + SPDX) |
| Static analysis | ruff, mypy (strict), bandit, cppcheck |
| Testing | pytest + pytest-asyncio; HIL tests with real hardware in CI |

---

## 8. Key Design Decisions

### ADR-001: Python + C extensions (not Rust or Go)

**Decision:** Use Python 3.11+ as the primary language with C extensions for performance-critical paths.

**Rationale:**
- The OT protocol library ecosystem (libiec61850, OpenDNP3, gurux-dlms) provides well-tested C/C++ implementations; Python wrappers are the fastest path to protocol compliance
- Python's asyncio provides sufficient throughput for the 50k tags/s target
- Driver development by field engineers is faster in Python than Rust/Go
- C extensions (via cffi/ctypes) eliminate GIL impact for compute-intensive paths

**Trade-offs accepted:**
- Higher memory footprint than Rust/Go (mitigated by 256 MB RSS limit)
- GIL impacts multi-core utilization for pure Python code (mitigated by thread executors and C extensions)
- Startup time higher than compiled binary (acceptable given 30s target)

### ADR-002: Sparkplug B as primary northbound payload

**Decision:** Sparkplug B v3.0 as the primary MQTT payload format; raw JSON as optional.

**Rationale:**
- Sparkplug B provides structured data lifecycle (birth/death/data) preventing stale data problems
- Native support in Ignition, HiveMQ, AWS IoT SiteWise, and Azure IoT Hub (via connector)
- Protobuf encoding is more efficient than JSON for high-throughput data streams
- Sparkplug B's primary application server (SCADA) model enables future command and control integration

### ADR-003: SQLite WAL for store-and-forward persistence

**Decision:** SQLite in WAL mode on SD card / eMMC rather than a time-series database or flat files.

**Rationale:**
- Zero-dependency: no separate database server process
- WAL mode provides power-loss safety without fsync per-write overhead
- Supports per-tag retention queries efficiently with simple SQL
- Proven at industrial scale on constrained hardware
- Portable across all target Linux distributions without extra packages

**Trade-offs accepted:**
- Limited to single-writer; multiple concurrent writes use WAL queue (acceptable for edge workloads)
- No built-in compression (mitigated by per-tag deadband filtering reducing write volume)

### ADR-004: Async-first with thread executors for blocking drivers

**Decision:** Single asyncio event loop with thread executor for blocking protocol libraries.

**Rationale:**
- Most OT protocol libraries (Modbus, libiec61850, OpenDNP3) provide blocking C APIs
- Thread executors prevent blocking the event loop while keeping concurrency model simple
- Alternative (separate processes per driver) adds IPC overhead and complexity

**Trade-offs accepted:**
- Thread pool management adds some complexity
- Debugging multi-threaded async code is harder than pure async (mitigated by comprehensive tracing)

### ADR-005: Self-hosted fleet manager (not AWS/Azure native)

**Decision:** Build a self-hosted fleet management service rather than integrating AWS Greengrass or Azure IoT Edge.

**Rationale:**
- Multi-cloud requirement makes cloud-native fleet managers a lock-in risk
- NERC CIP and IEC 62443 compliance requires controlled update procedures not always possible in managed cloud services
- Self-hosted allows full audit trail ownership
- RAUC provides proven A/B OTA on Linux without cloud dependency

**Trade-offs accepted:**
- More initial engineering effort to build fleet manager
- Operational responsibility for fleet manager uptime

### ADR-006: Per-protocol build-vs-buy for protocol stacks

**Decision:** Implement Modbus, Sparkplug B, and IEC 60870-5-104 stacks in-house (clean-room from official specifications); lean-build the DNP3 master with a go/no-go gate after IEC 104; use open62541 (MPL 2.0) for OPC UA client and server; license libiec61850 commercially for IEC 61850; keep MIT-licensed libraries (bacpypes3, pycomm3) for BACnet and EtherNet/IP.

**Rationale:**
- Dual GPL/commercial licensing: GPL-encumbered libraries (lib60870, gurux) would require commercial license fees either way; in-house stacks eliminate them for high-volume protocols
- Dead upstream risk eliminated: OpenDNP3/pydnp3 are archived/unmaintained
- Native asyncio drivers on the hot path (Modbus, IEC 104) avoid thread-executor hops, supporting the 50k tags/s NFR

**Trade-offs accepted:**
- Spec purchase budget (~USD 1,500–3,000: IEC 60870-5-104, IEEE 1815, IEC 62056)
- Conformance burden for in-house stacks borne internally (black-box testing against reference implementations)
- Clean-room discipline required: GPL sources used only as black-box test oracles, never read by implementing engineers

**Full analysis:** [ADR-006 — Protocol Stack Build vs. Buy](adr-006-protocol-stack-build-vs-buy.md) (decision matrix, clean-room rules, effort estimates).
