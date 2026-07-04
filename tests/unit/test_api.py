from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from tests.fixtures.fake_connector import FakeConnector
from tests.fixtures.fake_driver import FakeDriver
from xedge.api.server import create_app
from xedge.core.config import ConfigVersionHistory
from xedge.core.supervisor import DriverConfig, DriverRegistry, DriverSupervisor
from xedge.drivers.base import TagUpdate
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.store.ring_buffer import RingBufferManager


async def _running_supervisor() -> tuple[DriverSupervisor, FakeDriver]:
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=100)
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, queue)
    supervisor.start(DriverConfig(instance_id="d1", driver_type="fake", config={}))
    for _ in range(200):
        if driver.emitted_count >= 2:
            break
        await asyncio.sleep(0.01)
    return supervisor, driver


def test_health_endpoint(tmp_path: Path) -> None:
    history = ConfigVersionHistory(tmp_path)
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    app = create_app(supervisor, history)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_status_endpoint_reports_driver_count_and_no_dispatcher(tmp_path: Path) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    app = create_app(supervisor, history)
    client = TestClient(app)
    try:
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        body = response.json()
        assert body["driver_count"] == 1
        assert body["northbound_connected"] is None
        assert body["uptime_seconds"] >= 0
        assert "version" in body
    finally:
        await supervisor.stop_all()


async def test_status_endpoint_reports_northbound_connected_state(tmp_path: Path) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    connector = FakeConnector()
    dispatcher = NorthboundDispatcher(connector, RingBufferManager())
    task = asyncio.create_task(dispatcher.run())
    try:
        for _ in range(200):
            if dispatcher.connected:
                break
            await asyncio.sleep(0.01)
        app = create_app(supervisor, history, dispatcher=dispatcher)
        client = TestClient(app)
        response = client.get("/api/v1/status")
        assert response.json()["northbound_connected"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await supervisor.stop_all()


async def test_drivers_endpoint_lists_status_and_live_metrics(tmp_path: Path) -> None:
    supervisor, driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    app = create_app(supervisor, history)
    client = TestClient(app)
    try:
        response = client.get("/api/v1/drivers")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        entry = body[0]
        assert entry["instance_id"] == "d1"
        assert entry["driver_type"] == "fake"
        assert entry["state"] == "running"
        assert entry["metrics"]["tag_read_count"] == driver.emitted_count
    finally:
        await supervisor.stop_all()


def test_config_endpoint_returns_empty_when_no_versions_saved(tmp_path: Path) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    history = ConfigVersionHistory(tmp_path)
    app = create_app(supervisor, history)
    client = TestClient(app)
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    assert response.json() == {}


def test_config_endpoint_returns_latest_version_with_secrets_still_placeholders(
    tmp_path: Path,
) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    history = ConfigVersionHistory(tmp_path)
    history.save({"schema_version": "0.1", "drivers": []})
    history.save(
        {
            "schema_version": "0.1",
            "northbound": {"mqtt": {"host": "broker", "password": "${SECRET:mqtt_password}"}},
        }
    )
    app = create_app(supervisor, history)
    client = TestClient(app)
    response = client.get("/api/v1/config")
    body = response.json()
    assert body["northbound"]["mqtt"]["password"] == "${SECRET:mqtt_password}"
