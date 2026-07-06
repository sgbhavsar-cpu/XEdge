# ADR-008: Driver Isolation Model

**Status:** ACCEPTED
**Date:** 2026-07-06
**Deciders:** Lead Architect, Core Engineering

## Context

Sprint 25 ("Driver Framework v2") calls for documenting "the driver
isolation model and thread executor design." By this point, five driver
types (Modbus TCP/RTU-serial/RTU-over-TCP, OPC UA client, BACnet/IP, plus
the Sprint 17 loopback driver) have been built, run concurrently, and
exercised through crash/reconnect/hot-reload/enable-disable paths across
several hundred tests and repeated live manual verification. This ADR
documents the isolation model **as it was actually built and proven**,
not a prospective design — including explaining why a thread-executor
model, despite being in the sprint's own title, was never adopted.

## Decision

### 1. Isolation unit: one asyncio task per driver instance, not an OS thread

`DriverSupervisor.start()` (`xedge/core/supervisor.py`) schedules each
driver instance as `asyncio.ensure_future(self._supervise(config))` — a
single-process, single-thread, cooperative-concurrency task, not a
worker thread or process. Every driver instance shares the same event
loop as the pipeline, the northbound dispatcher, and the REST/Web UI
server.

This works because **every driver implemented here is I/O-bound, not
CPU-bound**: Modbus/OPC UA/BACnet reads are all `await`-based socket I/O
with no meaningful CPU-side computation between requests. An OS thread
(or process) pool exists specifically to work around Python's GIL for
CPU-bound work, or to isolate code that can't cooperate with an event
loop (blocking C extensions, etc.) — neither applies to any driver built
so far. Adopting a thread pool here would add real overhead (thread
creation/context-switch cost, cross-thread queue handoff for every
`TagUpdate`) for zero isolation benefit over what asyncio tasks already
provide.

### 2. Fault isolation: a crash in one instance never reaches another, or the pipeline

`DriverSupervisor._supervise()`'s per-task `try/except Exception` boundary
(lines 179-201) is the actual isolation mechanism: any exception raised
by a driver's `connect()`/`run()` is caught *inside that instance's own
task*, logged, and turned into an exponential-backoff retry — it never
propagates to the pipeline consumer, the northbound dispatcher, or any
other driver's task. This has been exercised directly: a BACnet/IP local
UDP bind conflict, a Modbus TCP connection refusal, an OPC UA endpoint
timeout — none of these have ever taken down another concurrently-running
driver or the process as a whole, confirmed across this session's BACnet,
TLS, RBAC, and hardening-sprint manual verification passes (three
simulated drivers running concurrently throughout).

### 3. Three distinct terminal-ish states, not just "running" and "not running"

- **`STOPPED`** — a deliberate `stop()` call (config removed, or a
  supervisor-level shutdown) cancelled the task; not retried.
- **`BACKOFF`**/**`RUNNING`**/**`STARTING`** — the crash-and-retry cycle;
  the task is still alive and will keep retrying forever (no give-up
  state — a permanently unreachable device is expected to be fixed by an
  operator, not silently abandoned).
- **`DISABLED`** (new, Sprint 25/XEDGE-186) — a deliberate, config-driven
  "off" distinct from `STOPPED`: the instance is cancelled the same way,
  but the *reason* is recorded differently so the Web UI/REST API can
  show "an operator turned this off" separately from "this was removed
  from config" or "this crashed." `DriverSupervisor.disable()` reuses
  `stop()`'s exact cancellation path, only overriding the final state.

### 4. Config changes reconcile per-instance, not via a global restart

`hot_reload.py::apply_driver_changes()` diffs the new `drivers:` list
against the previously-applied one and only touches instances that are
new, changed, removed, or newly disabled — every other running instance
is left completely undisturbed. This was true before Sprint 25 and
remains true with the `DISABLED` state added: a disabled entry is now
*remembered* (not forgotten) precisely so a later re-enable is correctly
detected as "this instance's entry changed" rather than "this is a brand
new instance," without needing any new tracking structure beyond the one
dict `hot_reload.py` already threads through `config_watch_loop()`.

## Consequences

- **Positive**: no thread-pool sizing/tuning surface exists at all — the
  isolation model scales to however many driver instances a device's
  config defines, bounded only by the event loop's own capacity (which,
  for I/O-bound work, is very large relative to what an industrial edge
  gateway's realistic tag-group count needs).
- **Positive**: a single supervisor object, a single `_status`/`_tasks`
  dict pair, no cross-thread synchronization primitives (locks,
  queues-between-threads) anywhere in the driver framework.
- **Trade-off, accepted**: if a future driver type needs genuinely
  CPU-bound work (e.g. a protocol requiring heavy in-process
  cryptography or parsing on every message), it would need to hand that
  specific work off to `asyncio.to_thread()`/a process pool *within* its
  own `run()` implementation — the supervisor itself provides no thread
  pool to reach for. No driver built so far has needed this; if one
  does, that's a per-driver decision, not a framework-wide one.
- **Trade-off, accepted**: because everything shares one event loop, a
  driver that blocks synchronously (forgets to `await` a genuinely
  blocking call) would stall every other driver and the whole app, not
  just itself — this is the standard asyncio single-loop hazard, mitigated
  today only by code review discipline (every driver implemented so far
  uses `asyncio`-native I/O libraries end to end: `asyncio.open_connection`,
  `asyncua`, `bacpypes3`), not by a runtime guard.
