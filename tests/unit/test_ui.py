from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures.fake_driver import FakeDriver
from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
from xedge.api.server import create_app
from xedge.core.alarms import AlarmEngine, AlarmRule
from xedge.core.config import ConfigVersionHistory
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverConfig, DriverRegistry, DriverSupervisor
from xedge.drivers.base import Quality, TagUpdate
from xedge.fleet.agent import FleetAgentStatus
from xedge.observability.audit_log import AuditLog
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferManager


def _build_app(
    tmp_path: Path,
    core_schema_path: Path,
    supervisor: DriverSupervisor | None = None,
    fleet_status: FleetAgentStatus | None = None,
    alarm_engine: AlarmEngine | None = None,
) -> FastAPI:
    config_path = tmp_path / "xedge.yaml"
    if not config_path.is_file():
        config_path.write_text("schema_version: '0.1'\n", encoding="utf-8")
    return create_app(
        supervisor or DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1)),
        ConfigVersionHistory(tmp_path),
        None,
        user_store=UserStore(tmp_path / "webui" / "users.json"),
        session_manager=SessionManager(secret_key=b"test-secret-key"),
        login_tracker=LoginAttemptTracker(),
        config_path=config_path,
        schema_path=core_schema_path,
        latest_values=LatestValueStore(),
        audit_log=AuditLog(tmp_path / "webui" / "audit.jsonl"),
        ring_buffers=RingBufferManager(),
        fleet_status=fleet_status,
        alarm_engine=alarm_engine,
    )


def test_index_redirects_to_setup_when_no_account(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app, follow_redirects=False)
    response = client.get("/ui")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/setup"


def test_setup_page_renders_form(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    response = client.get("/ui/setup")
    assert response.status_code == 200
    assert "Set up this device" in response.text


def test_setup_submit_creates_account_and_redirects_to_dashboard(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    response = client.post(
        "/ui/setup",
        data={"password": "hunter2hunter2", "confirm_password": "hunter2hunter2"},
    )
    assert response.status_code == 200  # after following the redirect
    assert response.url.path == "/ui/dashboard"
    assert (tmp_path / "webui" / "users.json").is_file()


def test_setup_submit_mismatched_passwords_shows_error_and_no_account_created(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    response = client.post(
        "/ui/setup", data={"password": "hunter2hunter2", "confirm_password": "different"}
    )
    assert response.status_code == 200
    assert "do not match" in response.text
    assert not (tmp_path / "webui" / "users.json").is_file()


def test_setup_submit_short_password_rejected(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    response = client.post("/ui/setup", data={"password": "short", "confirm_password": "short"})
    assert "at least" in response.text
    assert not (tmp_path / "webui" / "users.json").is_file()


def test_index_redirects_to_login_when_account_exists_but_not_authenticated(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    setup_client = TestClient(app)
    setup_client.post(
        "/ui/setup", data={"password": "hunter2hunter2", "confirm_password": "hunter2hunter2"}
    )

    fresh_client = TestClient(app, follow_redirects=False)
    response = fresh_client.get("/ui")
    assert response.headers["location"] == "/ui/login"


def test_login_submit_with_correct_password_reaches_dashboard(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    TestClient(app).post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )

    fresh_client = TestClient(app)
    response = fresh_client.post("/ui/login", data={"password": "correct-password"})
    assert response.url.path == "/ui/dashboard"


def test_login_submit_records_client_ip_in_audit_log(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    TestClient(app).post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )

    fresh_client = TestClient(app)
    fresh_client.post("/ui/login", data={"password": "correct-password"})

    events = fresh_client.get("/api/v1/audit").json()
    login_events = [e for e in events if e["event"] == "auth.login_success"]
    assert login_events
    assert login_events[0]["details"]["ip"] is not None


def test_login_submit_with_wrong_password_shows_error(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    TestClient(app).post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )

    fresh_client = TestClient(app)
    response = fresh_client.post("/ui/login", data={"password": "wrong"})
    assert "Invalid username or password" in response.text


def test_dashboard_redirects_to_login_when_unauthenticated(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    TestClient(app).post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    fresh_client = TestClient(app, follow_redirects=False)
    response = fresh_client.get("/ui/dashboard")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_dashboard_shows_driver_status_when_authenticated(
    tmp_path: Path, core_schema_path: Path
) -> None:
    queue: asyncio.Queue[TagUpdate] = asyncio.Queue(maxsize=100)
    driver = FakeDriver(emit_interval_seconds=0.001)
    registry = DriverRegistry()
    registry.register("fake", lambda: driver)
    supervisor = DriverSupervisor(registry, queue)
    supervisor.start(DriverConfig(instance_id="d1", driver_type="fake", config={}))
    try:
        for _ in range(200):
            if driver.emitted_count >= 1:
                break
            await asyncio.sleep(0.01)

        app = _build_app(tmp_path, core_schema_path, supervisor=supervisor)
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        response = client.get("/ui/dashboard")
        assert response.status_code == 200
        assert "d1" in response.text
        assert "fake" in response.text
    finally:
        await supervisor.stop_all()


def test_dashboard_omits_fleet_card_when_fleet_disabled(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path, fleet_status=FleetAgentStatus())
    client = TestClient(app)
    client.post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    response = client.get("/ui/dashboard")
    assert "Fleet</h1>" not in response.text


def test_dashboard_shows_fleet_card_when_fleet_enabled(
    tmp_path: Path, core_schema_path: Path
) -> None:
    status = FleetAgentStatus(enabled=True, manager_url="https://fleet.example", device_id="dev1")
    app = _build_app(tmp_path, core_schema_path, fleet_status=status)
    client = TestClient(app)
    client.post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    response = client.get("/ui/dashboard")
    assert "Fleet</h1>" in response.text


def test_fleet_status_endpoint_reports_disabled_by_default(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    client.post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    response = client.get("/api/v1/fleet/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_fleet_status_endpoint_reports_live_agent_state(
    tmp_path: Path, core_schema_path: Path
) -> None:
    status = FleetAgentStatus(
        enabled=True,
        manager_url="https://fleet.example",
        device_id="dev1",
        registered=True,
        last_heartbeat_ok=True,
    )
    app = _build_app(tmp_path, core_schema_path, fleet_status=status)
    client = TestClient(app)
    client.post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    response = client.get("/api/v1/fleet/status")
    body = response.json()
    assert body["enabled"] is True
    assert body["registered"] is True
    assert body["device_id"] == "dev1"
    assert body["last_heartbeat_ok"] is True


def test_logout_redirects_to_login_and_clears_session(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    client.post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )
    client.post("/ui/logout")

    fresh_check = TestClient(app, follow_redirects=False)
    fresh_check.cookies = client.cookies
    response = fresh_check.get("/ui/dashboard")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


class TestUsersPage:
    def _authenticated_client(self, app: FastAPI) -> TestClient:
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        return client

    def test_users_page_redirects_when_unauthenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/users")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_users_page_shows_admin_when_authenticated_as_admin(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        response = client.get("/ui/users")
        assert response.status_code == 200
        assert "admin" in response.text

    def test_users_page_denied_for_readonly_role(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        admin_client = self._authenticated_client(app)
        admin_client.post(
            "/ui/users/new",
            data={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )

        viewer_client = TestClient(app, follow_redirects=False)
        viewer_client.post("/ui/login", data={"username": "viewer", "password": "viewerpass123"})
        response = viewer_client.get("/ui/users")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/dashboard"


class TestAuditPage:
    def _authenticated_client(self, app: FastAPI) -> TestClient:
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        return client

    def test_audit_page_redirects_when_unauthenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/audit")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_audit_page_shows_entries_for_admin(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)  # setup produces an auth.setup event
        response = client.get("/ui/audit")
        assert response.status_code == 200
        assert "auth.setup" in response.text

    def test_audit_page_denied_for_operator_role(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        admin_client = self._authenticated_client(app)
        admin_client.post(
            "/ui/users/new",
            data={"username": "opuser", "password": "opuserpass123", "role": "operator"},
        )

        op_client = TestClient(app, follow_redirects=False)
        op_client.post("/ui/login", data={"username": "opuser", "password": "opuserpass123"})
        response = op_client.get("/ui/audit")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/dashboard"


class TestDiagnosticsPage:
    def _authenticated_client(self, app: FastAPI) -> TestClient:
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        return client

    def test_diagnostics_page_redirects_when_unauthenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/diagnostics")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_diagnostics_page_reachable_by_readonly_role(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        # tag:read (the broadest permission any diagnostic command needs)
        # is granted to every role, including readonly — the page itself
        # isn't gated tighter than that; individual commands still enforce
        # their own stricter permission over the WebSocket.
        app = _build_app(tmp_path, core_schema_path)
        admin_client = self._authenticated_client(app)
        admin_client.post(
            "/ui/users/new",
            data={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )

        viewer_client = TestClient(app)
        viewer_client.post("/ui/login", data={"username": "viewer", "password": "viewerpass123"})
        response = viewer_client.get("/ui/diagnostics")
        assert response.status_code == 200
        assert "ws/diagnostics" in response.text


class TestConfigRootPage:
    """/ui/config now shows the schema-driven tree + landing page (see
    tests/unit/test_config_ui.py for the forms themselves); the raw-YAML
    editor these tests used to cover moved to /ui/config/advanced."""

    def _authenticated_client(self, app: FastAPI) -> TestClient:
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        return client

    def test_config_root_redirects_when_unauthenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/config")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_config_root_shows_tree_when_authenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        response = client.get("/ui/config")
        assert response.status_code == 200
        assert "Core Settings" in response.text


def _seeded_alarm_engine() -> AlarmEngine:
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


class TestAlarmsPage:
    def _authenticated_client(self, app: FastAPI) -> TestClient:
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        return client

    def test_alarms_page_redirects_when_unauthenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/alarms")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_alarms_page_shows_message_when_engine_disabled(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        response = client.get("/ui/alarms")
        assert response.status_code == 200
        assert "not enabled" in response.text

    def test_alarms_page_shows_active_alarm(self, tmp_path: Path, core_schema_path: Path) -> None:
        app = _build_app(tmp_path, core_schema_path, alarm_engine=_seeded_alarm_engine())
        client = self._authenticated_client(app)
        response = client.get("/ui/alarms")
        assert response.status_code == 200
        assert "d1/temp" in response.text
        assert "active" in response.text

    def test_acknowledge_button_hidden_for_readonly_role(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path, alarm_engine=_seeded_alarm_engine())
        admin_client = self._authenticated_client(app)
        admin_client.post(
            "/ui/users/new",
            data={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )

        viewer_client = TestClient(app)
        viewer_client.post("/ui/login", data={"username": "viewer", "password": "viewerpass123"})
        response = viewer_client.get("/ui/alarms")
        assert response.status_code == 200
        assert ">Acknowledge<" not in response.text

    def test_acknowledge_form_submit_updates_state_and_audits(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        engine = _seeded_alarm_engine()
        app = _build_app(tmp_path, core_schema_path, alarm_engine=engine)
        client = self._authenticated_client(app)

        response = client.post("/ui/alarms/d1/temp/acknowledge")
        assert response.url.path == "/ui/alarms"
        assert engine.all_status()["d1/temp"].acknowledged_by == "admin"

        events = client.get("/api/v1/audit").json()
        assert any(e["event"] == "alarm.acknowledged" for e in events)

    def test_shelve_and_unshelve_form_submit(self, tmp_path: Path, core_schema_path: Path) -> None:
        engine = _seeded_alarm_engine()
        app = _build_app(tmp_path, core_schema_path, alarm_engine=engine)
        client = self._authenticated_client(app)

        client.post("/ui/alarms/d1/temp/shelve", data={"duration_seconds": "60"})
        assert engine.all_status()["d1/temp"].shelved_until is not None

        client.post("/ui/alarms/d1/temp/unshelve")
        assert engine.all_status()["d1/temp"].shelved_until is None

    def test_alarm_actions_denied_for_readonly_role(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        engine = _seeded_alarm_engine()
        app = _build_app(tmp_path, core_schema_path, alarm_engine=engine)
        admin_client = self._authenticated_client(app)
        admin_client.post(
            "/ui/users/new",
            data={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )

        viewer_client = TestClient(app, follow_redirects=False)
        viewer_client.post("/ui/login", data={"username": "viewer", "password": "viewerpass123"})
        response = viewer_client.post("/ui/alarms/d1/temp/acknowledge")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/dashboard"
        assert engine.all_status()["d1/temp"].acknowledged_by is None
