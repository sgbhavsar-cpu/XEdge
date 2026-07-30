"""SNMP manager driver (FR-SB-006; Sprint C8, XEDGE-481/487).

Built directly on `pysnmp` (BSD-2-Clause; license-audit.md §4 item 8)
rather than a clean-room build — ADR-012 §2 follows the same "buy"
precedent as BACnet/EtherNet/IP (ADR-006/ADR-012 §1): SNMPv3's USM
authentication and privacy (RFC 3414) are exactly the kind of thing that
is easy to get subtly wrong from spec, and getting it wrong is a security
defect, not a functional bug.

Unlike every TCP-based driver in this codebase (Modbus TCP, OPC UA,
EtherNet/IP), SNMP is connectionless (UDP): there is no session to
establish, monitor, or tear down and restart. `connect()` only builds the
local `SnmpEngine`/auth/transport-target objects (which do no network I/O
themselves) and validates the configured security parameters; per-request
success/failure feeds a `ConnectivityTracker`
(`xedge.core.connectivity`, ADR-011) exactly the way
`xedge.drivers.modbus.polling` already does, rather than the
DriverConnectionError-propagates-to-the-supervisor pattern the TCP drivers
use — a lost UDP request is normal, transient network behavior, not
evidence the "connection" is dead and the whole instance should restart.

`pysnmp`'s asyncio hlapi (`pysnmp.hlapi.v3arch.asyncio`) is genuinely
async-native — `get_cmd`/`next_cmd`/`bulk_cmd`/`set_cmd` are real
`async def` coroutines, confirmed by inspecting the installed package
directly, not assumed from its examples. No `asyncio.to_thread` wrapping
is needed here, unlike `pycomm3`/`paho-mqtt`/`smtplib` elsewhere in this
codebase.

SNMP's error reporting has three tiers, each confirmed empirically against
a real local test agent before this driver was written (not assumed from
docs):

1. `errorIndication` (truthy) — a communication/security-level failure
   (timeout, wrong credentials, ...). No response was obtained at all.
   Recorded as a `ConnectivityTracker` failure; every tag in the request
   is marked Bad for this cycle.
2. `errorStatus` (nonzero) — a whole-PDU-level failure (SNMPv1's only
   error-reporting mechanism, since v1 has no per-varbind exception
   values: one bad OID in a multi-OID v1 GET fails the entire response,
   confirmed against a real agent). Every tag in the request is marked
   Bad for this cycle, same as (1), but not a connectivity failure — the
   device answered, it just rejected the request.
3. Per-varbind exception *values* — `NoSuchObject`/`NoSuchInstance`/
   `EndOfMibView`, SNMPv2c/v3's mechanism for a partial failure inside an
   otherwise-successful multi-OID response (confirmed against a real
   agent: a nonexistent OID comes back as one of these value types, not
   an errorStatus). Only that one tag is marked Bad.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pysnmp.error import PySnmpError
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    EndOfMibView,
    NoSuchInstance,
    NoSuchObject,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_cmd,
    get_cmd,
    next_cmd,
    set_cmd,
    usm3DESEDEPrivProtocol,
    usmAesCfb128Protocol,
    usmAesCfb192Protocol,
    usmAesCfb256Protocol,
    usmDESPrivProtocol,
    usmHMAC128SHA224AuthProtocol,
    usmHMAC192SHA256AuthProtocol,
    usmHMAC256SHA384AuthProtocol,
    usmHMAC384SHA512AuthProtocol,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
)

from xedge.core.connectivity import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_RECOVERY_THRESHOLD,
    ConnectivityState,
    ConnectivityTracker,
)
from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
    DriverConnectionError,
    DriverMetrics,
    Quality,
    TagUpdate,
    TagValue,
    WriteResult,
)
from xedge.drivers.snmp import asn1types
from xedge.observability.logging import get_logger
from xedge.observability.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_DEFAULT_PORT = 161
_DEFAULT_TIMEOUT_SECONDS = 1.0
_DEFAULT_RETRIES = 3

ACCESS_READ_WRITE = "read_write"
ACCESS_READ_ONLY = "read_only"
ACCESS_WRITE_ONLY = "write_only"

_AUTH_PROTOCOLS: dict[str, Any] = {
    "none": usmNoAuthProtocol,
    "md5": usmHMACMD5AuthProtocol,
    "sha": usmHMACSHAAuthProtocol,
    "sha224": usmHMAC128SHA224AuthProtocol,
    "sha256": usmHMAC192SHA256AuthProtocol,
    "sha384": usmHMAC256SHA384AuthProtocol,
    "sha512": usmHMAC384SHA512AuthProtocol,
}
_PRIV_PROTOCOLS: dict[str, Any] = {
    "none": usmNoPrivProtocol,
    "des": usmDESPrivProtocol,
    "3des": usm3DESEDEPrivProtocol,
    "aes128": usmAesCfb128Protocol,
    "aes192": usmAesCfb192Protocol,
    "aes256": usmAesCfb256Protocol,
}

_EXCEPTION_VALUE_TYPES = (NoSuchObject, NoSuchInstance, EndOfMibView)

__all__ = ["SnmpClientDriver", "SnmpDriverStateError"]


class SnmpDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class SnmpConfigError(DriverConnectionError):
    """Raised for a configuration combination pysnmp itself has no single
    validation error for (e.g. GETBULK under v1, privacy without
    authentication) — surfaced as a connect()-time failure rather than
    discovered as a confusing per-poll error later."""


def _tag_access(tag: dict[str, Any]) -> str:
    return tag.get("access", ACCESS_READ_WRITE)  # type: ignore[no-any-return]


def _inverse_scale(value: TagValue, scaling: dict[str, Any] | None) -> TagValue:
    """See xedge.drivers.ethernet_ip.client._inverse_scale — identical
    rationale: a tag with no scaling configured is returned unchanged
    (not coerced to float), since an SNMP tag may be octet_string-typed."""
    if not scaling:
        return value
    scale: float = scaling.get("scale", 1.0)
    offset: float = scaling.get("offset", 0.0)
    return (float(value) - offset) / scale


def _build_auth_data(cfg: dict[str, Any]) -> CommunityData | UsmUserData:
    version = cfg["version"]
    if version in ("v1", "v2c"):
        if not cfg.get("community"):
            raise SnmpConfigError(f"version {version!r} requires 'community'")
        return CommunityData(cfg["community"], mpModel=0 if version == "v1" else 1)

    if not cfg.get("v3_username"):
        raise SnmpConfigError("version 'v3' requires 'v3_username'")
    auth_protocol_name = cfg.get("v3_auth_protocol", "none")
    priv_protocol_name = cfg.get("v3_priv_protocol", "none")
    if priv_protocol_name != "none" and auth_protocol_name == "none":
        # RFC 3414: authPriv requires authNoPriv first -- privacy with no
        # authentication is not a valid USM security level at all, not
        # just a discouraged one.
        raise SnmpConfigError(
            "v3_priv_protocol requires v3_auth_protocol != 'none' "
            "(RFC 3414: privacy without authentication is not a valid USM security level)"
        )
    return UsmUserData(
        cfg["v3_username"],
        authKey=cfg.get("v3_auth_password") or None,
        privKey=cfg.get("v3_priv_password") or None,
        authProtocol=_AUTH_PROTOCOLS[auth_protocol_name],
        privProtocol=_PRIV_PROTOCOLS[priv_protocol_name],
    )


class SnmpClientDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._metrics = DriverMetrics()
        self._tags_by_id: dict[str, dict[str, Any]] = {}
        self._connectivity = ConnectivityTracker()
        self._engine: SnmpEngine | None = None
        self._auth_data: CommunityData | UsmUserData | None = None
        self._transport_target: UdpTransportTarget | None = None

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise SnmpDriverStateError("configure() must be called before this operation")
        return self._config

    def _require_session(
        self,
    ) -> tuple[SnmpEngine, CommunityData | UsmUserData, UdpTransportTarget]:
        if self._engine is None or self._auth_data is None or self._transport_target is None:
            raise SnmpDriverStateError("connect() must be called before this operation")
        return self._engine, self._auth_data, self._transport_target

    async def configure(self, config: DriverConfig) -> None:
        self._config = config
        self._tags_by_id = {tag["id"]: tag for group in config.tag_groups for tag in group["tags"]}
        cfg = config.config
        self._connectivity = ConnectivityTracker(
            failure_threshold=int(
                cfg.get("consecutive_failure_threshold", DEFAULT_FAILURE_THRESHOLD)
            ),
            recovery_threshold=int(cfg.get("recovery_threshold", DEFAULT_RECOVERY_THRESHOLD)),
        )

    async def connect(self) -> None:
        cfg = self._require_config().config
        if cfg["version"] == "v1":
            for group in self._require_config().tag_groups:
                if group.get("use_bulk"):
                    raise SnmpConfigError(
                        f"tag_group {group['id']!r}: use_bulk is not valid under version 'v1' "
                        "(GETBULK does not exist in the SNMPv1 PDU set)"
                    )
        self._auth_data = _build_auth_data(cfg)
        try:
            self._transport_target = await UdpTransportTarget.create(
                (cfg["host"], cfg.get("port", _DEFAULT_PORT)),
                timeout=cfg.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS),
                retries=cfg.get("retries", _DEFAULT_RETRIES),
            )
        except PySnmpError as exc:
            raise DriverConnectionError(f"failed to resolve SNMP transport target: {exc}") from exc
        self._engine = SnmpEngine()

    async def disconnect(self) -> None:
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None
        self._auth_data = None
        self._transport_target = None

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        config = self._require_config()
        group_tasks = [
            asyncio.create_task(self._poll_group(group, output)) for group in config.tag_groups
        ]
        try:
            await asyncio.gather(*group_tasks)
        finally:
            for task in group_tasks:
                task.cancel()
            await asyncio.gather(*group_tasks, return_exceptions=True)

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        tag = self._tags_by_id.get(tag_id)
        if tag is None:
            return WriteResult(success=False, tag_id=tag_id, error_message="Unknown tag")
        if _tag_access(tag) == ACCESS_READ_ONLY:
            return WriteResult(
                success=False,
                tag_id=tag_id,
                error_message="Tag is configured read-only (access: read_only)",
            )
        data_type = tag.get("data_type")
        if data_type is None:
            return WriteResult(
                success=False,
                tag_id=tag_id,
                error_message="Tag has no data_type configured for SET",
            )

        engine, auth_data, transport_target = self._require_session()
        raw_value = _inverse_scale(value, tag.get("scaling"))
        asn1_value = asn1types.encode(data_type, raw_value)
        try:
            error_indication, error_status, error_index, _var_binds = await set_cmd(
                engine,
                auth_data,
                transport_target,
                ContextData(),
                ObjectType(ObjectIdentity(tag["oid"]), asn1_value),
            )
        except PySnmpError as exc:
            self._metrics.error_count += 1
            return WriteResult(success=False, tag_id=tag_id, error_message=str(exc))

        if error_indication:
            self._metrics.error_count += 1
            return WriteResult(success=False, tag_id=tag_id, error_message=str(error_indication))
        if error_status:
            self._metrics.error_count += 1
            return WriteResult(
                success=False, tag_id=tag_id, error_message=error_status.prettyPrint()
            )
        return WriteResult(success=True, tag_id=tag_id)

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    def get_connectivity_state(self) -> ConnectivityState:
        """Device-level connectivity (see module docstring: SNMP is
        connectionless, so this tracker — not the asyncio task's own
        liveness — is what represents device reachability, the same
        role it plays for xedge.drivers.modbus.polling)."""
        return self._connectivity.state

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        instance_id = self._config.instance_id if self._config else "snmp"
        interval_seconds = group["scan_rate_ms"] / 1000
        tags = [tag for tag in group["tags"] if _tag_access(tag) != ACCESS_WRITE_ONLY]
        if not tags:
            return
        use_bulk = bool(group.get("use_bulk"))
        while True:
            updates = await self._read_tags(instance_id, tags, use_bulk)
            for update in updates:
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tags(
        self, instance_id: str, tags: list[dict[str, Any]], use_bulk: bool
    ) -> list[TagUpdate]:
        """One poll cycle, one span, but not necessarily one PDU: `get`
        and `get_next` tags need two different SNMP operations, so a
        group mixing both issues one batched request per operation, not
        one request per tag.

        GETBULK (`use_bulk`) is deliberately never applied to `get`-
        operation tags: GETBULK/`bulk_cmd`'s semantics are "the N next
        values after each OID" (an efficient multi-step GETNEXT), not
        "the value at each OID" — using it for the latter is not a
        harmless equivalent request, it is answering a different
        question and would silently return the wrong data. It applies
        only to `get_next` tags, as one bulk_cmd(maxRepetitions=1)
        instead of one next_cmd -- both are one round trip for the same
        result, but this is what GETBULK actually means, confirmed by
        first getting this wrong and cross-checking against a real agent
        (see this driver's own test suite).
        """
        with tracer.start_as_current_span(
            "driver.read",
            attributes={"driver.instance_id": instance_id, "tag.count": len(tags)},
        ) as span:
            get_tags = [tag for tag in tags if tag.get("operation", "get") != "get_next"]
            next_tags = [tag for tag in tags if tag.get("operation") == "get_next"]
            updates: list[TagUpdate] = []
            if get_tags:
                updates.extend(
                    await self._request(instance_id, get_tags, bulk=False, next_op=False)
                )
            if next_tags:
                updates.extend(
                    await self._request(instance_id, next_tags, bulk=use_bulk, next_op=True)
                )
            bad_count = sum(1 for update in updates if update.quality == Quality.BAD)
            span.set_attribute("quality", Quality.BAD.value if bad_count else Quality.GOOD.value)
            span.set_attribute("tag.bad_count", bad_count)
            return updates

    async def _request(
        self, instance_id: str, tags: list[dict[str, Any]], *, bulk: bool, next_op: bool
    ) -> list[TagUpdate]:
        engine, auth_data, transport_target = self._require_session()
        var_binds = [ObjectType(ObjectIdentity(tag["oid"])) for tag in tags]
        try:
            if bulk:
                error_indication, error_status, error_index, result_binds = await bulk_cmd(
                    engine, auth_data, transport_target, ContextData(), 0, 1, *var_binds
                )
            elif next_op:
                error_indication, error_status, error_index, result_binds = await next_cmd(
                    engine, auth_data, transport_target, ContextData(), *var_binds
                )
            else:
                error_indication, error_status, error_index, result_binds = await get_cmd(
                    engine, auth_data, transport_target, ContextData(), *var_binds
                )
        except PySnmpError as exc:
            return self._all_bad(instance_id, tags, str(exc), connectivity_failure=True)

        if error_indication:
            return self._all_bad(
                instance_id, tags, str(error_indication), connectivity_failure=True
            )
        if error_status:
            detail = f"{error_status.prettyPrint()} at index {error_index}"
            return self._all_bad(instance_id, tags, detail, connectivity_failure=False)

        self._connectivity.record_success()
        now = datetime.now(UTC)
        return [
            self._build_update(instance_id, tag, var_bind, now)
            for tag, var_bind in zip(tags, result_binds, strict=True)
        ]

    def _all_bad(
        self,
        instance_id: str,
        tags: list[dict[str, Any]],
        error: str,
        *,
        connectivity_failure: bool,
    ) -> list[TagUpdate]:
        if connectivity_failure:
            self._connectivity.record_failure(reason=error)
        self._metrics.error_count += len(tags)
        logger.warning("snmp.read_failed", instance_id=instance_id, error=error)
        now = datetime.now(UTC)
        return [
            TagUpdate(
                tag_id=f"{instance_id}/{tag['id']}",
                timestamp=now,
                value=0,
                quality=Quality.BAD,
                source_driver=instance_id,
                source_address=tag["oid"],
                metadata={"snmp_error": error},
            )
            for tag in tags
        ]

    def _build_update(
        self, instance_id: str, tag: dict[str, Any], var_bind: Any, now: datetime
    ) -> TagUpdate:
        tag_id = f"{instance_id}/{tag['id']}"
        _oid, value = var_bind
        if isinstance(value, _EXCEPTION_VALUE_TYPES):
            self._metrics.error_count += 1
            error = value.prettyPrint()
            logger.warning(
                "snmp.tag_read_failed", instance_id=instance_id, tag_id=tag["id"], error=error
            )
            return TagUpdate(
                tag_id=tag_id,
                timestamp=now,
                value=0,
                quality=Quality.BAD,
                source_driver=instance_id,
                source_address=tag["oid"],
                metadata={"snmp_error": error},
            )
        self._metrics.tag_read_count += 1
        self._metrics.last_successful_read = now
        return TagUpdate(
            tag_id=tag_id,
            timestamp=now,
            value=asn1types.decode(value),
            quality=Quality.GOOD,
            source_driver=instance_id,
            source_address=tag["oid"],
            metadata={"snmp_type": type(value).__name__},
        )
