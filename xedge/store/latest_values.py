"""In-memory latest-value cache (FR-WU-* driver detail monitoring).

Distinct from RingBuffer/RingBufferManager (xedge.store.ring_buffer): those
are drain-once FIFOs feeding the northbound dispatcher, unsuitable for
"what is this tag's current value right now" — draining one would steal the
sample from northbound delivery. This is a plain last-write-wins cache,
updated on every UnifiedTag regardless of deadband suppression (deadband
only governs northbound publish-worthiness, not "current state" for
monitoring).
"""

from __future__ import annotations

from xedge.core.pipeline import UnifiedTag


class LatestValueStore:
    """Tracks the most recent UnifiedTag seen for each tag_id.

    tag_id follows the `f"{instance_id}/{tag['id']}"` convention used
    throughout the pipeline (xedge.core.pipeline.build_tag_pipeline_configs),
    so filtering by driver instance is a prefix match.
    """

    def __init__(self) -> None:
        self._values: dict[str, UnifiedTag] = {}

    def update(self, tag: UnifiedTag) -> None:
        self._values[tag.tag_id] = tag

    def get(self, tag_id: str) -> UnifiedTag | None:
        return self._values.get(tag_id)

    def for_driver(self, instance_id: str) -> list[UnifiedTag]:
        prefix = f"{instance_id}/"
        return [tag for tag_id, tag in self._values.items() if tag_id.startswith(prefix)]
