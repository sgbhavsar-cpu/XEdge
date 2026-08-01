from __future__ import annotations

from pathlib import Path

from cryptography import x509
from httpx import ASGITransport, AsyncClient

from xedge.fleet.audit import FleetAuditLog
from xedge.fleet.auth import FleetSessionManager, FleetUserStore, LoginLockout
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.manager_device_app import create_fleet_device_app
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, load_or_create_ca
from xedge.security.csr import generate_key_and_csr

_ADMIN_PASSWORD = "correct horse battery staple"
_CERT_VALIDITY_DAYS = 90


async def _enrolled_client(
    tmp_path: Path,
    registry: DeviceRegistry,
    default_tenant_id: str,
    user_store: FleetUserStore,
    session_manager: FleetSessionManager,
    audit_log: FleetAuditLog,
    login_lockout: LoginLockout,
    device_id: str = "dev1",
) -> tuple[AsyncClient, AsyncClient, CertificateAuthority, str, str]:
    """Both apps, sharing one registry — the same relationship
    `xedge.fleet.manager_cli` wires them into for real, just without the
    two separate TCP ports/TLS listeners a unit test doesn't need. Returns
    (public_client, device_client, ca, admin_session_token, device_token)
    with `device_id` already enrolled. `httpx.AsyncClient` over
    `ASGITransport`, not `TestClient` — see test_fleet_manager_app.py's
    `_client` docstring for why (TestClient's separate event loop can't
    share asyncpg connections with the pytest-asyncio loop `fleet_registry`
    lives on)."""
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-fleet-ca", 3650)
    public_app = create_fleet_manager_app(
        registry,
        ca,
        user_store,
        session_manager,
        audit_log,
        login_lockout,
        cert_validity_days=_CERT_VALIDITY_DAYS,
    )
    device_app = create_fleet_device_app(registry, ca, cert_validity_days=_CERT_VALIDITY_DAYS)
    public_client = AsyncClient(transport=ASGITransport(app=public_app), base_url="http://test")
    device_client = AsyncClient(transport=ASGITransport(app=device_app), base_url="http://test")

    await user_store.create_user(default_tenant_id, "admin", _ADMIN_PASSWORD, "admin")
    login = await public_client.post(
        "/api/v1/fleet/auth/login",
        json={"tenant_name": "default", "username": "admin", "password": _ADMIN_PASSWORD},
    )
    admin_session_token: str = login.json()["session_token"]

    join_token = (
        await public_client.post(
            "/api/v1/fleet/join-tokens",
            headers={"Authorization": f"Bearer {admin_session_token}"},
            json={"device_id": device_id},
        )
    ).json()["join_token"]
    _private_key_pem, csr_pem = generate_key_and_csr(device_id)
    enroll = await public_client.post(
        "/api/v1/fleet/enroll",
        json={"device_id": device_id, "join_token": join_token, "csr_pem": csr_pem.decode("ascii")},
    )
    device_token: str = enroll.json()["device_token"]
    return public_client, device_client, ca, admin_session_token, device_token


async def test_heartbeat_requires_a_valid_device_token(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    _public, device_client, _ca, _admin_token, _token = await _enrolled_client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )

    no_auth = await device_client.post("/api/v1/fleet/devices/dev1/heartbeat", json={})
    assert no_auth.status_code == 401

    wrong_token = await device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers={"Authorization": "Bearer not-the-real-token"},
        json={},
    )
    assert wrong_token.status_code == 401


async def test_heartbeat_with_valid_token_updates_registry_and_returns_no_pending_config(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    _public, device_client, _ca, _admin_token, token = await _enrolled_client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )

    response = await device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"driver_count": 3, "uptime_seconds": 42.0},
    )
    assert response.status_code == 200
    assert response.json() == {"pending_config": None, "pending_config_version": None}
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.status == "online"


async def test_config_push_is_delivered_on_next_heartbeat_and_reported_on_the_one_after(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    public_client, device_client, _ca, admin_token, token = await _enrolled_client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    auth = {"Authorization": f"Bearer {token}"}
    admin_auth = {"Authorization": f"Bearer {admin_token}"}

    push = await public_client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=admin_auth,
        json={"config": {"schema_version": "0.1", "logging": {"level": "DEBUG"}}},
    )
    assert push.status_code == 202

    heartbeat = await device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat", headers=auth, json={}
    )
    assert heartbeat.json()["pending_config"] == {
        "schema_version": "0.1",
        "logging": {"level": "DEBUG"},
    }
    assert heartbeat.json()["pending_config_version"] == 1

    # Delivered at most once.
    second_heartbeat = await device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat", headers=auth, json={}
    )
    assert second_heartbeat.json()["pending_config"] is None

    report = await device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers=auth,
        json={"last_config_apply": {"version": 1, "success": True, "error": None}},
    )
    assert report.status_code == 200
    status_after = (
        await public_client.get("/api/v1/fleet/devices/dev1/config/status", headers=admin_auth)
    ).json()
    assert status_after["has_pending_config"] is False
    assert status_after["last_config_apply"] == {"version": 1, "success": True, "error": None}


async def test_rotate_certificate_requires_a_valid_device_token(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    _public, device_client, _ca, _admin_token, _token = await _enrolled_client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    _private_key_pem, csr_pem = generate_key_and_csr("dev1")

    response = await device_client.post(
        "/api/v1/fleet/devices/dev1/rotate-certificate",
        headers={"Authorization": "Bearer wrong"},
        json={"csr_pem": csr_pem.decode("ascii")},
    )
    assert response.status_code == 401


async def test_rotate_certificate_issues_a_new_certificate_for_the_same_identity(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    _public, device_client, ca, _admin_token, token = await _enrolled_client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )
    original_record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    original_serial = original_record.cert_serial_number
    _private_key_pem, csr_pem = generate_key_and_csr("dev1")

    response = await device_client.post(
        "/api/v1/fleet/devices/dev1/rotate-certificate",
        headers={"Authorization": f"Bearer {token}"},
        json={"csr_pem": csr_pem.decode("ascii")},
    )

    assert response.status_code == 200
    certificate = x509.load_pem_x509_certificate(response.json()["certificate_pem"].encode("ascii"))
    assert certificate.subject.rfc4514_string() == "CN=dev1"
    assert certificate.issuer == ca.certificate.subject
    assert str(certificate.serial_number) != original_serial
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.cert_serial_number == str(certificate.serial_number)


async def test_rotate_certificate_rejects_a_malformed_csr(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    fleet_user_store: FleetUserStore,
    fleet_session_manager: FleetSessionManager,
    fleet_audit_log: FleetAuditLog,
    fleet_login_lockout: LoginLockout,
) -> None:
    _public, device_client, _ca, _admin_token, token = await _enrolled_client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        fleet_user_store,
        fleet_session_manager,
        fleet_audit_log,
        fleet_login_lockout,
    )

    response = await device_client.post(
        "/api/v1/fleet/devices/dev1/rotate-certificate",
        headers={"Authorization": f"Bearer {token}"},
        json={"csr_pem": "not a real csr"},
    )

    assert response.status_code == 400
