"""A minimal in-memory BaseDriver implementation for exercising the
DriverSupervisor and pipeline without any real protocol I/O."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
    DriverMetrics,
    Quality,
    TagUpdate,
    TagValue,
    WriteResult,
)


class FakeDriver(BaseDriver):
    """Emits one TagUpdate per `emit_interval_seconds` until cancelled.

    `fail_on_connect_count` lets tests force N consecutive connect()
    failures before it succeeds, to exercise supervisor backoff/restart.
    """

    def __init__(self, emit_interval_seconds: float = 0.01, fail_on_connect_count: int = 0) -> None:
        self.emit_interval_seconds = emit_interval_seconds
        self._remaining_connect_failures = fail_on_connect_count
        self.configured_with: DriverConfig | None = None
        self.connected = False
        self.disconnected = False
        self.emitted_count = 0
        self.written: list[tuple[str, TagValue]] = []

    async def configure(self, config: DriverConfig) -> None:
        self.configured_with = config

    async def connect(self) -> None:
        if self._remaining_connect_failures > 0:
            self._remaining_connect_failures -= 1
            raise ConnectionError("simulated connect failure")
        self.connected = True

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        instance_id = self.configured_with.instance_id if self.configured_with else "fake"
        while True:
            await output.put(
                TagUpdate(
                    tag_id=f"{instance_id}/counter",
                    timestamp=datetime.now(UTC),
                    value=self.emitted_count,
                    quality=Quality.GOOD,
                    source_driver=instance_id,
                    source_address="0",
                )
            )
            self.emitted_count += 1
            await asyncio.sleep(self.emit_interval_seconds)

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        self.written.append((tag_id, value))
        return WriteResult(success=True, tag_id=tag_id)

    def get_metrics(self) -> DriverMetrics:
        return DriverMetrics(tag_read_count=self.emitted_count)
