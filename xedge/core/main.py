"""xEdge application entrypoint: asyncio main loop with graceful shutdown.

Wires together the config engine, structured logging, the systemd watchdog
kick, and the driver supervisor skeleton. Concrete protocol drivers are
registered elsewhere (Sprint 2+) and imported here only once they exist.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from importlib import resources
from pathlib import Path

from xedge import __version__
from xedge.core.config import ConfigEngine, ConfigStore
from xedge.core.driver_config import build_driver_config
from xedge.core.pipeline import normalize
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.core.watchdog import watchdog_loop
from xedge.drivers.base import TagUpdate
from xedge.drivers.modbus.tcp import ModbusTcpDriver
from xedge.observability.logging import configure_logging, get_logger

_SCHEMA_FILENAME = "xedge-core.schema.json"
_TAG_QUEUE_MAX_DEPTH = 10_000


def _default_schema_path() -> Path:
    """Locate the bundled core config schema.

    Tries the packaged copy first (xedge/schema/..., included via
    `[tool.hatch.build.targets.wheel.force-include]` — present after a real
    `pip install` such as the Docker image's). Falls back to the repo-root
    `config/schema/` location, which is what resolves for `pip install -e .`
    from a source checkout, where force-include isn't applied.
    """
    packaged = resources.files("xedge") / "schema" / _SCHEMA_FILENAME
    if packaged.is_file():
        return Path(str(packaged))
    return Path(__file__).resolve().parent.parent.parent / "config" / "schema" / _SCHEMA_FILENAME


_DEFAULT_SCHEMA_PATH = _default_schema_path()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xedge", description="xEdge IIoT edge software stack")
    parser.add_argument("--config", type=Path, required=True, help="Path to xedge.yaml")
    parser.add_argument(
        "--schema",
        type=Path,
        default=_DEFAULT_SCHEMA_PATH,
        help="Path to the core config JSON Schema (default: bundled schema)",
    )
    parser.add_argument("--version", action="version", version=f"xedge {__version__}")
    return parser.parse_args(argv)


def _build_registry() -> DriverRegistry:
    """Known driver types (FR-DF-001: plugin-based, but Sprint 2 has exactly
    one concrete driver so registration is still explicit here rather than
    via a discovered entry-point mechanism)."""
    registry = DriverRegistry()
    registry.register("modbus_tcp", ModbusTcpDriver)
    return registry


def _start_configured_drivers(
    store: ConfigStore, registry: DriverRegistry, supervisor: DriverSupervisor
) -> None:
    for entry in store.get_section("drivers", []):
        if not entry.get("enabled", True):
            continue
        driver_config = build_driver_config(entry)
        supervisor.start(driver_config)


async def _drain_tag_queue(queue: asyncio.Queue[TagUpdate]) -> None:
    """Phase 1 pipeline consumer (XEDGE-014/017): normalize each TagUpdate
    and log it. Store-and-forward and northbound dispatch are later sprints
    (XEDGE-030, XEDGE-023) — for now this is what makes tag reads observable."""
    logger = get_logger(__name__)
    while True:
        update = await queue.get()
        tag = normalize(update)
        logger.debug(
            "tag.update",
            tag_id=tag.tag_id,
            value=tag.value,
            quality=tag.quality.value,
            source_driver=tag.source_driver,
        )


async def _wait_for_shutdown_signal() -> None:
    """Block until SIGTERM or SIGINT, using loop signal handlers where the
    platform supports them (all target deployments — NFR-C-001 — do), and
    falling back to signal.signal() for development on platforms that don't
    (e.g. Windows, where SIGTERM delivery is not supported)."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(*_: object) -> None:
        stop_event.set()

    registered_via_loop = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            registered_via_loop = False

    if not registered_via_loop:
        signal.signal(signal.SIGINT, _on_signal)

    await stop_event.wait()


async def async_main(config_path: Path, schema_path: Path) -> int:
    engine = ConfigEngine(base_path=config_path, schema_path=schema_path)
    store: ConfigStore = engine.load()

    logging_config = store.get_section("logging", {})
    configure_logging(level=logging_config.get("level", "INFO"))
    logger = get_logger(__name__)
    logger.info("xedge.starting", version=__version__, config_path=str(config_path))

    watchdog_config = store.get_section("watchdog", {})
    watchdog_task = asyncio.create_task(
        watchdog_loop(
            interval_seconds=watchdog_config.get("interval_seconds", 15),
            enabled=watchdog_config.get("enabled", True),
        )
    )

    tag_queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=_TAG_QUEUE_MAX_DEPTH)
    registry = _build_registry()
    supervisor = DriverSupervisor(registry, tag_queue)
    _start_configured_drivers(store, registry, supervisor)
    drain_task = asyncio.create_task(_drain_tag_queue(tag_queue))

    logger.info("xedge.ready")
    try:
        await _wait_for_shutdown_signal()
        logger.info("xedge.shutdown_signal_received")
    finally:
        watchdog_task.cancel()
        drain_task.cancel()
        await asyncio.gather(watchdog_task, drain_task, return_exceptions=True)
        await supervisor.stop_all()
        logger.info("xedge.stopped")

    return 0


def run(argv: list[str] | None = None) -> int:
    """Synchronous entrypoint (registered as the `xedge` console script)."""
    args = parse_args(argv)
    return asyncio.run(async_main(args.config, args.schema))


if __name__ == "__main__":
    sys.exit(run())
