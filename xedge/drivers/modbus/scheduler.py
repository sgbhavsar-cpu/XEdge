"""Write-priority request scheduler (Sprint C2, XEDGE-424; ADR-011 Part 2).

XEDGE-CRD-001 §4.1/§4.2 ask to "prioritize write parameters vs read" — today
writes and reads compete for the same per-transport `asyncio.Lock` with no
ordering guarantee: a write queued right after a 100-register read starts
waits for that entire read to finish, indistinguishable from any other
pending read.

`RequestScheduler` replaces that lock with a priority queue and a single
consumer task: every read and write submitted to the same instance funnels
through it, one at a time, with writes always dequeued ahead of any pending
read regardless of submission order. This is "transport-neutral" per
ADR-011 — the queue and priority logic have no serial/TCP-specific code, so
the same class is used by every Modbus transport unmodified.

ADR-011 Part 1 goes one step further for RS-485: a *shared* bus manager
that gives several driver instances (several slaves) a scheduler each, all
multiplexed onto one physical port. That is Sprint C3 (XEDGE-431) — this
sprint's scheduler is one instance per driver, which is already the full
answer for TCP (each instance owns its own connection) and is the
foundation C3 wraps rather than replaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


class RequestPriority:
    """Lower value dequeues first. Not an IntEnum: a plain int comparison is
    all `_QueuedRequest`'s ordering needs, and two bands are all any Modbus
    transport needs today — a third tier (e.g. background diagnostics) can
    slot in at a value between these without renumbering everything."""

    WRITE = 0
    READ = 10


class SchedulerStoppedError(RuntimeError):
    """Raised to any caller still awaiting `submit()` when `stop()` is
    called — otherwise that caller would hang forever on a future nothing
    will ever resolve."""


@dataclass(order=True)
class _QueuedRequest:
    priority: int
    sequence: int
    # Excluded from ordering: neither a coroutine function nor a Future is
    # orderable, and correctness only needs (priority, sequence) — sequence
    # alone (assigned by an ever-increasing counter) already breaks ties
    # between same-priority items in submission order, so comparison never
    # needs to reach these fields.
    coro_factory: Callable[[], Awaitable[Any]] = field(compare=False)
    future: asyncio.Future[Any] = field(compare=False)


class RequestScheduler:
    """One priority queue plus one consumer task per driver instance.

    Not started until `start()` (called from `BaseModbusPollingDriver.run()`,
    so its lifetime matches "connected and polling") and stopped by `stop()`
    (in `run()`'s `finally`, alongside the poll-group tasks) — mirroring how
    `_poll_group` tasks are themselves scoped to `run()`.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[_QueuedRequest] = asyncio.PriorityQueue()
        self._counter = itertools.count()
        self._consumer_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Idempotent — a second call while already running is a no-op, so
        callers never need to check `is_running` first."""
        if self._consumer_task is None:
            self._consumer_task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Cancel the consumer and fail every request still queued, so a
        caller awaiting `submit()` never hangs past `stop()`. Safe to call
        when never started, and safe to call twice."""
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None

        while not self._queue.empty():
            item = self._queue.get_nowait()
            if not item.future.done():
                item.future.set_exception(SchedulerStoppedError("RequestScheduler.stop() called"))

    async def submit(self, priority: int, coro_factory: Callable[[], Awaitable[T]]) -> T:
        """Queue `coro_factory` at `priority` and await its result.

        Every submission — read or write, from any caller — passes through
        the same queue, so exactly one request is ever in flight on the
        connection at a time (the property the per-transport lock used to
        provide), while writes still cut ahead of any read queued before or
        after them.
        """
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        item = _QueuedRequest(
            priority=priority,
            sequence=next(self._counter),
            coro_factory=coro_factory,
            future=future,
        )
        await self._queue.put(item)
        return await future

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item.future.cancelled():
                continue
            try:
                result = await item.coro_factory()
            except Exception as exc:  # noqa: BLE001 — propagate to the submitting caller, not here
                if not item.future.done():
                    item.future.set_exception(exc)
            else:
                if not item.future.done():
                    item.future.set_result(result)
