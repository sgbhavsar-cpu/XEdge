from __future__ import annotations

from pathlib import Path

from cryptography import x509
from fastapi.testclient import TestClient

from xedge.core.config import ConfigValidator
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, load_or_create_ca
from xedge.security.csr import generate_key_and_csr

_ADMIN_TOKEN = "admin-secret"
_CERT_VALIDITY_DAYS = 90


def _client(
    tmp_path: Path, config_validator: ConfigValidator | None = None
) -> tuple[TestClient, DeviceRegistry, CertificateAuthority]:
    registry = DeviceRegistry(tmp_path / "devices.db")
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-fleet-ca", 3650)
    app = create_fleet_manager_app(
        registry,
        ca,
        admin_token=_ADMIN_TOKEN,
        cert_validity_days=_CERT_VALIDITY_DAYS,
        config_validator=config_validator,
    )
    return TestClient(app), registry, ca


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _provision_join_token(client: TestClient, device_id: str, ttl_seconds: float = 3600) -> str:
    response = client.post(
        "/api/v1/fleet/join-tokens",
        headers=_admin_headers(),
        json={"device_id": device_id, "ttl_seconds": ttl_seconds},
    )
    assert response.status_code == 200
    token: str = response.json()["join_token"]
    return token


def _enroll(client: TestClient, device_id: str, join_token: str) -> object:
    _private_key_pem, csr_pem = generate_key_and_csr(device_id)
    return client.post(
        "/api/v1/fleet/enroll",
        json={"device_id": device_id, "join_token": join_token, "csr_pem": csr_pem.decode("ascii")},
    )


def test_create_join_token_requires_admin(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    response = client.post("/api/v1/fleet/join-tokens", json={"device_id": "dev1"})
    assert response.status_code == 401


def test_create_join_token_returns_a_device_scoped_token(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    response = client.post(
        "/api/v1/fleet/join-tokens", headers=_admin_headers(), json={"device_id": "dev1"}
    )
    assert response.status_code == 200
    assert response.json()["join_token"]
    assert response.json()["device_id"] == "dev1"


def test_enroll_with_valid_join_token_returns_certificate_and_device_token(tmp_path: Path) -> None:
    client, _registry, ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")

    response = _enroll(client, "dev1", join_token)

    assert response.status_code == 200
    body = response.json()
    assert body["device_token"]
    assert body["ca_certificate_pem"] == ca.certificate_pem.decode("ascii")
    certificate = x509.load_pem_x509_certificate(body["certificate_pem"].encode("ascii"))
    assert certificate.subject.rfc4514_string() == "CN=dev1"
    assert certificate.issuer == ca.certificate.subject


def test_enroll_records_certificate_status_on_the_device_record(tmp_path: Path) -> None:
    client, registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    record = registry.get("dev1")
    assert record is not None
    assert record.cert_serial_number
    assert record.cert_not_after is not None


def test_enroll_with_invalid_join_token_is_rejected(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    response = _enroll(client, "dev1", "not-a-real-token")
    assert response.status_code == 401


def test_enroll_join_token_is_single_use(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")

    first = _enroll(client, "dev1", join_token)
    second = _enroll(client, "dev1", join_token)

    assert first.status_code == 200
    assert second.status_code == 401


def test_enroll_rejects_a_join_token_provisioned_for_a_different_device(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")

    response = _enroll(client, "dev2", join_token)

    assert response.status_code == 401


def test_list_and_get_devices_require_admin_token(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    assert client.get("/api/v1/fleet/devices").status_code == 401
    wrong_auth = client.get("/api/v1/fleet/devices", headers={"Authorization": "Bearer wrong"})
    assert wrong_auth.status_code == 401

    ok = client.get("/api/v1/fleet/devices", headers=_admin_headers())
    assert ok.status_code == 200
    assert [d["device_id"] for d in ok.json()] == ["dev1"]

    detail = client.get("/api/v1/fleet/devices/dev1", headers=_admin_headers())
    assert detail.status_code == 200
    assert detail.json()["status"] == "unknown"
    assert detail.json()["connection_state"] == "inactive"
    assert detail.json()["cert_serial_number"]


def test_get_unknown_device_is_404(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    response = client.get("/api/v1/fleet/devices/nope", headers=_admin_headers())
    assert response.status_code == 404


def test_update_metadata_requires_admin(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    response = client.patch("/api/v1/fleet/devices/dev1/metadata", json={"make": "Acme"})
    assert response.status_code == 401


def test_update_metadata_sets_only_the_provided_fields(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    response = client.patch(
        "/api/v1/fleet/devices/dev1/metadata",
        headers=_admin_headers(),
        json={"serial_number": "SN-123", "make": "Acme Gateways"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["serial_number"] == "SN-123"
    assert body["make"] == "Acme Gateways"
    assert body["protocol"] is None

    detail = client.get("/api/v1/fleet/devices/dev1", headers=_admin_headers()).json()
    assert detail["serial_number"] == "SN-123"


def test_update_metadata_for_unknown_device_is_404(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    response = client.patch(
        "/api/v1/fleet/devices/nope/metadata", headers=_admin_headers(), json={"make": "Acme"}
    )
    assert response.status_code == 404


def test_config_push_queues_for_an_enrolled_device(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    push = client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"schema_version": "0.1"}},
    )
    assert push.status_code == 202
    assert push.json() == {"queued": True, "pending_config_version": 1}

    status_response = client.get(
        "/api/v1/fleet/devices/dev1/config/status", headers=_admin_headers()
    )
    assert status_response.json()["has_pending_config"] is True


def test_config_push_to_unknown_device_is_404(tmp_path: Path) -> None:
    client, _registry, _ca = _client(tmp_path)
    response = client.post(
        "/api/v1/fleet/devices/nope/config", headers=_admin_headers(), json={"config": {}}
    )
    assert response.status_code == 404


def test_config_push_without_a_validator_configured_skips_validation(tmp_path: Path) -> None:
    """`config_validator=None` (this fixture's default) is a real supported
    mode, not just a test convenience -- confirms an invalid config is
    still queued rather than raising, since no validator was supplied."""
    client, _registry, _ca = _client(tmp_path)
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    push = client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"this_is_not_a_valid_xedge_config": True}},
    )
    assert push.status_code == 202


def test_config_push_with_a_validator_rejects_an_invalid_config(
    tmp_path: Path, core_schema_path: Path
) -> None:
    """XEDGE-446: an operator authoring a config finds out immediately,
    not only after the device reports a failed apply on its next
    heartbeat."""
    client, registry, _ca = _client(tmp_path, ConfigValidator.from_file(core_schema_path))
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    push = client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"this_is_not_a_valid_xedge_config": True}},
    )

    assert push.status_code == 400
    assert registry.get("dev1").has_pending_config is False


def test_config_push_with_a_validator_accepts_a_valid_config(
    tmp_path: Path, core_schema_path: Path
) -> None:
    client, _registry, _ca = _client(tmp_path, ConfigValidator.from_file(core_schema_path))
    join_token = _provision_join_token(client, "dev1")
    _enroll(client, "dev1", join_token)

    push = client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"schema_version": "0.1"}},
    )

    assert push.status_code == 202
