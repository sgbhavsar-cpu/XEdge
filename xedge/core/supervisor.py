"""DriverSupervisor: owns the lifecycle of all driver instances.

Each driver instance runs as its own asyncio task. A crash in one instance
is caught and triggers a restart with exponential backoff (NFR-R-006); it
does not affect the pipeline or any other driver instance (system-architecture.md §3.2).
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from xedge.drivers.base import BaseDriver, DriverConfig, DriverMetrics, TagUpdate
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

DriverFactory = Callable[[], BaseDriver]

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 300.0
_BACKOFF_MULTIPLIER = 2.0
# A run lasting at least this long is considered "healthy enough" to reset
# the backoff counter back to the initial value.
_BACKOFF_RESET_THRESHOLD_SECONDS = 60.0


class DriverState(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    FAILED = "failed"
    # Deliberately disabled via config (`enabled: false`) or the
    # enable/disable REST endpoints (Sprint 25, XEDGE-186) — distinct from
    # STOPPED (a generic cancellation) so the Web UI/API can tell "an
    # operator turned this off on purpose" from "this driver was removed
    # from config" or "this driver crashed."
    DISABLED = "disabled"


@dataclass(slots=True)
class DriverInstanceStatus:
    instance_id: str
    driver_type: str
    state: DriverState
    consecutive_failures: int = 0
    last_error: str | None = None
    metrics: DriverMetrics = field(default_factory=DriverMetrics)
    # Total tags across this instance's tag_groups (FR-WU-* system tags) — set
    # once at start() from its DriverConfig, not recomputed on every status
    # read, since it only changes when hot-reload stops+restarts the instance
    # for a config change (xedge.core.hot_reload.apply_driver_changes).
    tag_count: int = 0
    state_changed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _set_state(status: DriverInstanceStatus, new_state: DriverState) -> None:
    """Update `state` and stamp `state_changed_at` together — system tags'
    `status_time` depends on these never drifting apart."""
    status.state = new_state
    status.state_changed_at = datetime.now(UTC)


class DriverRegistry:
    """Maps a driver `type` string (from config) to a factory function.

    Concrete drivers register themselves here (typically via an entry point
    or explicit import) rather than the supervisor importing every protocol
    module directly — this is what keeps the framework plugin-based (FR-DF-001).
    """

    def __init__(self) -> None:
        self._factories: dict[str, DriverFactory] = {}

    def register(self, driver_type: str, factory: DriverFactory) -> None:
        if driver_type in self._factories:
            raise ValueError(f"Driver type already registered: {driver_type!r}")
        self._factories[driver_type] = factory

    def create(self, driver_type: str) -> BaseDriver:
        try:
            factory = self._factories[driver_type]
        except KeyError as exc:
            raise ValueError(f"Unknown driver type: {driver_type!r}") from exc
        return factory()


class DriverSupervisor:
    """Starts, stops, and supervises one asyncio task per driver instance.

    `output_queue` is the shared, bounded queue that all driver instances
    feed TagUpdate objects into for the pipeline to consume.
    """

    def __init__(self, registry: DriverRegistry, output_queue: asyncio.Queue[TagUpdate]) -> None:
        self._registry = registry
        self._output_queue = output_queue
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._status: dict[str, DriverInstanceStatus] = {}
        # Current live driver instance per instance_id, so status()/
        # all_status() can report real, up-to-date metrics (get_metrics())
        # rather than the always-default value baked into DriverInstanceStatus
        # at construction.
        self._drivers: dict[str, BaseDriver] = {}

    def status(self, instance_id: str) -> DriverInstanceStatus:
        return self._with_live_metrics(self._status[instance_id])

    def all_status(self) -> dict[str, DriverInstanceStatus]:
        return {
            instance_id: self._with_live_metrics(status)
            for instance_id, status in self._status.items()
        }

    def _with_live_metrics(self, status: DriverInstanceStatus) -> DriverInstanceStatus:
        driver = self._drivers.get(status.instance_id)
        if driver is None:
            return status
        return replace(status, metrics=driver.get_metrics())

    def start(self, config: DriverConfig) -> None:
        """Start supervising a driver instance. Raises if already running."""
        if config.instance_id in self._tasks:
            raise ValueError(f"Driver instance already running: {config.instance_id!r}")

        tag_count = sum(len(group.get("tags", [])) for group in config.tag_groups)
        self._status[config.instance_id] = DriverInstanceStatus(
            instance_id=config.instance_id,
            driver_type=config.driver_type,
            state=DriverState.STARTING,
            tag_count=tag_count,
        )
        task = asyncio.ensure_future(self._supervise(config))
        self._tasks[config.instance_id] = task

    async def stop(self, instance_id: str) -> None:
        """Cancel and await a driver instance's supervised task."""
        task = self._tasks.pop(instance_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if instance_id in self._status:
            _set_state(self._status[instance_id], DriverState.STOPPED)

    async def stop_all(self) -> None:
        for instance_id in list(self._tasks):
            await self.stop(instance_id)

    async def disable(self, instance_id: str) -> None:
        """Like `stop()`, but the resulting state is `DISABLED` rather than
        `STOPPED` — a deliberate, config-driven "off," not a generic
        cancellation. Safe to call on an already-stopped/disabled/unknown
        instance (same no-op-if-absent behavior `stop()` already has)."""
        await self.stop(instance_id)
        if instance_id in self._status:
            _set_state(self._status[instance_id], DriverState.DISABLED)

    async def _supervise(self, config: DriverConfig) -> None:
        instance_id = config.instance_id
        status = self._status[instance_id]
        backoff = _INITIAL_BACKOFF_SECONDS

        while True:
            driver = self._registry.create(config.driver_type)
            self._drivers[instance_id] = driver
            _set_state(status, DriverState.STARTING)
            run_started_at = time.monotonic()
            try:
                await driver.configure(config)
                await driver.connect()
                _set_state(status, DriverState.RUNNING)
                logger.info(
                    "driver.started", instance_id=instance_id, driver_type=config.driver_type
                )
                await driver.run(self._output_queue)
                # A driver's run() returning normally (rather than raising or
                # being cancelled) is treated as a request to stop supervising.
                _set_state(status, DriverState.STOPPED)
                await driver.disconnect()
                return
            except asyncio.CancelledError:
                _set_state(status, DriverState.STOPPED)
                await _safe_disconnect(driver, instance_id)
                raise
            except Exception as exc:  # noqa: BLE001 — isolate any driver-raised failure
                status.consecutive_failures += 1
                status.last_error = str(exc)
                _set_state(status, DriverState.BACKOFF)
                logger.error(
                    "driver.failed",
                    instance_id=instance_id,
                    driver_type=config.driver_type,
                    error=str(exc),
                    consecutive_failures=status.consecutive_failures,
                )
                await _safe_disconnect(driver, instance_id)

                if time.monotonic() - run_started_at >= _BACKOFF_RESET_THRESHOLD_SECONDS:
                    backoff = _INITIAL_BACKOFF_SECONDS
                    status.consecutive_failures = 1

                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    _set_state(status, DriverState.STOPPED)
                    raise
                backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SECONDS)


async def _safe_disconnect(driver: BaseDriver, instance_id: str) -> None:
    try:
        await driver.disconnect()
    except Exception as exc:  # noqa: BLE001 — disconnect must never crash the supervisor
        logger.warning("driver.disconnect_error", instance_id=instance_id, error=str(exc))
