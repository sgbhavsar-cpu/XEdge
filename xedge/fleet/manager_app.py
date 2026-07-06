"""Fleet Manager REST API (Sprint 29, XEDGE-211/213): a separate,
standalone service from the per-device xEdge process — it never imports
`xedge.core`/`xedge.drivers`, only `xedge.fleet.registry`, matching Sprint
32's documented split ("the two share no code").

Auth: two distinct bearer tokens, never the same value.
  - `join_token`: presented once by a device at `/register` (the manager's
    own admission-control secret — anyone who has it can enroll a device).
  - `admin_token`: presented by an operator/CLI for every other endpoint
    (list/inspect devices, push a config). Both are plain shared secrets
    (ADR-009) — no RBAC/multi-operator model yet, mirroring how the
    per-device Web UI started as a single account before Sprint 14 added
    RBAC.
  - A registered device's own `device_token` (returned by `/register`)
    authenticates its own `/heartbeat` calls only.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from xedge import __version__
from xedge.fleet.registry import DeviceRegistry


class _RegisterBody(BaseModel):
    device_id: str
    display_name: str | None = None
    agent_version: str | None = None
    heartbeat_interval_seconds: float = 60
    join_token: str


class _HeartbeatBody(BaseModel):
    agent_version: str | None = None
    driver_count: int | None = None
    uptime_seconds: float | None = None
    last_config_apply: dict[str, Any] | None = None


class _ConfigPushBody(BaseModel):
    config: dict[str, Any]


def _device_summary(record: Any) -> dict[str, Any]:
    return {
        "device_id": record.device_id,
        "display_name": record.display_name,
        "status": record.status,
        "registered_at": record.registered_at.isoformat(),
        "agent_version": record.agent_version,
        "last_seen_at": record.last_seen_at.isoformat() if record.last_seen_at else None,
        "driver_count": record.driver_count,
        "uptime_seconds": record.uptime_seconds,
        "last_config_apply": record.last_config_apply,
        "has_pending_config": record.has_pending_config,
        "pending_config_version": record.pending_config_version,
    }


def create_fleet_manager_app(
    registry: DeviceRegistry, *, join_token: str, admin_token: str
) -> FastAPI:
    app = FastAPI(title="xEdge Fleet Manager", version=__version__)

    def require_admin(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(_bearer_value(authorization), admin_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing admin token")

    def require_device_token(device_id: str, authorization: str = Header(default="")) -> None:
        if not registry.verify_token(device_id, _bearer_value(authorization)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing device token")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/fleet/register")
    def register(body: _RegisterBody) -> dict[str, str]:
        if not hmac.compare_digest(body.join_token, join_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid join token")
        token = registry.register(
            body.device_id, body.display_name, body.agent_version, body.heartbeat_interval_seconds
        )
        return {"device_token": token}

    @app.post("/api/v1/fleet/devices/{device_id}/heartbeat")
    def heartbeat(
        device_id: str, body: _HeartbeatBody, _auth: None = Depends(require_device_token)
    ) -> dict[str, Any]:
        registry.heartbeat(
            device_id, body.agent_version, body.driver_count, body.uptime_seconds,
            body.last_config_apply,
        )
        pending = registry.take_pending_config(device_id)
        if pending is None:
            return {"pending_config": None, "pending_config_version": None}
        config, version = pending
        return {"pending_config": config, "pending_config_version": version}

    @app.get("/api/v1/fleet/devices")
    def list_devices(_auth: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return [_device_summary(r) for r in registry.list_devices()]

    @app.get("/api/v1/fleet/devices/{device_id}")
    def get_device(device_id: str, _auth: None = Depends(require_admin)) -> dict[str, Any]:
        record = registry.get(device_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return _device_summary(record)

    @app.post("/api/v1/fleet/devices/{device_id}/config", status_code=status.HTTP_202_ACCEPTED)
    def push_config(
        device_id: str, body: _ConfigPushBody, _auth: None = Depends(require_admin)
    ) -> dict[str, Any]:
        """Queues `config` for delivery on the device's next heartbeat
        (XEDGE-213) — not applied synchronously; see ADR-009 for why this
        is a pull, not a push, despite the story title. Returns 202, not
        200: "accepted for delivery," matching the async reality."""
        try:
            version = registry.queue_config(device_id, body.config)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        return {"queued": True, "pending_config_version": version}

    @app.get("/api/v1/fleet/devices/{device_id}/config/status")
    def config_status(device_id: str, _auth: None = Depends(require_admin)) -> dict[str, Any]:
        record = registry.get(device_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such device: {device_id!r}")
        return {
            "has_pending_config": record.has_pending_config,
            "pending_config_version": record.pending_config_version,
            "last_config_apply": record.last_config_apply,
        }

    return app


def _bearer_value(authorization_header: str) -> str:
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        return ""
    return authorization_header[len(prefix) :]
