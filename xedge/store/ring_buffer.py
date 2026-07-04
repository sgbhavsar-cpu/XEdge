"""RAM ring buffer, hot tier (FR-SF-001, XEDGE-030).

One buffer per stream key (currently `source_driver` — per-tag-group
buffering needs a tag_group id threaded through TagUpdate/UnifiedTag, which
doesn't exist yet; this is a known interim simplification). Non-alarm
streams evict the oldest sample when full — spilling it to the cold tier
(xedge.store.sqlite_store, ADR-003) if one is configured, rather than
discarding it, per FR-SF-001/002. Alarm streams (none exist until the
Sprint 31 alarm engine) are meant to apply backpressure instead — see
`RingBufferBackpressureError`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from xedge.core.pipeline import UnifiedTag

DEFAULT_MAX_DEPTH = 10_000

OnEvictCallback = Callable[[UnifiedTag], None]


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

    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        is_alarm_stream: bool = False,
        on_evict: OnEvictCallback | None = None,
    ) -> None:
        self._max_depth = max_depth
        self._is_alarm_stream = is_alarm_stream
        self._on_evict = on_evict
        self._buffer: deque[UnifiedTag] = deque()
        self._evicted_count = 0

    def push(self, tag: UnifiedTag) -> None:
        if len(self._buffer) >= self._max_depth:
            if self._is_alarm_stream:
                raise RingBufferBackpressureError(
                    f"Alarm ring buffer full at {self._max_depth} samples; "
                    "caller must retry, not drop"
                )
            evicted = self._buffer.popleft()
            self._evicted_count += 1
            if self._on_evict is not None:
                self._on_evict(evicted)
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


OnEvictWithKeyCallback = Callable[[str, UnifiedTag], None]


class RingBufferManager:
    """Owns one RingBuffer per stream key, creating them lazily on first push."""

    def __init__(
        self, max_depth: int = DEFAULT_MAX_DEPTH, on_evict: OnEvictWithKeyCallback | None = None
    ) -> None:
        """`on_evict(stream_key, tag)`, if given, is called for every sample
        evicted from any stream's buffer — typically wired to a cold store's
        `append(stream_key, tag)` (ADR-003) so evicted samples are spilled,
        not dropped."""
        self._max_depth = max_depth
        self._on_evict = on_evict
        self._buffers: dict[str, RingBuffer] = {}

    def push(self, stream_key: str, tag: UnifiedTag, *, is_alarm_stream: bool = False) -> None:
        buffer = self._buffers.get(stream_key)
        if buffer is None:
            on_evict = (
                (lambda evicted_tag: self._on_evict(stream_key, evicted_tag))  # type: ignore[misc]
                if self._on_evict is not None
                else None
            )
            buffer = RingBuffer(self._max_depth, is_alarm_stream, on_evict)
            self._buffers[stream_key] = buffer
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
