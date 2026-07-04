"""Driver framework core types: TagUpdate, DriverConfig, WriteResult, BaseDriver.

Implements the Protocol Abstraction Layer per system-architecture.md §3.2 and
FR-DF-001..FR-DF-007. Concrete protocol drivers (Modbus, OPC UA, ...) subclass
BaseDriver; the pipeline only ever sees TagUpdate objects, never protocol
specifics.
"""

from __future__ import annotations

import asyncio
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TagValue = bool | int | float | str | bytes


class Quality(enum.Enum):
    """OPC UA-aligned quality model (FR-DP-005). Protocol-specific quality
    codes (Modbus exceptions, OPC UA StatusCodes, IEC 104 flags, DNP3 flags)
    are mapped to this set by each driver before emitting a TagUpdate."""

    GOOD = "Good"
    UNCERTAIN = "Uncertain"
    BAD = "Bad"


@dataclass(frozen=True, slots=True)
class TagUpdate:
    """Raw update emitted by a driver onto its output queue.

    Distinct from the pipeline's UnifiedTag (system-architecture.md §3.3):
    a TagUpdate has not yet been normalized, deadband-filtered, or checked
    for alarms — that is the pipeline's job.
    """

    tag_id: str
    timestamp: datetime
    """Ingestion timestamp — always set, regardless of whether the source
    device has its own clock (FR-DP-002)."""
    value: TagValue
    quality: Quality
    source_driver: str
    source_address: str
    source_timestamp: datetime | None = None
    """Device-native timestamp, when the protocol carries one and it's
    trusted (e.g. an OPC UA node's SourceTimestamp). None for protocols with
    no device clock (e.g. Modbus registers) — the pipeline then falls back
    to `timestamp` and flags the value as an estimated timestamp."""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Result of a Driver.write() call, e.g. for northbound write-back (FR-NB-009)."""

    success: bool
    tag_id: str
    error_message: str | None = None


@dataclass(slots=True)
class DriverMetrics:
    """Per-driver-instance metrics exposed via observability (FR-DF-006)."""

    tag_read_count: int = 0
    error_count: int = 0
    reconnect_count: int = 0
    last_successful_read: datetime | None = None


@dataclass(frozen=True, slots=True)
class DriverConfig:
    """Configuration handed to a driver instance by the supervisor.

    `config` and `tag_groups` are driver-type-specific and validated against
    that driver type's own JSON Schema before this object is constructed
    (FR-DF-004) — BaseDriver itself does not know their shape.
    """

    instance_id: str
    driver_type: str
    config: dict[str, Any]
    tag_groups: list[dict[str, Any]] = field(default_factory=list)


class DriverConnectionError(Exception):
    """Raised by connect()/run() on a recoverable connection failure.

    The supervisor catches this (and any other exception) to trigger the
    restart-with-backoff policy (NFR-R-006) without taking down the process.
    """


class BaseDriver(ABC):
    """Lifecycle: load -> configure -> connect -> run -> disconnect -> unload
    (FR-DF-002). Each instance is independently startable/stoppable/restartable
    at runtime (FR-DF-003) and isolated from other driver instances (a crash
    here must not affect the pipeline or other drivers).
    """

    @abstractmethod
    async def configure(self, config: DriverConfig) -> None:
        """Apply configuration. Called once before the first connect()."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the southbound connection. Raise DriverConnectionError
        (or let the underlying exception propagate) on failure — the
        supervisor handles retry/backoff."""

    @abstractmethod
    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        """Run the read loop, pushing TagUpdate objects onto `output` until
        cancelled. `output` is bounded and backpressure-aware; a full queue
        means `put` will block, which is the intended signal to slow down."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Release the southbound connection. Must be safe to call even if
        connect() never succeeded."""

    @abstractmethod
    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        """Write a value to the field device, for drivers/tags that support
        it (FR-NB-009 write-back path). Drivers without write support should
        return WriteResult(success=False, ...)."""

    def get_metrics(self) -> DriverMetrics:
        """Return current metrics. Default implementation for drivers that
        don't track anything beyond the supervisor-level counters."""
        return DriverMetrics()
