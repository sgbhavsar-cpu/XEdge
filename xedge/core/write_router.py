"""Write-back routing (Sprint 31, XEDGE-223): the single chokepoint every
write-back caller goes through — the REST API (`xedge.api.server`) and the
MQTT NCMD command path (`xedge.northbound.mqtt`) both call `WriteRouter.write`
rather than reaching into `DriverSupervisor`/`BaseDriver.write()` directly.
Centralizing here means both callers get identical audit-logging and
"is this driver actually running" behavior for free, and a future third
caller (e.g. a remote diagnostic write command) only needs to call this,
not re-derive the routing/audit logic.

Tag addressing matches every other tag-scoped REST endpoint already in this
codebase (`GET /api/v1/drivers/{instance_id}/tags`, etc.): a `(instance_id,
tag_name)` pair, not the single slash-joined `tag_id` string `TagUpdate`
carries — `BaseDriver.write()` itself takes the bare `tag_name` (see
`xedge.drivers.loopback.driver.LoopbackDriver.write`'s docstring), so this
module only needs to resolve `instance_id` to a live driver, not parse a
combined id.
"""

from __future__ import annotations

from dataclasses import replace

from xedge.core.supervisor import DriverState, DriverSupervisor
from xedge.drivers.base import TagValue, WriteResult
from xedge.observability.audit_log import AuditLog


class WriteRouter:
    def __init__(self, supervisor: DriverSupervisor, audit_log: AuditLog) -> None:
        self._supervisor = supervisor
        self._audit_log = audit_log

    async def write(
        self, actor: str, instance_id: str, tag_name: str, value: TagValue
    ) -> WriteResult:
        """Route a write to `instance_id`'s live driver, if it's actually
        running (a disabled/stopped/backing-off instance rejects the write
        rather than risking a call against a stale/disconnected driver
        object — see `DriverSupervisor.get_driver`'s docstring). Every
        attempt is audit-logged, success or failure, under one event name
        (`tag.write`) regardless of which caller (REST, MQTT NCMD) invoked
        it — `actor` distinguishes a human username from a system actor
        like `"mqtt-ncmd"`."""
        tag_id = f"{instance_id}/{tag_name}"
        try:
            status = self._supervisor.status(instance_id)
        except KeyError:
            result = WriteResult(
                success=False,
                tag_id=tag_id,
                error_message=f"No such driver instance: {instance_id!r}",
            )
            self._audit(actor, result, value)
            return result
        if status.state != DriverState.RUNNING:
            result = WriteResult(
                success=False,
                tag_id=tag_id,
                error_message=(
                    f"Driver instance {instance_id!r} is not running (state: {status.state.value})"
                ),
            )
            self._audit(actor, result, value)
            return result

        driver = self._supervisor.get_driver(instance_id)
        if driver is None:
            result = WriteResult(
                success=False, tag_id=tag_id, error_message=f"No live driver for {instance_id!r}"
            )
            self._audit(actor, result, value)
            return result

        # Every BaseDriver.write() implementation returns a WriteResult
        # carrying the *bare* tag name it was called with (see
        # xedge.drivers.loopback.driver.LoopbackDriver.write's docstring) —
        # re-stamp it with the fully-qualified `instance_id/tag_name` id
        # every other tag-scoped API in this codebase uses.
        result = replace(await driver.write(tag_name, value), tag_id=tag_id)
        self._audit(actor, result, value)
        return result

    def _audit(self, actor: str, result: WriteResult, value: TagValue) -> None:
        self._audit_log.append(
            actor,
            "tag.write",
            {
                "tag_id": result.tag_id,
                "value": value if isinstance(value, (bool, int, float, str)) else repr(value),
                "success": result.success,
                "error": result.error_message,
            },
        )
