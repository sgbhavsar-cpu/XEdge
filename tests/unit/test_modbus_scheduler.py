"""Write-priority scheduler tests (Sprint C2, XEDGE-424).

The property that matters is observable ordering: when a write and a read
are both waiting, the write must execute first, regardless of which was
submitted first. These tests assert on an execution-order log rather than
just final results, since a bug here would still return the right values —
it would just serve them in the wrong order.
"""

from __future__ import annotations

import asyncio

import pytest

from xedge.drivers.modbus.scheduler import RequestPriority, RequestScheduler, SchedulerStoppedError


async def _wait_until(predicate: object, poll_interval: float = 0.005) -> None:
    # Polling an arbitrary caller-supplied predicate has no Event to wait on
    # instead — ASYNC110's suggestion doesn't apply to this generic helper
    # (same shape as tests/unit/test_dispatcher.py's own `_wait_until`).
    while not predicate():  # type: ignore[operator]  # noqa: ASYNC110
        await asyncio.sleep(poll_interval)


class TestBasicSubmission:
    async def test_submit_returns_the_coroutine_result(self) -> None:
        scheduler = RequestScheduler()
        scheduler.start()
        try:
            result = await scheduler.submit(RequestPriority.READ, lambda: _immediate(42))
            assert result == 42
        finally:
            await scheduler.stop()

    async def test_submit_propagates_the_coroutine_exception_to_the_caller(self) -> None:
        scheduler = RequestScheduler()
        scheduler.start()
        try:
            with pytest.raises(ValueError, match="boom"):
                await scheduler.submit(RequestPriority.READ, _raise_value_error)
        finally:
            await scheduler.stop()

    async def test_start_is_idempotent(self) -> None:
        scheduler = RequestScheduler()
        scheduler.start()
        scheduler.start()  # must not raise, must not start a second consumer
        try:
            assert await scheduler.submit(RequestPriority.READ, lambda: _immediate(1)) == 1
        finally:
            await scheduler.stop()


class TestSerialization:
    async def test_only_one_request_executes_at_a_time(self) -> None:
        """The property the removed per-transport asyncio.Lock used to
        provide: no two coro_factories run concurrently."""
        scheduler = RequestScheduler()
        scheduler.start()
        concurrent = 0
        max_concurrent = 0

        async def tracked() -> None:
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

        try:
            await asyncio.gather(
                *(scheduler.submit(RequestPriority.READ, tracked) for _ in range(5))
            )
            assert max_concurrent == 1
        finally:
            await scheduler.stop()


class TestPriorityOrdering:
    async def test_write_queued_behind_pending_reads_still_goes_first(self) -> None:
        """The headline XEDGE-424 property: a write submitted *after* several
        reads are already queued must still execute before them."""
        scheduler = RequestScheduler()
        order: list[str] = []

        async def slow_first_read() -> None:
            order.append("read-0")
            await asyncio.sleep(0.03)  # holds the consumer so the rest queue up

        async def read(n: int) -> None:
            order.append(f"read-{n}")

        async def write() -> None:
            order.append("write")

        scheduler.start()
        try:
            first = asyncio.ensure_future(scheduler.submit(RequestPriority.READ, slow_first_read))
            await _wait_until(lambda: order == ["read-0"])
            # Queue three more reads, then a write behind all of them.
            pending = [
                asyncio.ensure_future(scheduler.submit(RequestPriority.READ, lambda n=n: read(n)))
                for n in range(1, 4)
            ]
            write_future = asyncio.ensure_future(scheduler.submit(RequestPriority.WRITE, write))
            await asyncio.gather(first, *pending, write_future)
        finally:
            await scheduler.stop()

        assert order[0] == "read-0", "the request already executing must not be preempted"
        assert order[1] == "write", "the write must run before any of the reads queued behind it"
        assert order[2:] == ["read-1", "read-2", "read-3"], (
            "same-priority reads keep submission order"
        )

    async def test_multiple_writes_still_preserve_their_own_submission_order(self) -> None:
        scheduler = RequestScheduler()
        order: list[str] = []

        async def blocker() -> None:
            await asyncio.sleep(0.02)

        async def write(n: int) -> None:
            order.append(f"write-{n}")

        scheduler.start()
        try:
            first = asyncio.ensure_future(scheduler.submit(RequestPriority.READ, blocker))
            await asyncio.sleep(0.001)
            writes = [
                asyncio.ensure_future(scheduler.submit(RequestPriority.WRITE, lambda n=n: write(n)))
                for n in range(3)
            ]
            await asyncio.gather(first, *writes)
        finally:
            await scheduler.stop()

        assert order == ["write-0", "write-1", "write-2"]

    async def test_a_read_submitted_after_a_write_still_waits_behind_it(self) -> None:
        """Priority is about the *pending* queue at dequeue time, not a
        permanent reordering — a read submitted after the write is already
        running has no reason to jump ahead of anything."""
        scheduler = RequestScheduler()
        order: list[str] = []

        async def blocker() -> None:
            order.append("blocker")
            await asyncio.sleep(0.02)

        async def write() -> None:
            order.append("write")

        async def read() -> None:
            order.append("read")

        scheduler.start()
        try:
            first = asyncio.ensure_future(scheduler.submit(RequestPriority.READ, blocker))
            await _wait_until(lambda: order == ["blocker"])
            w = asyncio.ensure_future(scheduler.submit(RequestPriority.WRITE, write))
            r = asyncio.ensure_future(scheduler.submit(RequestPriority.READ, read))
            await asyncio.gather(first, w, r)
        finally:
            await scheduler.stop()

        assert order == ["blocker", "write", "read"]


class TestStop:
    async def test_stop_before_start_is_a_safe_no_op(self) -> None:
        await RequestScheduler().stop()  # must not raise

    async def test_stop_twice_is_safe(self) -> None:
        scheduler = RequestScheduler()
        scheduler.start()
        await scheduler.stop()
        await scheduler.stop()  # must not raise

    async def test_a_request_queued_but_never_reached_is_failed_on_stop(self) -> None:
        """Nothing should hang forever awaiting a future that stop() has no
        further intention of resolving."""
        scheduler = RequestScheduler()
        scheduler.start()

        async def blocker() -> None:
            await asyncio.sleep(10)  # long enough to still be "in flight" at stop()

        blocked = asyncio.ensure_future(scheduler.submit(RequestPriority.READ, blocker))
        await asyncio.sleep(0.005)
        never_reached = asyncio.ensure_future(
            scheduler.submit(RequestPriority.READ, lambda: _immediate(1))
        )
        await asyncio.sleep(0.005)

        await scheduler.stop()

        with pytest.raises(SchedulerStoppedError):
            await never_reached
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked


async def _immediate(value: int) -> int:
    return value


async def _raise_value_error() -> None:
    raise ValueError("boom")
