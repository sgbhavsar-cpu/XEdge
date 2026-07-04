from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.fake_driver import FakeDriver
from xedge.core.supervisor import DriverRegistry, DriverState, DriverSupervisor
from xedge.drivers.base import DriverConfig, TagUpdate


@pytest.fixture
def tag_queue() -> asyncio.Queue[TagUpdate]:
    return asyncio.Queue(maxsize=1000)


async def test_supervisor_starts_driver_and_receives_tag_updates(
    tag_queue: asyncio.Queue[TagUpdate],
) -> None:
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, tag_queue)

    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    try:
        update = await asyncio.wait_for(tag_queue.get(), timeout=1.0)
        assert update.tag_id == "fake_01/counter"
        assert supervisor.status("fake_01").state == DriverState.RUNNING
    finally:
        await supervisor.stop_all()

    assert driver.disconnected


async def test_supervisor_restarts_after_connect_failure(
    tag_queue: asyncio.Queue[TagUpdate],
) -> None:
    driver = FakeDriver(emit_interval_seconds=0.001, fail_on_connect_count=2)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, tag_queue)

    # Patch backoff to be fast for the test by starting directly and polling status.
    import xedge.core.supervisor as supervisor_module

    supervisor_module._INITIAL_BACKOFF_SECONDS = 0.001  # noqa: SLF001
    supervisor_module._MAX_BACKOFF_SECONDS = 0.01  # noqa: SLF001

    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    try:
        update = await asyncio.wait_for(tag_queue.get(), timeout=2.0)
        assert update is not None
        assert supervisor.status("fake_01").consecutive_failures >= 2
    finally:
        await supervisor.stop_all()


async def test_supervisor_stop_disconnects_driver(tag_queue: asyncio.Queue[TagUpdate]) -> None:
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, tag_queue)

    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    await asyncio.sleep(0.02)
    await supervisor.stop("fake_01")

    assert driver.disconnected
    assert supervisor.status("fake_01").state == DriverState.STOPPED


async def test_status_reports_live_metrics_from_the_running_driver(
    tag_queue: asyncio.Queue[TagUpdate],
) -> None:
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, tag_queue)

    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    try:
        for _ in range(200):
            if driver.emitted_count >= 3:
                break
            await asyncio.sleep(0.01)
        status = supervisor.status("fake_01")
        assert status.metrics.tag_read_count == driver.emitted_count
        # all_status() must report the same live value, not a stale default
        assert supervisor.all_status()["fake_01"].metrics.tag_read_count == driver.emitted_count
    finally:
        await supervisor.stop_all()


def test_registry_rejects_duplicate_registration() -> None:
    registry = DriverRegistry()
    registry.register("fake", lambda: FakeDriver())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fake", lambda: FakeDriver())


def test_registry_unknown_type_raises() -> None:
    registry = DriverRegistry()
    with pytest.raises(ValueError, match="Unknown driver type"):
        registry.create("nonexistent")
