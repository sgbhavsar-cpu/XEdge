"""REST API v1 (FR-CM-003 / FR-OB-* / FR-WU-*): status, monitoring, and
full read-write configuration, backing both direct API consumers and the
local Web UI (xedge.api.ui, ADR-007).

Endpoints: `/health` (liveness, always unauthenticated), `/api/v1/status`,
`/api/v1/drivers`, `GET /api/v1/config` (secrets-safe, from version
history), `PUT /api/v1/config` (validate + write — the existing hot-reload
watcher applies it, so every property already established for hot-reload
applies automatically: version history, rollback, restart-only-affected-
drivers), `/api/v1/auth/*`, and `/api/v1/users*` (multi-user RBAC, Sprint 14
— see `xedge.api.permissions` for the role/permission matrix).

`xedge.core.main` binds this to loopback (127.0.0.1) by default — load-
bearing since this API is write-capable.

`GET /api/v1/config` returns the latest saved config *version*
(ConfigVersionHistory), not `store.data` directly — the version history is
captured before secrets substitution, so this is the one place in the
codebase already guaranteed never to contain a resolved plaintext secret.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from serial.tools import list_ports

from xedge import __version__
from xedge.api.auth import SESSION_COOKIE_NAME as _SESSION_COOKIE_NAME
from xedge.api.auth import (
    AuthError,
    LoginAttemptTracker,
    SessionManager,
    UserStore,
    resolve_session,
)
from xedge.api.config_ui import create_config_ui_router
from xedge.api.diagnostics import create_diagnostics_router
from xedge.api.permissions import ROLE_PERMISSIONS, has_permission
from xedge.api.rate_limit import RateLimitMiddleware
from xedge.api.ui import create_ui_router
from xedge.core.alarms import AlarmEngine
from xedge.core.config import ConfigValidationError, ConfigValidator, ConfigVersionHistory
from xedge.core.connectivity import ConnectivityState
from xedge.core.driver_config import build_driver_config
from xedge.core.sntp import SntpSyncStatus
from xedge.core.supervisor import DriverState, DriverSupervisor
from xedge.core.write_router import WriteRouter
from xedge.drivers.base import DriverMetrics
from xedge.fleet.agent import FleetAgentStatus
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.observability.audit_log import AuditLog
from xedge.observability.logging import get_log_ring_buffer, get_logger
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferManager
from xedge.store.sqlite_store import SqliteColdStore

_STATIC_DIR = Path(__file__).parent / "static"
logger = get_logger(__name__)


class _PasswordBody(BaseModel):
    password: str


class _LoginBody(BaseModel):
    # Defaults to the account first-login setup always creates — every
    # existing password-only login request keeps authenticating as that
    # one account, unmodified; a username only needs to be sent once
    # additional accounts exist (see /api/v1/users).
    username: str = "admin"
    password: str


class _CreateUserBody(BaseModel):
    username: str
    password: str
    role: str


class _RoleBody(BaseModel):
    role: str


class _DriverValidateBody(BaseModel):
    """Sprint 25, XEDGE-187 — a proposed single-driver config to dry-run
    validate, same shape as one `drivers[]` entry minus `id`/`enabled`
    (the instance id comes from the URL; enabled state is irrelevant to
    whether the config itself is valid)."""

    type: str
    config: dict[str, Any] = {}
    tag_groups: list[dict[str, Any]] = []


class _AlarmShelveBody(BaseModel):
    duration_seconds: float = 3600


class _TagWriteBody(BaseModel):
    """Sprint 31, XEDGE-223 write-back — `bytes` (part of the broader
    TagValue union every driver's `write()` accepts) is deliberately
    excluded here: it has no natural JSON representation, and no driver
    tag is configured as a bytes type today."""

    value: bool | int | float | str


def create_app(
    supervisor: DriverSupervisor,
    version_history: ConfigVersionHistory,
    dispatcher: NorthboundDispatcher | None = None,
    *,
    user_store: UserStore,
    session_manager: SessionManager,
    login_tracker: LoginAttemptTracker,
    config_path: Path,
    schema_path: Path,
    latest_values: LatestValueStore,
    audit_log: AuditLog,
    ring_buffers: RingBufferManager,
    cold_store: SqliteColdStore | None = None,
    secure_cookies: bool = False,
    dashboard_url: str | None = None,
    rate_limit_enabled: bool = True,
    requests_per_minute: int = 100,
    fleet_status: FleetAgentStatus | None = None,
    alarm_engine: AlarmEngine | None = None,
    sntp_status: SntpSyncStatus | None = None,
) -> FastAPI:
    app = FastAPI(title="xEdge API", version=__version__)
    started_at = datetime.now(UTC)
    validator = ConfigValidator.from_file(schema_path)
    write_router = WriteRouter(supervisor, audit_log)

    if rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware, audit_log=audit_log, requests_per_minute=requests_per_minute
        )

    def require_permission(permission: str) -> Callable[[Request, Response], str]:
        """Build a FastAPI dependency that enforces `permission` and returns
        the authenticated username."""

        def dependency(request: Request, response: Response) -> str:
            session = resolve_session(request, session_manager)
            if session is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
            new_token, username = session
            role = user_store.get_role(username)
            if not has_permission(role, permission):
                logger.warning(
                    "auth.permission_denied", username=username, role=role, permission=permission
                )
                audit_log.append(
                    username, "auth.permission_denied", {"role": role, "permission": permission}
                )
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permission")
            # Sliding idle timeout: every authenticated request extends the
            # session, matching FR-WU-007's "idle timeout" (not a fixed
            # absolute expiry from login).
            response.set_cookie(
                _SESSION_COOKIE_NAME,
                new_token,
                httponly=True,
                samesite="strict",
                # Driven by the `tls` config section (Sprint 13,
                # XEDGE-107/280) via xedge.core.main — True once the server
                # is actually listening over HTTPS, per ADR-007 /
                # system-architecture.md §6.1's documented port note.
                secure=secure_cookies,
            )
            return username

        return dependency

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def get_prometheus_metrics() -> Response:
        # Unauthenticated, like /health: a real Prometheus scraper can't do
        # cookie-based session auth, and the loopback-only default bind is
        # the same security boundary /health already relies on.
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/auth/status")
    def auth_status(request: Request) -> dict[str, bool]:
        return {
            "account_exists": user_store.exists(),
            "authenticated": resolve_session(request, session_manager) is not None,
        }

    @app.post("/api/v1/auth/setup")
    def auth_setup(body: _PasswordBody, response: Response) -> dict[str, str]:
        if user_store.exists():
            raise HTTPException(
                status.HTTP_409_CONFLICT, "An account already exists; use login instead"
            )
        user_store.create(body.password)
        token = session_manager.issue("admin")
        response.set_cookie(
            _SESSION_COOKIE_NAME, token, httponly=True, samesite="strict", secure=secure_cookies
        )
        audit_log.append("admin", "auth.setup")
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    def auth_login(body: _LoginBody, request: Request, response: Response) -> dict[str, str]:
        client_ip = request.client.host if request.client else None
        if login_tracker.is_locked_out():
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many failed login attempts; try again later",
            )
        if not user_store.verify(body.username, body.password):
            login_tracker.record_failure()
            audit_log.append(body.username, "auth.login_failure", {"ip": client_ip})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
        login_tracker.record_success()
        token = session_manager.issue(body.username)
        response.set_cookie(
            _SESSION_COOKIE_NAME, token, httponly=True, samesite="strict", secure=secure_cookies
        )
        audit_log.append(body.username, "auth.login_success", {"ip": client_ip})
        return {"status": "ok"}

    @app.post("/api/v1/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, str]:
        # Deliberately not behind require_auth: clearing your own cookie
        # needs no server-side permission check, and gating it on
        # require_auth previously caused a real bug — require_auth's
        # sliding-timeout cookie *refresh* raced with this handler's
        # delete on the same response, and the refresh won.
        session = resolve_session(request, session_manager)
        if session is not None:
            audit_log.append(session[1], "auth.logout")
        response.delete_cookie(_SESSION_COOKIE_NAME)
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def get_status(_user: str = Depends(require_permission("tag:read"))) -> dict[str, Any]:
        return {
            "version": __version__,
            "started_at": started_at.isoformat(),
            "uptime_seconds": (datetime.now(UTC) - started_at).total_seconds(),
            "driver_count": len(supervisor.all_status()),
            "northbound_connected": dispatcher.connected if dispatcher is not None else None,
        }

    @app.post("/api/v1/northbound/republish")
    def republish_northbound(
        user: str = Depends(require_permission("northbound:publish")),
    ) -> dict[str, Any]:
        """Sprint C5, XEDGE-452 (CRD §4.10 "manual republish"): wakes the
        dispatcher immediately rather than waiting out the rest of
        `publish_interval_seconds` — see `NorthboundDispatcher.
        trigger_publish()`. Does not publish synchronously itself, and
        `queued` reflects only whether a dispatcher exists to signal, not
        whether the following publish actually succeeds."""
        if dispatcher is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Northbound is not configured")
        dispatcher.trigger_publish()
        audit_log.append(user, "northbound.republish_triggered")
        return {"queued": True}

    @app.get("/api/v1/drivers")
    def get_drivers(_user: str = Depends(require_permission("tag:read"))) -> list[dict[str, Any]]:
        return [
            {
                "instance_id": status_.instance_id,
                "driver_type": status_.driver_type,
                "state": status_.state.value,
                "consecutive_failures": status_.consecutive_failures,
                "last_error": status_.last_error,
                "metrics": asdict(status_.metrics),
                "connectivity_state": status_.connectivity_state.value,
            }
            for status_ in supervisor.all_status().values()
        ]

    @app.get("/api/v1/drivers/{instance_id}/tags")
    def get_driver_tags(
        instance_id: str, _user: str = Depends(require_permission("tag:read"))
    ) -> dict[str, Any]:
        if instance_id not in supervisor.all_status():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No driver instance {instance_id!r}")
        # Synthetic per-instance health/statistics tags (docs/planning/pendingtasks.md
        # — "Driver system tags", built by xedge.core.system_tags) live under
        # this instance's `_system/` sub-namespace; split them out of the real
        # tag list into their own flat {name: value} block for the driver-
        # detail page's "Driver Health" card, rather than mixing them into
        # the ordinary tag table.
        system_prefix = f"{instance_id}/_system/"
        tags: list[dict[str, Any]] = []
        system: dict[str, Any] = {}
        for tag in latest_values.for_driver(instance_id):
            if tag.tag_id.startswith(system_prefix):
                system[tag.tag_id[len(system_prefix) :]] = tag.value
            else:
                tags.append(
                    {
                        "tag_id": tag.tag_id,
                        "value": tag.value,
                        "quality": tag.quality.value,
                        "timestamp": tag.timestamp.isoformat(),
                        "source_address": tag.source_address,
                        "engineering_unit": tag.engineering_unit,
                        # Human-readable Modbus exception name (Sprint C1,
                        # XEDGE-425) for a Bad-quality tag — before this,
                        # only the raw numeric exception code ever reached
                        # the operator. None for driver types that don't set
                        # it, and for Good-quality tags.
                        "detail": tag.metadata.get("modbus_exception_name"),
                    }
                )
        return {"tags": tags, "system": system}

    @app.post("/api/v1/drivers/{instance_id}/tags/{tag_name}/write")
    async def write_tag(
        instance_id: str,
        tag_name: str,
        body: _TagWriteBody,
        user: str = Depends(require_permission("tag:write")),
    ) -> dict[str, Any]:
        """FR-NB-009 write-back (Sprint 31, XEDGE-223) — the direct REST
        path; the MQTT Sparkplug NCMD path (xedge.northbound.mqtt) goes
        through the same WriteRouter and gets the same audit trail."""
        result = await write_router.write(user, instance_id, tag_name, body.value)
        if not result.success:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, result.error_message)
        return {"tag_id": result.tag_id, "success": True}

    def _full_driver_config() -> dict[str, Any]:
        """Read the config directly from the file the forms/endpoints write
        to — not from ConfigVersionHistory, which is only populated
        asynchronously by hot-reload's poll loop (same reasoning as
        xedge.api.config_ui's `_full_config` and xedge.api.diagnostics'
        own copy of this same small helper)."""
        if not config_path.is_file():
            return {"schema_version": "0.1"}
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {"schema_version": "0.1"}

    def _find_driver_entry(config: dict[str, Any], instance_id: str) -> dict[str, Any]:
        for entry in config.get("drivers", []):
            if entry.get("id") == instance_id:
                found: dict[str, Any] = entry
                return found
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No driver {instance_id!r} in config")

    @app.get("/api/v1/drivers/{instance_id}/health")
    def get_driver_health(
        instance_id: str, _user: str = Depends(require_permission("tag:read"))
    ) -> dict[str, Any]:
        all_status = supervisor.all_status()
        if instance_id in all_status:
            status_ = all_status[instance_id]
            last_read = status_.metrics.last_successful_read
            last_read_age_seconds = (
                (datetime.now(UTC) - last_read).total_seconds() if last_read is not None else None
            )
            return {
                "instance_id": status_.instance_id,
                "driver_type": status_.driver_type,
                "state": status_.state.value,
                "state_changed_at": status_.state_changed_at.isoformat(),
                "tag_count": status_.tag_count,
                "consecutive_failures": status_.consecutive_failures,
                "last_error": status_.last_error,
                "metrics": asdict(status_.metrics),
                "last_read_age_seconds": last_read_age_seconds,
                # Device-level connectivity (Sprint C2, XEDGE-420/421) —
                # distinct from `state` above; see xedge.core.connectivity.
                "connectivity_state": status_.connectivity_state.value,
            }
        # Never started (e.g. disabled since before this process booted) —
        # the supervisor has no status to report; synthesize one from config
        # rather than a bare 404, since the driver is a real, known entry.
        entry = _find_driver_entry(_full_driver_config(), instance_id)
        return {
            "instance_id": instance_id,
            "driver_type": entry.get("type"),
            "state": "disabled" if not entry.get("enabled", True) else "stopped",
            "state_changed_at": None,
            "tag_count": sum(len(g.get("tags", [])) for g in entry.get("tag_groups", [])),
            "consecutive_failures": 0,
            "last_error": None,
            "metrics": asdict(DriverMetrics()),
            "last_read_age_seconds": None,
            "connectivity_state": ConnectivityState.UNKNOWN.value,
        }

    def _set_driver_enabled(instance_id: str, enabled: bool, user: str) -> dict[str, str]:
        config = _full_driver_config()
        entry = _find_driver_entry(config, instance_id)
        entry["enabled"] = enabled
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        audit_log.append(
            user, "driver.enabled" if enabled else "driver.disabled", {"instance_id": instance_id}
        )
        if enabled:
            try:
                driver_config = build_driver_config(entry)
            except ConfigValidationError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
            try:
                supervisor.start(driver_config)
            except ValueError:
                pass  # already running — enabling an already-enabled driver is a no-op
        return {"instance_id": instance_id, "enabled": str(enabled).lower()}

    @app.post("/api/v1/drivers/{instance_id}/disable")
    async def disable_driver(
        instance_id: str, user: str = Depends(require_permission("driver:restart"))
    ) -> dict[str, str]:
        result = _set_driver_enabled(instance_id, False, user)
        await supervisor.disable(instance_id)
        return result

    @app.post("/api/v1/drivers/{instance_id}/enable")
    async def enable_driver(
        instance_id: str, user: str = Depends(require_permission("driver:restart"))
    ) -> dict[str, str]:
        # Must be async (not a plain `def`): FastAPI dispatches sync route
        # handlers to a worker thread pool, and `supervisor.start()` calls
        # `asyncio.ensure_future()`, which needs a running loop *in the
        # calling thread* — confirmed live, a sync `def` here raised
        # "no current event loop" from the thread pool worker.
        return _set_driver_enabled(instance_id, True, user)

    @app.post("/api/v1/drivers/{instance_id}/validate")
    def validate_driver(
        instance_id: str,
        body: _DriverValidateBody,
        _user: str = Depends(require_permission("config:read")),
    ) -> dict[str, Any]:
        """Dry-run only (Sprint 25, XEDGE-187) — no file write, no
        supervisor effect, regardless of the outcome."""
        entry = {
            "id": instance_id,
            "type": body.type,
            "config": body.config,
            "tag_groups": body.tag_groups,
        }
        try:
            build_driver_config(entry)
        except ConfigValidationError as exc:
            return {"valid": False, "errors": [str(exc)]}
        return {"valid": True, "errors": []}

    @app.post("/api/v1/drivers/{instance_id}/tag-groups/{group_id}/poll")
    async def poll_tag_group_now(
        instance_id: str, group_id: str, _user: str = Depends(require_permission("tag:read"))
    ) -> dict[str, Any]:
        """Trigger an immediate read of an `on_demand` tag group (XEDGE-435,
        ADR-011 Part 2) — the REST side of `poll_now()` on the live driver
        instance. Gated on `tag:read`, matching the permission that already
        governs seeing this data at all: this endpoint changes *when* a read
        happens, not what an authorized caller may already read or write.
        """
        try:
            status_ = supervisor.status(instance_id)
        except KeyError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No driver instance {instance_id!r}"
            ) from None
        if status_.state != DriverState.RUNNING:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Driver instance {instance_id!r} is not running (state: {status_.state.value})",
            )
        driver = supervisor.get_driver(instance_id)
        poll_now = getattr(driver, "poll_now", None) if driver is not None else None
        if poll_now is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Driver instance {instance_id!r} does not support on-demand polling",
            )
        triggered = await poll_now(group_id)
        if not triggered:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No on-demand tag group {group_id!r} on driver instance {instance_id!r}",
            )
        return {"instance_id": instance_id, "tag_group": group_id, "triggered": True}

    @app.get("/api/v1/logs")
    def get_logs(
        since_seq: int = 0,
        instance_id: str | None = None,
        source: str | None = None,
        limit: int = 200,
        _user: str = Depends(require_permission("tag:read")),
    ) -> list[dict[str, Any]]:
        return get_log_ring_buffer().tail(
            since_seq=since_seq, instance_id=instance_id, source=source, limit=limit
        )

    @app.get("/api/v1/serial-ports")
    def get_serial_ports(
        _user: str = Depends(require_permission("config:read")),
    ) -> list[str]:
        """Serial device paths physically present on this host right now
        (XEDGE-434) — backs the `port` field's suggestion list on the
        Modbus RTU serial driver form (`x-suggestions-endpoint`, see
        xedge.api.schema_forms). `pyserial` (already a dependency via
        pyserial-asyncio) does the actual OS-specific enumeration; no
        detected list is ever authoritative here, only a convenience — the
        field stays a free-text input an operator can fill in by hand for
        a port this pass doesn't find (a mount that appears later, an
        unusual adapter).
        """
        return sorted(port.device for port in list_ports.comports())

    @app.get("/api/v1/config")
    def get_config(_user: str = Depends(require_permission("config:read"))) -> dict[str, Any]:
        versions = version_history.list_versions()
        if not versions:
            return {}
        return version_history.load_version(versions[-1])

    @app.put("/api/v1/config")
    def put_config(
        new_config: dict[str, Any], user: str = Depends(require_permission("config:write"))
    ) -> dict[str, str]:
        """Validate, then write to the same file the hot-reload watcher
        already polls (FR-WU-005) — no separate UI-only mutation path.
        Secrets: the caller is responsible for sending `${SECRET:name}`
        placeholders back for any field it didn't intend to change (the UI
        never round-trips resolved secret values — see xedge.api.ui)."""
        try:
            validator.validate(new_config)
        except ConfigValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        config_path.write_text(yaml.safe_dump(new_config, sort_keys=False), encoding="utf-8")
        audit_log.append(user, "config.write")
        return {"status": "accepted"}

    @app.get("/api/v1/users")
    def list_users(_user: str = Depends(require_permission("user:manage"))) -> list[dict[str, str]]:
        return user_store.list_users()

    @app.post("/api/v1/users", status_code=status.HTTP_201_CREATED)
    def create_user(
        body: _CreateUserBody, user: str = Depends(require_permission("user:manage"))
    ) -> dict[str, str]:
        try:
            user_store.create_user(body.username, body.password, body.role)
        except AuthError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        audit_log.append(user, "user.created", {"username": body.username, "role": body.role})
        return {"status": "created"}

    @app.post("/api/v1/users/{username}/role")
    def set_user_role(
        username: str, body: _RoleBody, user: str = Depends(require_permission("user:manage"))
    ) -> dict[str, str]:
        try:
            user_store.set_role(username, body.role)
        except AuthError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        audit_log.append(user, "user.role_changed", {"username": username, "role": body.role})
        return {"status": "updated"}

    @app.delete("/api/v1/users/{username}")
    def delete_user(
        username: str, current_user: str = Depends(require_permission("user:manage"))
    ) -> dict[str, str]:
        if username == current_user:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete your own account")
        try:
            user_store.delete_user(username)
        except AuthError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        audit_log.append(current_user, "user.deleted", {"username": username})
        return {"status": "deleted"}

    @app.get("/api/v1/roles")
    def list_roles(_user: str = Depends(require_permission("user:manage"))) -> list[str]:
        return sorted(ROLE_PERMISSIONS.keys())

    @app.get("/api/v1/fleet/status")
    def get_fleet_status(_user: str = Depends(require_permission("tag:read"))) -> dict[str, Any]:
        """Live status of this device's own fleet agent (Sprint 29,
        XEDGE-296) — distinct from the separate Fleet Manager service's own
        `/api/v1/fleet/devices/*` endpoints, which this process never
        calls into."""
        if fleet_status is None:
            return {"enabled": False}
        return {
            "enabled": fleet_status.enabled,
            "manager_url": fleet_status.manager_url,
            "device_id": fleet_status.device_id,
            "registered": fleet_status.registered,
            "last_heartbeat_at": (
                fleet_status.last_heartbeat_at.isoformat()
                if fleet_status.last_heartbeat_at
                else None
            ),
            "last_heartbeat_ok": fleet_status.last_heartbeat_ok,
            "last_error": fleet_status.last_error,
            "last_config_apply": fleet_status.last_config_apply,
            "connection_state": fleet_status.connection_state.value,
            "cert_not_after": (
                fleet_status.cert_not_after.isoformat() if fleet_status.cert_not_after else None
            ),
        }

    @app.get("/api/v1/sntp/status")
    def get_sntp_status(_user: str = Depends(require_permission("tag:read"))) -> dict[str, Any]:
        """XEDGE-437 (CRD §4.8) sync-status reporting: whether HLR's ASM-001
        assumption (host OS is NTP-synced) is actually holding, from
        xEdge's own independent point of view — which server last answered,
        how large the offset was, and whether it's gone stale."""
        if sntp_status is None:
            return {"enabled": False}
        return {
            "enabled": sntp_status.enabled,
            "servers": sntp_status.servers,
            "sync_interval_seconds": sntp_status.sync_interval_seconds,
            "timezone": sntp_status.timezone,
            "last_sync_at": (
                sntp_status.last_sync_at.isoformat() if sntp_status.last_sync_at else None
            ),
            "last_sync_server": sntp_status.last_sync_server,
            "offset_seconds": sntp_status.offset_seconds,
            "round_trip_delay_seconds": sntp_status.round_trip_delay_seconds,
            "consecutive_failures": sntp_status.consecutive_failures,
            "last_error": sntp_status.last_error,
            "stale": sntp_status.is_stale,
        }

    @app.get("/api/v1/alarms")
    def list_alarms(_user: str = Depends(require_permission("tag:read"))) -> list[dict[str, Any]]:
        """Sprint 31, XEDGE-224/298. Only tags with a configured alarm rule
        ever appear here — a tag's entry persists (in whatever state) once
        the alarm engine has evaluated it at least once, not just while
        currently active, so an operator can see a recently-cleared or
        shelved alarm's history without a separate query."""
        if alarm_engine is None:
            return []
        return [
            {
                "tag_id": status_.tag_id,
                "state": status_.state.value,
                "condition": status_.condition,
                "active_since": status_.active_since.isoformat() if status_.active_since else None,
                "acknowledged_by": status_.acknowledged_by,
                "acknowledged_at": (
                    status_.acknowledged_at.isoformat() if status_.acknowledged_at else None
                ),
                "shelved_until": (
                    status_.shelved_until.isoformat() if status_.shelved_until else None
                ),
                "last_value": status_.last_value,
                "last_rate_per_second": status_.last_rate_per_second,
            }
            for status_ in alarm_engine.all_status().values()
        ]

    @app.post("/api/v1/alarms/{tag_id:path}/acknowledge")
    def acknowledge_alarm(
        tag_id: str, user: str = Depends(require_permission("alarm:manage"))
    ) -> dict[str, Any]:
        if alarm_engine is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Alarm engine is not enabled")
        acknowledged = alarm_engine.acknowledge(tag_id, user)
        if not acknowledged:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{tag_id!r} is not currently an unacknowledged active alarm",
            )
        audit_log.append(user, "alarm.acknowledged", {"tag_id": tag_id})
        return {"tag_id": tag_id, "acknowledged": True}

    @app.post("/api/v1/alarms/{tag_id:path}/shelve")
    def shelve_alarm(
        tag_id: str,
        body: _AlarmShelveBody,
        user: str = Depends(require_permission("alarm:manage")),
    ) -> dict[str, Any]:
        if alarm_engine is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Alarm engine is not enabled")
        alarm_engine.shelve(tag_id, body.duration_seconds)
        audit_log.append(
            user, "alarm.shelved", {"tag_id": tag_id, "duration_seconds": body.duration_seconds}
        )
        return {"tag_id": tag_id, "shelved": True, "duration_seconds": body.duration_seconds}

    @app.post("/api/v1/alarms/{tag_id:path}/unshelve")
    def unshelve_alarm(
        tag_id: str, user: str = Depends(require_permission("alarm:manage"))
    ) -> dict[str, Any]:
        if alarm_engine is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Alarm engine is not enabled")
        unshelved = alarm_engine.unshelve(tag_id)
        if not unshelved:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{tag_id!r} is not shelved")
        audit_log.append(user, "alarm.unshelved", {"tag_id": tag_id})
        return {"tag_id": tag_id, "shelved": False}

    @app.get("/api/v1/audit")
    def get_audit_log(
        since_seq: int = 0,
        actor: str | None = None,
        event: str | None = None,
        limit: int = 200,
        _user: str = Depends(require_permission("audit:read")),
    ) -> list[dict[str, Any]]:
        return audit_log.tail(limit=limit, actor=actor, event=event, since_seq=since_seq)

    app.mount("/ui-static", StaticFiles(directory=str(_STATIC_DIR)), name="ui-static")
    app.include_router(
        create_ui_router(
            supervisor,
            version_history,
            dispatcher,
            user_store=user_store,
            session_manager=session_manager,
            login_tracker=login_tracker,
            audit_log=audit_log,
            secure_cookies=secure_cookies,
            dashboard_url=dashboard_url,
            fleet_status=fleet_status,
            alarm_engine=alarm_engine,
            sntp_status=sntp_status,
        )
    )
    app.include_router(
        create_config_ui_router(
            session_manager=session_manager,
            user_store=user_store,
            audit_log=audit_log,
            config_path=config_path,
            core_schema_path=schema_path,
            dashboard_url=dashboard_url,
        )
    )
    app.include_router(
        create_diagnostics_router(
            supervisor,
            dispatcher,
            ring_buffers,
            cold_store,
            latest_values,
            version_history,
            session_manager=session_manager,
            user_store=user_store,
            audit_log=audit_log,
            config_path=config_path,
            core_schema_path=schema_path,
        )
    )

    return app
