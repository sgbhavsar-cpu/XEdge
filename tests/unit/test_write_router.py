from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.fixtures.fake_driver import FakeDriver
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.core.write_router import WriteRouter
from xedge.drivers.base import DriverConfig, TagUpdate
from xedge.observability.audit_log import AuditLog


@pytest.fixture
def tag_queue() -> asyncio.Queue[TagUpdate]:
    return asyncio.Queue(maxsize=1000)


async def test_write_routes_to_the_running_driver_and_audit_logs_success(
    tag_queue: asyncio.Queue[TagUpdate], tmp_path: Path
) -> None:
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, tag_queue)
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    router = WriteRouter(supervisor, audit_log)

    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    try:
        await asyncio.wait_for(tag_queue.get(), timeout=1.0)  # wait for RUNNING
        result = await router.write("alice", "fake_01", "setpoint", 42)

        assert result.success is True
        assert result.tag_id == "fake_01/setpoint"
        assert driver.written == [("setpoint", 42)]

        events = audit_log.tail(limit=10)
        assert len(events) == 1
        assert events[0]["actor"] == "alice"
        assert events[0]["event"] == "tag.write"
        assert events[0]["details"]["success"] is True
        assert events[0]["details"]["tag_id"] == "fake_01/setpoint"
    finally:
        await supervisor.stop_all()


async def test_write_to_unknown_instance_fails_without_calling_any_driver(
    tag_queue: asyncio.Queue[TagUpdate], tmp_path: Path
) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), tag_queue)
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    router = WriteRouter(supervisor, audit_log)

    result = await router.write("alice", "no_such_instance", "tag", 1)

    assert result.success is False
    assert "No such driver instance" in result.error_message
    assert audit_log.tail(limit=10)[0]["details"]["success"] is False


async def test_write_to_a_stopped_driver_is_rejected(
    tag_queue: asyncio.Queue[TagUpdate], tmp_path: Path
) -> None:
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, tag_queue)
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    router = WriteRouter(supervisor, audit_log)

    supervisor.start(DriverConfig(instance_id="fake_01", driver_type="fake", config={}))
    await asyncio.wait_for(tag_queue.get(), timeout=1.0)
    await supervisor.stop("fake_01")

    result = await router.write("alice", "fake_01", "setpoint", 1)

    assert result.success is False
    assert "not running" in result.error_message
    assert driver.written == []
