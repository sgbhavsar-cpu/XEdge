from __future__ import annotations

from pathlib import Path

from cryptography import x509
from httpx import ASGITransport, AsyncClient, Response

from xedge.core.config import ConfigValidator
from xedge.fleet.manager_app import create_fleet_manager_app
from xedge.fleet.registry import DeviceRegistry
from xedge.security.ca import CertificateAuthority, load_or_create_ca
from xedge.security.csr import generate_key_and_csr

_ADMIN_TOKEN = "admin-secret"
_CERT_VALIDITY_DAYS = 90


def _client(
    tmp_path: Path,
    registry: DeviceRegistry,
    default_tenant_id: str,
    admin_token: str = _ADMIN_TOKEN,
    config_validator: ConfigValidator | None = None,
) -> tuple[AsyncClient, CertificateAuthority]:
    """`httpx.AsyncClient` over `ASGITransport`, not `fastapi.testclient.
    TestClient`: `TestClient` drives the app from a separate event loop of
    its own (an `anyio` worker thread/portal), and an asyncpg connection
    created on the pytest-asyncio loop that `fleet_registry` lives on
    cannot be used from a different loop -- confirmed directly (`RuntimeError:
    ... got Future ... attached to a different loop`) before switching to
    this pattern. `AsyncClient` makes every call a real `await` on the
    *same* loop the test function and `fleet_registry` already share."""
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-fleet-ca", 3650)
    app = create_fleet_manager_app(
        registry,
        ca,
        admin_token=admin_token,
        default_tenant_id=default_tenant_id,
        cert_validity_days=_CERT_VALIDITY_DAYS,
        config_validator=config_validator,
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, ca


def _admin_headers(admin_token: str = _ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


async def _provision_join_token(
    client: AsyncClient, device_id: str, ttl_seconds: float = 3600
) -> str:
    response = await client.post(
        "/api/v1/fleet/join-tokens",
        headers=_admin_headers(),
        json={"device_id": device_id, "ttl_seconds": ttl_seconds},
    )
    assert response.status_code == 200
    token: str = response.json()["join_token"]
    return token


async def _enroll(client: AsyncClient, device_id: str, join_token: str) -> Response:
    _private_key_pem, csr_pem = generate_key_and_csr(device_id)
    return await client.post(
        "/api/v1/fleet/enroll",
        json={"device_id": device_id, "join_token": join_token, "csr_pem": csr_pem.decode("ascii")},
    )


async def test_create_join_token_requires_admin(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    response = await client.post("/api/v1/fleet/join-tokens", json={"device_id": "dev1"})
    assert response.status_code == 401


async def test_create_join_token_returns_a_device_scoped_token(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    response = await client.post(
        "/api/v1/fleet/join-tokens", headers=_admin_headers(), json={"device_id": "dev1"}
    )
    assert response.status_code == 200
    assert response.json()["join_token"]
    assert response.json()["device_id"] == "dev1"


async def test_enroll_with_valid_join_token_returns_certificate_and_device_token(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")

    response = await _enroll(client, "dev1", join_token)

    assert response.status_code == 200
    body = response.json()
    assert body["device_token"]
    assert body["ca_certificate_pem"] == ca.certificate_pem.decode("ascii")
    certificate = x509.load_pem_x509_certificate(body["certificate_pem"].encode("ascii"))
    assert certificate.subject.rfc4514_string() == "CN=dev1"
    assert certificate.issuer == ca.certificate.subject


async def test_enroll_records_certificate_status_on_the_device_record(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record is not None
    assert record.cert_serial_number
    assert record.cert_not_after is not None


async def test_enroll_with_invalid_join_token_is_rejected(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    response = await _enroll(client, "dev1", "not-a-real-token")
    assert response.status_code == 401


async def test_enroll_join_token_is_single_use(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")

    first = await _enroll(client, "dev1", join_token)
    second = await _enroll(client, "dev1", join_token)

    assert first.status_code == 200
    assert second.status_code == 401


async def test_enroll_rejects_a_join_token_provisioned_for_a_different_device(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")

    response = await _enroll(client, "dev2", join_token)

    assert response.status_code == 401


async def test_list_and_get_devices_require_admin_token(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    assert (await client.get("/api/v1/fleet/devices")).status_code == 401
    wrong_auth = await client.get(
        "/api/v1/fleet/devices", headers={"Authorization": "Bearer wrong"}
    )
    assert wrong_auth.status_code == 401

    ok = await client.get("/api/v1/fleet/devices", headers=_admin_headers())
    assert ok.status_code == 200
    assert [d["device_id"] for d in ok.json()] == ["dev1"]

    detail = await client.get("/api/v1/fleet/devices/dev1", headers=_admin_headers())
    assert detail.status_code == 200
    assert detail.json()["status"] == "unknown"
    assert detail.json()["connection_state"] == "inactive"
    assert detail.json()["cert_serial_number"]


async def test_get_unknown_device_is_404(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    response = await client.get("/api/v1/fleet/devices/nope", headers=_admin_headers())
    assert response.status_code == 404


async def test_update_metadata_requires_admin(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    response = await client.patch("/api/v1/fleet/devices/dev1/metadata", json={"make": "Acme"})
    assert response.status_code == 401


async def test_update_metadata_sets_only_the_provided_fields(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    response = await client.patch(
        "/api/v1/fleet/devices/dev1/metadata",
        headers=_admin_headers(),
        json={"serial_number": "SN-123", "make": "Acme Gateways"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["serial_number"] == "SN-123"
    assert body["make"] == "Acme Gateways"
    assert body["protocol"] is None

    detail = (await client.get("/api/v1/fleet/devices/dev1", headers=_admin_headers())).json()
    assert detail["serial_number"] == "SN-123"


async def test_update_metadata_for_unknown_device_is_404(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    response = await client.patch(
        "/api/v1/fleet/devices/nope/metadata", headers=_admin_headers(), json={"make": "Acme"}
    )
    assert response.status_code == 404


async def test_config_push_queues_for_an_enrolled_device(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"schema_version": "0.1"}},
    )
    assert push.status_code == 202
    assert push.json() == {"queued": True, "pending_config_version": 1}

    status_response = await client.get(
        "/api/v1/fleet/devices/dev1/config/status", headers=_admin_headers()
    )
    assert status_response.json()["has_pending_config"] is True


async def test_config_push_to_unknown_device_is_404(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    response = await client.post(
        "/api/v1/fleet/devices/nope/config", headers=_admin_headers(), json={"config": {}}
    )
    assert response.status_code == 404


async def test_config_push_without_a_validator_configured_skips_validation(
    tmp_path: Path, fleet_registry: DeviceRegistry, fleet_default_tenant_id: str
) -> None:
    """`config_validator=None` (this fixture's default) is a real supported
    mode, not just a test convenience -- confirms an invalid config is
    still queued rather than raising, since no validator was supplied."""
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"this_is_not_a_valid_xedge_config": True}},
    )
    assert push.status_code == 202


async def test_config_push_with_a_validator_rejects_an_invalid_config(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    core_schema_path: Path,
) -> None:
    """XEDGE-446: an operator authoring a config finds out immediately,
    not only after the device reports a failed apply on its next
    heartbeat."""
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        config_validator=ConfigValidator.from_file(core_schema_path),
    )
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"this_is_not_a_valid_xedge_config": True}},
    )

    assert push.status_code == 400
    record = await fleet_registry.get(fleet_default_tenant_id, "dev1")
    assert record.has_pending_config is False


async def test_config_push_with_a_validator_accepts_a_valid_config(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    core_schema_path: Path,
) -> None:
    client, _ca = _client(
        tmp_path,
        fleet_registry,
        fleet_default_tenant_id,
        config_validator=ConfigValidator.from_file(core_schema_path),
    )
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    push = await client.post(
        "/api/v1/fleet/devices/dev1/config",
        headers=_admin_headers(),
        json={"config": {"schema_version": "0.1"}},
    )

    assert push.status_code == 202


async def test_create_join_token_for_a_device_owned_by_another_tenant_is_409(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    other_tenant_id: str,
) -> None:
    """XEDGE-504 surfaced through the API: `DeviceTenantConflictError`
    from the registry becomes a 409, not a 500."""
    client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(client, "dev1")
    await _enroll(client, "dev1", join_token)

    other_client, _other_ca = _client(
        tmp_path, fleet_registry, other_tenant_id, admin_token="other-admin-secret"
    )
    response = await other_client.post(
        "/api/v1/fleet/join-tokens",
        headers=_admin_headers("other-admin-secret"),
        json={"device_id": "dev1"},
    )

    assert response.status_code == 409


async def test_two_tenants_do_not_see_each_others_devices_through_the_api(
    tmp_path: Path,
    fleet_registry: DeviceRegistry,
    fleet_default_tenant_id: str,
    other_tenant_id: str,
) -> None:
    """End-to-end version of XEDGE-503's isolation requirement: two
    separate manager apps (as two separate tenant deployments would be,
    once XEDGE-502 gives each tenant its own admin identity) sharing one
    registry must not see each other's devices through real HTTP calls."""
    tenant_a_client, _ca = _client(tmp_path, fleet_registry, fleet_default_tenant_id)
    join_token = await _provision_join_token(tenant_a_client, "dev1")
    await _enroll(tenant_a_client, "dev1", join_token)

    tenant_b_client, _other_ca = _client(
        tmp_path, fleet_registry, other_tenant_id, admin_token="other-admin-secret"
    )

    tenant_a_devices = (
        await tenant_a_client.get("/api/v1/fleet/devices", headers=_admin_headers())
    ).json()
    tenant_b_devices = (
        await tenant_b_client.get(
            "/api/v1/fleet/devices", headers=_admin_headers("other-admin-secret")
        )
    ).json()

    assert [d["device_id"] for d in tenant_a_devices] == ["dev1"]
    assert tenant_b_devices == []
    tenant_b_detail = await tenant_b_client.get(
        "/api/v1/fleet/devices/dev1", headers=_admin_headers("other-admin-secret")
    )
    assert tenant_b_detail.status_code == 404
