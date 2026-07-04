"""Northbound dispatcher: drains the ring buffer and delivers batches to a
connector, with connect/reconnect backoff (FR-NB-010, system-architecture.md §3.5).

Mirrors the DriverSupervisor's backoff shape (xedge.core.supervisor) so the
two subsystems behave consistently, but is intentionally separate: a
northbound outage must not affect driver polling, and vice versa.
"""

from __future__ import annotations

import asyncio

from xedge.northbound.base import NorthboundConnector
from xedge.observability.logging import get_logger
from xedge.store.ring_buffer import RingBufferManager
from xedge.store.sqlite_store import SqliteColdStore

logger = get_logger(__name__)

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 300.0
_BACKOFF_MULTIPLIER = 2.0
_DEFAULT_REPLAY_BATCH_SIZE = 500


class NorthboundDispatcher:
    """Owns one connector's connect/publish lifecycle.

    `run()` loops forever: reconnect with exponential backoff while
    disconnected — replaying any cold-store backlog (FR-SF-004) immediately
    after a successful (re)connect, oldest first — then sleep
    `publish_interval_seconds`, drain the ring buffer, and publish. A failed
    publish drops back into the reconnect branch on the next iteration.
    """

    def __init__(
        self,
        connector: NorthboundConnector,
        ring_buffers: RingBufferManager,
        publish_interval_seconds: float = 1.0,
        cold_store: SqliteColdStore | None = None,
        replay_batch_size: int = _DEFAULT_REPLAY_BATCH_SIZE,
    ) -> None:
        self._connector = connector
        self._ring_buffers = ring_buffers
        self._publish_interval_seconds = publish_interval_seconds
        self._cold_store = cold_store
        self._replay_batch_size = replay_batch_size
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        backoff = _INITIAL_BACKOFF_SECONDS
        while True:
            if not self._connected:
                try:
                    await self._connector.connect()
                    self._connected = True
                    backoff = _INITIAL_BACKOFF_SECONDS
                    logger.info("northbound.connected")
                    if self._cold_store is not None:
                        await self._replay_cold_store()
                except Exception as exc:  # noqa: BLE001 — isolate connector failures from the app
                    logger.error("northbound.connect_failed", error=str(exc))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_SECONDS)
                    continue

            await asyncio.sleep(self._publish_interval_seconds)
            batch = self._ring_buffers.drain_all()
            if not batch:
                continue

            result = await self._connector.publish(batch)
            if not result.success:
                logger.warning("northbound.publish_failed", error=result.error_message)
                self._connected = False

    async def _replay_cold_store(self) -> None:
        """Publish every stream's cold-store backlog, oldest first, before
        resuming live publishing. Uses peek + delete-on-confirmed-success
        (not drain, which deletes unconditionally) so a failed publish
        leaves the batch on disk to retry after the next reconnect, instead
        of losing it (FR-SF-002's whole point). Stops (without raising) on
        the first publish failure."""
        cold_store = self._cold_store
        if cold_store is None:
            return
        for stream_key in self._ring_buffers.stream_keys():
            while True:
                rows = cold_store.peek(stream_key, self._replay_batch_size)
                if not rows:
                    break
                batch = [tag for _, tag in rows]
                result = await self._connector.publish(batch)
                if not result.success:
                    logger.warning(
                        "northbound.replay_publish_failed",
                        stream_key=stream_key,
                        error=result.error_message,
                    )
                    self._connected = False
                    return
                cold_store.delete_ids(stream_key, [row_id for row_id, _ in rows])
                logger.info("northbound.replayed_batch", stream_key=stream_key, count=len(batch))

    async def stop(self) -> None:
        if self._connected:
            await self._connector.disconnect()
            self._connected = False
