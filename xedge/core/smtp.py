"""SMTP client (Sprint C6, XEDGE-466; CRD §4.7/FR-SC-002): email
notifications on alarm transitions, plus periodic summary reports. The
Alarm Engine v2 (Sprint 31) already has the trigger logic (raise/clear/
ack state machine, shelving) -- only the email transport was missing.

Wired in directly rather than behind a generic "notification channel"
interface: SMTP is the only channel this delivery's CRD calls for, and
there is no second channel yet to justify the abstraction.

`smtplib` has no async API, so every send runs off-thread via
`asyncio.to_thread` -- the same reason paho-mqtt's blocking `connect()`
calls get the same treatment elsewhere in this codebase (fleet agent,
generic MQTT publisher).

Two independent loops, not one: `alarm_notification_loop` polls
`AlarmEngine.all_status()` for state transitions (no event/callback hook
exists on `AlarmEngine` itself, so this is an observer built entirely on
its existing public API — deliberately not adding one, given the only
consumer so far is this one loop); `scheduled_report_loop` fires on a
fixed interval, one task per configured report, matching how multiple
driver instances each already get their own task.

A scheduled report's "alarm summary" is a live snapshot of *currently*
active alarms at send time, not a historical digest of everything that
transitioned since the last report — the latter would need either the
same polling-diff bookkeeping the notification loop already does (data
this module would then own two copies of) or a cold-store query this
module has no reason to depend on otherwise. Stated plainly as a scope
choice, not silently assumed.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from xedge.core.alarms import AlarmEngine, AlarmState, AlarmStatus
from xedge.core.assets import parse_assets
from xedge.core.config import ConfigStore
from xedge.observability.logging import get_logger
from xedge.store.latest_values import LatestValueStore

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    tls_mode: str = "starttls"
    """"none" (plaintext), "starttls" (upgrade a plaintext connection --
    the common port-587 case), or "smtps" (TLS from the first byte --
    port 465). Two different stdlib client classes (`smtplib.SMTP` vs
    `SMTP_SSL`), not one boolean, because that's genuinely two different
    protocols on the wire, not one feature with an on/off switch."""
    username: str | None = None
    password: str | None = None
    from_address: str = ""
    connect_timeout_seconds: float = 10.0
    tls_ca_certs_path: str | None = None
    """CA bundle verifying the server's certificate, for "starttls" and
    "smtps" alike. Omit to use the system trust store (a publicly-
    trusted mail relay); set for a self-signed or private-CA internal
    relay -- the same reasoning `northbound.mqtt.tls_ca_certs_path`
    already documents for a customer's MQTT broker being a separate
    trust domain applies here too."""


@dataclass(frozen=True, slots=True)
class AlarmNotificationConfig:
    enabled: bool = False
    recipients: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduledReportConfig:
    id: str
    enabled: bool = True
    recipients: tuple[str, ...] = ()
    interval_seconds: float = 86400.0
    include_tag_ids: tuple[str, ...] = ()
    include_asset_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class SmtpStatus:
    """Live snapshot the Web UI/REST API read, mutated in place by both
    loops — same pattern as `xedge.fleet.agent.FleetAgentStatus`."""

    enabled: bool = False
    last_send_at: datetime | None = None
    last_send_ok: bool | None = None
    last_error: str | None = None
    emails_sent: int = 0


def _build_tls_context(config: SmtpConfig) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=config.tls_ca_certs_path)


def _send_email_sync(config: SmtpConfig, to_addresses: list[str], subject: str, body: str) -> None:
    """Blocking. Callers must run this via `asyncio.to_thread` -- smtplib
    has no async API of its own."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.from_address
    message["To"] = ", ".join(to_addresses)
    message.set_content(body)

    if config.tls_mode == "smtps":
        with smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.connect_timeout_seconds,
            context=_build_tls_context(config),
        ) as client:
            if config.username:
                client.login(config.username, config.password or "")
            client.send_message(message)
    else:
        with smtplib.SMTP(
            config.host, config.port, timeout=config.connect_timeout_seconds
        ) as client:
            if config.tls_mode == "starttls":
                client.starttls(context=_build_tls_context(config))
            if config.username:
                client.login(config.username, config.password or "")
            client.send_message(message)


async def send_email(
    config: SmtpConfig, to_addresses: list[str], subject: str, body: str, status: SmtpStatus
) -> None:
    """Updates `status` in place regardless of outcome -- callers don't
    need their own try/except to keep the Web UI's SMTP status card
    current, matching `fleet_heartbeat_loop`'s status-mutation style."""
    try:
        await asyncio.to_thread(_send_email_sync, config, to_addresses, subject, body)
        status.last_send_at = datetime.now(UTC)
        status.last_send_ok = True
        status.last_error = None
        status.emails_sent += 1
    except (smtplib.SMTPException, OSError) as exc:
        status.last_send_at = datetime.now(UTC)
        status.last_send_ok = False
        status.last_error = str(exc)
        logger.warning("smtp.send_failed", error=str(exc))


def _is_alarming(state: AlarmState) -> bool:
    return state != AlarmState.NORMAL


def _is_shelved(alarm_status: AlarmStatus, now: datetime) -> bool:
    return alarm_status.shelved_until is not None and now < alarm_status.shelved_until


def _format_alarm_notification(alarm_status: AlarmStatus, *, raised: bool) -> tuple[str, str]:
    verb = "ALARM" if raised else "CLEARED"
    subject = f"[xEdge] {verb}: {alarm_status.tag_id}"
    lines = [
        f"Tag: {alarm_status.tag_id}",
        f"Condition: {alarm_status.condition or 'normal'}",
        f"Value: {alarm_status.last_value!r}",
    ]
    if raised and alarm_status.active_since is not None:
        lines.append(f"Active since: {alarm_status.active_since.isoformat()}")
    return subject, "\n".join(lines)


async def alarm_notification_loop(
    alarm_engine: AlarmEngine,
    smtp_config: SmtpConfig,
    notification_config: AlarmNotificationConfig,
    status: SmtpStatus,
    *,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Polls rather than hooking a callback into `AlarmEngine` (which has
    none) -- 2s default keeps a raise/clear noticed quickly without
    adding meaningfully to the alarm engine's own per-tag evaluation
    cost, since this reads `all_status()` once per tick regardless of
    how many tags were actually evaluated in that window."""
    previous_states: dict[str, AlarmState] = {}
    recipients = list(notification_config.recipients)
    while True:
        now = datetime.now(UTC)
        for tag_id, alarm_status in alarm_engine.all_status().items():
            prior_state = previous_states.get(tag_id, AlarmState.NORMAL)
            previous_states[tag_id] = alarm_status.state
            if alarm_status.state == prior_state:
                continue
            if _is_alarming(alarm_status.state) == _is_alarming(prior_state):
                # Crossed no normal/alarming boundary -- an ACTIVE ->
                # ACTIVE_ACKED ack, not a raise or a clear. Nothing new
                # for a recipient to act on.
                continue
            if _is_shelved(alarm_status, now):
                continue
            subject, body = _format_alarm_notification(
                alarm_status, raised=_is_alarming(alarm_status.state)
            )
            if recipients:
                await send_email(smtp_config, recipients, subject, body, status)

        await asyncio.sleep(poll_interval_seconds)


def _resolve_report_tag_ids(
    report_config: ScheduledReportConfig, assets_config: list[dict[str, Any]]
) -> list[str]:
    tag_ids = list(report_config.include_tag_ids)
    if report_config.include_asset_ids:
        assets_by_id = {asset.id: asset for asset in parse_assets(assets_config)}
        for asset_id in report_config.include_asset_ids:
            asset = assets_by_id.get(asset_id)
            if asset is None:
                continue
            tag_ids.extend(p.tag_ref for p in asset.parameters)
    return tag_ids


def _format_report_body(
    report_config: ScheduledReportConfig,
    alarm_engine: AlarmEngine | None,
    latest_values: LatestValueStore,
    assets_config: list[dict[str, Any]],
) -> str:
    lines = [f"xEdge scheduled report: {report_config.id}", ""]

    lines.append("Active alarms:")
    active = (
        [s for s in alarm_engine.all_status().values() if _is_alarming(s.state)]
        if alarm_engine is not None
        else []
    )
    if active:
        lines.extend(
            f"  {s.tag_id}: {s.condition} (value={s.last_value!r})"
            for s in sorted(active, key=lambda s: s.tag_id)
        )
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Tag values:")
    tag_ids = _resolve_report_tag_ids(report_config, assets_config)
    if tag_ids:
        for tag_id in tag_ids:
            tag = latest_values.get(tag_id)
            if tag is None:
                lines.append(f"  {tag_id}: (no data)")
            else:
                lines.append(f"  {tag_id}: {tag.value!r} ({tag.quality.value})")
    else:
        lines.append("  (none configured)")

    return "\n".join(lines)


async def scheduled_report_loop(
    report_config: ScheduledReportConfig,
    smtp_config: SmtpConfig,
    store: ConfigStore,
    alarm_engine: AlarmEngine | None,
    latest_values: LatestValueStore,
    status: SmtpStatus,
) -> None:
    """One task per configured report (`xedge.core.main` spawns one per
    entry in `smtp.scheduled_reports`), matching how each driver instance
    already gets its own task rather than one task iterating a list.
    Reads `assets` config fresh from `store` every cycle rather than
    once at startup, since a report's own interval is commonly much
    longer than how often assets change."""
    recipients = list(report_config.recipients)
    while True:
        await asyncio.sleep(report_config.interval_seconds)
        if not recipients:
            continue
        assets_config = store.get_section("assets", [])
        body = _format_report_body(report_config, alarm_engine, latest_values, assets_config)
        subject = f"[xEdge] Scheduled report: {report_config.id}"
        await send_email(smtp_config, recipients, subject, body, status)


def build_smtp_config(smtp_section: dict[str, Any]) -> SmtpConfig:
    return SmtpConfig(
        enabled=smtp_section.get("enabled", False),
        host=smtp_section.get("host", ""),
        port=smtp_section.get("port", 587),
        tls_mode=smtp_section.get("tls_mode", "starttls"),
        username=smtp_section.get("username"),
        password=smtp_section.get("password"),
        from_address=smtp_section.get("from_address", ""),
        connect_timeout_seconds=smtp_section.get("connect_timeout_seconds", 10.0),
        tls_ca_certs_path=smtp_section.get("tls_ca_certs_path"),
    )


def build_alarm_notification_config(smtp_section: dict[str, Any]) -> AlarmNotificationConfig:
    notif = smtp_section.get("alarm_notifications", {})
    return AlarmNotificationConfig(
        enabled=notif.get("enabled", False), recipients=tuple(notif.get("recipients", []))
    )


def build_scheduled_report_configs(smtp_section: dict[str, Any]) -> list[ScheduledReportConfig]:
    return [
        ScheduledReportConfig(
            id=entry["id"],
            enabled=entry.get("enabled", True),
            recipients=tuple(entry.get("recipients", [])),
            interval_seconds=entry.get("interval_seconds", 86400.0),
            include_tag_ids=tuple(entry.get("include_tag_ids", [])),
            include_asset_ids=tuple(entry.get("include_asset_ids", [])),
        )
        for entry in smtp_section.get("scheduled_reports", [])
        if entry.get("enabled", True)
    ]
