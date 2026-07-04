from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures.fake_driver import FakeDriver
from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
from xedge.api.server import create_app
from xedge.core.config import ConfigVersionHistory
from xedge.core.supervisor import DriverConfig, DriverRegistry, DriverSupervisor
from xedge.drivers.base import TagUpdate


def _build_app(
    tmp_path: Path, core_schema_path: Path, supervisor: DriverSupervisor | None = None
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


def test_login_submit_with_wrong_password_shows_error(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    TestClient(app).post(
        "/ui/setup", data={"password": "correct-password", "confirm_password": "correct-password"}
    )

    fresh_client = TestClient(app)
    response = fresh_client.post("/ui/login", data={"password": "wrong"})
    assert "Invalid password" in response.text


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


class TestConfigEditorPage:
    def _authenticated_client(self, app: FastAPI) -> TestClient:
        client = TestClient(app)
        client.post(
            "/ui/setup",
            data={"password": "correct-password", "confirm_password": "correct-password"},
        )
        return client

    def test_config_page_shows_current_yaml(self, tmp_path: Path, core_schema_path: Path) -> None:
        history = ConfigVersionHistory(tmp_path)
        history.save({"schema_version": "0.1", "logging": {"level": "DEBUG"}})
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        response = client.get("/ui/config")
        assert response.status_code == 200
        assert "DEBUG" in response.text

    def test_config_page_redirects_when_unauthenticated(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/ui/config")
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    def test_valid_config_submit_writes_file_and_shows_success(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        response = client.post(
            "/ui/config", data={"yaml_text": "schema_version: '0.1'\nlogging:\n  level: DEBUG\n"}
        )
        assert response.status_code == 200
        assert "Saved" in response.text
        assert "DEBUG" in (tmp_path / "xedge.yaml").read_text(encoding="utf-8")

    def test_invalid_yaml_submit_shows_error_and_preserves_input(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        bad_yaml = "schema_version: '0.1'\n  bad indentation: [oops\n"
        response = client.post("/ui/config", data={"yaml_text": bad_yaml})
        assert "Invalid YAML" in response.text
        assert "oops" in response.text  # user's input preserved for correction

    def test_schema_invalid_config_submit_shows_validation_error(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        client = self._authenticated_client(app)
        response = client.post(
            "/ui/config", data={"yaml_text": "schema_version: 'not-a-valid-version'\n"}
        )
        assert response.status_code == 200
        assert "error" in response.text.lower()
