"""Remote diagnostic WebSocket server (Sprint 17, XEDGE-133/134/135/137/138).

One JSON request/response per message over `wss://<host>/ws/diagnostics`
(`ws://` when TLS is disabled, matching the rest of the app) — a persistent
connection rather than a REST call per command, since both consumers
(`xedge-cli`, the Web UI's embedded console) issue a back-and-forth stream
of commands against one session.

Auth reuses the same session cookie as the rest of the Web UI/REST API
(`xedge.api.auth.resolve_session` — Starlette's `WebSocket` shares the same
cookie-bearing `HTTPConnection` base as `Request`, so no adapter is
needed). Identity resolves once at connect time; an unauthenticated socket
is rejected with `close(code=1008)` before ever being `accept()`-ed. Each
subsequent command is permission-checked individually — one socket may run
several commands needing different permissions over its lifetime — and
audit-logged via the same `AuditLog` the RBAC/Audit Log phases built,
tagged with a per-connection session id (XEDGE-138's "session ID"
requirement) rather than a persistent cross-reconnect identifier.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, WebSocket
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketDisconnect

from xedge import __version__
from xedge.api.auth import SessionManager, UserStore, resolve_session
from xedge.api.permissions import has_permission
from xedge.core.config import ConfigValidationError, ConfigValidator, ConfigVersionHistory
from xedge.core.driver_config import build_driver_config
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverSupervisor
from xedge.drivers.base import DriverConfig, Quality, TagUpdate
from xedge.drivers.loopback.driver import LoopbackDriver
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.observability.audit_log import AuditLog
from xedge.observability.logging import get_log_ring_buffer, get_logger
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferManager
from xedge.store.sqlite_store import SqliteColdStore

logger = get_logger(__name__)

_SELFTEST_STREAM_KEY = "__selftest__"


class DiagnosticCommandError(Exception):
    """Raised by a command handler for an expected, reportable failure
    (bad args, unknown driver id, validation failure) — turned into an
    `{"status": "error", ...}` response, never a raw traceback."""


def _required_permission(command: str, args: list[Any]) -> str | None:
    """None means "unknown command" (a protocol error, not a permission
    question)."""
    if command == "status":
        return "tag:read"
    if command == "driver":
        sub = args[0] if args else None
        if sub == "restart":
            return "driver:restart"
        if sub in ("list", "logs"):
            return "tag:read"
        return None
    if command == "tag":
        return "tag:read" if args and args[0] == "read" else None
    if command == "store-forward":
        return "tag:read" if args and args[0] == "status" else None
    if command == "network":
        return "tag:read" if args and args[0] == "check" else None
    if command == "config":
        return "config:read" if args and args[0] in ("show", "validate") else None
    if command == "self-test":
        return "diagnostics:run"
    if command == "compliance":
        return "audit:read" if args and args[0] == "cip-007" else None
    return None


def create_diagnostics_router(
    supervisor: DriverSupervisor,
    dispatcher: NorthboundDispatcher | None,
    ring_buffers: RingBufferManager,
    cold_store: SqliteColdStore | None,
    latest_values: LatestValueStore,
    version_history: ConfigVersionHistory,
    *,
    session_manager: SessionManager,
    user_store: UserStore,
    audit_log: AuditLog,
    config_path: Path,
    core_schema_path: Path,
) -> APIRouter:
    router = APIRouter()
    core_validator = ConfigValidator.from_file(core_schema_path)

    async def _dispatch(command: str, args: list[Any]) -> Any:
        if command == "status":
            return _status()
        if command == "driver":
            sub, *rest = args
            if sub == "list":
                return _driver_list()
            if sub == "logs":
                return _driver_logs(rest)
            if sub == "restart":
                return await _driver_restart(rest)
        if command == "tag":
            return _tag_read(args[1:])
        if command == "store-forward":
            return _store_forward_status()
        if command == "network":
            return _network_check()
        if command == "config":
            sub = args[0]
            if sub == "show":
                return _config_show()
            if sub == "validate":
                return _config_validate()
        if command == "self-test":
            return await _self_test()
        if command == "compliance":
            sub = args[0]
            if sub == "cip-007":
                return _compliance_cip_007()
        raise DiagnosticCommandError(f"Unknown command {command!r}")

    def _status() -> dict[str, Any]:
        return {
            "driver_count": len(supervisor.all_status()),
            "northbound_connected": dispatcher.connected if dispatcher is not None else None,
        }

    def _driver_list() -> list[dict[str, Any]]:
        return [
            {
                "instance_id": s.instance_id,
                "driver_type": s.driver_type,
                "state": s.state.value,
                "consecutive_failures": s.consecutive_failures,
                "last_error": s.last_error,
                "metrics": asdict(s.metrics),
            }
            for s in supervisor.all_status().values()
        ]

    def _driver_logs(args: list[Any]) -> list[dict[str, Any]]:
        if not args:
            raise DiagnosticCommandError("Usage: driver logs <instance_id>")
        instance_id = args[0]
        limit = int(args[1]) if len(args) > 1 else 200
        return get_log_ring_buffer().tail(instance_id=instance_id, limit=limit)

    async def _driver_restart(args: list[Any]) -> dict[str, str]:
        if not args:
            raise DiagnosticCommandError("Usage: driver restart <instance_id>")
        instance_id = args[0]
        if instance_id not in supervisor.all_status():
            raise DiagnosticCommandError(f"No driver instance {instance_id!r}")
        entry = _find_driver_entry(instance_id)
        try:
            driver_config = build_driver_config(entry)
        except ConfigValidationError as exc:
            raise DiagnosticCommandError(f"Cannot restart: {exc}") from exc
        await supervisor.stop(instance_id)
        supervisor.start(driver_config)
        logger.info("diagnostic.driver_restarted", instance_id=instance_id)
        return {"instance_id": instance_id, "status": "restarted"}

    def _find_driver_entry(instance_id: str) -> dict[str, Any]:
        config = _full_config()
        for entry in config.get("drivers", []):
            if entry.get("id") == instance_id:
                found: dict[str, Any] = entry
                return found
        raise DiagnosticCommandError(f"No driver {instance_id!r} in current config")

    def _tag_read(args: list[Any]) -> dict[str, Any]:
        if not args:
            raise DiagnosticCommandError("Usage: tag read <tag_id>")
        tag_id = args[0]
        tag = latest_values.get(tag_id)
        if tag is None:
            raise DiagnosticCommandError(f"No known value for tag {tag_id!r}")
        return {
            "tag_id": tag.tag_id,
            "value": tag.value,
            "quality": tag.quality.value,
            "timestamp": tag.timestamp.isoformat(),
        }

    def _store_forward_status() -> dict[str, Any]:
        streams: dict[str, Any] = {}
        for stream_key in ring_buffers.stream_keys():
            ring_metrics = ring_buffers.metrics(stream_key)
            streams[stream_key] = {
                "depth": ring_metrics.depth if ring_metrics else 0,
                "max_depth": ring_metrics.max_depth if ring_metrics else 0,
                "evicted_count": ring_metrics.evicted_count if ring_metrics else 0,
                "cold_backlog": cold_store.count(stream_key) if cold_store is not None else None,
            }
        return {
            "streams": streams,
            "northbound_connected": dispatcher.connected if dispatcher is not None else None,
            "northbound_metrics": (
                asdict(dispatcher.get_metrics()) if dispatcher is not None else None
            ),
        }

    def _network_check() -> dict[str, Any]:
        drivers = {
            s.instance_id: {"state": s.state.value, "consecutive_failures": s.consecutive_failures}
            for s in supervisor.all_status().values()
        }
        northbound: dict[str, Any] | None = None
        if dispatcher is not None:
            northbound = {"connected": dispatcher.connected, "is_alive": dispatcher.is_alive()}
        return {"drivers": drivers, "northbound": northbound}

    def _full_config() -> dict[str, Any]:
        if not config_path.is_file():
            return {"schema_version": "0.1"}
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {"schema_version": "0.1"}

    def _config_show() -> dict[str, Any]:
        versions = version_history.list_versions()
        if not versions:
            return {}
        return version_history.load_version(versions[-1])

    def _config_validate() -> dict[str, Any]:
        config = _full_config()
        errors: list[str] = []
        try:
            core_validator.validate(config)
        except ConfigValidationError as exc:
            errors.append(str(exc))
        for entry in config.get("drivers", []):
            try:
                build_driver_config(entry)
            except ConfigValidationError as exc:
                errors.append(f"driver {entry.get('id', '?')!r}: {exc}")
        return {"valid": not errors, "errors": errors}

    def _compliance_cip_007() -> dict[str, Any]:
        """NERC CIP-007 (Systems Security Management) evidence export
        (Sprint 18, XEDGE-143) — reuses AuditLog entirely (already
        durable, per-username, tamper-evident) rather than adding new
        tracking; "patch history" is honestly the current installed
        version/dependency snapshot, not a fabricated upgrade log — there
        is no real OTA/upgrade-history system yet (tracked in the SL-1 gap
        analysis, not silently glossed over here)."""
        failed_logins = audit_log.tail(event="auth.login_failure", limit=1000)
        successful_logins = audit_log.tail(event="auth.login_success", limit=1000)
        dependency_versions: dict[str, str | None] = {}
        for package in ("fastapi", "uvicorn", "cryptography", "bcrypt", "jsonschema", "pyyaml"):
            try:
                dependency_versions[package] = version(package)
            except PackageNotFoundError:
                dependency_versions[package] = None
        return {
            "failed_logins": failed_logins,
            "successful_logins": successful_logins,
            "xedge_version": __version__,
            "dependency_versions": dependency_versions,
        }

    async def _self_test() -> dict[str, Any]:
        loopback = await _self_test_loopback()
        store = await _self_test_store()
        northbound = _self_test_northbound()
        overall = loopback["passed"] and store["passed"] and northbound.get("passed", True)
        return {"passed": overall, "loopback": loopback, "store": store, "northbound": northbound}

    async def _self_test_loopback() -> dict[str, Any]:
        driver = LoopbackDriver()
        config = DriverConfig(
            instance_id="selftest",
            driver_type="loopback",
            config={},
            tag_groups=[
                {"id": "g1", "scan_rate_ms": 100, "tags": [{"id": "echo", "initial_value": 1}]}
            ],
        )
        started_at = datetime.now(UTC)
        try:
            await driver.configure(config)
            await driver.connect()
            queue: asyncio.Queue[TagUpdate] = asyncio.Queue()
            task = asyncio.create_task(driver.run(queue))
            try:
                first = await asyncio.wait_for(queue.get(), timeout=2.0)
                if first.value != 1:
                    return {"passed": False, "error": f"unexpected initial value {first.value!r}"}
                write_result = await driver.write("echo", 2)
                if not write_result.success:
                    return {"passed": False, "error": "write() reported failure"}
                second = await asyncio.wait_for(queue.get(), timeout=2.0)
                if second.value != 2:
                    return {
                        "passed": False,
                        "error": f"write did not round-trip: got {second.value!r}",
                    }
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await driver.disconnect()
        except Exception as exc:  # noqa: BLE001 — a self-test failure is a result, not a crash
            return {"passed": False, "error": str(exc)}
        latency_ms = (datetime.now(UTC) - started_at).total_seconds() * 1000
        return {"passed": True, "latency_ms": round(latency_ms, 2)}

    async def _self_test_store() -> dict[str, Any]:
        tag = UnifiedTag(
            tag_id=f"{_SELFTEST_STREAM_KEY}/probe",
            timestamp=datetime.now(UTC),
            value=1,
            data_type="int",
            quality=Quality.GOOD,
            source_driver=_SELFTEST_STREAM_KEY,
            source_address="probe",
        )
        ring_buffers.push(_SELFTEST_STREAM_KEY, tag)
        drained = ring_buffers.drain(_SELFTEST_STREAM_KEY, max_items=1)
        if not drained or drained[0].tag_id != tag.tag_id:
            return {"passed": False, "error": "ring buffer round-trip failed"}
        if cold_store is None:
            return {"passed": True, "cold_tier": "not configured"}
        cold_store.append(_SELFTEST_STREAM_KEY, tag)
        rows = cold_store.peek(_SELFTEST_STREAM_KEY, 1)
        if not rows or rows[0][1].tag_id != tag.tag_id:
            return {"passed": False, "error": "cold store round-trip failed"}
        cold_store.delete_ids(_SELFTEST_STREAM_KEY, [rows[0][0]])
        return {"passed": True, "cold_tier": "ok"}

    def _self_test_northbound() -> dict[str, Any]:
        if dispatcher is None:
            return {"configured": False}
        return {
            "configured": True,
            "connected": dispatcher.connected,
            "is_alive": dispatcher.is_alive(),
        }

    @router.websocket("/ws/diagnostics")
    async def diagnostics_ws(websocket: WebSocket) -> None:
        session = resolve_session(websocket, session_manager)
        if session is None:
            await websocket.close(code=1008)
            return
        _new_token, username = session
        role = user_store.get_role(username)
        await websocket.accept()
        session_id = uuid.uuid4().hex[:12]
        try:
            while True:
                try:
                    message = await websocket.receive_json()
                except WebSocketDisconnect:
                    break
                except ValueError:
                    await websocket.send_json({"status": "error", "message": "Malformed JSON"})
                    continue
                command = message.get("command", "")
                args = message.get("args", []) or []
                permission = _required_permission(command, args)
                if permission is None:
                    await websocket.send_json(
                        {"status": "error", "message": f"Unknown command {command!r}"}
                    )
                    continue
                if not has_permission(role, permission):
                    audit_log.append(
                        username,
                        "diagnostic.command",
                        {
                            "session_id": session_id,
                            "command": command,
                            "args": args,
                            "result": "denied",
                        },
                    )
                    await websocket.send_json(
                        {"status": "error", "message": "Insufficient permission"}
                    )
                    continue
                try:
                    result = await _dispatch(command, args)
                except DiagnosticCommandError as exc:
                    audit_log.append(
                        username,
                        "diagnostic.command",
                        {
                            "session_id": session_id,
                            "command": command,
                            "args": args,
                            "result": "error",
                        },
                    )
                    await websocket.send_json({"status": "error", "message": str(exc)})
                    continue
                audit_log.append(
                    username,
                    "diagnostic.command",
                    {"session_id": session_id, "command": command, "args": args, "result": "ok"},
                )
                # jsonable_encoder, not Starlette's send_json's plain
                # json.dumps: command results can carry datetime values
                # (e.g. DriverMetrics.last_successful_read via asdict()),
                # which plain json.dumps can't serialize — confirmed live,
                # this crashed the connection on a real "driver list" call
                # against actually-running drivers.
                await websocket.send_json(jsonable_encoder({"status": "ok", "result": result}))
        except WebSocketDisconnect:
            pass

    return router
