"""Northbound connector plugin interface (system-architecture.md §3.5, FR-NB-007).

A connector reads batches of UnifiedTag from the northbound dispatcher and
delivers them to a broker or cloud endpoint. Built-in: MQTT + Sparkplug B
(xedge.northbound.mqtt). Additional connectors (AWS IoT Core, Azure IoT Hub)
are later-sprint work behind this same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from xedge.core.pipeline import UnifiedTag


@dataclass(frozen=True, slots=True)
class PublishResult:
    success: bool
    count: int
    error_message: str | None = None


@dataclass(slots=True)
class ConnectorMetrics:
    """Per-connector metrics exposed via observability (mirrors DriverMetrics)."""

    published_count: int = 0
    error_count: int = 0
    reconnect_count: int = 0
    last_successful_publish: datetime | None = None


class NorthboundConnector(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Establish the northbound connection (e.g. MQTT connect + birth
        certificate). Raise on failure — the dispatcher handles retry/backoff."""

    @abstractmethod
    async def publish(self, batch: list[UnifiedTag]) -> PublishResult:
        """Deliver a batch of tags. Must not raise for a single bad tag in
        the batch — report failure via PublishResult instead."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanly close the northbound connection."""

    def get_metrics(self) -> ConnectorMetrics:
        return ConnectorMetrics()

    def is_alive(self) -> bool:
        """Best-effort, non-invasive connectivity probe for the diagnostic
        `network check`/`self-test` commands (Sprint 17, XEDGE-135/137) —
        distinct from `NorthboundDispatcher.connected`, which only reflects
        the *last* connect/publish outcome, not a live check. Default
        `False` for connectors that don't implement one; not abstract, so
        existing/future connectors aren't forced to add it."""
        return False
