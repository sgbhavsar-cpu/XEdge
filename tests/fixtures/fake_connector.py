"""A minimal in-memory NorthboundConnector for testing the dispatcher
without any real network I/O."""

from __future__ import annotations

from xedge.core.pipeline import UnifiedTag
from xedge.northbound.base import ConnectorMetrics, NorthboundConnector, PublishResult


class FakeConnector(NorthboundConnector):
    def __init__(self, fail_connect_count: int = 0, fail_publish_count: int = 0) -> None:
        self._remaining_connect_failures = fail_connect_count
        self._remaining_publish_failures = fail_publish_count
        self.connected = False
        self.disconnected_count = 0
        self.connect_count = 0
        self.published_batches: list[list[UnifiedTag]] = []

    async def connect(self) -> None:
        self.connect_count += 1
        if self._remaining_connect_failures > 0:
            self._remaining_connect_failures -= 1
            raise ConnectionError("simulated connect failure")
        self.connected = True

    async def publish(self, batch: list[UnifiedTag]) -> PublishResult:
        if self._remaining_publish_failures > 0:
            self._remaining_publish_failures -= 1
            return PublishResult(success=False, count=0, error_message="simulated publish failure")
        self.published_batches.append(batch)
        return PublishResult(success=True, count=len(batch))

    async def disconnect(self) -> None:
        self.disconnected_count += 1
        self.connected = False

    def get_metrics(self) -> ConnectorMetrics:
        return ConnectorMetrics()
