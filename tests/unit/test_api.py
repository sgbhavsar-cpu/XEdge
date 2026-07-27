from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures.fake_connector import FakeConnector
from tests.fixtures.fake_driver import FakeDriver
from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
from xedge.api.server import create_app
from xedge.core.alarms import AlarmEngine, AlarmRule
from xedge.core.config import ConfigVersionHistory
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverConfig, DriverRegistry, DriverState, DriverSupervisor
from xedge.drivers.base import Quality, TagUpdate
from xedge.northbound.dispatcher import NorthboundDispatcher
from xedge.observability.audit_log import AuditLog
from xedge.store.latest_values import LatestValueStore
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
    latest_values: LatestValueStore | None = None,
    secure_cookies: bool = False,
    alarm_engine: AlarmEngine | None = None,
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
        latest_values=latest_values if latest_values is not None else LatestValueStore(),
        audit_log=AuditLog(tmp_path / "webui" / "audit.jsonl"),
        ring_buffers=RingBufferManager(),
        secure_cookies=secure_cookies,
        alarm_engine=alarm_engine,
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


class TestSecureCookies:
    """secure_cookies (Sprint 13, XEDGE-107/280) controls the session
    cookie's Secure flag — set by xedge.core.main from the `tls` config
    section. FastAPI's TestClient only resends a Secure-flagged cookie when
    constructed with an https:// base_url; a plain TestClient(app) silently
    drops it, same as a real browser would over plain HTTP."""

    def test_secure_cookie_persists_over_an_https_test_client(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path, secure_cookies=True)
        client = TestClient(app, base_url="https://testserver")

        setup_response = client.post("/api/v1/auth/setup", json={"password": "hunter2hunter2"})
        assert setup_response.status_code == 200
        assert "Secure" in setup_response.headers["set-cookie"]

        status_response = client.get("/api/v1/status")
        assert status_response.status_code == 200

    def test_secure_cookie_is_dropped_by_a_plain_http_test_client(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path, secure_cookies=True)
        client = TestClient(app)  # base_url defaults to http://testserver

        setup_response = client.post("/api/v1/auth/setup", json={"password": "hunter2hunter2"})
        assert setup_response.status_code == 200

        status_response = client.get("/api/v1/status")
        assert status_response.status_code == 401


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


async def test_republish_triggers_the_dispatcher_immediately(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    connector = FakeConnector()
    ring_buffers = RingBufferManager()
    ring_buffers.push(
        "d1",
        UnifiedTag(
            tag_id="d1/t1",
            timestamp=datetime.now(UTC),
            value=1,
            data_type="INT64",
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        ),
    )
    # A long interval that would fail this test (via timeout) if the
    # republish endpoint didn't actually wake the dispatcher early.
    dispatcher = NorthboundDispatcher(connector, ring_buffers, publish_interval_seconds=30.0)
    task = asyncio.create_task(dispatcher.run())
    try:
        for _ in range(200):
            if dispatcher.connected:
                break
            await asyncio.sleep(0.01)

        app = _build_app(supervisor, history, tmp_path, core_schema_path, dispatcher=dispatcher)
        client = _authenticated_client(app)

        response = client.post("/api/v1/northbound/republish")
        assert response.status_code == 200
        assert response.json() == {"queued": True}

        for _ in range(200):
            if connector.published_batches:
                break
            await asyncio.sleep(0.01)
        assert connector.published_batches
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await supervisor.stop_all()


async def test_republish_without_a_dispatcher_configured_is_404(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = _authenticated_client(app)

    response = client.post("/api/v1/northbound/republish")

    assert response.status_code == 404
    await supervisor.stop_all()


async def test_republish_requires_northbound_publish_permission(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = TestClient(app)  # no login

    response = client.post("/api/v1/northbound/republish")

    assert response.status_code == 401
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
        # XEDGE-421: "unknown" here is correct, not a placeholder oversight —
        # FakeDriver (this fixture's driver) doesn't implement
        # get_connectivity_state(), so the dashboard listing must degrade to
        # "unknown" for driver types the adapter doesn't cover yet, not error.
        assert entry["connectivity_state"] == "unknown"
    finally:
        await supervisor.stop_all()


async def test_driver_tags_endpoint_splits_system_tags_from_real_tags(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    latest_values = LatestValueStore()
    now = datetime.now(UTC)
    latest_values.update(
        UnifiedTag(
            tag_id="d1/counter",
            timestamp=now,
            value=42,
            data_type="INT64",
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
    )
    latest_values.update(
        UnifiedTag(
            tag_id="d1/_system/status",
            timestamp=now,
            value="running",
            data_type="STRING",
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="status",
        )
    )
    app = _build_app(supervisor, history, tmp_path, core_schema_path, latest_values=latest_values)
    client = _authenticated_client(app)
    try:
        response = client.get("/api/v1/drivers/d1/tags")
        assert response.status_code == 200
        body = response.json()
        assert [t["tag_id"] for t in body["tags"]] == ["d1/counter"]
        assert body["system"] == {"status": "running"}
    finally:
        await supervisor.stop_all()


async def test_driver_tags_endpoint_surfaces_modbus_exception_name_as_detail(
    tmp_path: Path, core_schema_path: Path
) -> None:
    """XEDGE-425: before Sprint C1 only the raw numeric exception code ever
    reached the operator; this is the Web UI's Detail column's data source."""
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    latest_values = LatestValueStore()
    latest_values.update(
        UnifiedTag(
            tag_id="d1/bad_tag",
            timestamp=datetime.now(UTC),
            value=0,
            data_type="INT64",
            quality=Quality.BAD,
            source_driver="d1",
            source_address="7",
            metadata={"modbus_exception": 2, "modbus_exception_name": "ILLEGAL_DATA_ADDRESS"},
        )
    )
    app = _build_app(supervisor, history, tmp_path, core_schema_path, latest_values=latest_values)
    client = _authenticated_client(app)
    try:
        body = client.get("/api/v1/drivers/d1/tags").json()
        assert body["tags"][0]["detail"] == "ILLEGAL_DATA_ADDRESS"
    finally:
        await supervisor.stop_all()


async def test_driver_tags_endpoint_detail_is_none_for_a_good_tag(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor, _driver = await _running_supervisor()
    history = ConfigVersionHistory(tmp_path)
    latest_values = LatestValueStore()
    latest_values.update(
        UnifiedTag(
            tag_id="d1/counter",
            timestamp=datetime.now(UTC),
            value=1,
            data_type="INT64",
            quality=Quality.GOOD,
            source_driver="d1",
            source_address="0",
        )
    )
    app = _build_app(supervisor, history, tmp_path, core_schema_path, latest_values=latest_values)
    client = _authenticated_client(app)
    try:
        body = client.get("/api/v1/drivers/d1/tags").json()
        assert body["tags"][0]["detail"] is None
    finally:
        await supervisor.stop_all()


def test_driver_tags_endpoint_404s_for_unknown_instance(
    tmp_path: Path, core_schema_path: Path
) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    history = ConfigVersionHistory(tmp_path)
    app = _build_app(supervisor, history, tmp_path, core_schema_path)
    client = _authenticated_client(app)
    response = client.get("/api/v1/drivers/nope/tags")
    assert response.status_code == 404


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


class TestUserManagementEndpoints:
    def test_list_users_returns_admin_initially(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.get("/api/v1/users")
        assert response.status_code == 200
        assert response.json() == [{"username": "admin", "role": "admin"}]

    def test_create_list_and_delete_user(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        create_response = client.post(
            "/api/v1/users", json={"username": "bob", "password": "bobpass123", "role": "operator"}
        )
        assert create_response.status_code == 201

        usernames = {u["username"] for u in client.get("/api/v1/users").json()}
        assert usernames == {"admin", "bob"}

        delete_response = client.delete("/api/v1/users/bob")
        assert delete_response.status_code == 200
        assert {u["username"] for u in client.get("/api/v1/users").json()} == {"admin"}

    def test_create_user_with_unknown_role_rejected(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.post(
            "/api/v1/users",
            json={"username": "bob", "password": "bobpass123", "role": "not-a-role"},
        )
        assert response.status_code == 422

    def test_set_user_role(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post(
            "/api/v1/users", json={"username": "bob", "password": "bobpass123", "role": "operator"}
        )
        response = client.post("/api/v1/users/bob/role", json={"role": "readonly"})
        assert response.status_code == 200
        users_by_name = {u["username"]: u["role"] for u in client.get("/api/v1/users").json()}
        assert users_by_name["bob"] == "readonly"

    def test_cannot_delete_own_account(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)
        response = client.delete("/api/v1/users/admin")
        assert response.status_code == 400

    def test_non_admin_gets_403_on_user_management(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "ron", "password": "ronpass123", "role": "readonly"},
        )

        ron_client = TestClient(app)
        login_response = ron_client.post(
            "/api/v1/auth/login", json={"username": "ron", "password": "ronpass123"}
        )
        assert login_response.status_code == 200

        assert ron_client.get("/api/v1/users").status_code == 403


class TestPermissionMatrixEnforcement:
    def test_operator_can_read_and_write_config_but_not_manage_users(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "opuser", "password": "opuserpass123", "role": "operator"},
        )

        op_client = TestClient(app)
        op_client.post(
            "/api/v1/auth/login", json={"username": "opuser", "password": "opuserpass123"}
        )

        assert op_client.get("/api/v1/config").status_code == 200
        assert op_client.put("/api/v1/config", json={"schema_version": "0.1"}).status_code == 200
        assert op_client.get("/api/v1/users").status_code == 403

    def test_readonly_can_read_tags_but_not_config(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )

        viewer_client = TestClient(app)
        viewer_client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "viewerpass123"}
        )

        assert viewer_client.get("/api/v1/status").status_code == 200
        assert viewer_client.get("/api/v1/config").status_code == 403
        assert (
            viewer_client.put("/api/v1/config", json={"schema_version": "0.1"}).status_code == 403
        )


class TestAuditLogEndpoint:
    def test_requires_audit_read_permission(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "opuser", "password": "opuserpass123", "role": "operator"},
        )
        admin_client.post(
            "/api/v1/users",
            json={"username": "auditor1", "password": "auditor1pass123", "role": "auditor"},
        )

        op_client = TestClient(app)
        op_client.post(
            "/api/v1/auth/login", json={"username": "opuser", "password": "opuserpass123"}
        )
        assert op_client.get("/api/v1/audit").status_code == 403

        auditor_client = TestClient(app)
        auditor_client.post(
            "/api/v1/auth/login", json={"username": "auditor1", "password": "auditor1pass123"}
        )
        assert auditor_client.get("/api/v1/audit").status_code == 200

        assert admin_client.get("/api/v1/audit").status_code == 200

    def test_login_and_config_write_and_user_creation_all_produce_audit_events(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(
            app
        )  # setup -> auth.setup, auth.login_success not logged here (setup path)

        client.put("/api/v1/config", json={"schema_version": "0.1"})
        client.post(
            "/api/v1/users",
            json={"username": "bob", "password": "bobpass123", "role": "operator"},
        )

        events = [e["event"] for e in client.get("/api/v1/audit").json()]
        assert "auth.setup" in events
        assert "config.write" in events
        assert "user.created" in events

    def test_failed_login_produces_audit_event(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)

        attacker_client = TestClient(app)
        attacker_client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})

        events = [e["event"] for e in admin_client.get("/api/v1/audit").json()]
        assert "auth.login_failure" in events

    def test_login_audit_events_capture_client_ip(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post("/api/v1/auth/login", json={"password": "hunter2hunter2"})

        events = admin_client.get("/api/v1/audit").json()
        login_events = [e for e in events if e["event"] == "auth.login_success"]
        assert login_events
        assert login_events[0]["details"]["ip"] is not None


class TestDriverHealthEnableDisableValidate:
    """Sprint 25, XEDGE-185/186/187.

    `DriverSupervisor.start()`/`stop()` must run on the same event loop
    that later awaits their task — TestClient's WebSocket/request dispatch
    runs the ASGI app in its own portal thread with its own loop (the same
    constraint already documented in tests/integration/test_diagnostics_ws.py).
    So every driver here starts *disabled in config* and is only ever
    actually started via the `/enable` HTTP endpoint itself — never by
    calling `supervisor.start()` directly from the test's own context —
    keeping every supervised task's lifecycle on TestClient's one loop.
    """

    def _build_app_with_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> tuple[FastAPI, DriverSupervisor]:
        config_path = tmp_path / "xedge.yaml"
        config_path.write_text(
            "schema_version: '0.1'\ndrivers:\n  - id: modbus_sim_01\n    type: modbus_tcp\n"
            "    enabled: false\n    config:\n      host: 127.0.0.1\n"
            "      port: 1502\n    tag_groups: []\n",
            encoding="utf-8",
        )
        from xedge.drivers.modbus.tcp import ModbusTcpDriver

        registry = DriverRegistry()
        registry.register("modbus_tcp", ModbusTcpDriver)
        supervisor = DriverSupervisor(registry, asyncio.Queue(maxsize=100))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        return app, supervisor

    def test_health_returns_live_status_for_running_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/api/v1/drivers/modbus_sim_01/enable")

        response = client.get("/api/v1/drivers/modbus_sim_01/health")

        assert response.status_code == 200
        body = response.json()
        assert body["instance_id"] == "modbus_sim_01"
        assert body["driver_type"] == "modbus_tcp"
        assert "tag_count" in body
        assert "last_read_age_seconds" in body

    def test_health_synthesizes_disabled_state_for_never_started_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.get("/api/v1/drivers/modbus_sim_01/health")

        assert response.status_code == 200
        assert response.json()["state"] == "disabled"

    def test_health_requires_tag_read_permission(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )
        viewer_client = TestClient(app)
        viewer_client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "viewerpass123"}
        )

        assert viewer_client.get("/api/v1/drivers/modbus_sim_01/health").status_code == 200

    def test_health_unknown_driver_returns_404(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)

        assert client.get("/api/v1/drivers/nonexistent/health").status_code == 404

    def test_health_includes_connectivity_state_for_running_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        """XEDGE-421: distinct from `state` — see xedge.core.connectivity."""
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/api/v1/drivers/modbus_sim_01/enable")

        body = client.get("/api/v1/drivers/modbus_sim_01/health").json()

        assert body["connectivity_state"] == "unknown"

    def test_health_synthesizes_unknown_connectivity_for_never_started_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)

        body = client.get("/api/v1/drivers/modbus_sim_01/health").json()

        assert body["connectivity_state"] == "unknown"

    def test_disable_persists_config_and_updates_live_state(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        client.post("/api/v1/drivers/modbus_sim_01/enable")

        response = client.post("/api/v1/drivers/modbus_sim_01/disable")

        assert response.status_code == 200
        health = client.get("/api/v1/drivers/modbus_sim_01/health").json()
        assert health["state"] == "disabled"
        config_path = tmp_path / "xedge.yaml"
        assert "modbus_sim_01" in config_path.read_text()
        assert "enabled: false" in config_path.read_text()

    def test_enable_starts_a_previously_disabled_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/modbus_sim_01/enable")

        assert response.status_code == 200
        config_path = tmp_path / "xedge.yaml"
        assert "enabled: true" in config_path.read_text()

    def test_enable_disable_require_driver_restart_permission(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )
        viewer_client = TestClient(app)
        viewer_client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "viewerpass123"}
        )

        assert viewer_client.post("/api/v1/drivers/modbus_sim_01/disable").status_code == 403

    def test_disable_and_enable_are_audit_logged(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)

        client.post("/api/v1/drivers/modbus_sim_01/enable")
        client.post("/api/v1/drivers/modbus_sim_01/disable")

        events = [e["event"] for e in client.get("/api/v1/audit").json()]
        assert "driver.disabled" in events
        assert "driver.enabled" in events

    def test_validate_accepts_good_config_without_writing_or_affecting_live_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)
        config_path = tmp_path / "xedge.yaml"
        before = config_path.read_text()

        response = client.post(
            "/api/v1/drivers/modbus_sim_01/validate",
            json={
                "type": "modbus_tcp",
                "config": {"host": "127.0.0.1", "port": 502},
                "tag_groups": [],
            },
        )

        assert response.status_code == 200
        assert response.json() == {"valid": True, "errors": []}
        assert config_path.read_text() == before  # no file write

    def test_validate_rejects_bad_config(self, tmp_path: Path, core_schema_path: Path) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post(
            "/api/v1/drivers/modbus_sim_01/validate",
            json={"type": "modbus_tcp", "config": {}, "tag_groups": []},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert body["errors"]

    def test_validate_requires_config_read_permission(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app, _supervisor = self._build_app_with_driver(tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "auditor1", "password": "auditor1pass123", "role": "auditor"},
        )
        auditor_client = TestClient(app)
        auditor_client.post(
            "/api/v1/auth/login", json={"username": "auditor1", "password": "auditor1pass123"}
        )

        response = auditor_client.post(
            "/api/v1/drivers/modbus_sim_01/validate",
            json={"type": "modbus_tcp", "config": {"host": "x", "port": 502}, "tag_groups": []},
        )
        assert response.status_code == 200  # auditor has config:read


class TestTagWriteEndpoint:
    async def test_write_succeeds_for_operator_and_reaches_the_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor, driver = await _running_supervisor()
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)  # first-login setup account is admin

        response = client.post("/api/v1/drivers/d1/tags/setpoint/write", json={"value": 42})

        assert response.status_code == 200
        assert response.json() == {"tag_id": "d1/setpoint", "success": True}
        assert driver.written == [("setpoint", 42)]
        await supervisor.stop_all()

    async def test_write_is_audit_logged(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor, _driver = await _running_supervisor()
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        client.post("/api/v1/drivers/d1/tags/setpoint/write", json={"value": True})

        events = client.get("/api/v1/audit").json()
        write_events = [e for e in events if e["event"] == "tag.write"]
        assert len(write_events) == 1
        assert write_events[0]["details"]["tag_id"] == "d1/setpoint"
        assert write_events[0]["details"]["success"] is True
        await supervisor.stop_all()

    async def test_write_to_unknown_instance_is_422_not_500(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/nope/tags/x/write", json={"value": 1})

        assert response.status_code == 422

    async def test_write_requires_tag_write_permission_not_just_tag_read(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor, _driver = await _running_supervisor()
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )
        viewer_client = TestClient(app)
        viewer_client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "viewerpass123"}
        )

        response = viewer_client.post("/api/v1/drivers/d1/tags/setpoint/write", json={"value": 1})

        assert response.status_code == 403
        await supervisor.stop_all()


class TestAlarmsEndpoint:
    def _seeded_engine(self) -> AlarmEngine:
        engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
        engine.evaluate(
            UnifiedTag(
                tag_id="d1/temp",
                timestamp=datetime.now(UTC),
                value=95.0,
                data_type="FLOAT64",
                quality=Quality.GOOD,
                source_driver="d1",
                source_address="0",
            )
        )
        return engine

    def test_list_alarms_empty_when_engine_disabled(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.get("/api/v1/alarms")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_alarms_reports_active_alarm(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        client = _authenticated_client(app)

        response = client.get("/api/v1/alarms")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["tag_id"] == "d1/temp"
        assert body[0]["state"] == "active"
        assert body[0]["condition"] == "high"

    def test_acknowledge_active_alarm(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        client = _authenticated_client(app)  # admin

        response = client.post("/api/v1/alarms/d1/temp/acknowledge")

        assert response.status_code == 200
        assert response.json() == {"tag_id": "d1/temp", "acknowledged": True}
        assert client.get("/api/v1/alarms").json()[0]["state"] == "active_acked"

    def test_acknowledge_unknown_tag_is_422(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        client = _authenticated_client(app)

        response = client.post("/api/v1/alarms/no/such/tag/acknowledge")

        assert response.status_code == 422

    def test_alarm_actions_404_when_engine_disabled(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        assert client.post("/api/v1/alarms/d1/temp/acknowledge").status_code == 404
        assert client.post("/api/v1/alarms/d1/temp/shelve", json={}).status_code == 404
        assert client.post("/api/v1/alarms/d1/temp/unshelve").status_code == 404

    def test_shelve_and_unshelve_round_trip(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        client = _authenticated_client(app)

        shelve_response = client.post(
            "/api/v1/alarms/d1/temp/shelve", json={"duration_seconds": 60}
        )
        assert shelve_response.status_code == 200
        assert shelve_response.json()["shelved"] is True
        assert client.get("/api/v1/alarms").json()[0]["shelved_until"] is not None

        unshelve_response = client.post("/api/v1/alarms/d1/temp/unshelve")
        assert unshelve_response.status_code == 200
        assert client.get("/api/v1/alarms").json()[0]["shelved_until"] is None

    def test_unshelve_when_not_shelved_is_422(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        client = _authenticated_client(app)

        assert client.post("/api/v1/alarms/d1/temp/unshelve").status_code == 422

    def test_alarm_actions_are_audit_logged(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        client = _authenticated_client(app)

        client.post("/api/v1/alarms/d1/temp/acknowledge")

        events = [e["event"] for e in client.get("/api/v1/audit").json()]
        assert "alarm.acknowledged" in events

    def test_acknowledge_requires_alarm_manage_not_just_tag_read(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(
            supervisor, history, tmp_path, core_schema_path, alarm_engine=self._seeded_engine()
        )
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )
        viewer_client = TestClient(app)
        viewer_client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "viewerpass123"}
        )

        response = viewer_client.post("/api/v1/alarms/d1/temp/acknowledge")

        assert response.status_code == 403


class TestPrometheusMetricsEndpoint:
    def test_metrics_endpoint_requires_no_auth(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        anonymous_client = TestClient(app)

        response = anonymous_client.get("/metrics")

        assert response.status_code == 200
        assert "# HELP" in response.text
        assert "# TYPE" in response.text


class TestSerialPortsEndpoint:
    """XEDGE-434: backs the modbus_rtu_serial driver form's `port` field
    suggestions (schema `x-suggestions-endpoint`, see test_schema_forms.py)."""

    def test_returns_detected_ports_sorted(
        self, tmp_path: Path, core_schema_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xedge.api.server as server_module

        class _FakePortInfo:
            def __init__(self, device: str) -> None:
                self.device = device

        monkeypatch.setattr(
            server_module.list_ports,
            "comports",
            lambda: [_FakePortInfo("/dev/ttyUSB1"), _FakePortInfo("/dev/ttyUSB0")],
        )
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.get("/api/v1/serial-ports")

        assert response.status_code == 200
        assert response.json() == ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    def test_empty_when_nothing_detected(
        self, tmp_path: Path, core_schema_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xedge.api.server as server_module

        monkeypatch.setattr(server_module.list_ports, "comports", lambda: [])
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        assert client.get("/api/v1/serial-ports").json() == []

    def test_requires_authentication(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        anonymous_client = TestClient(app)

        assert anonymous_client.get("/api/v1/serial-ports").status_code == 401


class _PollableFakeDriver(FakeDriver):
    """FakeDriver + `poll_now`, standing in for a real on_demand-capable
    Modbus driver instance. The on_demand/on_connect polling loop itself
    (dispatch inside `_poll_group`, the Event-based trigger) is covered
    against the real driver in tests/integration/test_modbus_polling_modes.py;
    this fake only needs to prove the REST endpoint calls through to
    whatever `poll_now` the live driver instance happens to expose.
    """

    def __init__(self) -> None:
        super().__init__(emit_interval_seconds=1000)
        self.polled: list[str] = []

    async def poll_now(self, group_id: str) -> bool:
        if group_id != "ondemand_group":
            return False
        self.polled.append(group_id)
        return True


async def _start_driver(instance_id: str, driver: FakeDriver) -> DriverSupervisor:
    """Starts `driver` and waits for it to reach RUNNING, same shape as
    `_running_supervisor()` above generalized to an arbitrary driver double.
    Called `supervisor.start()` directly (never through the `/enable` HTTP
    endpoint) and waited on with `await asyncio.sleep()` in the *same* event
    loop, exactly like `_running_supervisor()` — TestClient's ASGI dispatch
    runs on its own loop/thread and offers no guarantee of interleaving a
    background task's progress with a synchronous `.post()` call, so
    starting through HTTP and immediately polling `health` for "running" is
    not reliable here (confirmed: it isn't, that's what this replaced).
    The `modbus_tcp` type name is arbitrary — these tests bypass config-file
    schema validation entirely by calling `start()` directly, so nothing
    reads it as anything other than a registry key.
    """
    registry = DriverRegistry()
    registry.register("modbus_tcp", lambda: driver)
    supervisor = DriverSupervisor(registry, asyncio.Queue(maxsize=100))
    supervisor.start(
        DriverConfig(instance_id=instance_id, driver_type="modbus_tcp", config={}, tag_groups=[])
    )
    for _ in range(300):
        if supervisor.status(instance_id).state == DriverState.RUNNING:
            return supervisor
        await asyncio.sleep(0.01)
    raise AssertionError(f"{instance_id!r} never reached the running state")


class TestPollTagGroupEndpoint:
    """XEDGE-435 (ADR-011 Part 2) — REST side of `poll_now()`. Covers only
    the endpoint's routing/permission/error contract; the on_demand/
    on_connect polling-loop behavior itself is covered against a real
    driver in tests/integration/test_modbus_polling_modes.py.
    """

    async def test_unknown_driver_instance_returns_404(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/nope/tag-groups/g1/poll")

        assert response.status_code == 404

    async def test_driver_not_running_returns_409(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = await _start_driver("pollable_01", _PollableFakeDriver())
        await supervisor.disable("pollable_01")
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/pollable_01/tag-groups/ondemand_group/poll")

        assert response.status_code == 409
        assert "disabled" in response.json()["detail"]

    async def test_driver_without_poll_now_support_returns_400(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = await _start_driver("fake_01", FakeDriver(emit_interval_seconds=1000))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/fake_01/tag-groups/g1/poll")

        assert response.status_code == 400
        await supervisor.stop_all()

    async def test_unknown_tag_group_returns_404(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        supervisor = await _start_driver("pollable_01", _PollableFakeDriver())
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/pollable_01/tag-groups/no_such_group/poll")

        assert response.status_code == 404
        await supervisor.stop_all()

    async def test_successful_trigger_returns_200_and_reaches_the_driver(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        driver = _PollableFakeDriver()
        supervisor = await _start_driver("pollable_01", driver)
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        client = _authenticated_client(app)

        response = client.post("/api/v1/drivers/pollable_01/tag-groups/ondemand_group/poll")

        assert response.status_code == 200
        assert response.json() == {
            "instance_id": "pollable_01",
            "tag_group": "ondemand_group",
            "triggered": True,
        }
        assert driver.polled == ["ondemand_group"]
        await supervisor.stop_all()

    async def test_requires_authentication(self, tmp_path: Path, core_schema_path: Path) -> None:
        supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
        history = ConfigVersionHistory(tmp_path)
        app = _build_app(supervisor, history, tmp_path, core_schema_path)
        anonymous_client = TestClient(app)

        response = anonymous_client.post("/api/v1/drivers/x/tag-groups/g/poll")

        assert response.status_code == 401
