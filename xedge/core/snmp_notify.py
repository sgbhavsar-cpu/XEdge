"""SNMP TRAP/INFORM originator (FR-SB-006; Sprint C8, XEDGE-483), wired to
the Alarm Engine.

Same polling shape as `xedge.core.smtp`'s `alarm_notification_loop`
(`AlarmEngine` has no event hook to subscribe to): snapshots
`all_status()` every `poll_interval_seconds` and diffs against its own
previous snapshot to detect a genuine NORMAL<->alarming boundary crossing,
not every state change -- an ACTIVE -> ACTIVE_ACKED acknowledgement
crosses no such boundary and notifies no one, matching the SMTP loop's own
`_is_alarming` reasoning exactly (an operator who just acked the alarm
already knows about it).

Placeholder Private Enterprise Number, same one and same caveat as
`xedge.northbound.snmp_agent`'s (XEDGE-DR-001 Q-8): xEdge has no real
IANA-assigned PEN yet. This module's notification-type and varbind OIDs
live under a *different* sub-arc of the identical placeholder number
(`.3.*`/`.4.*` here vs the agent's `.1.*`/`.2.*`) so the two modules never
collide on-wire, but both need the same real-PEN swap before production.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pysnmp.error import PySnmpError
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    NotificationType,
    ObjectIdentity,
    ObjectType,
    OctetString,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    send_notification,
)

from xedge.core.alarms import AlarmEngine, AlarmState, AlarmStatus
from xedge.drivers.snmp.client import AUTH_PROTOCOLS, PRIV_PROTOCOLS, SnmpConfigError
from xedge.observability.logging import get_logger

logger = get_logger(__name__)

_PLACEHOLDER_ENTERPRISE_OID = "1.3.6.1.4.1.999999"
_NOTIFY_TYPE_RAISED_OID = f"{_PLACEHOLDER_ENTERPRISE_OID}.3.1"
_NOTIFY_TYPE_CLEARED_OID = f"{_PLACEHOLDER_ENTERPRISE_OID}.3.2"
_VARBIND_TAG_ID_OID = f"{_PLACEHOLDER_ENTERPRISE_OID}.4.1"
_VARBIND_CONDITION_OID = f"{_PLACEHOLDER_ENTERPRISE_OID}.4.2"
_VARBIND_VALUE_OID = f"{_PLACEHOLDER_ENTERPRISE_OID}.4.3"
_VARBIND_STATE_OID = f"{_PLACEHOLDER_ENTERPRISE_OID}.4.4"

_DEFAULT_PORT = 162


@dataclass(frozen=True, slots=True)
class SnmpTrapDestination:
    host: str
    port: int = _DEFAULT_PORT
    version: str = "v2c"
    community: str = "public"
    v3_username: str | None = None
    v3_auth_protocol: str = "none"
    v3_auth_password: str | None = None
    v3_priv_protocol: str = "none"
    v3_priv_password: str | None = None
    notify_type: str = "trap"


@dataclass(frozen=True, slots=True)
class SnmpNotifyConfig:
    enabled: bool = False
    destinations: tuple[SnmpTrapDestination, ...] = ()


@dataclass(slots=True)
class SnmpNotifyStatus:
    """Live snapshot the Web UI/REST API read, mutated in place -- same
    pattern as `xedge.core.smtp.SmtpStatus`."""

    enabled: bool = False
    last_send_at: datetime | None = None
    last_send_ok: bool | None = None
    last_error: str | None = None
    notifications_sent: int = 0


def build_snmp_notify_config(section: dict[str, Any]) -> SnmpNotifyConfig:
    destinations = tuple(
        SnmpTrapDestination(
            host=d["host"],
            port=d.get("port", _DEFAULT_PORT),
            version=d.get("version", "v2c"),
            community=d.get("community", "public"),
            v3_username=d.get("v3_username"),
            v3_auth_protocol=d.get("v3_auth_protocol", "none"),
            v3_auth_password=d.get("v3_auth_password"),
            v3_priv_protocol=d.get("v3_priv_protocol", "none"),
            v3_priv_password=d.get("v3_priv_password"),
            notify_type=d.get("notify_type", "trap"),
        )
        for d in section.get("destinations", [])
    )
    return SnmpNotifyConfig(enabled=section.get("enabled", False), destinations=destinations)


def _build_auth_data(dest: SnmpTrapDestination) -> CommunityData | UsmUserData:
    if dest.version in ("v1", "v2c"):
        if not dest.community:
            raise SnmpConfigError(f"version {dest.version!r} requires 'community'")
        return CommunityData(dest.community, mpModel=0 if dest.version == "v1" else 1)
    if not dest.v3_username:
        raise SnmpConfigError("version 'v3' requires 'v3_username'")
    if dest.v3_priv_protocol != "none" and dest.v3_auth_protocol == "none":
        raise SnmpConfigError(
            "v3_priv_protocol requires v3_auth_protocol != 'none' "
            "(RFC 3414: privacy without authentication is not a valid USM security level)"
        )
    return UsmUserData(
        dest.v3_username,
        authKey=dest.v3_auth_password or None,
        privKey=dest.v3_priv_password or None,
        authProtocol=AUTH_PROTOCOLS[dest.v3_auth_protocol],
        privProtocol=PRIV_PROTOCOLS[dest.v3_priv_protocol],
    )


def _is_alarming(state: AlarmState) -> bool:
    return state != AlarmState.NORMAL


def _is_shelved(alarm_status: AlarmStatus, now: datetime) -> bool:
    return alarm_status.shelved_until is not None and now < alarm_status.shelved_until


async def _send_one(
    engine: SnmpEngine, destination: SnmpTrapDestination, alarm_status: AlarmStatus, *, raised: bool
) -> None:
    auth_data = _build_auth_data(destination)
    transport_target = await UdpTransportTarget.create((destination.host, destination.port))
    notification_oid = _NOTIFY_TYPE_RAISED_OID if raised else _NOTIFY_TYPE_CLEARED_OID
    notification = NotificationType(ObjectIdentity(notification_oid)).add_varbinds(
        ObjectType(ObjectIdentity(_VARBIND_TAG_ID_OID), OctetString(alarm_status.tag_id)),
        ObjectType(
            ObjectIdentity(_VARBIND_CONDITION_OID), OctetString(alarm_status.condition or "normal")
        ),
        ObjectType(ObjectIdentity(_VARBIND_VALUE_OID), OctetString(repr(alarm_status.last_value))),
        ObjectType(ObjectIdentity(_VARBIND_STATE_OID), OctetString(alarm_status.state.value)),
    )
    error_indication, error_status, _error_index, _var_binds = await send_notification(
        engine, auth_data, transport_target, ContextData(), destination.notify_type, notification
    )
    if error_indication:
        raise PySnmpError(str(error_indication))
    if error_status:
        raise PySnmpError(error_status.prettyPrint())


async def send_alarm_notification(
    config: SnmpNotifyConfig, alarm_status: AlarmStatus, status: SnmpNotifyStatus, *, raised: bool
) -> None:
    """Sends to every configured destination, same as `xedge.core.smtp
    .send_email` sends to every recipient in one call -- one destination's
    failure is logged and does not stop the others, and `status` is
    updated in place regardless of outcome."""
    engine = SnmpEngine()
    any_failed = False
    for destination in config.destinations:
        try:
            await _send_one(engine, destination, alarm_status, raised=raised)
        except PySnmpError as exc:
            any_failed = True
            status.last_error = str(exc)
            logger.warning(
                "snmp_notify.send_failed",
                host=destination.host,
                port=destination.port,
                error=str(exc),
            )
    status.last_send_at = datetime.now(UTC)
    status.last_send_ok = not any_failed
    if not any_failed:
        status.notifications_sent += 1


async def snmp_alarm_notification_loop(
    alarm_engine: AlarmEngine,
    config: SnmpNotifyConfig,
    status: SnmpNotifyStatus,
    *,
    poll_interval_seconds: float = 2.0,
) -> None:
    previous_states: dict[str, AlarmState] = {}
    while True:
        now = datetime.now(UTC)
        for tag_id, alarm_status in alarm_engine.all_status().items():
            prior_state = previous_states.get(tag_id, AlarmState.NORMAL)
            previous_states[tag_id] = alarm_status.state
            if alarm_status.state == prior_state:
                continue
            if _is_alarming(alarm_status.state) == _is_alarming(prior_state):
                continue
            if _is_shelved(alarm_status, now):
                continue
            if config.destinations:
                await send_alarm_notification(
                    config, alarm_status, status, raised=_is_alarming(alarm_status.state)
                )

        await asyncio.sleep(poll_interval_seconds)
