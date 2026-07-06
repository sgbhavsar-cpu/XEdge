from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.registry import DeviceRegistry


def _client(tmp_path: Path) -> TestClient:
    registry = DeviceRegistry(tmp_path / "devices.db")
    app = create_fleet_manager_app(registry, join_token="join-secret", admin_token="admin-secret")
    return TestClient(app)


def test_register_with_valid_join_token_returns_a_device_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/fleet/register",
        json={"device_id": "dev1", "join_token": "join-secret", "agent_version": "0.1.0"},
    )
    assert response.status_code == 200
    assert response.json()["device_token"]


def test_register_with_invalid_join_token_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/fleet/register", json={"device_id": "dev1", "join_token": "wrong"}
    )
    assert response.status_code == 401


def test_heartbeat_requires_a_valid_device_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/v1/fleet/register", json={"device_id": "dev1", "join_token": "join-secret"})

    no_auth = client.post("/api/v1/fleet/devices/dev1/heartbeat", json={})
    assert no_auth.status_code == 401

    wrong_token = client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers={"Authorization": "Bearer not-the-real-token"},
        json={},
    )
    assert wrong_token.status_code == 401


def test_heartbeat_with_valid_token_updates_registry_and_returns_no_pending_config(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    register = client.post(
        "/api/v1/fleet/register", json={"device_id": "dev1", "join_token": "join-secret"}
    )
    token = register.json()["device_token"]

    response = client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"driver_count": 3, "uptime_seconds": 42.0},
    )
    assert response.status_code == 200
    assert response.json() == {"pending_config": None, "pending_config_version": None}


def test_list_and_get_devices_require_admin_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/v1/fleet/register", json={"device_id": "dev1", "join_token": "join-secret"})

    assert client.get("/api/v1/fleet/devices").status_code == 401
    wrong_auth = client.get("/api/v1/fleet/devices", headers={"Authorization": "Bearer wrong"})
    assert wrong_auth.status_code == 401

    ok = client.get("/api/v1/fleet/devices", headers={"Authorization": "Bearer admin-secret"})
    assert ok.status_code == 200
    assert [d["device_id"] for d in ok.json()] == ["dev1"]

    detail = client.get(
        "/api/v1/fleet/devices/dev1", headers={"Authorization": "Bearer admin-secret"}
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "unknown"


def test_get_unknown_device_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/api/v1/fleet/devices/nope", headers={"Authorization": "Bearer admin-secret"}
    )
    assert response.status_code == 404


def test_config_push_is_delivered_on_next_heartbeat_and_reported_on_the_one_after(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    register = client.post(
        "/api/v1/fleet/register", json={"device_id": "dev1", "join_token": "join-secret"}
    )
    token = register.json()["device_token"]
    auth = {"Authorization": f"Bearer {token}"}
    admin_auth = {"Authorization": "Bearer admin-secret"}

    push = client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=admin_auth,
        json={"config": {"schema_version": "0.1", "logging": {"level": "DEBUG"}}},
    )
    assert push.status_code == 202
    assert push.json() == {"queued": True, "pending_config_version": 1}

    status_before = client.get(
        "/api/v1/fleet/devices/dev1/config/status", headers=admin_auth
    ).json()
    assert status_before["has_pending_config"] is True
    assert status_before["last_config_apply"] is None

    heartbeat = client.post("/api/v1/fleet/devices/dev1/heartbeat", headers=auth, json={})
    assert heartbeat.json()["pending_config"] == {
        "schema_version": "0.1",
        "logging": {"level": "DEBUG"},
    }
    assert heartbeat.json()["pending_config_version"] == 1

    # Delivered at most once: a second heartbeat with no new push sees nothing pending.
    second_heartbeat = client.post("/api/v1/fleet/devices/dev1/heartbeat", headers=auth, json={})
    assert second_heartbeat.json()["pending_config"] is None

    # Device reports the apply result on this next heartbeat.
    report = client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers=auth,
        json={"last_config_apply": {"version": 1, "success": True, "error": None}},
    )
    assert report.status_code == 200

    status_after = client.get(
        "/api/v1/fleet/devices/dev1/config/status", headers=admin_auth
    ).json()
    assert status_after["has_pending_config"] is False
    assert status_after["last_config_apply"] == {"version": 1, "success": True, "error": None}


def test_config_push_to_unknown_device_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/fleet/devices/nope/config",
        headers={"Authorization": "Bearer admin-secret"},
        json={"config": {}},
    )
    assert response.status_code == 404
