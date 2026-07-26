"""Per-driver-instance "system tags" (docs/planning/pendingtasks.md — "Driver
system tags"): synthetic health/statistics tags published alongside each
driver's real tags, under the reserved `{instance_id}/_system/{name}` id
namespace, so northbound consumers (MQTT Sparkplug B, the OPC UA server) and
the Web UI's driver-detail page can tell "driver stopped reading" apart from
"the field device's value just hasn't changed" — something no existing tag
carries today.

These aren't raw driver reads, so they skip xedge.core.pipeline.normalize()
(there's nothing to scale/deadband) and are built directly as UnifiedTag,
fed into the same three sinks xedge.core.main._pipeline_to_buffer already
uses per real tag: the OPC UA server, the Web UI's LatestValueStore, and the
driver's ring buffer (so northbound drains and publishes them like any other
tag — no dispatcher/connector changes needed).
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta

from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverInstanceStatus, DriverState, DriverSupervisor
from xedge.drivers.base import Quality, TagValue
from xedge.northbound.opcua_server import OpcUaTagServer
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferManager

DEFAULT_PUBLISH_INTERVAL_SECONDS = 10.0

SYSTEM_TAG_NAMES = (
    "status",
    "status_time",
    "tag_count",
    "reads_per_second",
    "reads_per_minute",
    "reads_per_hour",
    "error_count",
    "consecutive_failures",
    "uptime_seconds",
    # Device-level connectivity (Sprint C2, XEDGE-420/421) — distinct from
    # `status` above, which only reflects whether this instance's asyncio
    # task is alive. Stays "unknown" for driver types that don't implement
    # `get_connectivity_state()` yet (currently: only the Modbus family).
    "connectivity_state",
)

_ONE_HOUR_SECONDS = 3600.0


def system_tag_id(instance_id: str, name: str) -> str:
    return f"{instance_id}/_system/{name}"


class _ReadRateTracker:
    """Derives reads/sec, /min, /hour from periodic samples of a driver's
    cumulative `tag_read_count` — there's no existing rate-over-time counter
    in the codebase, only raw cumulative counters (DriverMetrics), so this
    keeps a short per-instance history and divides count-delta by time-delta
    over each window."""

    def __init__(self, history_seconds: float = _ONE_HOUR_SECONDS) -> None:
        self._history_seconds = history_seconds
        self._history: dict[str, deque[tuple[datetime, int]]] = {}

    def sample(self, instance_id: str, count: int, now: datetime) -> dict[str, float]:
        history = self._history.setdefault(instance_id, deque())
        history.append((now, count))
        cutoff = now - timedelta(seconds=self._history_seconds)
        while len(history) > 1 and history[0][0] < cutoff:
            history.popleft()
        return {
            f"reads_per_{unit}": self._rate_over(history, now, window)
            for unit, window in (("second", 1.0), ("minute", 60.0), ("hour", _ONE_HOUR_SECONDS))
        }

    @staticmethod
    def _rate_over(
        history: deque[tuple[datetime, int]], now: datetime, window_seconds: float
    ) -> float:
        cutoff = now - timedelta(seconds=window_seconds)
        # The oldest sample at-or-before the window's cutoff, falling back to
        # the oldest sample available if the driver hasn't been running that
        # long yet — an instance running for 10s has no real "per hour" rate,
        # so it's reported over however much history actually exists.
        reference = history[0]
        for sample in history:
            if sample[0] <= cutoff:
                reference = sample
            else:
                break
        elapsed = (now - reference[0]).total_seconds()
        if elapsed <= 0:
            return 0.0
        return (history[-1][1] - reference[1]) / elapsed


def _data_type_name(value: TagValue) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    return "STRING"


def build_system_tags(
    status: DriverInstanceStatus, rates: dict[str, float], now: datetime
) -> list[UnifiedTag]:
    """Build this instance's nine system tags for one publish tick."""
    uptime_seconds = (
        (now - status.state_changed_at).total_seconds()
        if status.state is DriverState.RUNNING
        else 0.0
    )
    values: dict[str, TagValue] = {
        "status": status.state.value,
        "status_time": status.state_changed_at.isoformat(),
        "tag_count": status.tag_count,
        "error_count": status.metrics.error_count,
        "consecutive_failures": status.consecutive_failures,
        "uptime_seconds": uptime_seconds,
        "connectivity_state": status.connectivity_state.value,
        **rates,
    }
    return [
        UnifiedTag(
            tag_id=system_tag_id(status.instance_id, name),
            timestamp=now,
            value=values[name],
            data_type=_data_type_name(values[name]),
            quality=Quality.GOOD,
            source_driver=status.instance_id,
            source_address=name,
        )
        for name in SYSTEM_TAG_NAMES
    ]


async def system_tag_publish_loop(
    supervisor: DriverSupervisor,
    ring_buffers: RingBufferManager,
    latest_values: LatestValueStore,
    opcua_server: OpcUaTagServer | None,
    interval_seconds: float = DEFAULT_PUBLISH_INTERVAL_SECONDS,
) -> None:
    """Periodically publish every driver instance's system tags — same
    while-True-sleep-then-work shape as xedge.core.watchdog.watchdog_loop /
    xedge.store.sqlite_store.purge_loop. Re-enumerates `supervisor.all_status()`
    every tick (rather than a static instance list) so driver instances added
    or removed by hot-reload are picked up with no extra wiring, matching how
    purge_loop re-reads `ring_buffers.stream_keys()` each cycle."""
    tracker = _ReadRateTracker()
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(UTC)
        for status in supervisor.all_status().values():
            rates = tracker.sample(status.instance_id, status.metrics.tag_read_count, now)
            for tag in build_system_tags(status, rates, now):
                if opcua_server is not None:
                    await opcua_server.update_tag(tag)
                latest_values.update(tag)
                ring_buffers.push(status.instance_id, tag)
