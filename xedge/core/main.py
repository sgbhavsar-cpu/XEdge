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
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import uvicorn

from xedge import __version__
from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore, load_or_create_secret_key
from xedge.api.server import create_app
from xedge.api.tls import load_or_create_server_certificate
from xedge.core.alarms import ALARM_STREAM_KEY_SUFFIX, AlarmEngine, build_alarm_rules
from xedge.core.config import ConfigEngine, ConfigStore, ConfigValidator, ConfigVersionHistory
from xedge.core.driver_config import build_driver_config
from xedge.core.hot_reload import config_watch_loop
from xedge.core.pipeline import (
    DeadbandFilter,
    TagPipelineConfig,
    UnifiedTag,
    build_tag_pipeline_configs,
    normalize,
)
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.core.system_tags import SYSTEM_TAG_NAMES, system_tag_id, system_tag_publish_loop
from xedge.core.watchdog import watchdog_loop
from xedge.core.write_router import WriteRouter
from xedge.drivers.bacnet.client import BacnetIpDriver
from xedge.drivers.base import TagUpdate, TagValue
from xedge.drivers.loopback.driver import LoopbackDriver
from xedge.drivers.modbus.rtu_over_tcp import ModbusRtuOverTcpDriver
from xedge.drivers.modbus.serial import ModbusRtuSerialDriver
from xedge.drivers.modbus.tcp import ModbusTcpDriver
from xedge.drivers.opcua.client import OpcUaClientDriver
from xedge.fleet.agent import FleetAgentConfig, FleetAgentStatus, fleet_heartbeat_loop
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.northbound.mqtt import MqttSparkplugConnector, SparkplugConnectorConfig
from xedge.northbound.opcua_server import OpcUaServerConfig, OpcUaTagServer
from xedge.observability.audit_log import AuditLog
from xedge.observability.logging import configure_logging, get_logger
from xedge.observability.otel_metrics import configure_metrics
from xedge.observability.tracing import configure_tracing, get_tracer
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferBackpressureError, RingBufferManager
from xedge.store.sqlite_store import SqliteColdStore, purge_loop

logger = get_logger(__name__)
_tracer = get_tracer(__name__)

_MODBUS_DRIVER_TYPES = frozenset({"modbus_tcp", "modbus_rtu_tcp", "modbus_rtu_serial"})
_MODBUS_BIT_FUNCTION_CODES = frozenset({"read_coils", "read_discrete_inputs"})

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
    """Known driver types (FR-DF-001: plugin-based; registration is
    explicit here rather than via a discovered entry-point mechanism)."""
    registry = DriverRegistry()
    registry.register("modbus_tcp", ModbusTcpDriver)
    registry.register("modbus_rtu_tcp", ModbusRtuOverTcpDriver)
    registry.register("modbus_rtu_serial", ModbusRtuSerialDriver)
    registry.register("opcua_client", OpcUaClientDriver)
    registry.register("bacnet_ip", BacnetIpDriver)
    registry.register("loopback", LoopbackDriver)
    return registry


def _start_configured_drivers(
    store: ConfigStore, registry: DriverRegistry, supervisor: DriverSupervisor
) -> None:
    for entry in store.get_section("drivers", []):
        if not entry.get("enabled", True):
            continue
        driver_config = build_driver_config(entry)
        supervisor.start(driver_config)


_BACKPRESSURE_RETRY_INTERVAL_SECONDS = 0.5


async def _push_with_backpressure_retry(
    ring_buffers: RingBufferManager, stream_key: str, tag: UnifiedTag, is_alarm_stream: bool
) -> None:
    """`ring_buffers.push()` raises `RingBufferBackpressureError` for a full
    alarm-tier stream (`xedge.store.ring_buffer.RingBuffer`'s own docstring:
    "caller must retry, not drop") — retry indefinitely rather than
    dropping alarm data or letting the exception crash this whole
    consumer task (which would silently stop *all* driver instances'
    pipeline processing, not just the one alarm-tier stream that's full).
    A sustained retry loop here means the northbound dispatcher has
    fallen behind; each attempt logs a warning so that's visible."""
    while True:
        try:
            ring_buffers.push(stream_key, tag, is_alarm_stream=is_alarm_stream)
            return
        except RingBufferBackpressureError:
            logger.warning("store.alarm_buffer_full", stream_key=stream_key)
            await asyncio.sleep(_BACKPRESSURE_RETRY_INTERVAL_SECONDS)


async def _pipeline_to_buffer(
    queue: asyncio.Queue[TagUpdate],
    ring_buffers: RingBufferManager,
    tag_configs: dict[str, TagPipelineConfig] | None = None,
    deadband_filter: DeadbandFilter | None = None,
    opcua_server: OpcUaTagServer | None = None,
    latest_values: LatestValueStore | None = None,
    alarm_engine: AlarmEngine | None = None,
) -> None:
    """Pipeline consumer (XEDGE-014/017): normalize each TagUpdate (scaling,
    timestamp resolution), evaluate it against the alarm engine (Sprint 31,
    XEDGE-224 — sets `is_alarm`), reflect the live value into the OPC UA
    server and the Web UI's latest-value cache (if configured — every
    value, deadband does not apply to "current state"), apply deadband
    suppression, and push surviving updates into their driver's ring
    buffer for the northbound dispatcher to drain.

    Buffering key is `source_driver`, not a tag-group id — see
    xedge.store.ring_buffer module docstring for why (tag_group id isn't
    threaded through TagUpdate/UnifiedTag yet) — *except* for a tag with
    an alarm rule configured, which buffers under a distinct
    `{source_driver}::alarm` key instead: alarm-tier data gets
    backpressure-not-eviction and its own (normally longer) retention,
    regardless of whether it's *currently* alarming — see
    `xedge.core.alarms` module docstring.
    """
    tag_configs = tag_configs if tag_configs is not None else {}
    deadband_filter = deadband_filter if deadband_filter is not None else DeadbandFilter()
    while True:
        update = await queue.get()
        with _tracer.start_as_current_span(
            "pipeline.process", attributes={"tag.id": update.tag_id}
        ):
            config = tag_configs.get(update.tag_id)
            tag = normalize(update, config)
            if alarm_engine is not None:
                tag = alarm_engine.evaluate(tag)
            logger.debug(
                "tag.update",
                tag_id=tag.tag_id,
                value=tag.value,
                quality=tag.quality.value,
                source_driver=tag.source_driver,
                is_alarm=tag.is_alarm,
            )
            if opcua_server is not None:
                await opcua_server.update_tag(tag)
            if latest_values is not None:
                latest_values.update(tag)
            if not deadband_filter.should_publish(tag, config.deadband if config else None):
                continue
            is_alarm_tier = alarm_engine is not None and alarm_engine.has_rule(tag.tag_id)
            stream_key = (
                f"{tag.source_driver}{ALARM_STREAM_KEY_SUFFIX}"
                if is_alarm_tier
                else tag.source_driver
            )
            with _tracer.start_as_current_span(
                "store.write", attributes={"stream_key": stream_key}
            ):
                await _push_with_backpressure_retry(ring_buffers, stream_key, tag, is_alarm_tier)


def _all_tag_ids(drivers: list[dict[str, Any]]) -> frozenset[str]:
    real_tag_ids = {
        f"{driver.get('id')}/{tag['id']}"
        for driver in drivers
        for group in driver.get("tag_groups", [])
        for tag in group.get("tags", [])
    }
    system_tag_ids = {
        system_tag_id(driver.get("id"), name) for driver in drivers for name in SYSTEM_TAG_NAMES
    }
    return frozenset(real_tag_ids | system_tag_ids)


_SYSTEM_TAG_INITIAL_VALUES: dict[str, TagValue] = {
    "status": "",
    "status_time": "",
    "tag_count": 0,
    "reads_per_second": 0.0,
    "reads_per_minute": 0.0,
    "reads_per_hour": 0.0,
    "error_count": 0,
    "consecutive_failures": 0,
    "uptime_seconds": 0.0,
}


def _infer_tag_initial_values(drivers: list[dict[str, Any]]) -> dict[str, TagValue]:
    """Guess a representative initial value (of the right *type*) for each
    Modbus tag, for OpcUaServerConfig.initial_values — see that field's
    docstring for why the type must be right from the start.

    Only produced for Modbus tags, where the type is knowable from config
    alone (bit-reading function codes are bool; a scaled register is
    float; else int). Deliberately not guessed for OPC UA client tags (or
    any other driver type) — their remote node's real type isn't known
    before connecting, and asyncua permanently fixes a node's DataType
    from its first-written value, so a wrong guess would silently corrupt
    the value forever (e.g. a boolean tag showing as 0.0). Those tags are
    left for OpcUaTagServer to create lazily on first real update instead —
    see OpcUaServerConfig.tag_ids."""
    values: dict[str, TagValue] = {}
    for driver in drivers:
        instance_id = driver.get("id")
        driver_type = driver.get("type")
        # System tags (docs/planning/pendingtasks.md — "Driver system
        # tags"): every driver type gets these, unlike the Modbus-only
        # real-tag inference below — their types are fixed by
        # xedge.core.system_tags.build_system_tags, not guessed from config.
        for name in SYSTEM_TAG_NAMES:
            values[system_tag_id(instance_id, name)] = _SYSTEM_TAG_INITIAL_VALUES[name]
        if driver_type not in _MODBUS_DRIVER_TYPES:
            continue
        for group in driver.get("tag_groups", []):
            for tag in group.get("tags", []):
                tag_id = f"{instance_id}/{tag['id']}"
                if tag.get("function_code") in _MODBUS_BIT_FUNCTION_CODES:
                    values[tag_id] = False
                elif not tag.get("scaling"):
                    values[tag_id] = 0
                else:
                    values[tag_id] = 0.0
    return values


def _build_opcua_server(store: ConfigStore) -> OpcUaTagServer | None:
    opcua_config = store.get_section("opcua_server", {})
    if not opcua_config.get("enabled", False):
        return None
    endpoint_url = opcua_config.get("endpoint_url")
    if not endpoint_url:
        return None
    drivers = store.get_section("drivers", [])
    return OpcUaTagServer(
        OpcUaServerConfig(
            endpoint_url=endpoint_url,
            server_name=opcua_config.get("server_name", "xEdge"),
            tag_ids=_all_tag_ids(drivers),
            initial_values=_infer_tag_initial_values(drivers),
        )
    )


def _build_fleet_agent_config(store: ConfigStore) -> FleetAgentConfig | None:
    fleet_config = store.get_section("fleet", {})
    if not fleet_config.get("enabled", False):
        return None
    manager_url = fleet_config.get("manager_url")
    device_id = fleet_config.get("device_id")
    join_token = fleet_config.get("join_token")
    if not manager_url or not device_id or not join_token:
        logger.error("fleet.config_incomplete", manager_url=bool(manager_url), device_id=device_id)
        return None
    return FleetAgentConfig(
        manager_url=manager_url,
        device_id=device_id,
        join_token=join_token,
        display_name=fleet_config.get("display_name"),
        heartbeat_interval_seconds=fleet_config.get("heartbeat_interval_seconds", 60),
        verify_tls=fleet_config.get("verify_tls", True),
    )


def _build_northbound_dispatcher(
    store: ConfigStore,
    ring_buffers: RingBufferManager,
    cold_store: SqliteColdStore,
    supervisor: DriverSupervisor,
    audit_log: AuditLog,
) -> NorthboundDispatcher | None:
    northbound_config = store.get_section("northbound", {})
    if not northbound_config.get("enabled", True):
        return None
    mqtt_config = northbound_config.get("mqtt")
    if not mqtt_config:
        return None

    connector = MqttSparkplugConnector(
        SparkplugConnectorConfig(
            host=mqtt_config["host"],
            port=mqtt_config.get("port", 1883),
            group_id=mqtt_config.get("group_id", "xedge"),
            edge_node_id=mqtt_config.get("edge_node_id", "edge01"),
            client_id=mqtt_config.get("client_id", ""),
            keepalive_seconds=mqtt_config.get("keepalive_seconds", 60),
            connect_timeout_seconds=mqtt_config.get("connect_timeout_seconds", 10),
            qos=mqtt_config.get("qos", 1),
            username=mqtt_config.get("username"),
            password=mqtt_config.get("password"),
        ),
        # Sprint 31, XEDGE-223: routes decoded NCMD write commands through
        # the same WriteRouter the REST API's write endpoint uses.
        WriteRouter(supervisor, audit_log),
    )
    return NorthboundDispatcher(
        connector,
        ring_buffers,
        publish_interval_seconds=northbound_config.get("publish_interval_seconds", 1.0),
        cold_store=cold_store,
    )


async def _run_api_server(server: uvicorn.Server) -> None:
    """Isolate REST API startup/serve failures from the rest of the app.

    uvicorn.Server.serve() calls sys.exit() internally on a bind failure
    (e.g. the port is already in use) — that raises SystemExit, a
    BaseException, which if left uncaught inside an unawaited asyncio.Task
    can propagate up through the event loop and crash the entire process.
    A REST API bind failure must not take down driver polling or
    northbound publishing, the same way a driver or dispatcher failure
    doesn't take down the API."""
    try:
        await server.serve()
    except SystemExit as exc:
        logger.error("api.startup_failed", exit_code=exc.code)
    except Exception as exc:  # noqa: BLE001 — isolate any other API failure
        logger.error("api.failed", error=str(exc))


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
    logger.info("xedge.starting", version=__version__, config_path=str(config_path))

    tracing_config = store.get_section("tracing", {})
    configure_tracing(tracing_config, __version__)

    config_mgmt = store.get_section("config_management", {})
    version_history = ConfigVersionHistory(
        Path(config_mgmt.get("history_directory", "/data/config-history")),
        max_versions=config_mgmt.get("max_versions", 10),
    )
    engine.attach_version_history(version_history)

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
    current_drivers = {
        entry["id"]: entry
        for entry in store.get_section("drivers", [])
        if entry.get("enabled", True)
    }
    watch_task = (
        asyncio.create_task(
            config_watch_loop(
                engine,
                store,
                config_path,
                registry,
                supervisor,
                current_drivers,
                poll_interval_seconds=config_mgmt.get("poll_interval_seconds", 2.0),
            )
        )
        if config_mgmt.get("hot_reload_enabled", True)
        else None
    )

    opcua_server = _build_opcua_server(store)
    if opcua_server is not None:
        await opcua_server.start()

    store_config = store.get_section("store", {})
    cold_store = SqliteColdStore(Path(store_config.get("directory", "/data/store")))

    # Web UI auth state (ADR-007): siblings of the store/config-history
    # dirs under /data, matching system-architecture.md §6.4's layout.
    # Constructed unconditionally (not gated on api.enabled) since Sprint 31
    # (XEDGE-223)'s MQTT NCMD write-back path needs `audit_log` for its
    # WriteRouter too, independent of whether the REST API/Web UI is on.
    webui_dir = Path(store_config.get("directory", "/data/store")).parent / "webui"
    audit_log = AuditLog(webui_dir / "audit.jsonl")

    ring_buffers = RingBufferManager(
        max_depth=store_config.get("ram_max_depth", 10_000), on_evict=cold_store.append
    )
    tag_configs = build_tag_pipeline_configs(store.get_section("drivers", []))
    deadband_filter = DeadbandFilter()
    latest_values = LatestValueStore()
    alarms_config = store.get_section("alarms", {})
    alarm_engine = (
        AlarmEngine(build_alarm_rules(alarms_config.get("rules", [])))
        if alarms_config.get("enabled", False)
        else None
    )
    buffer_task = asyncio.create_task(
        _pipeline_to_buffer(
            tag_queue,
            ring_buffers,
            tag_configs,
            deadband_filter,
            opcua_server,
            latest_values,
            alarm_engine,
        )
    )
    purge_task = asyncio.create_task(
        purge_loop(
            cold_store,
            ring_buffers.stream_keys,
            retention_duration_seconds=store_config.get("retention_duration_seconds", 604_800),
            interval_seconds=store_config.get("purge_interval_seconds", 3_600),
            alarm_retention_duration_seconds=alarms_config.get(
                "retention_duration_seconds", 2_592_000
            ),
        )
    )

    system_tags_config = store.get_section("system_tags", {})
    system_tags_task = (
        asyncio.create_task(
            system_tag_publish_loop(
                supervisor,
                ring_buffers,
                latest_values,
                opcua_server,
                interval_seconds=system_tags_config.get("publish_interval_seconds", 10.0),
            )
        )
        if system_tags_config.get("enabled", True)
        else None
    )

    dispatcher = _build_northbound_dispatcher(
        store, ring_buffers, cold_store, supervisor, audit_log
    )
    dispatcher_task = asyncio.create_task(dispatcher.run()) if dispatcher is not None else None

    fleet_status = FleetAgentStatus()
    fleet_agent_config = _build_fleet_agent_config(store)
    fleet_token_path = (
        Path(store_config.get("directory", "/data/store")).parent / "fleet" / "device_token"
    )
    fleet_task = (
        asyncio.create_task(
            fleet_heartbeat_loop(
                fleet_agent_config,
                fleet_token_path,
                supervisor,
                config_path,
                ConfigValidator.from_file(schema_path),
                datetime.now(UTC),
                fleet_status,
            )
        )
        if fleet_agent_config is not None
        else None
    )

    metrics_config = store.get_section("metrics", {})
    if metrics_config.get("enabled", True):
        configure_metrics(supervisor, dispatcher, ring_buffers, __version__)

    rate_limit_config = store.get_section("rate_limit", {})
    api_config = store.get_section("api", {})
    api_server: uvicorn.Server | None = None
    api_task: asyncio.Task[None] | None = None
    if api_config.get("enabled", True):
        user_store = UserStore(webui_dir / "users.json")
        session_manager = SessionManager(load_or_create_secret_key(webui_dir / "session_key"))
        login_tracker = LoginAttemptTracker()

        tls_config = store.get_section("tls", {})
        tls_enabled = tls_config.get("enabled", True)
        ssl_certfile: str | None = None
        ssl_keyfile: str | None = None
        if tls_enabled:
            cert_path = Path(tls_config.get("cert_path", webui_dir / "server_cert.pem"))
            key_path = Path(tls_config.get("key_path", webui_dir / "server_key.pem"))
            load_or_create_server_certificate(
                cert_path,
                key_path,
                tls_config.get("common_name", "xedge.local"),
                tls_config.get("validity_days", 825),
            )
            ssl_certfile = str(cert_path)
            ssl_keyfile = str(key_path)

        app = create_app(
            supervisor,
            version_history,
            dispatcher,
            user_store=user_store,
            session_manager=session_manager,
            login_tracker=login_tracker,
            config_path=config_path,
            schema_path=schema_path,
            latest_values=latest_values,
            audit_log=audit_log,
            ring_buffers=ring_buffers,
            cold_store=cold_store,
            secure_cookies=tls_enabled,
            dashboard_url=tracing_config.get("dashboard_url"),
            rate_limit_enabled=rate_limit_config.get("enabled", True),
            requests_per_minute=rate_limit_config.get("requests_per_minute", 100),
            fleet_status=fleet_status,
            alarm_engine=alarm_engine,
        )
        api_server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=api_config.get("host", "127.0.0.1"),
                port=api_config.get("port", 8080),
                log_config=None,
                access_log=False,
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
            )
        )
        api_task = asyncio.create_task(_run_api_server(api_server))

    logger.info("xedge.ready")
    try:
        await _wait_for_shutdown_signal()
        logger.info("xedge.shutdown_signal_received")
    finally:
        watchdog_task.cancel()
        buffer_task.cancel()
        purge_task.cancel()
        tasks_to_await = [watchdog_task, buffer_task, purge_task]
        if system_tags_task is not None:
            system_tags_task.cancel()
            tasks_to_await.append(system_tags_task)
        if dispatcher_task is not None:
            dispatcher_task.cancel()
            tasks_to_await.append(dispatcher_task)
        if watch_task is not None:
            watch_task.cancel()
            tasks_to_await.append(watch_task)
        if fleet_task is not None:
            fleet_task.cancel()
            tasks_to_await.append(fleet_task)
        if api_server is not None and api_task is not None:
            api_server.should_exit = True
            tasks_to_await.append(api_task)
        await asyncio.gather(*tasks_to_await, return_exceptions=True)
        if dispatcher is not None:
            await dispatcher.stop()
        if opcua_server is not None:
            await opcua_server.stop()
        await supervisor.stop_all()
        cold_store.close()
        logger.info("xedge.stopped")

    return 0


def run(argv: list[str] | None = None) -> int:
    """Synchronous entrypoint (registered as the `xedge` console script)."""
    args = parse_args(argv)
    return asyncio.run(async_main(args.config, args.schema))


if __name__ == "__main__":
    sys.exit(run())
