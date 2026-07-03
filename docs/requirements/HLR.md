# xEdge — High-Level Requirements (HLR)

**Document ID:** XEDGE-HLR-001  
**Version:** 1.0  
**Status:** Draft  
**Date:** 2026-07-03  

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Stakeholders](#2-stakeholders)
3. [System Context](#3-system-context)
4. [Functional Requirements](#4-functional-requirements)
   - 4.1 Protocol Acquisition (Southbound)
   - 4.2 Data Pipeline & Normalization
   - 4.3 Store & Forward
   - 4.4 Northbound Publishing
   - 4.5 OPC UA Server
   - 4.6 Configuration Management
   - 4.7 Fleet Management & OTA
   - 4.8 Remote Diagnostics
5. [Non-Functional Requirements](#5-non-functional-requirements)
6. [Security Requirements](#6-security-requirements)
7. [Compliance Requirements](#7-compliance-requirements)
8. [Interface Requirements](#8-interface-requirements)
9. [Constraints & Assumptions](#9-constraints--assumptions)
10. [Glossary](#10-glossary)

---

## 1. Introduction

### 1.1 Purpose

This document captures the High-Level Requirements for **xEdge**, an IIoT edge software stack designed to run on Linux-based industrial edge devices. It serves as the authoritative requirements baseline for all subsequent design, architecture, and sprint planning activities.

### 1.2 Scope

xEdge runs on the edge device, between field-level OT equipment (PLCs, RTUs, meters, IEDs) and cloud/enterprise IT systems. It is responsible for:

- Acquiring data from field devices using standard industrial protocols
- Normalizing, quality-stamping, and filtering that data
- Persisting data reliably on-device during network outages
- Publishing data northbound to cloud platforms or local consumers
- Operating securely and in compliance with IEC 62443, NERC CIP, and SOC 2 / ISO 27001

xEdge does **not** include:
- Cloud-side data processing or storage
- HMI/SCADA visualization
- Business-layer analytics or AI/ML inference

### 1.3 Definitions

See [Section 10 — Glossary](#10-glossary).

---

## 2. Stakeholders

| ID | Stakeholder | Interest |
|---|---|---|
| STK-01 | **OT Engineer** | Commission and configure field devices and protocol drivers |
| STK-02 | **IT/Cloud Engineer** | Receive reliable, well-structured data at the cloud end |
| STK-03 | **Security / Compliance Officer** | Audit log access, IEC 62443/NERC CIP alignment, vulnerability management |
| STK-04 | **Fleet Operations** | Remote visibility, OTA updates, health monitoring across hundreds of devices |
| STK-05 | **System Integrator** | Extend xEdge with custom drivers, connectors, or data transformations |
| STK-06 | **End Customer / Asset Owner** | Business continuity, data integrity, no proprietary lock-in |
| STK-07 | **Hardware Vendor** | xEdge must certify on their hardware platform |
| STK-08 | **Open Source Community** | GPL community edition usability, public API stability |

---

## 3. System Context

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            EDGE DEVICE (Linux)                              │
│                                                                             │
│  ┌───────────────┐    ┌──────────────────────────────────────────────────┐ │
│  │  Field Devices│    │                   xEdge                          │ │
│  │               │    │                                                  │ │
│  │  PLCs/RTUs    │◄──►│  Protocol Drivers ──► Pipeline ──► Store/Fwd    │ │
│  │  IEDs/Meters  │    │                              │                   │ │
│  │  Drives/HMIs  │    │  Config Engine               ▼                  │ │
│  └───────────────┘    │  Fleet Agent        Northbound Publisher         │ │
│                        │  Observability      OPC UA Server               │ │
│                        └─────────────────────────┬────────────────────┬─┘ │
│                                                   │                    │   │
└───────────────────────────────────────────────────┼────────────────────┼───┘
                                                    │MQTT/Sparkplug B    │OPC UA
                                            ┌───────▼──────┐    ┌───────▼────┐
                                            │  Cloud / MQTT│    │ Local OPC  │
                                            │  Broker      │    │ UA Clients │
                                            └──────────────┘    └────────────┘
```

### 3.1 Deployment Scenarios

| Scenario | Description |
|---|---|
| **Industrial LAN** | xEdge on industrial PC with direct Ethernet to PLCs on same subnet |
| **Serial Edge** | xEdge on ARM SBC with RS-485/RS-232 to serial Modbus devices |
| **Cellular Gateway** | xEdge behind LTE/5G with intermittent connectivity; heavy store-and-forward use |
| **Air-gapped / OPC UA only** | No cloud connectivity; only local OPC UA server for local SCADA |
| **Multi-site fleet** | 100s of xEdge instances managed from central fleet manager |

---

## 4. Functional Requirements

### 4.1 Protocol Acquisition (Southbound)

#### 4.1.1 Tier-1 Protocols (MVP)

| ID | Requirement |
|---|---|
| FR-SA-001 | The system SHALL support Modbus RTU over RS-232 and RS-485 serial interfaces, including configurable baud rate (1200–115200), parity (none/even/odd), data bits (7/8), stop bits (1/2), and bus address (1–247). |
| FR-SA-002 | The system SHALL support Modbus TCP (client mode) over IPv4 and IPv6, connecting to up to 256 simultaneous Modbus TCP servers per driver instance. |
| FR-SA-003 | The system SHALL support Modbus RTU encapsulated over TCP (RTU-over-TCP), distinct from Modbus TCP in framing. |
| FR-SA-004 | Modbus drivers SHALL support reading all function codes: FC01 (coils), FC02 (discrete inputs), FC03 (holding registers), FC04 (input registers), and writing FC05/FC06/FC15/FC16. |
| FR-SA-005 | The system SHALL support OPC UA client connections (OPC UA 1.04) using both discovery (LDS) and direct endpoint URL, supporting all security modes: None, Sign, SignAndEncrypt. |
| FR-SA-006 | OPC UA client SHALL support subscriptions (MonitoredItems), polling, and one-shot read, for both Nodes and NodeSets defined by information model. |
| FR-SA-007 | OPC UA client SHALL handle session reconnection transparently with configurable retry backoff and re-subscription after reconnect. |
| FR-SA-008 | All southbound drivers SHALL report connection state (connected/disconnected/error) with timestamp and reason code to the pipeline. |
| FR-SA-009 | Drivers SHALL support configurable scan rates per register/node, from 50 ms minimum to 24 hours maximum. |
| FR-SA-010 | Drivers SHALL support change-of-value (CoV) detection with configurable deadband (absolute and percentage) to suppress redundant publishes. |

#### 4.1.2 Tier-2 Protocols (Post-MVP)

| ID | Requirement |
|---|---|
| FR-SB-001 | The system SHALL support IEC 60870-5-104 (IEC 104) as a controlling station (master/client), including spontaneous data (ASDU types 1–45), general interrogation (type 100), counter interrogation, and command issuance (types 45–51, 58–64). |
| FR-SB-002 | The system SHALL support DNP3 (IEEE 1815) as a master station over TCP and serial, including unsolicited responses, data link layer confirmation, and application-layer retries. |
| FR-SB-003 | The system SHALL support BACnet/IP (ASHRAE 135) as a client, including object discovery, COV subscriptions, and property polling for Analog Input/Output/Value, Binary Input/Output/Value, Multi-state objects. |
| FR-SB-004 | The system SHALL support BACnet MS/TP over RS-485, acting as a master node with configurable MAC address (0–127). |
| FR-SB-005 | The system SHALL support EtherNet/IP (CIP) as an originator (client), reading and writing tags from Allen-Bradley / Rockwell PLCs (ControlLogix, CompactLogix) using symbolic tag names. |
| FR-SB-006 | The system SHALL support PROFINET IO as a controller (IO-Controller), reading process data from PROFINET IO devices using their GSD/GSDML device descriptions. |
| FR-SB-007 | The system SHALL support IEC 61850 MMS client for reading XCBR, XSWI, MMXU, MMTR, and other LNs from IEDs, subscribing to reports (RCB buffered and unbuffered), and issuing control operations (SBO, direct). |
| FR-SB-008 | The system SHALL support IEC 61850 GOOSE subscriber, receiving GOOSE messages on Ethernet multicast, with stale-data detection per GOOSE dataset. |
| FR-SB-009 | The system SHALL support IEC 61850 Sampled Values (SV/SMV) subscriber for power quality and protection applications. |
| FR-SB-010 | The system SHALL support DLMS/COSEM (IEC 62056) as a client over HDLC (serial), TCP, and UDP wrappers, supporting logical device addressing, OBIS code data access, and push notifications. |

#### 4.1.3 Driver Framework

| ID | Requirement |
|---|---|
| FR-DF-001 | The system SHALL provide a plugin-based driver framework allowing third-party drivers to be added without modifying core engine code. |
| FR-DF-002 | Each driver SHALL implement a standard lifecycle: load → configure → connect → run → disconnect → unload. |
| FR-DF-003 | Each driver SHALL be independently configurable, startable, stoppable, and restartable at runtime without affecting other drivers. |
| FR-DF-004 | Driver configuration SHALL be validated against a JSON Schema before the driver starts, with human-readable error messages on validation failure. |
| FR-DF-005 | The system SHALL support multiple simultaneous instances of the same driver type (e.g., two separate Modbus TCP connections to different devices). |
| FR-DF-006 | Drivers SHALL emit metrics: tag read rate, error rate, reconnect count, last successful read timestamp — accessible via the observability subsystem. |
| FR-DF-007 | The driver framework SHALL include a hardware-in-the-loop (HIL) simulator mode allowing a driver to read from a configurable replay file rather than a live device, for testing without physical hardware. |

---

### 4.2 Data Pipeline & Normalization

| ID | Requirement |
|---|---|
| FR-DP-001 | The system SHALL normalize all ingested data points into a **Unified Tag** model regardless of source protocol. A Unified Tag SHALL include: tag ID, timestamp (UTC, nanosecond precision), value (typed), quality code (OPC UA quality model), source driver ID, and protocol-specific metadata. |
| FR-DP-002 | Tag timestamps SHALL use the source device timestamp when available and trusted; otherwise the ingestion timestamp SHALL be used with a quality flag indicating estimated timestamp. |
| FR-DP-003 | The system SHALL support configurable engineering unit conversion per tag (linear scaling: `engineering_value = raw * scale + offset`). |
| FR-DP-004 | The system SHALL support configurable expression-based virtual tags computed from one or more raw tags using a safe expression evaluator. |
| FR-DP-005 | The system SHALL map protocol-specific quality codes (Modbus exception codes, OPC UA StatusCodes, IEC 104 quality bits, DNP3 flags) to the unified OPC UA quality model (Good, Uncertain, Bad + substatus). |
| FR-DP-006 | The pipeline SHALL support configurable tag groups with independent pipeline parameters (scan rate, deadband, retention policy, priority). |
| FR-DP-007 | The system SHALL support alarm / event detection on tag values: threshold crossing (hi/lo/hi-hi/lo-lo), rate-of-change, state change, and quality degradation. Alarm events SHALL be treated as higher-priority data with independent retention. |
| FR-DP-008 | The pipeline SHALL be capable of processing a sustained throughput of ≥ 50,000 tag updates per second on a 4-core ARM Cortex-A72 at ≤ 50% CPU utilization. |

---

### 4.3 Store & Forward

| ID | Requirement |
|---|---|
| FR-SF-001 | The system SHALL maintain an in-memory ring buffer per tag group, with configurable maximum depth (default: 10,000 samples). When full, it SHALL evict oldest samples unless the tag group is marked as alarm (no eviction; apply backpressure). |
| FR-SF-002 | The system SHALL persist the store-and-forward buffer to non-volatile storage (SD card / eMMC) using a write-ahead log (WAL) strategy to survive unexpected power loss without data corruption. |
| FR-SF-003 | Each tag SHALL carry a configurable `retention_duration` (minimum 1 second, maximum 30 days) and `retention_max_samples`. Data older than the retention window SHALL be automatically purged. |
| FR-SF-004 | On northbound reconnect, the system SHALL replay buffered data in time order, interleaving historical data with live data, with configurable max replay rate to prevent cloud ingress overload. |
| FR-SF-005 | The system SHALL apply a configurable per-tag replay priority: `alarms_first`, `time_order`, or `newest_first`. |
| FR-SF-006 | The system SHALL monitor available storage space and emit a warning alert at 80% capacity and a critical alert at 95% capacity. At 100% capacity, it SHALL apply the oldest-data eviction policy and SHALL NOT fail silently. |
| FR-SF-007 | The store-and-forward engine SHALL operate correctly across unexpected power loss (validated by intentional power cut tests), with zero data corruption and at most N=configurable samples lost (default: 0 if WAL flush before ACK is enabled). |
| FR-SF-008 | The system SHALL expose store-and-forward queue depth, pending bytes, oldest pending timestamp, and replay lag as observable metrics. |

---

### 4.4 Northbound Publishing

| ID | Requirement |
|---|---|
| FR-NB-001 | The system SHALL publish data northbound using MQTT 3.1.1 and MQTT 5.0 over TLS 1.2 (minimum) and TLS 1.3. |
| FR-NB-002 | The primary MQTT payload format SHALL be **Eclipse Sparkplug B** (Specification Version 3.0), including NBIRTH, NDEATH, DBIRTH, DDEATH, NDATA, DDATA, and NCMD/DCMD message types. |
| FR-NB-003 | The system SHALL generate and manage Sparkplug B birth certificates, including full metric definitions with data type, engineering units, and metadata on NBIRTH/DBIRTH. |
| FR-NB-004 | The system SHALL support configurable Sparkplug B group ID, edge node ID, and device IDs, with macro substitution of hardware identifiers (hostname, MAC, serial number). |
| FR-NB-005 | The system SHALL support at least 3 simultaneous northbound connectors targeting different MQTT brokers or cloud endpoints with independent credential sets. |
| FR-NB-006 | The system SHALL support MQTT Last Will and Testament (LWT) using the Sparkplug B NDEATH payload to signal disconnection. |
| FR-NB-007 | The system SHALL support pluggable cloud connectors: a connector plugin exposes `connect`, `publish`, `disconnect`, and `get_metrics` interface. Built-in connectors: Generic MQTT, AWS IoT Core (X.509 auth), Azure IoT Hub (SAS and X.509). |
| FR-NB-008 | MQTT connections SHALL use client certificate authentication (mutual TLS) where the broker supports it; otherwise password + TLS. |
| FR-NB-009 | The system SHALL support northbound write-back: receiving NCMD/DCMD from the broker and routing the command to the appropriate southbound driver for write execution, with result reporting. |
| FR-NB-010 | The system SHALL implement MQTT connection state machine with exponential backoff retry (initial 1s, max 5 min, jitter ±20%) and reconnect storm prevention. |

---

### 4.5 OPC UA Server (Northbound)

| ID | Requirement |
|---|---|
| FR-UA-001 | The system SHALL expose an OPC UA server (OPC UA 1.04) allowing external clients to browse and subscribe to all active tags. |
| FR-UA-002 | The OPC UA information model SHALL be auto-generated from the tag configuration, organizing nodes in a hierarchy: `xEdge → Driver → Device → TagGroup → Tag`. |
| FR-UA-003 | The OPC UA server SHALL support subscriptions with configurable sampling intervals (minimum 100 ms) and publish intervals, with configurable maximum queue size per MonitoredItem. |
| FR-UA-004 | The OPC UA server SHALL support security policies: None (local loopback only), Basic256Sha256, and Aes128Sha256RsaOaep (recommended minimum). |
| FR-UA-005 | The OPC UA server SHALL implement user authentication: anonymous (configurable on/off), username/password (bcrypt hashed), and X.509 certificates. |
| FR-UA-006 | The OPC UA server SHALL support OPC UA write operations, routing writes to the appropriate southbound driver (where driver supports it). |
| FR-UA-007 | The OPC UA server SHALL expose diagnostic nodes: driver connection status, queue depths, data rates, system health. |
| FR-UA-008 | The OPC UA server SHALL support a configurable endpoint URL with DNS name, supporting both `opc.tcp://` and `opc.https://` transport. |

---

### 4.6 Configuration Management

| ID | Requirement |
|---|---|
| FR-CM-001 | All system configuration SHALL be expressed in human-readable YAML files with a versioned JSON Schema for validation. |
| FR-CM-002 | The system SHALL support a layered configuration model: base configuration + environment overlays + runtime overrides, with explicit precedence rules. |
| FR-CM-003 | The system SHALL expose a REST API (HTTP/2 with mTLS) for reading and modifying configuration at runtime without restart, for supported parameters. |
| FR-CM-004 | All configuration changes (REST API or file reload) SHALL be recorded in the audit log with timestamp, actor identity, old value, and new value. |
| FR-CM-005 | The system SHALL perform configuration schema validation on startup and on reload, refusing to apply invalid configuration with a human-readable error. |
| FR-CM-006 | The system SHALL support configuration rollback: the last 10 configuration versions SHALL be retained, and any prior version can be restored via API. |
| FR-CM-007 | Driver configurations SHALL support variable substitution for secrets (e.g., `${SECRET:my_password}` resolved from a secrets backend: environment variable, file, or HashiCorp Vault). |
| FR-CM-008 | The system SHALL support a dry-run config validation mode (`xedge config validate --dry-run`) that checks configuration without applying it. |
| FR-CM-009 | The system SHALL support importing/exporting device tag lists from/to CSV and JSON formats to simplify bulk commissioning. |

---

### 4.7 Fleet Management & OTA

| ID | Requirement |
|---|---|
| FR-FM-001 | The system SHALL include a fleet management agent that reports device identity (hostname, hardware ID, OS version, xEdge version, driver inventory) to the fleet manager on startup and on change. |
| FR-FM-002 | The fleet agent SHALL send a heartbeat to the fleet manager at a configurable interval (default: 60 s) including health status, active alarms, and connectivity state. |
| FR-FM-003 | The fleet manager SHALL be able to push configuration updates to devices, with the device validating and applying (or rejecting with error) the new configuration. |
| FR-FM-004 | The system SHALL support OTA software updates using RAUC (Robust Auto-Update Controller) with A/B partition scheme, ensuring a failed update automatically rolls back to the prior version. |
| FR-FM-005 | OTA update bundles SHALL be cryptographically signed; the system SHALL reject unsigned or signature-invalid bundles. |
| FR-FM-006 | The fleet agent SHALL support update scheduling: immediate, maintenance window (configurable time-of-day), and staged rollout (percentage of fleet per day). |
| FR-FM-007 | The fleet agent SHALL support remote command execution for a pre-approved command whitelist (e.g., `restart-driver`, `collect-diagnostics`, `run-self-test`). Arbitrary shell execution SHALL NOT be supported. |
| FR-FM-008 | Fleet communication SHALL occur over a dedicated management MQTT topic namespace or gRPC channel, independent of data publishing channels. |

---

### 4.8 Remote Diagnostics

| ID | Requirement |
|---|---|
| FR-RD-001 | The system SHALL provide a secure remote diagnostic CLI accessible over an authenticated WebSocket or SSH-tunneled gRPC channel. |
| FR-RD-002 | The diagnostic CLI SHALL support commands: `status`, `driver list`, `driver restart <id>`, `driver logs <id>`, `tag read <tag_id>`, `store-forward status`, `network check`, `config show`, `config validate`. |
| FR-RD-003 | The system SHALL support on-demand packet capture on southbound interfaces (configurable duration, max file size), encrypted and uploaded to fleet manager for analysis. |
| FR-RD-004 | The system SHALL support a self-test command that exercises driver loopback (where supported), store-and-forward write/read, and northbound connectivity and reports pass/fail per subsystem. |
| FR-RD-005 | All remote diagnostic sessions SHALL be authenticated, authorized (minimum RBAC role: `operator`), and fully logged in the audit trail with session ID, user, and all commands issued. |

---

## 5. Non-Functional Requirements

### 5.1 Performance

| ID | Requirement |
|---|---|
| NFR-P-001 | The pipeline SHALL sustain ≥ 50,000 tag updates/second on a 4-core ARM Cortex-A72 @ 1.8 GHz, 4 GB RAM, at ≤ 50% average CPU utilization. |
| NFR-P-002 | End-to-end latency from southbound data acquisition to northbound MQTT publish SHALL be ≤ 500 ms at p99 under nominal load (≤ 10,000 tags/s). |
| NFR-P-003 | System startup to first data publish SHALL complete within 30 seconds on a Raspberry Pi 4 or equivalent. |
| NFR-P-004 | Store-and-forward write throughput SHALL be ≥ 20,000 samples/second to SD card / eMMC without data loss under power-loss scenarios. |
| NFR-P-005 | The OPC UA server SHALL support ≥ 50 simultaneous client sessions, each with ≥ 1,000 MonitoredItems. |
| NFR-P-006 | Memory footprint of the full xEdge process SHALL not exceed 256 MB RSS with 10,000 active tags and 5 active drivers on a 1 GB RAM device. |

### 5.2 Reliability & Availability

| ID | Requirement |
|---|---|
| NFR-R-001 | xEdge SHALL achieve 99.9% uptime over a 30-day period, excluding planned maintenance windows. |
| NFR-R-002 | The system SHALL recover automatically from all driver-level failures (connection loss, protocol error, timeout) without requiring a process restart. |
| NFR-R-003 | The system SHALL recover automatically from northbound connectivity loss, resuming publishing and replaying buffered data on reconnect. |
| NFR-R-004 | The system SHALL survive unexpected power loss without filesystem corruption or data pipeline state inconsistency. |
| NFR-R-005 | Watchdog integration SHALL be required: the system SHALL kick a hardware or systemd watchdog at least once per 30 seconds; failure to do so SHALL trigger a controlled process restart. |
| NFR-R-006 | A crashed or unresponsive driver SHALL be automatically restarted by the supervision framework within 5 seconds, with exponential backoff on repeated failures. |

### 5.3 Scalability

| ID | Requirement |
|---|---|
| NFR-S-001 | The system SHALL scale from 10 to 100,000 active tags on appropriate hardware without configuration model changes. |
| NFR-S-002 | The driver framework SHALL support up to 64 simultaneous driver instances. |
| NFR-S-003 | The fleet manager SHALL support ≥ 10,000 registered devices with ≤ 5 second command delivery latency to any individual device. |

### 5.4 Portability & Compatibility

| ID | Requirement |
|---|---|
| NFR-C-001 | xEdge SHALL run on Linux kernel ≥ 5.10 LTS on AMD64, ARM64, and ARMv7 architectures. |
| NFR-C-002 | The system SHALL support the following Linux distributions without modification: Ubuntu 22.04/24.04 LTS, Debian 11/12, Alpine Linux 3.18+, Yocto-based distributions (kirkstone and later). |
| NFR-C-003 | Deployment SHALL be supported via Docker (≥ 24.0), Podman (≥ 4.0), and bare-metal systemd service (≥ 248). |
| NFR-C-004 | The system SHALL run in a read-only root filesystem with writable mounts only for `/data`, `/var/log`, and `/tmp`. |
| NFR-C-005 | All protocol drivers SHALL be configurable to use specific network interfaces and serial port devices, supporting hotplug detection. |

### 5.5 Maintainability

| ID | Requirement |
|---|---|
| NFR-M-001 | All public APIs (REST, gRPC, driver interface) SHALL be versioned using semantic versioning with a published deprecation policy (minimum 2 major versions before removal). |
| NFR-M-002 | Unit test coverage SHALL be ≥ 80% for core engine components; integration tests SHALL cover all Tier-1 protocols against real or simulated devices. |
| NFR-M-003 | The system SHALL ship with a migration tool that upgrades configuration files from one version to the next without data loss. |
| NFR-M-004 | Build artifacts SHALL be reproducible: given the same source commit and dependency lock file, the build SHALL produce a byte-for-byte identical binary. |

---

## 6. Security Requirements

### 6.1 Authentication & Authorization

| ID | Requirement |
|---|---|
| SR-AA-001 | All management interfaces (REST API, gRPC, diagnostic CLI) SHALL require authentication. Anonymous access SHALL be disabled by default. |
| SR-AA-002 | The system SHALL implement Role-Based Access Control (RBAC) with the following predefined roles: `admin`, `operator`, `auditor`, `readonly`. Custom roles SHALL be configurable. |
| SR-AA-003 | RBAC roles and permissions SHALL be stored in a signed, tamper-evident configuration file; any modification SHALL be audited. |
| SR-AA-004 | All API tokens SHALL have configurable expiry (default: 24 hours) and SHALL support revocation without system restart. |
| SR-AA-005 | The system SHALL support X.509 certificate-based authentication for all service-to-service communication, including fleet agent ↔ fleet manager, and device ↔ cloud broker. |
| SR-AA-006 | Passwords SHALL be stored using bcrypt (cost factor ≥ 12) or Argon2id; plaintext password storage SHALL be strictly prohibited. |

### 6.2 Transport Security

| ID | Requirement |
|---|---|
| SR-TS-001 | All northbound connections SHALL use TLS 1.2 (minimum) or TLS 1.3; TLS 1.0 and 1.1 SHALL be disabled. |
| SR-TS-002 | All management API connections SHALL use TLS 1.3 with mutual certificate authentication. |
| SR-TS-003 | The system SHALL support PKCS#11-compatible Hardware Security Modules (HSMs) for private key storage (e.g., TPM 2.0). |
| SR-TS-004 | The system SHALL implement certificate rotation without service interruption, supporting ACME (Let's Encrypt compatible) and manual certificate upload. |
| SR-TS-005 | Cipher suite selection SHALL default to TLS 1.3 ciphersuites only; for TLS 1.2 compatibility, only ECDHE + AES-GCM/ChaCha20 suites SHALL be permitted. |
| SR-TS-006 | The system SHALL validate server certificates against a configurable CA bundle; certificate pinning SHALL be optionally configurable per endpoint. |

### 6.3 Data Security

| ID | Requirement |
|---|---|
| SR-DS-001 | Sensitive configuration values (passwords, API keys, certificates) SHALL be stored encrypted at rest using AES-256-GCM with the encryption key stored in the platform's secure storage (TPM / HSM / encrypted keystore). |
| SR-DS-002 | The store-and-forward database on SD card SHALL support optional encryption at rest (AES-256-XTS or LUKS), enabled by configuration. |
| SR-DS-003 | Log files SHALL NOT contain sensitive values (passwords, keys, raw tag values for configured sensitive tags). |

### 6.4 Audit & Integrity

| ID | Requirement |
|---|---|
| SR-AI-001 | The system SHALL maintain a tamper-evident audit log of all: authentication attempts (success/failure), configuration changes, OTA updates, remote command executions, and privileged API calls. |
| SR-AI-002 | The audit log SHALL be structured (JSON), append-only, and SHALL be forwarded to the external SIEM via syslog-ng or Fluentd. |
| SR-AI-003 | The system SHALL verify the integrity of its own binaries on startup using a signed manifest (SHA-256 hashes); tampered binaries SHALL cause an alert and, optionally, a startup abort. |
| SR-AI-004 | The system SHALL support Secure Boot integration: xEdge container image or systemd service SHALL be verifiable using signed boot chain (UEFI Secure Boot or U-Boot verified boot). |

### 6.5 Vulnerability Management

| ID | Requirement |
|---|---|
| SR-VM-001 | The CI/CD pipeline SHALL run dependency vulnerability scanning (pip-audit, SBOM-based CVE scan) on every merge to the main branch. |
| SR-VM-002 | The system SHALL publish a Software Bill of Materials (SBOM) in CycloneDX or SPDX format with every release. |
| SR-VM-003 | Critical and High CVEs affecting xEdge SHALL be remediated within 30 and 90 days respectively of public disclosure, per a documented Vulnerability Response Policy. |

---

## 7. Compliance Requirements

### 7.1 IEC 62443 (Industrial Cybersecurity)

| ID | Requirement | IEC 62443 Reference |
|---|---|---|
| CR-62443-001 | xEdge SHALL achieve Security Level 2 (SL-2) capability for all components under IEC 62443-3-3. | SL-2 system requirements |
| CR-62443-002 | The system SHALL implement identification and authentication control (IAC) for all users and devices. | SR 1.1 |
| CR-62443-003 | The system SHALL enforce use control with least privilege (deny by default). | SR 2.1 |
| CR-62443-004 | The system SHALL provide system integrity checking via signed software and configuration. | SR 3.4 |
| CR-62443-005 | The system SHALL support data confidentiality in transit using approved cryptography. | SR 4.1 |
| CR-62443-006 | The system SHALL restrict the data flow to least-privilege network paths (firewall/zone-conduit documentation required). | SR 5.1 |
| CR-62443-007 | The system SHALL generate a security audit log meeting IEC 62443-3-3 SR 6.1 requirements. | SR 6.1 |
| CR-62443-008 | The system SHALL support secure remote sessions via authenticated, encrypted channels only. | SR 1.13 |
| CR-62443-009 | The system SHALL include a hardening guide (CIS-benchmark style) for the underlying Linux OS and xEdge configuration. | Component hardening |

### 7.2 NERC CIP (Electric Utility)

| ID | Requirement | NERC CIP Standard |
|---|---|---|
| CR-NERC-001 | The system SHALL maintain an asset inventory record exportable to a format compatible with CIP-002 Cyber Asset categorization. | CIP-002 |
| CR-NERC-002 | The system SHALL enforce minimum access control requirements for Electronic Security Perimeter access. | CIP-005 |
| CR-NERC-003 | The system SHALL log all interactive user access with minimum fields required by CIP-007 R4 and CIP-007 R5. | CIP-007 |
| CR-NERC-004 | The system SHALL alert on failed authentication attempts exceeding configurable threshold (default: 3 within 5 minutes). | CIP-007 R5.3 |
| CR-NERC-005 | The system SHALL support evidence collection for CIP compliance reporting (audit-ready log exports with time-correlation). | CIP-010 |
| CR-NERC-006 | The OTA update process SHALL be documented and auditable, meeting CIP-010 R1 change management requirements. | CIP-010 |

### 7.3 SOC 2 / ISO 27001

| ID | Requirement | Control |
|---|---|---|
| CR-SOC2-001 | All data at rest on the device SHALL be encrypted or the device SHALL physically protected (documented). | CC6.1 |
| CR-SOC2-002 | Logical access to xEdge SHALL be controlled and regularly reviewed. | CC6.2 |
| CR-SOC2-003 | System and application changes SHALL follow a documented change management process. | CC8.1 |
| CR-SOC2-004 | Availability monitoring SHALL alert within 5 minutes of system health degradation. | A1.2 |
| CR-SOC2-005 | Incidents SHALL be logged with severity, timeline, impact, and resolution, retained for ≥ 1 year. | CC7.3 |

---

## 8. Interface Requirements

### 8.1 Hardware Interfaces

| ID | Requirement |
|---|---|
| IR-HW-001 | Serial driver SHALL support RS-232 (DB9), RS-485 (2-wire and 4-wire), and RS-422 via standard Linux serial device paths (`/dev/ttyS*`, `/dev/ttyUSB*`, `/dev/ttyAMA*`). |
| IR-HW-002 | Network driver SHALL support Ethernet (100/1000 Mbps), Wi-Fi (hostapd-managed), and cellular (ModemManager managed) interfaces. |
| IR-HW-003 | The system SHALL detect and handle serial port hotplug (USB-to-serial adapters) via udev rules, restarting the affected driver automatically. |
| IR-HW-004 | The system SHALL support external RTC (real-time clock) for accurate timestamps when NTP is unavailable, reading from `/dev/rtc`. |
| IR-HW-005 | The system SHALL support TPM 2.0 for secure key storage via the Linux kernel TPM stack and tpm2-tools. |

### 8.2 Software / API Interfaces

| ID | Requirement |
|---|---|
| IR-SW-001 | REST management API: HTTP/2, JSON, OpenAPI 3.1 specification published with every release. |
| IR-SW-002 | Fleet management interface: gRPC (proto3), with published `.proto` files, versioned, backward-compatible. |
| IR-SW-003 | Driver plugin interface: Python ABC (Abstract Base Class) with version marker; C extension drivers wrapped via ctypes/cffi. |
| IR-SW-004 | Configuration schema: JSON Schema draft-07, published in the repository, used by IDE plugins and config validators. |
| IR-SW-005 | MQTT/Sparkplug B: conform to Eclipse Sparkplug B Specification v3.0, compatible with Ignition (Cirrus Link), HiveMQ Sparkplug, and AWS IoT SiteWise. |

### 8.3 External System Interfaces

| ID | Requirement |
|---|---|
| IR-ES-001 | SIEM integration: CEF (Common Event Format) over syslog-UDP/TCP, or JSON over syslog with RFC 5424 structured data. |
| IR-ES-002 | OpenTelemetry: OTLP/gRPC exporter, compatible with Grafana Alloy, Datadog Agent, and OpenTelemetry Collector. |
| IR-ES-003 | Fleet manager north-facing API: REST + WebSocket, with a published Swagger spec. |

---

## 9. Constraints & Assumptions

### Constraints

| ID | Constraint |
|---|---|
| CON-001 | Core runtime SHALL be Python 3.11+ with C extensions; no JVM, .NET CLR, or Node.js runtime dependencies in the core process. |
| CON-002 | Total installed footprint (OS + xEdge + dependencies) SHALL fit in 2 GB storage. |
| CON-003 | The dual-license (GPL v3 / Commercial) SHALL be respected; GPL-licensed dependencies SHALL not be included in the commercial edition without written permission. |
| CON-004 | Protocol library dependencies with GPL-only licenses (e.g., raw libiec61850 GPL build) SHALL use dynamic linking in the GPL edition; the commercial edition SHALL use LGPL or commercially licensed builds. |
| CON-006 | In-house protocol stacks (Modbus, Sparkplug B, IEC 60870-5-104, DNP3 per ADR-006) SHALL be developed clean-room from the official specifications only. GPL-licensed implementations SHALL be used solely as black-box test oracles; engineers implementing an in-house stack SHALL NOT read the corresponding GPL source. Each in-house driver SHALL maintain a provenance record documenting the specifications and references used. |
| CON-005 | All C extensions SHALL pass static analysis (cppcheck + clang-analyzer) and have no memory-safety violations in Valgrind memcheck at the unit test level. |

### Assumptions

| ID | Assumption |
|---|---|
| ASM-001 | Edge devices run NTP-synchronized clocks (Stratum ≤ 3); timestamp accuracy is ±1 second typical. |
| ASM-002 | SD card write endurance is managed by the hardware vendor; xEdge minimizes write amplification via WAL and configurable sync policies. |
| ASM-003 | Network connectivity to the cloud is intermittent; the system is designed-first for disconnected operation. |
| ASM-004 | Physical security of the edge device is the responsibility of the site operator (covered in hardening guide). |
| ASM-005 | IPv4 is the primary network stack; IPv6 is supported but not required at initial deployment. |

---

## 10. Glossary

| Term | Definition |
|---|---|
| **CoV** | Change of Value — a trigger to publish when a tag's value changes beyond a deadband |
| **DLMS/COSEM** | Device Language Message Specification / Companion Specification for Energy Metering (IEC 62056) |
| **EoN** | Edge of Network — Sparkplug B term for an edge node |
| **GOOSE** | Generic Object-Oriented Substation Event — IEC 61850 fast message for protection |
| **HIL** | Hardware-in-the-Loop — testing approach connecting software to real (or emulated) hardware |
| **HSM** | Hardware Security Module — dedicated hardware for cryptographic operations and key storage |
| **IED** | Intelligent Electronic Device — a device in a substation that acquires and processes data |
| **LWT** | Last Will and Testament — MQTT mechanism for publishing a message on unexpected disconnect |
| **MMS** | Manufacturing Message Specification — application layer protocol used in IEC 61850 |
| **NBIRTH/NDEATH** | Sparkplug B messages for edge node birth (online) and death (offline) certificates |
| **OT** | Operational Technology — hardware and software controlling industrial equipment |
| **PAL** | Protocol Abstraction Layer — the driver plugin framework in xEdge |
| **RAUC** | Robust Auto-Update Controller — Linux OTA framework with A/B partition support |
| **RTU** | Remote Terminal Unit — a field device that interfaces with physical equipment |
| **SBOM** | Software Bill of Materials — a formal record of all software components in a product |
| **SL** | Security Level (IEC 62443) — a measure of security capability (SL-1 through SL-4) |
| **Sparkplug B** | MQTT-based IIoT messaging specification from the Eclipse Foundation |
| **SV / SMV** | Sampled Values — IEC 61850 streaming message for power quality measurements |
| **WAL** | Write-Ahead Log — a technique ensuring data durability by writing to a log before applying changes |
| **xEdge** | The name of this software product |
