"""Config hot-reload: watches xedge.yaml for changes, validates before
applying (FR-CM-005), and restarts only the driver instances whose config
actually changed — system-architecture.md §3.2: "Config hot-reload triggers
a controlled driver restart (disconnect → reconfigure → connect)", scoped
to the affected instances only, per the Sprint 25 driver-framework-v2 intent
brought forward for the config engine's own reload path.

Uses mtime polling rather than OS-level file-system events (inotify, etc.)
for portability across every target distribution (NFR-C-002) without an
extra dependency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from xedge.core.config import ConfigEngine, ConfigStore, ConfigValidationError
from xedge.core.driver_config import build_driver_config
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0


async def apply_driver_changes(
    new_drivers: list[dict[str, Any]],
    current: dict[str, dict[str, Any]],
    registry: DriverRegistry,
    supervisor: DriverSupervisor,
) -> dict[str, dict[str, Any]]:
    """Reconcile running driver instances with a new `drivers` config list.

    Only instances that are new, changed, or removed are stopped/(re)started;
    unchanged instances keep running undisturbed — this is the "restart only
    affected drivers" behavior. Returns the new instance_id -> raw config
    entry tracking dict (callers should replace their prior one with it).
    """
    new_by_id = {entry["id"]: entry for entry in new_drivers if entry.get("enabled", True)}

    for instance_id in list(current):
        if instance_id not in new_by_id:
            await supervisor.stop(instance_id)
            logger.info("driver.stopped_removed_from_config", instance_id=instance_id)

    for instance_id, entry in new_by_id.items():
        if current.get(instance_id) == entry:
            continue  # unchanged: leave it running
        if instance_id in current:
            await supervisor.stop(instance_id)
        driver_config = build_driver_config(entry)
        supervisor.start(driver_config)
        logger.info("driver.restarted_for_config_change", instance_id=instance_id)

    return new_by_id


async def config_watch_loop(
    engine: ConfigEngine,
    store: ConfigStore,
    config_path: Path,
    registry: DriverRegistry,
    supervisor: DriverSupervisor,
    current_drivers: dict[str, dict[str, Any]],
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll `config_path`'s mtime; on change, reload + validate (rejecting
    and logging on failure, per FR-CM-005 — the prior config keeps running),
    then reconcile driver instances against the new config.

    `current_drivers` is mutated in place (cleared and repopulated) rather
    than reassigned, so the caller's reference stays valid across iterations.
    """
    try:
        # A local-filesystem stat() is a fast syscall, not a blocking I/O
        # operation worth the overhead of asyncio.to_thread() on every poll.
        last_mtime: float | None = config_path.stat().st_mtime  # noqa: ASYNC240
    except OSError:
        last_mtime = None

    while True:
        await asyncio.sleep(poll_interval_seconds)
        try:
            current_mtime = config_path.stat().st_mtime  # noqa: ASYNC240
        except OSError:
            continue
        if current_mtime == last_mtime:
            continue
        last_mtime = current_mtime

        try:
            engine.reload(store)
        except ConfigValidationError as exc:
            logger.error("config.reload_rejected", error=str(exc))
            continue

        logger.info("config.reloaded", config_path=str(config_path))
        new_drivers = store.get_section("drivers", [])
        updated = await apply_driver_changes(new_drivers, current_drivers, registry, supervisor)
        current_drivers.clear()
        current_drivers.update(updated)
