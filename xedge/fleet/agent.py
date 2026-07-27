"""Fleet agent (Sprint 29, XEDGE-210/213; reworked Sprint C4, XEDGE-442 —
see ADR-013 §3): device-side counterpart to `xedge.fleet.manager_app` /
`xedge.fleet.manager_device_app`. Runs as one more asyncio task inside
`xedge.core.main.async_main`, the same shape as
`system_tag_publish_loop`/`purge_loop`.

Enrollment happens once: this device generates its own keypair locally
(`xedge.security.csr.generate_key_and_csr`) and submits only the CSR and a
join token to `manager_app`'s `/enroll` — the private key never leaves this
process. Everything after that (heartbeat, config pull) talks to
`manager_device_app` instead, over a client presenting the certificate
`/enroll` returned, on a *different port* than enrollment used (ADR-013 §3
+ `xedge.fleet.manager_cli`'s two-port split).

Pull, not push (deliberate deviation from the original sprint story's
"push" title — see ADR-009): the agent calls the manager on a heartbeat
interval, and any config an operator queued for this device rides along in
that response. This avoids the manager needing inbound network
reachability to every device (many are behind NAT/firewalls with no public
address at all), consistent with `xedge.core.hot_reload`'s existing
poll-based config model. Applying a pending config reuses the exact same
file-write path `xedge.api.server.put_config` already uses — hot-reload's
own poll loop does the actual restart, so this module never touches
`DriverSupervisor` directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography import x509

from xedge import __version__
from xedge.core.config import ConfigValidationError, ConfigValidator
from xedge.core.supervisor import DriverSupervisor
from xedge.fleet.registry import GatewayConnectionState, classify_connection_state
from xedge.observability.logging import get_logger
from xedge.security.csr import generate_key_and_csr
from xedge.security.tls_context import build_mtls_client_context

logger = get_logger(__name__)

_ENROLLMENT_RETRY_BACKOFF_SECONDS = 5.0


@dataclass(slots=True)
class FleetAgentConfig:
    manager_url: str
    device_manager_url: str
    device_id: str
    join_token: str
    display_name: str | None = None
    heartbeat_interval_seconds: float = 60
    verify_tls: bool = True


@dataclass(slots=True, frozen=True)
class FleetAgentPaths:
    """Where this device's own enrollment artifacts live —
    `xedge.core.main.DataPaths.fleet` is the shared root. `device_key`
    holds a private key and never travels anywhere; the other three are
    exchanged with the manager during enrollment/rotation."""

    device_token: Path
    device_cert: Path
    device_key: Path
    ca_certificate: Path


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
    heartbeat_interval_seconds: float = 60
    last_heartbeat_at: datetime | None = None
    last_heartbeat_ok: bool | None = None
    last_error: str | None = None
    last_config_apply: dict[str, Any] | None = None
    cert_not_after: datetime | None = None

    @property
    def connection_state(self) -> GatewayConnectionState:
        """Sprint C4, XEDGE-447 — the device-local Web UI's fleet status
        card shows this device's *own* view of its connectivity, using the
        same four-state vocabulary the manager computes for its view of
        this same device (`xedge.fleet.registry.DeviceRecord.
        connection_state`) — same classification function, applied to
        "when did *my* last heartbeat succeed" instead of "when did the
        *manager* last hear from this device." The two can disagree
        briefly (e.g. right after a heartbeat the manager hasn't received
        yet) without either being wrong."""
        return classify_connection_state(self.last_heartbeat_at, self.heartbeat_interval_seconds)


def _read_text_if_present(path: Path) -> str | None:
    if not path.is_file():
        return None
    existing = path.read_text(encoding="utf-8").strip()
    return existing or None


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _already_enrolled(paths: FleetAgentPaths) -> bool:
    return (
        paths.device_token.is_file()
        and paths.device_cert.is_file()
        and paths.device_key.is_file()
        and paths.ca_certificate.is_file()
    )


async def _enroll(
    client: httpx.AsyncClient, paths: FleetAgentPaths, config: FleetAgentConfig
) -> None:
    """One-shot: generate a keypair, submit its CSR with the (single-use)
    join token, and persist everything `/enroll` returns. Not idempotent by
    itself — call only when `_already_enrolled` is false. A join token that
    fails here (wrong device_id, expired, already consumed) cannot succeed
    by retrying the same token; the retry loop in `fleet_heartbeat_loop`
    exists for the transient case (manager not reachable yet), not this
    one — see that loop's warning-vs-error log levels."""
    private_key_pem, csr_pem = generate_key_and_csr(config.device_id)
    response = await client.post(
        "/api/v1/fleet/enroll",
        json={
            "device_id": config.device_id,
            "join_token": config.join_token,
            "csr_pem": csr_pem.decode("ascii"),
            "display_name": config.display_name,
            "agent_version": __version__,
            "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        },
    )
    response.raise_for_status()
    body = response.json()
    _write_text(paths.device_key, private_key_pem.decode("ascii"))
    _write_text(paths.device_cert, body["certificate_pem"])
    _write_text(paths.ca_certificate, body["ca_certificate_pem"])
    _write_text(paths.device_token, body["device_token"])


async def _ensure_enrolled(paths: FleetAgentPaths, config: FleetAgentConfig) -> None:
    """Retries with a fixed backoff until enrollment succeeds — appropriate
    for the expected failure mode at this stage (the manager isn't up yet,
    e.g. both containers starting together in compose/k8s with no ordering
    guarantee), logged at WARNING. An auth-shaped failure (bad/expired/
    used join token) is logged at ERROR on every attempt instead, since no
    amount of retrying fixes it without an operator issuing a fresh token —
    but this function still doesn't give up, both because it can't tell
    "manager rejected the token" apart from "manager not up yet enough to
    say so," and because a human fixing it (issuing a new token) shouldn't
    also have to restart the device's process for the retry to notice."""
    if _already_enrolled(paths):
        return
    async with httpx.AsyncClient(base_url=config.manager_url, verify=config.verify_tls) as client:
        while True:
            try:
                await _enroll(client, paths, config)
                return
            except httpx.HTTPStatusError as exc:
                logger.error("fleet.enroll_rejected", status_code=exc.response.status_code)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("fleet.enroll_unreachable", error=str(exc))
            await asyncio.sleep(_ENROLLMENT_RETRY_BACKOFF_SECONDS)


async def fleet_heartbeat_loop(
    config: FleetAgentConfig,
    paths: FleetAgentPaths,
    supervisor: DriverSupervisor,
    config_path: Path,
    validator: ConfigValidator,
    started_at: datetime,
    status: FleetAgentStatus,
) -> None:
    """Enrolls once (retrying as described in `_ensure_enrolled`), then
    heartbeats every `config.heartbeat_interval_seconds` forever over a
    client presenting the certificate enrollment returned. Never raises
    once past enrollment — a Fleet Manager that's unreachable degrades to
    `status.last_heartbeat_ok = False` (mirrors every other best-effort
    background loop in this codebase: driver polling, northbound
    publishing, hot-reload all keep running independently of one
    another's failures)."""
    status.enabled = True
    status.manager_url = config.manager_url
    status.device_id = config.device_id
    status.heartbeat_interval_seconds = config.heartbeat_interval_seconds

    await _ensure_enrolled(paths, config)
    status.registered = True
    device_token = _read_text_if_present(paths.device_token)
    if device_token is None:
        raise RuntimeError("enrollment just completed but left no device_token")
    status.cert_not_after = x509.load_pem_x509_certificate(
        paths.device_cert.read_bytes()
    ).not_valid_after_utc

    # Result of the *previous* heartbeat's pending-config application,
    # reported on the *next* heartbeat (XEDGE-213's "reports result") —
    # None until this device has ever applied a pushed config.
    pending_report: dict[str, Any] | None = None

    # Always verified against *this device's own* fleet CA copy, not
    # `config.verify_tls` — that flag only covers the bootstrap trust gap
    # during enrollment (no CA knowledge exists yet); once enrolled, the
    # real CA is sitting on disk and skipping verification against it would
    # be a pure downgrade with no corresponding benefit.
    mtls_context = build_mtls_client_context(
        paths.device_cert, paths.device_key, paths.ca_certificate
    )
    async with httpx.AsyncClient(
        base_url=config.device_manager_url, verify=mtls_context
    ) as client:
        while True:
            try:
                all_status = supervisor.all_status()
                response = await client.post(
                    f"/api/v1/fleet/devices/{config.device_id}/heartbeat",
                    headers={"Authorization": f"Bearer {device_token}"},
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
