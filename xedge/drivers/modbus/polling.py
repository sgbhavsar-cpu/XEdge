"""Shared tag-group polling loop for all Modbus transport variants (TCP,
RTU-over-TCP, RTU-serial). A Modbus-level exception response marks only the
affected tags Bad (FR-DP-005) without tearing down the connection, while a
transport failure propagates so the DriverSupervisor restarts the whole
instance (NFR-R-006).

Subclasses implement only the transport: `_connect_transport()`,
`_disconnect_transport()`, `_read_block(function_code, address, quantity)`,
`_write_one(...)`, and `_write_registers(...)`. `connect()`/`disconnect()`
themselves are concrete here (see Sprint C2 note below) — a transport never
overrides them directly.

Sprint C1 rewrote this loop in three ways (XEDGE-410/411/412/414):

* **Block reads, not per-tag reads.** A tag group is planned into the fewest
  requests that cover it (`xedge.drivers.modbus.planner`). A 100-tag
  contiguous group cost 100 round trips per cycle; it is now one.
* **Fixed-period scheduling.** The old loop read every tag and *then* slept
  `scan_rate_ms`, so the true period was `read_time + scan_rate` and drifted
  with bus latency — a 50 ms scan rate over 100 tags at 5 ms each actually
  cycled every 550 ms. The next cycle is now scheduled against a monotonic
  deadline, so the configured rate is the rate.
* **Multi-register data types.** A tag may span 2 or 4 registers
  (`xedge.drivers.modbus.datatypes`), so a value is decoded from its slice of
  the block rather than assumed to be a single register.

Sprint C2 adds three more (XEDGE-421/422/423/424):

* **Device connectivity.** Every block outcome feeds a `ConnectivityTracker`
  (`xedge.core.connectivity`, ADR-011) — the one piece of state a Modbus
  exception genuinely couldn't represent: `state` on `DriverInstanceStatus`
  only reflects whether the asyncio task is alive, and a Modbus exception
  never tears that down, so a device rejecting every read for the last ten
  cycles used to report identically to one answering perfectly.
* **Write priority.** Every read and write now funnels through a
  `RequestScheduler` (`xedge.drivers.modbus.scheduler`) instead of a bare
  `asyncio.Lock` — writes dequeue ahead of any pending read. Scoped to
  `connect()`/`disconnect()`, not `run()`: `write()` is a valid call the
  moment a driver is connected — the write-back path, and several of this
  package's own tests, call it directly without ever starting the poll
  loop — so tying the scheduler to `run()` alone left exactly that case
  waiting on a consumer that was never started. `connect()`/`disconnect()`
  are therefore concrete methods here that start/stop the scheduler around
  a new transport-specific `_connect_transport()`/`_disconnect_transport()`
  hook, rather than abstract methods each transport overrode directly.
* **Dedicated write tags and FC16.** A tag's `access` (`read_write` default,
  `read_only`, `write_only`) decides whether it is polled at all and whether
  `write()` accepts it. A multi-register tag is written via FC16
  (`_write_registers`) instead of being refused, as it was for one sprint.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from opentelemetry.trace import Status, StatusCode

from xedge.core.connectivity import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_RECOVERY_THRESHOLD,
    ConnectivityState,
    ConnectivityTracker,
)
from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
    DriverMetrics,
    Quality,
    TagUpdate,
    TagValue,
    WriteResult,
)
from xedge.drivers.modbus import codec, datatypes, planner
from xedge.drivers.modbus.scheduler import RequestPriority, RequestScheduler
from xedge.observability.logging import get_logger
from xedge.observability.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

FUNCTION_CODE_BY_NAME: dict[str, codec.FunctionCode] = {
    "read_coils": codec.FunctionCode.READ_COILS,
    "read_discrete_inputs": codec.FunctionCode.READ_DISCRETE_INPUTS,
    "read_holding_registers": codec.FunctionCode.READ_HOLDING_REGISTERS,
    "read_input_registers": codec.FunctionCode.READ_INPUT_REGISTERS,
}

# Write function code for each *readable* function code, where the
# underlying Modbus memory class is writable at all (FR-NB-009 write-back,
# XEDGE-223). Discrete inputs and input registers are read-only memory
# classes on real devices — there is no Modbus write function code for
# them, so a write attempt against a tag configured with either is
# rejected before ever reaching the wire.
_WRITE_FUNCTION_CODE_FOR_READ: dict[str, codec.FunctionCode] = {
    "read_coils": codec.FunctionCode.WRITE_SINGLE_COIL,
    "read_holding_registers": codec.FunctionCode.WRITE_SINGLE_REGISTER,
}

_DEFAULT_RETRY_COUNT = 0
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.05
_OVERRUN_LOG_INTERVAL_SECONDS = 10.0

# Tag `access` values (XEDGE-422). `read_write` is the default and matches
# every config written before this sprint exactly: any tag whose function
# code maps to a write function code was, and still is, writable. The two
# new values are opt-in.
ACCESS_READ_WRITE = "read_write"
ACCESS_READ_ONLY = "read_only"
ACCESS_WRITE_ONLY = "write_only"


def _tag_access(tag: dict[str, Any]) -> str:
    return tag.get("access", ACCESS_READ_WRITE)  # type: ignore[no-any-return]


def _inverse_scale(value: TagValue, scaling: dict[str, Any] | None) -> float:
    """Invert xedge.core.pipeline.normalize's forward scaling
    (`engineering_value = raw * scale + offset`) so a write request
    expressed in engineering units lands as the correct raw value:
    `raw = (engineering_value - offset) / scale`.

    Returns a float; the caller rounds it for integer data types and keeps
    the fraction for float ones (see `datatypes.encode`).
    """
    if scaling is None:
        return float(value)
    scale: float = scaling.get("scale", 1.0)
    offset: float = scaling.get("offset", 0.0)
    return (float(value) - offset) / scale


class ModbusDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order (e.g. connect()
    before configure(), or a read before connect())."""


class BaseModbusPollingDriver(BaseDriver):
    """Common tag-group polling loop, TagUpdate construction, and metrics
    tracking. Not transport-specific — see module docstring."""

    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._metrics = DriverMetrics()
        self._tags_by_id: dict[str, dict[str, Any]] = {}
        self._blocks_by_group: dict[str, list[planner.ReadBlock]] = {}
        self._connectivity = ConnectivityTracker()
        self._scheduler = RequestScheduler()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise ModbusDriverStateError("configure() must be called before this operation")
        return self._config

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

        # Planned once here rather than per cycle: the plan is a pure function
        # of config, and a config change arrives as a driver restart
        # (hot-reload, XEDGE-184) rather than in-place mutation.
        self._blocks_by_group = {}
        for group in config.tag_groups:
            # write_only tags exist to be written, never polled (XEDGE-422) —
            # excluded here so they never enter a read request at all, not
            # filtered out of one after the fact.
            readable_tags = [tag for tag in group["tags"] if _tag_access(tag) != ACCESS_WRITE_ONLY]
            blocks = planner.plan_read_blocks(
                readable_tags,
                FUNCTION_CODE_BY_NAME,
                max_block_size=group.get("max_block_size"),
                max_block_gap=group.get("max_block_gap", planner.DEFAULT_MAX_BLOCK_GAP),
            )
            self._blocks_by_group[group["id"]] = blocks
            logger.info(
                "modbus.read_plan",
                instance_id=config.instance_id,
                tag_group=group["id"],
                tag_count=len(group["tags"]),
                readable_tag_count=len(readable_tags),
                request_count=len(blocks),
                largest_block=max((block.quantity for block in blocks), default=0),
            )

    async def connect(self) -> None:
        await self._connect_transport()
        # Started only once the transport is actually up — a failed
        # `_connect_transport()` raises before this line, so no scheduler is
        # ever left running with nothing behind it.
        self._scheduler.start()

    async def disconnect(self) -> None:
        # Stop accepting/serving new work before tearing down the transport
        # it depends on, not after.
        await self._scheduler.stop()
        await self._disconnect_transport()

    async def _connect_transport(self) -> None:
        """Transport hook: establish the underlying connection."""
        raise NotImplementedError

    async def _disconnect_transport(self) -> None:
        """Transport hook: release the underlying connection. Must be safe
        to call even if `_connect_transport()` never succeeded — same
        contract `BaseDriver.disconnect()` documents."""
        raise NotImplementedError

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
        """FR-NB-009 write-back (Sprint 31, XEDGE-223).

        Covers FC05/06 (single coil/register, unchanged since Sprint 2) and,
        as of Sprint C2 (XEDGE-423), FC16 for any tag whose `data_type` spans
        more than one register — computed via the same `datatypes.encode`
        that decodes reads, so a write is exactly `decode`'s inverse rather
        than a second, separately-maintained encoding.

        A tag configured `access: read_only` (XEDGE-422) is refused
        regardless of its function code — the CRD gap this closes wasn't
        "can't write multi-register tags," it was "no way to mark a tag as
        never writable at all," which every mappable tag was, implicitly,
        before this sprint.
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

        read_function_name = tag["function_code"]
        write_function_code = _WRITE_FUNCTION_CODE_FOR_READ.get(read_function_name)
        if write_function_code is None:
            return WriteResult(
                success=False,
                tag_id=tag_id,
                error_message=f"{read_function_name} is a read-only Modbus memory class",
            )

        address = tag["address"]
        try:
            if write_function_code == codec.FunctionCode.WRITE_SINGLE_COIL:
                await self._scheduler.submit(
                    RequestPriority.WRITE,
                    lambda: self._write_one(write_function_code, address, bool(value)),
                )
            else:
                await self._write_register_tag(write_function_code, address, value, tag)
        except (codec.ModbusException, datatypes.ModbusDataTypeError) as exc:
            self._metrics.error_count += 1
            logger.warning("modbus.write_rejected", tag_id=tag_id, error=str(exc))
            return WriteResult(success=False, tag_id=tag_id, error_message=str(exc))
        return WriteResult(success=True, tag_id=tag_id)

    async def _write_register_tag(
        self,
        write_function_code: codec.FunctionCode,
        address: int,
        value: TagValue,
        tag: dict[str, Any],
    ) -> None:
        data_type = tag.get("data_type", datatypes.DEFAULT_DATA_TYPE)
        inverse = _inverse_scale(value, tag.get("scaling"))
        raw_value: int | float = inverse if datatypes.is_float(data_type) else round(inverse)
        registers = datatypes.encode(
            raw_value,
            data_type,
            word_order=tag.get("word_order", datatypes.DEFAULT_WORD_ORDER),
            byte_order=tag.get("byte_order", datatypes.DEFAULT_BYTE_ORDER),
        )
        if len(registers) == 1:
            await self._scheduler.submit(
                RequestPriority.WRITE,
                lambda: self._write_one(write_function_code, address, registers[0]),
            )
        else:
            await self._scheduler.submit(
                RequestPriority.WRITE, lambda: self._write_registers(address, registers)
            )

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    def get_connectivity_state(self) -> ConnectivityState:
        """Device-level connectivity (XEDGE-421), distinct from whether this
        driver's asyncio task is alive — see the module docstring. Read
        live by `DriverSupervisor._with_live_metrics` on every status()
        call, the same pattern `get_metrics()` already uses."""
        return self._connectivity.state

    async def _read_block(
        self, function_code: codec.FunctionCode, address: int, quantity: int
    ) -> list[int] | list[bool]:
        """Transport hook: issue one read request and return its decoded
        values in ascending address order. Register function codes return
        ints, bit function codes return bools."""
        raise NotImplementedError

    async def _write_one(
        self, function_code: codec.FunctionCode, address: int, value: int | bool
    ) -> None:
        """Transport hook: FC05 (single coil) or FC06 (single register)."""
        raise NotImplementedError

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """Transport hook: FC16 (write multiple registers), for a tag whose
        data_type spans more than one register (XEDGE-423)."""
        raise NotImplementedError

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        """Read `group` on a fixed period (XEDGE-410).

        The deadline advances by exactly `scan_rate_ms` each cycle rather than
        the loop sleeping for it after the work, so read latency no longer
        accumulates into the period.

        When a cycle cannot fit its slot the deadline is reset to *now* rather
        than advanced by whole intervals. Advancing by whole intervals looks
        tidier but behaves far worse: a 3 ms overrun on a 60 ms group would
        push the next read a further 57 ms out, halving the effective rate
        because the cycle was 5% late. Resetting drops the accumulated
        lateness and keeps the loop running as fast as the bus allows, which
        is the best available answer once the configured rate is unreachable.
        """
        interval_seconds = group["scan_rate_ms"] / 1000
        instance_id = self._config.instance_id if self._config else "modbus"
        blocks = self._blocks_by_group.get(group["id"], [])
        overrunning = False
        last_overrun_log = 0.0

        next_due = time.monotonic()
        while True:
            for block in blocks:
                for update in await self._read_one_block(instance_id, block):
                    await output.put(update)

            next_due += interval_seconds
            now = time.monotonic()
            if now >= next_due:
                overrun_seconds = now - next_due
                next_due = now
                # Log on entering the overrun state, then at most every 10s.
                # A group overrunning at a 10 ms scan rate would otherwise
                # emit 100 identical warnings per second.
                if not overrunning or now - last_overrun_log >= _OVERRUN_LOG_INTERVAL_SECONDS:
                    logger.warning(
                        "modbus.scan_overrun",
                        instance_id=instance_id,
                        tag_group=group["id"],
                        scan_rate_ms=group["scan_rate_ms"],
                        overrun_ms=round(overrun_seconds * 1000, 1),
                        request_count=len(blocks),
                    )
                    last_overrun_log = now
                overrunning = True
                # Yield so a permanently-overrunning group cannot starve the
                # event loop, but do not wait out the interval.
                await asyncio.sleep(0)
                continue

            if overrunning:
                logger.info(
                    "modbus.scan_overrun_recovered",
                    instance_id=instance_id,
                    tag_group=group["id"],
                    scan_rate_ms=group["scan_rate_ms"],
                )
                overrunning = False
            await asyncio.sleep(next_due - now)

    async def _read_one_block(self, instance_id: str, block: planner.ReadBlock) -> list[TagUpdate]:
        """Read one planned block and decode every tag it covers.

        On a Modbus exception a multi-tag block is retried tag by tag.
        Batching otherwise costs error precision: one unmapped register inside
        a block makes the device reject the whole request, which would mark
        every tag in it Bad even though only one is genuinely unreadable.
        Falling back preserves the per-tag attribution the unbatched loop had,
        at the cost of one slow cycle for a group containing a bad address.

        Every outcome — this call is the recursive entry point for both the
        optimistic batched attempt and each individual fallback read — feeds
        `self._connectivity` (XEDGE-421): each is a genuine, distinct wire
        transaction, so recording all of them is a more honest connectivity
        signal than recording only the top-level attempt.
        """
        # A span now measures one *request*, not one tag, since a request can
        # cover many tags. `tag.id` is therefore only meaningful — and only
        # set — when the block covers exactly one tag; emitting the full list
        # for a batched block would put unbounded cardinality into a span
        # attribute, which tracing backends handle badly. `modbus.tag_count`
        # carries the batching factor for every block.
        attributes: dict[str, Any] = {
            "driver.instance_id": instance_id,
            "modbus.function_code": int(block.function_code),
            "modbus.address": block.address,
            "modbus.quantity": block.quantity,
            "modbus.tag_count": len(block.tags),
        }
        if not block.is_batched:
            attributes["tag.id"] = block.tags[0].tag["id"]

        with tracer.start_as_current_span("driver.read", attributes=attributes) as span:
            started_at = time.monotonic()
            try:
                values = await self._read_block_with_retries(block)
            except codec.ModbusException as exc:
                self._connectivity.record_failure(reason=_exception_name(exc))
                span.set_attribute("quality", Quality.BAD.value)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                if block.is_batched:
                    logger.info(
                        "modbus.block_exception_falling_back",
                        instance_id=instance_id,
                        address=block.address,
                        quantity=block.quantity,
                        tag_count=len(block.tags),
                        exception_code=exc.exception_code,
                        exception_name=_exception_name(exc),
                    )
                    return await self._read_block_tag_by_tag(instance_id, block)
                self._metrics.error_count += 1
                logger.warning(
                    "modbus.tag_exception",
                    instance_id=instance_id,
                    tag_id=block.tags[0].tag["id"],
                    exception_code=exc.exception_code,
                    exception_name=_exception_name(exc),
                )
                return [_bad_update(instance_id, block.tags[0], block.function_code, exc)]

            self._connectivity.record_success()
            latency_ms = (time.monotonic() - started_at) * 1000
            self._metrics.tag_read_count += len(block.tags)
            self._metrics.last_successful_read = datetime.now(UTC)
            span.set_attribute("quality", Quality.GOOD.value)
            return [
                _good_update(instance_id, planned, block, values, latency_ms)
                for planned in block.tags
            ]

    async def _read_block_with_retries(self, block: planner.ReadBlock) -> list[int] | list[bool]:
        """Issue a block read through the write-priority scheduler
        (XEDGE-424), retrying per XEDGE-414's configuration.

        `retry_on_exception` defaults to False because a Modbus exception is a
        considered answer rather than a lost message: ILLEGAL_DATA_ADDRESS
        gives the same answer however many times it is asked, and retrying it
        only spends bus time. SLAVE_DEVICE_BUSY is the case where a retry
        genuinely helps, which is why the option exists at all.

        Each attempt is a fresh `submit()` call, so a write that arrives
        between retries is still served ahead of the next attempt — exactly
        as it would be ahead of any other pending read.
        """
        cfg = self._require_config().config
        retry_count = int(cfg.get("retry_count", _DEFAULT_RETRY_COUNT))
        retry_on_exception = bool(cfg.get("retry_on_exception", False))
        backoff = float(cfg.get("retry_backoff_seconds", _DEFAULT_RETRY_BACKOFF_SECONDS))

        attempt = 0
        while True:
            try:
                return await self._scheduler.submit(
                    RequestPriority.READ,
                    lambda: self._read_block(block.function_code, block.address, block.quantity),
                )
            except codec.ModbusException:
                if not retry_on_exception or attempt >= retry_count:
                    raise
            except (TimeoutError, OSError, codec.ModbusFramingError):
                # Transport-level failure. Retry if configured; otherwise let
                # it propagate so the supervisor restarts the instance
                # (NFR-R-006) rather than degrading silently and forever.
                if attempt >= retry_count:
                    raise
            attempt += 1
            self._metrics.error_count += 1
            if backoff > 0:
                await asyncio.sleep(backoff)

    async def _read_block_tag_by_tag(
        self, instance_id: str, block: planner.ReadBlock
    ) -> list[TagUpdate]:
        """Re-read each tag of a rejected block individually, so one bad
        address does not mark its neighbours Bad."""
        updates: list[TagUpdate] = []
        for planned in block.tags:
            single = planner.ReadBlock(
                function_code=block.function_code,
                address=planned.tag["address"],
                quantity=planned.span,
                tags=(planner.PlannedTag(tag=planned.tag, offset=0, span=planned.span),),
            )
            updates.extend(await self._read_one_block(instance_id, single))
        return updates


def _exception_name(exc: codec.ModbusException) -> str:
    """Human-readable Modbus exception name (XEDGE-425). Before Sprint C1
    only the raw numeric code ever reached the operator."""
    try:
        return codec.ExceptionCode(exc.exception_code).name
    except ValueError:
        return f"UNKNOWN(0x{exc.exception_code:02X})"


def _good_update(
    instance_id: str,
    planned: planner.PlannedTag,
    block: planner.ReadBlock,
    values: list[int] | list[bool],
    latency_ms: float,
) -> TagUpdate:
    tag = planned.tag
    if codec.is_bit_function(block.function_code):
        value: TagValue = bool(values[planned.offset])
    else:
        registers = [int(v) for v in values[planned.offset : planned.offset + planned.span]]
        value = datatypes.decode(
            registers,
            tag.get("data_type", datatypes.DEFAULT_DATA_TYPE),
            word_order=tag.get("word_order", datatypes.DEFAULT_WORD_ORDER),
            byte_order=tag.get("byte_order", datatypes.DEFAULT_BYTE_ORDER),
        )
    return TagUpdate(
        tag_id=f"{instance_id}/{tag['id']}",
        timestamp=datetime.now(UTC),
        value=value,
        quality=Quality.GOOD,
        source_driver=instance_id,
        source_address=str(tag["address"]),
        metadata={
            "modbus_exception": None,
            "request_latency_ms": round(latency_ms, 2),
            # How many other tags shared this read. 0 means the tag was read
            # alone, so an operator can see batching actually taking effect.
            "batched_with": len(block.tags) - 1,
        },
    )


def _bad_update(
    instance_id: str,
    planned: planner.PlannedTag,
    function_code: codec.FunctionCode,
    exc: codec.ModbusException,
) -> TagUpdate:
    tag = planned.tag
    data_type = tag.get("data_type", datatypes.DEFAULT_DATA_TYPE)
    if codec.is_bit_function(function_code):
        placeholder: TagValue = False
    else:
        placeholder = 0.0 if datatypes.is_float(data_type) else 0
    return TagUpdate(
        tag_id=f"{instance_id}/{tag['id']}",
        timestamp=datetime.now(UTC),
        value=placeholder,
        quality=Quality.BAD,
        source_driver=instance_id,
        source_address=str(tag["address"]),
        metadata={
            "modbus_exception": exc.exception_code,
            "modbus_exception_name": _exception_name(exc),
        },
    )
