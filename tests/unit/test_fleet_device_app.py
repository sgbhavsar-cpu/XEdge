from __future__ import annotations

from pathlib import Path

from cryptography import x509
from fastapi.testclient import TestClient

from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.manager_device_app import create_fleet_device_app
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, load_or_create_ca
from xedge.security.csr import generate_key_and_csr

_ADMIN_TOKEN = "admin-secret"
_CERT_VALIDITY_DAYS = 90


def _enrolled_client(
    tmp_path: Path, device_id: str = "dev1"
) -> tuple[TestClient, TestClient, DeviceRegistry, CertificateAuthority, str]:
    """Both apps, sharing one registry — the same relationship
    `xedge.fleet.manager_cli` wires them into for real, just without the
    two separate TCP ports/TLS listeners a unit test doesn't need. Returns
    (public_client, device_client, registry, ca, device_token) with
    `device_id` already enrolled."""
    registry = DeviceRegistry(tmp_path / "devices.db")
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-fleet-ca", 3650)
    public_app = create_fleet_manager_app(
        registry, ca, admin_token=_ADMIN_TOKEN, cert_validity_days=_CERT_VALIDITY_DAYS
    )
    device_app = create_fleet_device_app(registry, ca, cert_validity_days=_CERT_VALIDITY_DAYS)
    public_client = TestClient(public_app)
    device_client = TestClient(device_app)

    join_token = public_client.post(
        "/api/v1/fleet/join-tokens",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json={"device_id": device_id},
    ).json()["join_token"]
    _private_key_pem, csr_pem = generate_key_and_csr(device_id)
    enroll = public_client.post(
        "/api/v1/fleet/enroll",
        json={"device_id": device_id, "join_token": join_token, "csr_pem": csr_pem.decode("ascii")},
    )
    device_token = enroll.json()["device_token"]
    return public_client, device_client, registry, ca, device_token


def test_heartbeat_requires_a_valid_device_token(tmp_path: Path) -> None:
    _public, device_client, _registry, _ca, _token = _enrolled_client(tmp_path)

    no_auth = device_client.post("/api/v1/fleet/devices/dev1/heartbeat", json={})
    assert no_auth.status_code == 401

    wrong_token = device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers={"Authorization": "Bearer not-the-real-token"},
        json={},
    )
    assert wrong_token.status_code == 401


def test_heartbeat_with_valid_token_updates_registry_and_returns_no_pending_config(
    tmp_path: Path,
) -> None:
    _public, device_client, registry, _ca, token = _enrolled_client(tmp_path)

    response = device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"driver_count": 3, "uptime_seconds": 42.0},
    )
    assert response.status_code == 200
    assert response.json() == {"pending_config": None, "pending_config_version": None}
    assert registry.get("dev1").status == "online"


def test_config_push_is_delivered_on_next_heartbeat_and_reported_on_the_one_after(
    tmp_path: Path,
) -> None:
    public_client, device_client, _registry, _ca, token = _enrolled_client(tmp_path)
    auth = {"Authorization": f"Bearer {token}"}
    admin_auth = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}

    push = public_client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=admin_auth,
        json={"config": {"schema_version": "0.1", "logging": {"level": "DEBUG"}}},
    )
    assert push.status_code == 202

    heartbeat = device_client.post("/api/v1/fleet/devices/dev1/heartbeat", headers=auth, json={})
    assert heartbeat.json()["pending_config"] == {
        "schema_version": "0.1",
        "logging": {"level": "DEBUG"},
    }
    assert heartbeat.json()["pending_config_version"] == 1

    # Delivered at most once.
    second_heartbeat = device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat", headers=auth, json={}
    )
    assert second_heartbeat.json()["pending_config"] is None

    report = device_client.post(
        "/api/v1/fleet/devices/dev1/heartbeat",
        headers=auth,
        json={"last_config_apply": {"version": 1, "success": True, "error": None}},
    )
    assert report.status_code == 200
    status_after = public_client.get(
        "/api/v1/fleet/devices/dev1/config/status", headers=admin_auth
    ).json()
    assert status_after["has_pending_config"] is False
    assert status_after["last_config_apply"] == {"version": 1, "success": True, "error": None}


def test_rotate_certificate_requires_a_valid_device_token(tmp_path: Path) -> None:
    _public, device_client, _registry, _ca, _token = _enrolled_client(tmp_path)
    _private_key_pem, csr_pem = generate_key_and_csr("dev1")

    response = device_client.post(
        "/api/v1/fleet/devices/dev1/rotate-certificate",
        headers={"Authorization": "Bearer wrong"},
        json={"csr_pem": csr_pem.decode("ascii")},
    )
    assert response.status_code == 401


def test_rotate_certificate_issues_a_new_certificate_for_the_same_identity(tmp_path: Path) -> None:
    _public, device_client, registry, ca, token = _enrolled_client(tmp_path)
    original_serial = registry.get("dev1").cert_serial_number
    _private_key_pem, csr_pem = generate_key_and_csr("dev1")

    response = device_client.post(
        "/api/v1/fleet/devices/dev1/rotate-certificate",
        headers={"Authorization": f"Bearer {token}"},
        json={"csr_pem": csr_pem.decode("ascii")},
    )

    assert response.status_code == 200
    certificate = x509.load_pem_x509_certificate(response.json()["certificate_pem"].encode("ascii"))
    assert certificate.subject.rfc4514_string() == "CN=dev1"
    assert certificate.issuer == ca.certificate.subject
    assert str(certificate.serial_number) != original_serial
    assert registry.get("dev1").cert_serial_number == str(certificate.serial_number)


def test_rotate_certificate_rejects_a_malformed_csr(tmp_path: Path) -> None:
    _public, device_client, _registry, _ca, token = _enrolled_client(tmp_path)

    response = device_client.post(
        "/api/v1/fleet/devices/dev1/rotate-certificate",
        headers={"Authorization": f"Bearer {token}"},
        json={"csr_pem": "not a real csr"},
    )

    assert response.status_code == 400
