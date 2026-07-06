"""WebSocket diagnostic command integration tests (Sprint 17, XEDGE-139).

Uses the real `create_app()` (mirroring tests/unit/test_api.py's `_build_app`
pattern) and FastAPI's `TestClient.websocket_connect` for a genuine
WebSocket round-trip against every command, not a mocked one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from xedge.api.auth import LoginAttemptTracker, SessionManager, UserStore
from xedge.api.server import create_app
from xedge.core.config import ConfigVersionHistory
from xedge.core.pipeline import UnifiedTag
from xedge.core.supervisor import DriverRegistry, DriverSupervisor
from xedge.drivers.base import Quality, TagUpdate
from xedge.observability.audit_log import AuditLog
from xedge.store.latest_values import LatestValueStore
from xedge.store.ring_buffer import RingBufferManager


def _build_app(tmp_path: Path, core_schema_path: Path, supervisor: DriverSupervisor | None = None) -> FastAPI:
    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        "schema_version: '0.1'\ndrivers:\n  - id: modbus_sim_01\n    type: modbus_tcp\n"
        "    enabled: true\n    config:\n      host: 127.0.0.1\n      port: 1502\n"
        "    tag_groups: []\n",
        encoding="utf-8",
    )
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
    )


def _authenticated_client(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.post("/api/v1/auth/setup", json={"password": "correct-password-123"})
    return client


def test_unauthenticated_connection_is_rejected(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws/diagnostics"):
            pass
        raise AssertionError("expected the connection to be rejected")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_status_command(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "status", "args": []})
        response = ws.receive_json()
    assert response["status"] == "ok"
    assert "driver_count" in response["result"]


def test_driver_list_command(tmp_path: Path, core_schema_path: Path) -> None:
    supervisor = DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1))
    app = _build_app(tmp_path, core_schema_path, supervisor)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "driver", "args": ["list"]})
        response = ws.receive_json()
    assert response["status"] == "ok"
    assert isinstance(response["result"], list)


def test_tag_read_returns_latest_value(tmp_path: Path, core_schema_path: Path) -> None:
    latest_values = LatestValueStore()
    latest_values.update(
        UnifiedTag(
            tag_id="modbus_sim_01/probe",
            timestamp=datetime.now(UTC),
            value=123,
            data_type="int",
            quality=Quality.GOOD,
            source_driver="modbus_sim_01",
            source_address="probe",
        )
    )
    config_path = tmp_path / "xedge.yaml"
    config_path.write_text("schema_version: '0.1'\ndrivers: []\n", encoding="utf-8")
    app = create_app(
        DriverSupervisor(DriverRegistry(), asyncio.Queue(maxsize=1)),
        ConfigVersionHistory(tmp_path),
        None,
        user_store=UserStore(tmp_path / "webui" / "users.json"),
        session_manager=SessionManager(secret_key=b"test-secret-key"),
        login_tracker=LoginAttemptTracker(),
        config_path=config_path,
        schema_path=core_schema_path,
        latest_values=latest_values,
        audit_log=AuditLog(tmp_path / "webui" / "audit.jsonl"),
        ring_buffers=RingBufferManager(),
    )
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "tag", "args": ["read", "modbus_sim_01/probe"]})
        response = ws.receive_json()
    assert response["status"] == "ok"
    assert response["result"]["value"] == 123


def test_tag_read_unknown_tag_returns_error_without_closing(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "tag", "args": ["read", "no/such/tag"]})
        response = ws.receive_json()
        assert response["status"] == "error"

        ws.send_json({"command": "status", "args": []})
        second = ws.receive_json()
        assert second["status"] == "ok"


def test_config_show_and_validate(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "config", "args": ["validate"]})
        validate_response = ws.receive_json()
        assert validate_response["status"] == "ok"
        assert validate_response["result"]["valid"] is True

        ws.send_json({"command": "config", "args": ["show"]})
        show_response = ws.receive_json()
        assert show_response["status"] == "ok"


def test_store_forward_status_command(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "store-forward", "args": ["status"]})
        response = ws.receive_json()
    assert response["status"] == "ok"
    assert "streams" in response["result"]


def test_network_check_command(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "network", "args": ["check"]})
        response = ws.receive_json()
    assert response["status"] == "ok"
    assert "drivers" in response["result"]


def test_self_test_command_passes_without_northbound(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "self-test", "args": []})
        response = ws.receive_json()
    assert response["status"] == "ok"
    result = response["result"]
    assert result["passed"] is True
    assert result["loopback"]["passed"] is True
    assert result["store"]["passed"] is True
    assert result["northbound"] == {"configured": False}


def test_unknown_command_returns_error(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "bogus", "args": []})
        response = ws.receive_json()
    assert response["status"] == "error"


class TestPermissionEnforcement:
    def test_readonly_role_can_read_status_but_not_restart_or_selftest(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "viewer", "password": "viewerpass123", "role": "readonly"},
        )
        viewer_client = TestClient(app)
        viewer_client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "viewerpass123"}
        )

        with viewer_client.websocket_connect("/ws/diagnostics") as ws:
            ws.send_json({"command": "status", "args": []})
            assert ws.receive_json()["status"] == "ok"

            ws.send_json({"command": "driver", "args": ["restart", "modbus_sim_01"]})
            assert ws.receive_json()["status"] == "error"

            ws.send_json({"command": "self-test", "args": []})
            assert ws.receive_json()["status"] == "error"

            ws.send_json({"command": "config", "args": ["show"]})
            assert ws.receive_json()["status"] == "error"

    def test_operator_role_can_restart_and_selftest(
        self, tmp_path: Path, core_schema_path: Path
    ) -> None:
        app = _build_app(tmp_path, core_schema_path)
        admin_client = _authenticated_client(app)
        admin_client.post(
            "/api/v1/users",
            json={"username": "opuser", "password": "opuserpass123", "role": "operator"},
        )
        op_client = TestClient(app)
        op_client.post(
            "/api/v1/auth/login", json={"username": "opuser", "password": "opuserpass123"}
        )

        with op_client.websocket_connect("/ws/diagnostics") as ws:
            ws.send_json({"command": "self-test", "args": []})
            assert ws.receive_json()["status"] == "ok"


def test_each_command_produces_an_audit_entry_with_session_id(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "status", "args": []})
        ws.receive_json()
        ws.send_json({"command": "status", "args": []})
        ws.receive_json()

    events = client.get("/api/v1/audit").json()
    diagnostic_events = [e for e in events if e["event"] == "diagnostic.command"]
    assert len(diagnostic_events) == 2
    session_ids = {e["details"]["session_id"] for e in diagnostic_events}
    assert len(session_ids) == 1  # both commands share the one connection's session id


def test_driver_restart_unknown_instance_returns_error(
    tmp_path: Path, core_schema_path: Path
) -> None:
    # A real end-to-end "does a live driver actually restart" round-trip
    # needs a driver instance started on the *same* event loop the ASGI
    # app's WebSocket handler runs on — TestClient runs the app in its own
    # portal thread/loop, so a supervisor.start() called from the test
    # function's own thread binds the task to the wrong loop and the
    # later stop()/await fails cross-loop. That full round-trip is instead
    # covered by manual verification against the real running app (see the
    # session's verification notes); this test covers the not-found path,
    # which doesn't depend on a live background task at all.
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)

    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "driver", "args": ["restart", "no_such_instance"]})
        response = ws.receive_json()

    assert response["status"] == "error"


def test_compliance_cip_007_command(tmp_path: Path, core_schema_path: Path) -> None:
    app = _build_app(tmp_path, core_schema_path)
    client = _authenticated_client(app)
    client.post("/api/v1/auth/login", json={"password": "correct-password-123"})

    with client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "compliance", "args": ["cip-007"]})
        response = ws.receive_json()

    assert response["status"] == "ok"
    result = response["result"]
    assert "failed_logins" in result
    assert "successful_logins" in result
    assert result["successful_logins"]
    assert "xedge_version" in result
    assert "dependency_versions" in result


def test_compliance_cip_007_requires_audit_read_permission(
    tmp_path: Path, core_schema_path: Path
) -> None:
    app = _build_app(tmp_path, core_schema_path)
    admin_client = _authenticated_client(app)
    admin_client.post(
        "/api/v1/users",
        json={"username": "opuser", "password": "opuserpass123", "role": "operator"},
    )
    op_client = TestClient(app)
    op_client.post("/api/v1/auth/login", json={"username": "opuser", "password": "opuserpass123"})

    with op_client.websocket_connect("/ws/diagnostics") as ws:
        ws.send_json({"command": "compliance", "args": ["cip-007"]})
        response = ws.receive_json()

    assert response["status"] == "error"
