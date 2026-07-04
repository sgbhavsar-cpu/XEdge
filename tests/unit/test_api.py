from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures.fake_connector import FakeConnector
from tests.fixtures.fake_driver import FakeDriver
from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
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


def _build_app(
    supervisor: DriverSupervisor,
    history: ConfigVersionHistory,
    tmp_path: Path,
    core_schema_path: Path,
    dispatcher: NorthboundDispatcher | None = None,
) -> FastAPI:
    config_path = tmp_path / "xedge.yaml"
    if not config_path.is_file():
        config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
    return create_app(
        supervisor,
        history,
        dispatcher,
        user_store=UserStore(tmp_path / "webui" / "users.json"),
        session_manager=SessionManager(secret_key=b"test-secret-key"),
        login_tracker=LoginAttemptTracker(),
        config_path=config_path,
        schema_path=core_schema_path,
    )


def _authenticated_client(app: FastAPI, password: str = "hunter2hunter2") -> TestClient:
    """A TestClient that's completed first-login setup — TestClient persists
    cookies across requests like a browser session, so subsequent calls on
    the same client are authenticated."""
    client = TestClient(app)
    response = client.post("/api/v1/auth/setup", json={"password": password})
    assert response.status_code == 200, response.text
    return client


def test_health_endpoint_never_requires_auth(tmp_path: Path, core_schema_path: Path) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class TestAuthEndpoints:
    def test_status_reports_no_account_and_unauthenticated_initially(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = TestClient(app)
        response = client.get("/api/v1/auth/status")
        assert response.json() == {"account_exists": False, "authenticated": False}

    def test_setup_creates_account_and_authenticates(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/api/v1/auth/status")
        assert response.json() == {"account_exists": True, "authenticated": True}

    def test_setup_twice_rejected(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post("/api/v1/auth/setup", json={"password": "another_password"})
        assert response.status_code == 409

    def test_login_with_correct_password_succeeds(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        _authenticated_client(app, password="correct-password")

        fresh_client = TestClient(app)
        response = fresh_client.post("/api/v1/auth/login", json={"password": "correct-password"})
        assert response.status_code == 200
        assert fresh_client.get("/api/v1/auth/status").json()["authenticated"] is True

    def test_login_with_wrong_password_fails(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        _authenticated_client(app, password="correct-password")

        fresh_client = TestClient(app)
        response = fresh_client.post("/api/v1/auth/login", json={"password": "wrong-password"})
        assert response.status_code == 401

    def test_login_locks_out_after_five_failures(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        _authenticated_client(app, password="correct-password")

        fresh_client = TestClient(app)
        for _ in range(5):
            fresh_client.post("/api/v1/auth/login", json={"password": "wrong"})
        response = fresh_client.post("/api/v1/auth/login", json={"password": "correct-password"})
        assert response.status_code == 429

    def test_logout_clears_session(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/api/v1/auth/logout")
        assert client.get("/api/v1/auth/status").json()["authenticated"] is False

    def test_protected_endpoint_rejects_unauthenticated_request(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = TestClient(app)
        response = client.get("/api/v1/status")
        assert response.status_code == 401


async def test_status_endpoint_reports_driver_count_and_no_dispatcher(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = _authenticated_client(app)
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


async def test_status_endpoint_reports_northbound_connected_state(
    tmp_path: Path, core_schema_path: Path
) -> None:
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
        app = _build_app(supervisor, history, tmp_path, core_schema_path, dispatcher=dispatcher)
        client = _authenticated_client(app)
        response = client.get("/api/v1/status")
        assert response.json()["northbound_connected"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await supervisor.stop_all()


async def test_drivers_endpoint_lists_status_and_live_metrics(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = _authenticated_client(app)
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


def test_config_endpoint_returns_empty_when_no_versions_saved(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = _authenticated_client(app)
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    assert response.json() == {}


def test_config_endpoint_returns_latest_version_with_secrets_still_placeholders(
    tmp_path: Path, core_schema_path: Path
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
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = _authenticated_client(app)
    response = client.get("/api/v1/config")
    body = response.json()
    assert body["northbound"]["mqtt"]["password"] == "${SECRET:mqtt_password}"


class TestConfigWriteEndpoint:
    def test_valid_config_write_is_accepted_and_written_to_disk(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        new_config = {"schema_version": "0.1", "logging": {"level": "DEBUG"}}
        response = client.put("/api/v1/config", json=new_config)
        assert response.status_code == 200

        written = yaml.safe_load((tmp_path / "xedge.yaml").read_text(encoding="utf-8"))
        assert written == new_config

    def test_invalid_config_write_is_rejected_and_file_untouched(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        config_path = tmp_path / "xedge.yaml"
        original_content = config_path.read_text(encoding="utf-8")

        response = client.put("/api/v1/config", json={"schema_version": "not-a-valid-version"})
        assert response.status_code == 422
        assert config_path.read_text(encoding="utf-8") == original_content

    def test_config_write_requires_auth(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = TestClient(app)
        response = client.put("/api/v1/config", json={"schema_version": "0.1"})
        assert response.status_code == 401
