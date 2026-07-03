"""RAM ring buffer, Phase 1 scope (FR-SF-001, XEDGE-030).

Hot-tier only: no SD card / WAL persistence yet (that's Sprint 10, ADR-003).
One buffer per stream key (currently `source_driver` — per-tag-group
buffering needs a tag_group id threaded through TagUpdate/UnifiedTag, which
doesn't exist yet; this is a known interim simplification). Non-alarm
streams evict the oldest sample when full; alarm streams (none exist until
the Sprint 31 alarm engine) are meant to apply backpressure instead — see
`RingBufferBackpressureError`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from xedge.core.pipeline import UnifiedTag

DEFAULT_MAX_DEPTH = 10_000


class RingBufferBackpressureError(Exception):
    """Raised by an alarm-marked buffer's push() when full — callers must
    not silently drop alarm data; NFR/FR-SF-001 requires backpressure
    instead of eviction for alarm-tier streams."""


@dataclass(slots=True)
class RingBufferMetrics:
    depth: int
    max_depth: int
    evicted_count: int


class RingBuffer:
    """A single bounded FIFO of UnifiedTag samples."""

    def __init__(self, max_depth: int = DEFAULT_MAX_DEPTH, is_alarm_stream: bool = False) -> None:
        self._max_depth = max_depth
        self._is_alarm_stream = is_alarm_stream
        self._buffer: deque[UnifiedTag] = deque()
        self._evicted_count = 0

    def push(self, tag: UnifiedTag) -> None:
        if len(self._buffer) >= self._max_depth:
            if self._is_alarm_stream:
                raise RingBufferBackpressureError(
                    f"Alarm ring buffer full at {self._max_depth} samples; "
                    "caller must retry, not drop"
                )
            self._buffer.popleft()
            self._evicted_count += 1
        self._buffer.append(tag)

    def drain(self, max_items: int | None = None) -> list[UnifiedTag]:
        """Remove and return up to `max_items` samples (all, if None), oldest first."""
        count = len(self._buffer) if max_items is None else min(max_items, len(self._buffer))
        return [self._buffer.popleft() for _ in range(count)]

    def __len__(self) -> int:
        return len(self._buffer)

    def metrics(self) -> RingBufferMetrics:
        return RingBufferMetrics(
            depth=len(self._buffer), max_depth=self._max_depth, evicted_count=self._evicted_count
        )


class RingBufferManager:
    """Owns one RingBuffer per stream key, creating them lazily on first push."""

    def __init__(self, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        self._max_depth = max_depth
        self._buffers: dict[str, RingBuffer] = {}

    def push(self, stream_key: str, tag: UnifiedTag, *, is_alarm_stream: bool = False) -> None:
        buffer = self._buffers.setdefault(stream_key, RingBuffer(self._max_depth, is_alarm_stream))
        buffer.push(tag)

    def drain(self, stream_key: str, max_items: int | None = None) -> list[UnifiedTag]:
        buffer = self._buffers.get(stream_key)
        if buffer is None:
            return []
        return buffer.drain(max_items)

    def drain_all(self, max_items_per_stream: int | None = None) -> list[UnifiedTag]:
        """Drain every stream, oldest-per-stream first, streams in insertion order."""
        drained: list[UnifiedTag] = []
        for buffer in self._buffers.values():
            drained.extend(buffer.drain(max_items_per_stream))
        return drained

    def stream_keys(self) -> list[str]:
        return list(self._buffers.keys())

    def metrics(self, stream_key: str) -> RingBufferMetrics | None:
        buffer = self._buffers.get(stream_key)
        return buffer.metrics() if buffer is not None else None
