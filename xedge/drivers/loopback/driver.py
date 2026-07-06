"""Loopback driver (Sprint 17, XEDGE-137) — a synthetic, hardware-free
driver: every tag's value is whatever was last `write()`-ten to it (or its
configured `initial_value`, before any write). No I/O of any kind.

Two roles: (1) a normal, user-configurable driver type (registered in
`_build_registry()`/the config UI's known-types list like any other —
useful on its own as a demo/test data source with no field device needed),
and (2) the read/write round-trip `self-test`'s diagnostic command
instantiates directly, standalone, to prove the driver-to-pipeline path
works without depending on real hardware being reachable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
    DriverMetrics,
    Quality,
    TagUpdate,
    TagValue,
    WriteResult,
)


class LoopbackDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._metrics = DriverMetrics()
        self._values: dict[str, TagValue] = {}

    async def configure(self, config: DriverConfig) -> None:
        self._config = config
        for group in config.tag_groups:
            for tag in group["tags"]:
                self._values[tag["id"]] = tag.get("initial_value", 0)

    async def connect(self) -> None:
        pass

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        config = self._config
        assert config is not None  # noqa: S101 — configure() always precedes run() (BaseDriver contract)
        instance_id = config.instance_id
        group_tasks = [
            asyncio.create_task(self._poll_group(instance_id, group, output))
            for group in config.tag_groups
        ]
        try:
            await asyncio.gather(*group_tasks)
        finally:
            for task in group_tasks:
                task.cancel()
            await asyncio.gather(*group_tasks, return_exceptions=True)

    async def _poll_group(
        self, instance_id: str, group: dict[str, Any], output: asyncio.Queue[TagUpdate]
    ) -> None:
        interval_seconds = group["scan_rate_ms"] / 1000
        while True:
            for tag in group["tags"]:
                self._metrics.tag_read_count += 1
                self._metrics.last_successful_read = datetime.now(UTC)
                await output.put(
                    TagUpdate(
                        tag_id=f"{instance_id}/{tag['id']}",
                        timestamp=datetime.now(UTC),
                        value=self._values[tag["id"]],
                        quality=Quality.GOOD,
                        source_driver=instance_id,
                        source_address=tag["id"],
                    )
                )
            await asyncio.sleep(interval_seconds)

    async def disconnect(self) -> None:
        pass

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        # tag_id here is the bare tag name (e.g. "echo"), matching how
        # xedge.api.server's write-back path would call this — the
        # "{instance_id}/{tag_id}" prefix is only on the TagUpdate side.
        if tag_id not in self._values:
            return WriteResult(success=False, tag_id=tag_id, error_message="Unknown tag")
        self._values[tag_id] = value
        return WriteResult(success=True, tag_id=tag_id)

    def get_metrics(self) -> DriverMetrics:
        return self._metrics
