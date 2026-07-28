"""EtherNet/IP (CIP) scanner driver (FR-SB-005; FR-SA-009 scan rate,
FR-SA-010 deadband, FR-NB-009 write-back; Sprint C7, XEDGE-471/472/473/474/475).

Built directly on `pycomm3` (MIT; license-audit.md item 7) rather than a
clean-room CIP build — ADR-012 §1 follows the `bacpypes3`/BACnet "buy"
precedent (ADR-006) instead of ADR-006's Modbus/Sparkplug B/IEC 104
clean-room path, since CIP's object model, connection manager, and two
distinct messaging classes roughly double a from-scratch estimate versus
Modbus's flat register map.

**"Cyclic" here means polled explicit messaging, not CIP Class 1 implicit
I/O.** ADR-012 §1 and XEDGE-DR-001 Q-7 record that no mainstream Python CIP
library — `pycomm3` included — implements Class 1 connected (UDP 2222)
I/O; the accepted answer is explicit messaging (TCP 44818 request/response,
what `pycomm3` does well) repeated at each tag group's `scan_rate_ms`, which
is how most commercial OT gateways implement "cyclic" polling anyway. Every
docstring, log message, and schema description in this driver says "polled
explicit messaging" for that reason — never "implicit I/O" or "Class 1".

Scoped down for this MVP (XEDGE-472): scalar symbolic tags only (BOOL, INT,
DINT, REAL, STRING, and program-scoped equivalents). Array elements
('MyArray[3]') and UDT members ('MyUdt.Member') work through `pycomm3`
unchanged since they are just tag-name strings to it, but there is no
dedicated array/UDT decomposition or a runtime tag-discovery UI — a
misconfigured or unsupported tag_name simply reads back Bad quality via its
own `Tag.error`, same as any other tag-level failure.

`pycomm3.LogixDriver.open`/`close`/`read`/`write` are all synchronous,
blocking calls (confirmed by reading the installed package directly, not
assumed); every one of them is run via `asyncio.to_thread` here, matching
the precedent already established for `paho-mqtt` and `smtplib` elsewhere
in this codebase — a blocking call made directly from a coroutine shares
the event loop with everything else in this process and starves it.

Exception policy (mirrors `xedge.drivers.modbus.polling`'s distinction
between a Modbus exception response and a transport failure): pycomm3's own
`CommError` means the connection itself is broken and is left to propagate
out of `run()` to the DriverSupervisor, which restarts the whole instance
with backoff (NFR-R-006) — the same as a bare `ConnectionError`/`OSError`
would. `DataError`/`ResponseError`/`RequestError` (and `Tag.error` on an
otherwise-successful call, which pycomm3 already partitions per-tag
internally) mean a problem with one specific request/tag, not a dead
connection, and are handled locally as Bad quality / a failed `WriteResult`
without tearing down the connection.

No real-server integration test exists for this driver. `cpppo`'s CIP
simulator (`cpppo.server.enip`) was tried and connects successfully, but
every `pycomm3.LogixDriver` read/write against it returns
`Tag(..., error="Tag doesn't exist")` — cpppo's simulator implements
generic/simple CIP (its own docs describe `-S` as a "simple (non-routing)
... device, e.g. MicroLogix"), not the Logix symbolic tag/type-upload
semantics `pycomm3` requires. No other maintained, permissively-licensed
Logix-compatible CIP simulator was found. Tests therefore mock
`pycomm3.LogixDriver`'s public methods (`open`/`close`/`read`/`write`) at
the boundary instead — the same boundary this module treats as "bought,
trusted, third-party" per ADR-012 §1 — rather than re-verifying pycomm3's
own wire-protocol correctness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pycomm3 import LogixDriver
from pycomm3.exceptions import CommError, PycommError
from pycomm3.tag import Tag as PycommTag

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
from xedge.observability.logging import get_logger
from xedge.observability.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_DEFAULT_PORT = 44818
_DEFAULT_SLOT = 0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

ACCESS_READ_WRITE = "read_write"
ACCESS_READ_ONLY = "read_only"
ACCESS_WRITE_ONLY = "write_only"

__all__ = ["EtherNetIpDriver", "EtherNetIpDriverStateError"]


class EtherNetIpDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


def _tag_access(tag: dict[str, Any]) -> str:
    return tag.get("access", ACCESS_READ_WRITE)  # type: ignore[no-any-return]


def _connection_path(cfg: dict[str, Any]) -> str:
    host = cfg["host"]
    port = cfg.get("port", _DEFAULT_PORT)
    slot = cfg.get("slot", _DEFAULT_SLOT)
    return f"{host}:{port}/{slot}"


def _inverse_scale(value: TagValue, scaling: dict[str, Any] | None) -> TagValue:
    """Invert xedge.core.pipeline.normalize's forward scaling
    (`engineering_value = raw * scale + offset`), mirroring
    `xedge.drivers.modbus.polling._inverse_scale` — except a tag with no
    `scaling` configured returns `value` unchanged here rather than
    `float(value)`, since a CIP tag (unlike a Modbus register) may
    natively be BOOL or STRING and must not be coerced to a float just
    because no scaling was actually configured for it.
    """
    if not scaling:
        return value
    scale: float = scaling.get("scale", 1.0)
    offset: float = scaling.get("offset", 0.0)
    return (float(value) - offset) / scale


class EtherNetIpDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._driver: LogixDriver | None = None
        self._metrics = DriverMetrics()
        self._tags_by_id: dict[str, dict[str, Any]] = {}

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise EtherNetIpDriverStateError("configure() must be called before this operation")
        return self._config

    def _require_driver(self) -> LogixDriver:
        if self._driver is None:
            raise EtherNetIpDriverStateError("connect() must be called before this operation")
        return self._driver

    async def configure(self, config: DriverConfig) -> None:
        self._config = config
        self._tags_by_id = {tag["id"]: tag for group in config.tag_groups for tag in group["tags"]}

    async def connect(self) -> None:
        cfg = self._require_config().config
        path = _connection_path(cfg)
        driver = LogixDriver(path)
        driver.socket_timeout = float(
            cfg.get("connect_timeout_seconds", _DEFAULT_CONNECT_TIMEOUT_SECONDS)
        )
        try:
            opened = await asyncio.to_thread(driver.open)
        except PycommError as exc:
            raise DriverConnectionError(f"failed to open EtherNet/IP session: {exc}") from exc
        if not opened:
            raise DriverConnectionError(f"failed to open EtherNet/IP session to {path}")
        self._driver = driver

    async def disconnect(self) -> None:
        if self._driver is not None:
            try:
                await asyncio.to_thread(self._driver.close)
            except PycommError as exc:
                logger.warning("ethernet_ip.disconnect_error", error=str(exc))
            self._driver = None

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
        """FR-NB-009 write-back / XEDGE-473 explicit write.

        A tag configured `access: read_only` is refused before ever
        reaching the wire, matching `xedge.drivers.modbus.polling`'s
        identical safeguard.
        """
        tag = self._tags_by_id.get(tag_id)
        if tag is None:
            return WriteResult(success=False, tag_id=tag_id, error_message="Unknown tag")
        if _tag_access(tag) == ACCESS_READ_ONLY:
            return WriteResult(
                success=False,
                tag_id=tag_id,
                error_message="Tag is configured read-only (access: read_only)",
            )

        driver = self._require_driver()
        raw_value = _inverse_scale(value, tag.get("scaling"))
        try:
            result: PycommTag = await asyncio.to_thread(driver.write, tag["tag_name"], raw_value)
        except CommError:
            # Connection is dead, not just this write — surface it as a
            # normal exception so run()'s read loop (or the next write)
            # hits the same failure and the supervisor restarts the
            # instance, rather than reporting a misleading one-off failure.
            raise
        except PycommError as exc:
            self._metrics.error_count += 1
            return WriteResult(success=False, tag_id=tag_id, error_message=str(exc))
        if not result:
            self._metrics.error_count += 1
            return WriteResult(
                success=False, tag_id=tag_id, error_message=result.error or "write failed"
            )
        return WriteResult(success=True, tag_id=tag_id)

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        instance_id = self._config.instance_id if self._config else "ethernet_ip"
        interval_seconds = group["scan_rate_ms"] / 1000
        # write_only tags exist to be written, never polled (matches
        # xedge.drivers.modbus.polling's identical ACCESS_WRITE_ONLY rule).
        tags = [tag for tag in group["tags"] if _tag_access(tag) != ACCESS_WRITE_ONLY]
        tag_names = [tag["tag_name"] for tag in tags]
        while tag_names:
            updates = await self._read_tags(instance_id, tags, tag_names)
            for update in updates:
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tags(
        self, instance_id: str, tags: list[dict[str, Any]], tag_names: list[str]
    ) -> list[TagUpdate]:
        driver = self._require_driver()
        with tracer.start_as_current_span(
            "driver.read",
            attributes={"driver.instance_id": instance_id, "tag.count": len(tags)},
        ) as span:
            try:
                raw_results = await asyncio.to_thread(driver.read, *tag_names)
            except CommError as exc:
                # Transport-level failure: let it propagate out of
                # run()/_poll_group so the DriverSupervisor restarts this
                # instance (NFR-R-006) instead of reporting every tag Bad
                # forever against a connection that is never coming back
                # on its own.
                span.set_attribute("quality", Quality.BAD.value)
                span.record_exception(exc)
                raise
            except PycommError as exc:
                # DataError/ResponseError/RequestError: a request/response
                # problem, not a dead connection — mark every tag in this
                # group Bad for this cycle and keep polling, mirroring how
                # xedge.drivers.modbus.polling handles a ModbusException.
                self._metrics.error_count += len(tags)
                span.set_attribute("quality", Quality.BAD.value)
                span.record_exception(exc)
                logger.warning("ethernet_ip.read_failed", instance_id=instance_id, error=str(exc))
                now = datetime.now(UTC)
                return [self._bad_update(instance_id, tag, str(exc), now) for tag in tags]

            results = raw_results if isinstance(raw_results, list) else [raw_results]
            now = datetime.now(UTC)
            updates = [
                self._build_update(instance_id, tag, result, now)
                for tag, result in zip(tags, results, strict=True)
            ]
            bad_count = sum(1 for update in updates if update.quality == Quality.BAD)
            span.set_attribute(
                "quality", Quality.BAD.value if bad_count else Quality.GOOD.value
            )
            span.set_attribute("tag.bad_count", bad_count)
            return updates

    def _build_update(
        self, instance_id: str, tag: dict[str, Any], result: PycommTag, now: datetime
    ) -> TagUpdate:
        tag_id = f"{instance_id}/{tag['id']}"
        if not result:
            self._metrics.error_count += 1
            logger.warning(
                "ethernet_ip.tag_read_failed",
                instance_id=instance_id,
                tag_id=tag["id"],
                error=result.error,
            )
            return self._bad_update(instance_id, tag, result.error or "read failed", now)
        self._metrics.tag_read_count += 1
        self._metrics.last_successful_read = now
        return TagUpdate(
            tag_id=tag_id,
            timestamp=now,
            value=_coerce_value(result.value),
            quality=Quality.GOOD,
            source_driver=instance_id,
            source_address=tag["tag_name"],
            metadata={"ethernet_ip_type": result.type},
        )

    def _bad_update(
        self, instance_id: str, tag: dict[str, Any], error: str, now: datetime
    ) -> TagUpdate:
        return TagUpdate(
            tag_id=f"{instance_id}/{tag['id']}",
            timestamp=now,
            value=0,
            quality=Quality.BAD,
            source_driver=instance_id,
            source_address=tag["tag_name"],
            metadata={"ethernet_ip_error": error},
        )


def _coerce_value(value: Any) -> TagValue:
    """Coerce a pycomm3 tag value to xEdge's TagValue union. Scalar
    BOOL/SINT/INT/DINT/REAL/STRING tags already come back as bool/int/
    float/str (confirmed directly against the installed package); this
    narrows anything else — a UDT's dict of members, an array's list of
    elements — to a string rather than letting an unsupported type reach
    the pipeline, matching this driver's scalar-tags-only MVP scope
    (XEDGE-472)."""
    if isinstance(value, bool | int | float | str | bytes):
        return value
    return str(value)
