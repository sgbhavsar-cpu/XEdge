"""Fleet agent (Sprint 29, XEDGE-210/213 — partial scope, see ADR-009):
device-side counterpart to `xedge.fleet.manager_app`. Runs as one more
asyncio task inside `xedge.core.main.async_main`, the same shape as
`system_tag_publish_loop`/`purge_loop`.

Pull, not push (deliberate deviation from the sprint story's "push" title —
see ADR-009): the agent calls the manager on a heartbeat interval, and any
config an operator queued for this device rides along in that response.
This avoids the manager needing inbound network reachability to every
device (many are behind NAT/firewalls with no public address at all),
consistent with `xedge.core.hot_reload`'s existing poll-based config
model. Applying a pending config reuses the exact same file-write path
`xedge.api.server.put_config` already uses — hot-reload's own poll loop
does the actual restart, so this module never touches `DriverSupervisor`
directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from xedge import __version__
from xedge.core.config import ConfigValidationError, ConfigValidator
from xedge.core.supervisor import DriverSupervisor
from xedge.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class FleetAgentConfig:
    manager_url: str
    device_id: str
    join_token: str
    display_name: str | None = None
    heartbeat_interval_seconds: float = 60
    verify_tls: bool = True


@dataclass(slots=True)
class FleetAgentStatus:
    """Live snapshot the Web UI/REST API read (xedge.api.server's
    `GET /api/v1/fleet/status`) — mutated in place by the heartbeat loop, a
    single shared instance rather than a queue/pubsub, matching how
    `xedge.core.supervisor.DriverInstanceStatus` is read directly rather
    than through a channel."""

    enabled: bool = False
    manager_url: str | None = None
    device_id: str | None = None
    registered: bool = False
    last_heartbeat_at: datetime | None = None
    last_heartbeat_ok: bool | None = None
    last_error: str | None = None
    last_config_apply: dict[str, Any] | None = None


def _read_persisted_token(token_path: Path) -> str | None:
    if not token_path.is_file():
        return None
    existing = token_path.read_text(encoding="utf-8").strip()
    return existing or None


def _persist_token(token_path: Path, token: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")


async def _ensure_registered(
    client: httpx.AsyncClient, token_path: Path, config: FleetAgentConfig
) -> str:
    """Return this device's persisted device_token, registering with the
    manager on first run (or if the token file is missing/empty — e.g. a
    fresh /data volume, mirroring how `xedge.api.auth.load_or_create_secret_key`
    treats an absent file as "first boot," not an error)."""
    existing = _read_persisted_token(token_path)
    if existing is not None:
        return existing
    response = await client.post(
        "/api/v1/fleet/register",
        json={
            "device_id": config.device_id,
            "display_name": config.display_name,
            "agent_version": __version__,
            "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
            "join_token": config.join_token,
        },
    )
    response.raise_for_status()
    token: str = response.json()["device_token"]
    _persist_token(token_path, token)
    return token


async def fleet_heartbeat_loop(
    config: FleetAgentConfig,
    token_path: Path,
    supervisor: DriverSupervisor,
    config_path: Path,
    validator: ConfigValidator,
    started_at: datetime,
    status: FleetAgentStatus,
) -> None:
    """Registers once (persisting the resulting token to `token_path`),
    then heartbeats every `config.heartbeat_interval_seconds` forever.
    Never raises — a Fleet Manager that's unreachable degrades to
    `status.last_heartbeat_ok = False` (mirrors every other best-effort
    background loop in this codebase: driver polling, northbound
    publishing, hot-reload all keep running independently of one
    another's failures)."""
    status.enabled = True
    status.manager_url = config.manager_url
    status.device_id = config.device_id

    # Result of the *previous* heartbeat's pending-config application,
    # reported on the *next* heartbeat (XEDGE-213's "reports result") —
    # None until this device has ever applied a pushed config.
    pending_report: dict[str, Any] | None = None

    async with httpx.AsyncClient(base_url=config.manager_url, verify=config.verify_tls) as client:
        while True:
            try:
                token = await _ensure_registered(client, token_path, config)
                status.registered = True
                all_status = supervisor.all_status()
                response = await client.post(
                    f"/api/v1/fleet/devices/{config.device_id}/heartbeat",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "agent_version": __version__,
                        "driver_count": len(all_status),
                        "uptime_seconds": (datetime.now(UTC) - started_at).total_seconds(),
                        "last_config_apply": pending_report,
                    },
                )
                response.raise_for_status()
                pending_report = None
                status.last_heartbeat_at = datetime.now(UTC)
                status.last_heartbeat_ok = True
                status.last_error = None

                body = response.json()
                pending_config = body.get("pending_config")
                if pending_config is not None:
                    pending_report = _apply_pending_config(
                        pending_config, body["pending_config_version"], config_path, validator
                    )
                    status.last_config_apply = pending_report
            except (httpx.HTTPError, OSError) as exc:
                status.last_heartbeat_ok = False
                status.last_error = str(exc)
                logger.warning("fleet.heartbeat_failed", error=str(exc))

            await asyncio.sleep(config.heartbeat_interval_seconds)


def _apply_pending_config(
    pending_config: dict[str, Any],
    version: int,
    config_path: Path,
    validator: ConfigValidator,
) -> dict[str, Any]:
    """Validate and write a manager-pushed config through the same file
    hot-reload already watches (no separate mutation path — same rule
    `xedge.api.server.put_config`'s docstring states). Actual restart of
    affected drivers happens asynchronously on hot-reload's own poll cycle,
    not here — so `success: True` below means "written and scheduled for
    reload," not "confirmed running," a gap documented in ADR-009."""
    try:
        validator.validate(pending_config)
    except ConfigValidationError as exc:
        logger.warning("fleet.config_push_rejected", version=version, error=str(exc))
        return {"version": version, "success": False, "error": str(exc)}
    config_path.write_text(yaml.safe_dump(pending_config, sort_keys=False), encoding="utf-8")
    logger.info("fleet.config_push_applied", version=version)
    return {"version": version, "success": True, "error": None}
