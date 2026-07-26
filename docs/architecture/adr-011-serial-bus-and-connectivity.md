# ADR-011: Shared Serial Bus Manager and Unified Connectivity State Machine

**Status:** ACCEPTED
**Date:** 2026-07-26
**Deciders:** Lead Architect, Project Owner
**Implements:** XEDGE-DR-001 decisions D-16, D-17
**Delivers:** XEDGE-CRD-001 §4.1 (Modbus RTU multi-slave, write priority,
RTS timing), §4.4 (Device Health & Availability), §4.9 (Gateway Connection
State), §4.11 (Asset Connection State)

## Context

Two problems, decided together because their solutions share a home.

### Problem 1 — one serial port, many slaves

A Modbus RTU driver instance is scoped to exactly one `unit_id`. Polling
several slaves on one RS-485 multi-drop bus therefore needs several
instances — but each instance opens the serial port independently
(`xedge/drivers/modbus/serial.py`), so two instances configured against the
same `/dev/ttyUSB0` both try to own the handle. On Linux this yields a lock
conflict or interleaved garbage on the wire, depending on the driver and
adapter. There is no arbiter.

This is the single largest functional gap in the protocol the customer uses
most (XEDGE-CRD-001 rates Modbus RTU ~45% complete), and it makes the
common industrial topology — one converter, many field devices —
unsupportable.

Three further CRD requirements land on the same code path:

- **Write priority.** Writes today execute ad hoc, competing with the poll
  loop for the same request lock, with no ordering guarantee.
- **RS-485 RTS pre/post-transmit delay** (µs), needed for converters that
  do not auto-toggle the transceiver direction. `pyserial-asyncio` does not
  expose this.
- **On-Demand and On-Connect** polling modes, alongside the continuous
  polling that exists today.

Each is a scheduling concern. Implementing them inside the per-instance
poll loop would mean implementing them three times, once per transport, and
would still not solve the shared-port problem.

### Problem 2 — three connectivity models

The CRD describes near-identical connectivity state in three places:

| Where | CRD wording | Today |
|---|---|---|
| Modbus device health | consecutive-failure threshold → offline / "Not Connected", auto-recovery | Nothing. The supervisor restarts a crashed instance (NFR-R-006) but there is no per-slave availability state distinct from a hard restart, and no configurable threshold |
| Gateway connection state | Connected / Disconnected / Active / Inactive | `DeviceRecord.status` derives `unknown`/`online`/`offline` from heartbeat age |
| Asset connection state | per-asset connectivity | No Asset concept exists (see ADR-010) |

Built three times, these will drift — different thresholds, different
hysteresis, different names for the same condition, and three separate bugs
when a flapping link produces state churn.

## Decision

### Part 1: A port-level bus manager owns the serial handle

**Driver instances stay one-per-slave.** A new `SerialBusManager` owns each
physical port and serializes all traffic on it.

```
ModbusRtuSerialDriver(unit_id=1) ─┐
ModbusRtuSerialDriver(unit_id=2) ─┼──► SerialBusManager("/dev/ttyUSB0") ──► port
ModbusRtuSerialDriver(unit_id=7) ─┘         (priority queue + RTS timing)
```

- One manager per port path, created lazily and shared by every instance
  configured against that path. Instances acquire it by port name.
- Every request is submitted as a work item and awaited; the manager runs a
  single consumer loop, so exactly one transaction is on the wire at a time
  and T3.5 inter-frame timing is enforced globally for the bus rather than
  per-instance.
- The manager is the only component that touches the serial handle. RTS
  pre/post-transmit delay is applied here, once, for all instances.

**Why one-instance-per-slave rather than one-instance-many-slaves:** it
preserves the existing config schema, the existing driver-per-device mental
model in the Web UI, the existing per-instance health/enable/disable REST
endpoints, and the existing supervisor isolation model (ADR-008). The
alternative — a `unit_ids: []` list inside one instance — would change all
of those, and would additionally mean a single slave's failure is no longer
independently observable or restartable.

### Part 2: The bus manager is the scheduler

Because all traffic for a port funnels through one queue, the three
remaining scheduling requirements become properties of that queue rather
than three separate features:

- **Write priority** — a two-band priority queue: writes drain before
  pending reads. Configurable per instance so a write-heavy bus can be
  tuned. This directly satisfies the CRD's "prioritize write parameters vs
  read" for both RTU and (via the same abstraction, see below) TCP.
- **Polling modes** — Polling (continuous, today's behaviour), On-Connect
  (enqueue once after connect, then idle) and On-Demand (enqueue only on
  explicit request) become submission policies, not loop rewrites.
- **Fixed-period scheduling** — the current loop reads all tags then sleeps
  `scan_rate_ms`, so the true period is `read_time + interval` and drifts
  (finding F-6 in XEDGE-DR-001). The manager schedules the *next due time*
  rather than sleeping after work, giving a stable period and making the
  configured scan rate mean what it says.

**The same scheduling abstraction is applied to the TCP transports.** The
queue and priority logic live in a transport-neutral scheduler; the serial
bus manager is that scheduler plus exclusive ownership of a serial handle.
TCP instances get their own scheduler per connection. This is what makes
write-priority, polling modes and fixed-period scanning work identically
across `modbus_tcp`, `modbus_rtu_tcp` and `modbus_rtu_serial` without
triplicating the logic.

### Part 3: One connectivity state machine

A single `ConnectivityState` component, consumed by all three contexts.

**States:** `UNKNOWN` → `CONNECTED` ⇄ `DEGRADED` → `NOT_CONNECTED`,
with recovery transitions back to `CONNECTED`.

**Transitions are driven by two configurable inputs:**

- `failure_threshold` — consecutive failures before leaving `CONNECTED`
- `recovery_threshold` — consecutive successes before returning to
  `CONNECTED`

Two thresholds rather than one is deliberate: a single threshold makes a
flapping link oscillate, generating alarm and audit-log churn. Asymmetric
hysteresis is the standard fix and costs nothing to implement here — but
would have been implemented inconsistently, or not at all, in three
separate places.

**Per-context adapters map the shared states to each context's vocabulary:**

| Context | Adapter maps to | Source of success/failure signal |
|---|---|---|
| Modbus device health | Connected / Not Connected | per-transaction result from the bus manager |
| Gateway connection state | Connected / Disconnected / Active / Inactive | heartbeat age in `fleet/registry.py`; Active/Inactive distinguishes a registered-but-idle device from a live one |
| Asset connection state | Connected / Degraded / Not Connected | aggregate over the backing driver instances (ADR-010 §4) |

The gateway's four-state enum is the reason for adapters rather than a
single shared vocabulary: the CRD's gateway states encode two orthogonal
axes (reachability and activity) that the device and asset contexts do not
have. Forcing all three into one enum would mean inventing meaningless
states for two of them.

**Where it lives:** `xedge/core/connectivity.py` — core, not driver-local,
because the fleet registry and the asset layer both consume it and neither
should import from `xedge/drivers/`.

## Consequences

**Positive**

- Multi-drop RS-485 becomes supportable — the topology most of this
  customer's field devices will actually use
- Four CRD requirements (multi-slave, write priority, polling modes, RTS
  timing) plus one defect (scan-rate drift, F-6) are addressed by one
  component rather than five changes scattered across three transports
- Connectivity behaviour is consistent and configurable in one place, with
  hysteresis that would likely have been missed in a three-way split
- Reduces the compliance report's estimate below its stated figure, as
  §8.6 of that report anticipated

**Negative**

- The bus manager is a new single point of failure for a port: a hung
  transaction stalls every slave on that bus. Mitigated by a hard
  per-transaction timeout in the manager, and by the fact that this is
  *physically true of RS-485 anyway* — the bus is genuinely serial, and
  pretending otherwise is what the current code does.
- Aggregate throughput on a shared bus is now visibly bounded by the slowest
  slave. This is honest rather than new, but it will look like a regression
  to anyone who had two instances "working" on one port by luck.
- `ConnectivityState` is consumed by three subsystems, so its API needs to
  be stable early. Changing it mid-delivery touches all three.

**Testing implications**

RS-485 serial is currently 23% covered with no fake-serial fixture (finding
F-8). The bus manager is not testable without one. A `pty`-based fake
serial fixture is therefore a prerequisite deliverable in Sprint C3, not an
afterthought — and it retroactively makes the existing RTU driver testable.

## Alternatives considered

**One driver instance polling many slaves.** Rejected — changes the config
schema, the UI model, and per-slave observability. See Part 1.

**A lock around the existing serial handle.** Rejected: a mutex fixes the
collision but provides no ordering, no priority, no fixed-period
scheduling, and no place for RTS timing. It solves one of five problems.

**Three separate connectivity implementations, matching the CRD's
structure.** Rejected — guarantees drift, triplicates the hysteresis
problem, and costs more in total. See Problem 2.

**Threshold-only state machine (no hysteresis).** Rejected: a flapping link
would produce alarm and audit-log churn. The asymmetric-threshold version
costs a few extra lines in one place.

## References

- XEDGE-CRD-001 §4.1, §4.2, §4.3, §4.4, §4.9, §4.11, §5 item 1, §8.6
- XEDGE-DR-001 D-16, D-17, findings F-6, F-8
- ADR-008 (driver isolation — one asyncio task per instance; the bus
  manager is a shared resource *between* those tasks, not a new isolation
  unit)
- ADR-010 (Asset Connection State consumes this state machine)
